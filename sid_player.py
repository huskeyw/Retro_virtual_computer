"""
sid_player.py - Direct SID register playback from SIDDump output
Feeds raw register values frame-by-frame to pyresidfp at 50fps (PAL)
This is how a real C64 works - CPU writes registers, SID produces sound
"""
import re, sys, time
from sound_chip import SoundSystem  # <--- NEW: Import your own engine!

def parse_hex(s, default=0):
    try: return int(s.replace('.',''), 16) if s and '.' not in s else default
    except: return default

def load_dump(path):
    with open(path, 'rb') as f: raw = f.read()
    try:    text = raw.decode('utf-16-le', errors='replace')
    except: text = raw.decode('latin-1',   errors='replace')

    data_re = re.compile(r'^\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|')
    
    # SID register state - carries forward unchanged values
    regs = bytearray(25)
    regs[24] = 0x0F  # max volume, no filter initially
    frames = []
    prev_pw  = [0, 0, 0]   # pulse width per voice carries forward

    for line in text.splitlines():
        if not line.startswith('|'): continue
        m = data_re.match(line)
        if not m: continue
        if 'Frame' in line or 'Freq' in line: continue

        frame_regs = bytearray(regs)  # copy current state

        for ch in range(3):
            col  = m.group(ch + 2)
            toks = col.split()
            if not toks: continue

            vb = ch * 7

            # Always check last token for pulse width
            last = toks[-1].strip()
            if len(last) == 3 and all(c in '0123456789ABCDEFabcdef' for c in last):
                pw = int(last, 16)
                prev_pw[ch] = pw

            # Always write current pulse width (carries forward)
            frame_regs[vb+2] = prev_pw[ch] & 0xFF
            frame_regs[vb+3] = (prev_pw[ch] >> 8) & 0x0F

            # Skip if freq unchanged
            if toks[0].strip() == '....': continue

            # Frequency (4 hex, first token)
            freq_s = toks[0].strip()
            if freq_s and '.' not in freq_s:
                freq = parse_hex(freq_s)
                frame_regs[vb+0] = freq & 0xFF
                frame_regs[vb+1] = (freq >> 8) & 0xFF

            rest = col[5:].strip()  # skip freq field
            if rest.startswith('('):
                close = rest.find(')')
                rest = rest[close+1:].strip() if close >= 0 else rest[3:].strip()
            else:
                parts = rest.split()
                if parts and re.match(r'^[A-G#.][#\-. ][0-9.]', parts[0]+'  '):
                    rest = ' '.join(parts[1:])  # skip note name
                    if parts[0] != '...':
                        rest = ' '.join(parts[2:]) if len(parts)>1 else ''  # skip abs too

            toks2 = rest.split()
            idx = 0

            # WF (2 hex or '..')
            if idx < len(toks2) and toks2[idx] != '..':
                wf = parse_hex(toks2[idx], None)
                if wf is not None:
                    frame_regs[vb+4] = wf
            idx += 1

            # ADSR (4 hex or '....')
            if idx < len(toks2) and '.' not in toks2[idx]:
                adsr = parse_hex(toks2[idx], None)
                if adsr is not None:
                    frame_regs[vb+5] = (adsr >> 8) & 0xFF
                    frame_regs[vb+6] = adsr & 0xFF
            idx += 1

        fcol = m.group(5).strip().split()
        if fcol and '.' not in fcol[0]:
            fc_raw = parse_hex(fcol[0])
            frame_regs[22] = (fc_raw >> 8) & 0xFF   # fc_hi
            frame_regs[21] = (fc_raw >> 5) & 0x07   # fc_lo
        if len(fcol) > 1 and '.' not in fcol[1]:
            frame_regs[23] = parse_hex(fcol[1])      # res_filt
        if len(fcol) > 2 and fcol[2] not in ('...', '.'):
            typ = fcol[2].lower()
            mode = frame_regs[24] & 0x0F             # keep volume bits
            if 'low' in typ: mode |= 0x10
            if 'ban' in typ: mode |= 0x20
            if 'hi'  in typ: mode |= 0x40
            frame_regs[24] = mode
        if len(fcol) > 3 and fcol[3] not in ('...', '.'):
            try:
                vol = int(fcol[3], 16) & 0xF
                frame_regs[24] = (frame_regs[24] & 0xF0) | vol
            except: pass

        regs = bytearray(frame_regs)
        frames.append(bytearray(frame_regs))

    return frames

# =========================================================================
# NEW: Raw Player Engine (replaces the old Pygame loop)
# =========================================================================
class RawPlayerEngine:
    def __init__(self, frames, sound_system):
        self.frames = frames
        self.sound = sound_system
        self.fi = 0
        self.playing = True

    def on_frame(self):
        if self.fi < len(self.frames):
            regs = self.frames[self.fi]
            # Write all 25 registers directly into the shadow state
            for i in range(25):
                self.sound.chip.shadow[i] = regs[i]
            self.fi += 1
        else:
            self.playing = False

def play(dump_path, stop_flag=None):
    print(f"Loading {dump_path}...")
    frames = load_dump(dump_path)
    if not frames:
        print("\nError: 0 frames loaded.")
        if stop_flag is None: sys.exit(1)
        return

    print(f"Loaded {len(frames)} frames. Booting SoundSystem...")
    
    # --- NEW: Boot sound_chip.py with recording turned ON ---
    sound = SoundSystem(record_file="sid_player_capture.wav")
    engine = RawPlayerEngine(frames, sound)
    sound.engine = engine
    sound.start()

    print("Playing via sound_chip.py... (Recording to sid_player_capture.wav)")
    print("Press Ctrl+C to stop early.")
    
    start = time.time()
    try:
        while engine.playing:
            if stop_flag and stop_flag():
                break
            # Just print the timer
            elapsed = time.time() - start
            print(f"  {elapsed:.1f}s / {len(frames)/50:.1f}s  ", end='\r')
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopping playback...")
    finally:
        sound.stop()
        print("\nSaved sid_player_capture.wav")


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'Cobra.txt'
    play(path)