"""
sid_packer.py - Converts SIDDump Text to Raw Binary with a 2-byte Frame Header
Usage: python sid_packer.py input.txt output.bin
"""
import sys, re, struct

# FIX: Added the 'default' parameter back in!
def parse_hex(s, default=0):
    try: return int(s.replace('.',''), 16) if s and '.' not in s else default
    except: return default

def pack_sid(txt_path, bin_path):
    with open(txt_path, 'rb') as f: raw = f.read()
    try:    text = raw.decode('utf-16-le', errors='replace')
    except: text = raw.decode('latin-1',   errors='replace')

    data_re = re.compile(r'^\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|')
    regs = bytearray(25)
    regs[24] = 0x0F  
    prev_pw  = [0, 0, 0]   
    out_data = bytearray()

    for line in text.splitlines():
        if not line.startswith('|'): continue
        m = data_re.match(line)
        if not m: continue
        if 'Frame' in line or 'Freq' in line: continue

        frame_regs = bytearray(regs)  
        for ch in range(3):
            col  = m.group(ch + 2)
            toks = col.split()
            if not toks: continue
            vb = ch * 7

            last = toks[-1].strip()
            if len(last) == 3 and all(c in '0123456789ABCDEFabcdef' for c in last):
                prev_pw[ch] = int(last, 16)

            frame_regs[vb+2] = prev_pw[ch] & 0xFF
            frame_regs[vb+3] = (prev_pw[ch] >> 8) & 0x0F

            if toks[0].strip() == '....': continue
            freq_s = toks[0].strip()
            if freq_s and '.' not in freq_s:
                freq = parse_hex(freq_s)
                frame_regs[vb+0] = freq & 0xFF
                frame_regs[vb+1] = (freq >> 8) & 0xFF

            rest = col[5:].strip()  
            if rest.startswith('('):
                close = rest.find(')')
                rest = rest[close+1:].strip() if close >= 0 else rest[3:].strip()
            else:
                parts = rest.split()
                if parts and re.match(r'^[A-G#.][#\-. ][0-9.]', parts[0]+'  '):
                    rest = ' '.join(parts[1:])  
                    if parts[0] != '...': rest = ' '.join(parts[2:]) if len(parts)>1 else ''  

            toks2 = rest.split()
            idx = 0
            if idx < len(toks2) and toks2[idx] != '..':
                wf = parse_hex(toks2[idx], None)
                if wf is not None: frame_regs[vb+4] = wf
            idx += 1
            if idx < len(toks2) and '.' not in toks2[idx]:
                adsr = parse_hex(toks2[idx], None)
                if adsr is not None:
                    frame_regs[vb+5] = (adsr >> 8) & 0xFF
                    frame_regs[vb+6] = adsr & 0xFF

        fcol = m.group(5).strip().split()
        if fcol and '.' not in fcol[0]:
            fc_raw = parse_hex(fcol[0])
            frame_regs[22] = (fc_raw >> 8) & 0xFF   
            frame_regs[21] = (fc_raw >> 5) & 0x07   
        if len(fcol) > 1 and '.' not in fcol[1]: frame_regs[23] = parse_hex(fcol[1])      
        if len(fcol) > 2 and fcol[2] not in ('...', '.'):
            typ = fcol[2].lower()
            mode = frame_regs[24] & 0x0F             
            if 'low' in typ: mode |= 0x10
            if 'ban' in typ: mode |= 0x20
            if 'hi'  in typ: mode |= 0x40
            frame_regs[24] = mode
        if len(fcol) > 3 and fcol[3] not in ('...', '.'):
            try: frame_regs[24] = (frame_regs[24] & 0xF0) | (int(fcol[3], 16) & 0xF)
            except: pass

        regs = bytearray(frame_regs)
        out_data.extend(frame_regs)

    # Calculate total frames (25 bytes per frame)
    total_frames = len(out_data) // 25
    
    # Prepend 2-byte header (Little Endian Frame Count)
    header = struct.pack('<H', total_frames)
    final_payload = header + out_data

    with open(bin_path, 'wb') as f: f.write(final_payload)
    print(f"Packed {total_frames} frames into {bin_path} with 2-byte header.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python sid_packer.py <input.txt> <output.bin>")
    else:
        pack_sid(sys.argv[1], sys.argv[2])