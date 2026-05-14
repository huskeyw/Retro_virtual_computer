"""
sound_chip.py — SID Sound Engine for RetroThree
================================================
Hardware-accurate SID chip emulation using pyresidfp.
Runs on a dedicated background thread separate from both BASIC and Pygame.

Architecture:
  SIDChip     — wraps the pyresidfp emulator. Holds a 25-byte shadow register
                array. BASIC writes to these via POKE. generate() feeds the
                shadow into the real emulator and returns PCM sample data.

  SoundSystem — owns the SIDChip and the audio playback thread.
                Provides set_register()/get_register() for the hardware
                memory map ($C030-$C061). Also contains the DMA controller
                which streams pre-packed SID frame data from RAM for music.

SID Register Map (base address $C030 = 49200):
  Offsets 0-6:   Voice 1 (freq lo, freq hi, pw lo, pw hi, control, AD, SR)
  Offsets 7-13:  Voice 2
  Offsets 14-20: Voice 3
  Offsets 21-23: Filter (fc lo, fc hi, res/filt)
  Offset  24:    Mode/Volume (bits 0-3 = master volume 0-15)

DMA Controller Registers (base + 32 through base + 38):
  +32/33/35: 24-bit pointer into RAM where SID frame data starts
  +36/37/38: 24-bit frame count (each frame = 25 bytes of register data)
  +34:       Control: write 2 to start DMA playback, 0 to stop

DMA playback: the audio thread reads 25 bytes per frame at 50fps directly
from RAM into the SID shadow, achieving music playback without involving BASIC.
"""

import threading
import time
import wave

try:
    import numpy as np
    NUMPY_OK = True
except ImportError:
    NUMPY_OK = False

try:
    import pygame
    import pygame.mixer
    import pygame.sndarray
    PYGAME_OK = True
except ImportError:
    PYGAME_OK = False

try:
    from pyresidfp import SoundInterfaceDevice as ReSID
    RESID_OK = True
except ImportError:
    RESID_OK = False
    print("WARNING: pyresidfp not found. pip install pyresidfp")

# ==========================
# SID Chip Constants
# ==========================
NUM_VOICES   = 3     # SID has 3 independent voices
NUM_REGS     = 25    # 25 hardware registers total
VSTRIDE      = 7     # 7 registers per voice

# Voice control register bit flags (register offset 4 in each voice block)
SID_GATE     = 0x01  # Gate: 1=note on, 0=note off (release phase)
SID_SYNC     = 0x02  # Sync oscillator with voice 1
SID_RING     = 0x04  # Ring modulation with voice 1
SID_TEST     = 0x08  # Test bit: resets oscillator
SID_TRI      = 0x10  # Triangle waveform
SID_SAW      = 0x20  # Sawtooth waveform
SID_PULSE    = 0x40  # Pulse/square waveform (width set by PW registers)
SID_NOISE    = 0x80  # Noise waveform (random, good for explosions)

# Filter mode flags (register 24, bits 4-6)
SID_LP       = 0x10  # Low-pass filter
SID_BP       = 0x20  # Band-pass filter
SID_HP       = 0x40  # High-pass filter

PAL_CLOCK    = 985248.0  # PAL system clock frequency in Hz
PAL_FPS      = 50        # PAL frame rate — DMA streams one frame per tick

# Waveform name constants for use with note_on()
WAVE_TRIANGLE = 0
WAVE_SAWTOOTH = 1
WAVE_PULSE    = 2
WAVE_NOISE    = 3

WAVE_TO_SID = {
    WAVE_TRIANGLE: SID_TRI,
    WAVE_SAWTOOTH: SID_SAW,
    WAVE_PULSE:    SID_PULSE,
    WAVE_NOISE:    SID_NOISE,
}

def midi_to_hz(note):
    if note <= 0: return 0.0
    return 440.0 * (2.0 ** ((note - 69) / 12.0))

def midi_to_sid_freq(note, clock=PAL_CLOCK):
    if note <= 0: return 0
    hz  = midi_to_hz(note)
    reg = int(hz * 16777216.0 / clock)
    return max(0, min(65535, reg))


# ==========================
# SIDChip
# ==========================
class SIDChip:
    """
    Wraps the pyresidfp SID emulator with a 25-byte shadow register array.

    BASIC writes to shadow[] via set_register(). On each audio frame,
    generate() copies the shadow into the real emulator then clocks it
    to produce PCM sample data. This shadow approach means multiple
    register writes between audio frames are batched efficiently.

    If pyresidfp is not installed, generate() returns silence (zeroes)
    so the rest of the system still runs without audio.
    """
    SAMPLE_RATE = 48000
    CHIP_NAME   = "SID"
    CLOCK       = PAL_CLOCK

    def __init__(self):
        self.shadow = bytearray(NUM_REGS)
        self._sid   = None
        self._init_sid()
        self._init_shadow()

    def _init_sid(self):
        if not RESID_OK: return
        self._sid = ReSID()
        self._sid.clock_frequency = self.CLOCK
        self.SAMPLE_RATE = int(self._sid.sampling_frequency)

    def _init_shadow(self):
        self.shadow = bytearray(NUM_REGS)
        self.shadow[24] = 0x0F
        for v in range(NUM_VOICES):
            b = v * VSTRIDE
            self.shadow[b + 2] = 0x00
            self.shadow[b + 3] = 0x08

    def reset(self):
        self._init_shadow()
        if self._sid:
            for reg in range(NUM_REGS): self._sid.write_register(reg, 0)

    def note_on(self, voice, midi_note, waveform, ad, sr, volume):
        b    = voice * VSTRIDE
        freq = midi_to_sid_freq(midi_note, self.CLOCK)
        self.shadow[b+0] = freq & 0xFF
        self.shadow[b+1] = (freq >> 8) & 0xFF
        self.shadow[b+5] = ad
        self.shadow[b+6] = sr
        wbits = WAVE_TO_SID.get(waveform, SID_PULSE)
        self.shadow[b+4] = wbits
        if self._sid: self._sid.write_register(b+4, wbits)
        self.shadow[b+4] = wbits | SID_GATE
        self.shadow[24] = (self.shadow[24] & 0xF0) | (volume & 0xF)

    def note_off(self, voice):
        b = voice * VSTRIDE
        self.shadow[b+4] &= ~SID_GATE

    def set_pw(self, voice, pw):
        b = voice * VSTRIDE
        self.shadow[b+2] = pw & 0xFF
        self.shadow[b+3] = (pw >> 8) & 0x0F

    def set_volume(self, vol):
        self.shadow[24] = (self.shadow[24] & 0xF0) | (vol & 0xF)

    def set_filter(self, fc_hi, fc_lo, res_filt, mode_vol):
        self.shadow[21] = fc_lo & 0x07
        self.shadow[22] = fc_hi & 0xFF
        self.shadow[23] = res_filt & 0xFF
        self.shadow[24] = mode_vol & 0xFF

    def generate(self, num_samples):
        if not NUMPY_OK or not self._sid: return np.zeros(num_samples, dtype=np.int16)
        for reg in range(NUM_REGS):
            self._sid.write_register(reg, self.shadow[reg])
        from datetime import timedelta
        dur = timedelta(seconds=num_samples / self.SAMPLE_RATE)
        raw = self._sid.clock(dur)
        arr = np.array(raw[:num_samples], dtype=np.int16)
        if len(arr) < num_samples:
            arr = np.pad(arr, (0, num_samples - len(arr)))
        return arr


# ==========================
# SoundSystem
# ==========================
class SoundSystem:
    """
    Manages the SID chip, audio playback thread, and DMA music controller.

    The audio thread (_loop) runs at PAL_FPS (50fps). Each tick it:
      1. Runs the DMA controller if active — copies one frame of SID register
         data from RAM directly into the SID shadow (music playback)
      2. Calls chip.generate() to produce PCM samples
      3. Buffers CHUNK_FRAMES worth of audio then queues it to pygame mixer

    The DMA controller enables background music without BASIC involvement.
    BASIC loads packed SID data to extended RAM, sets the pointer/length
    registers, then writes 2 to the control register to start playback.
    The audio thread handles everything from that point forward.

    set_register()/get_register() map the hardware address range
    $C030-$C061 to SID registers and DMA control registers.
    """
    FPS = PAL_FPS

    def __init__(self, ram=None, record_file=None):
        self.chip    = SIDChip()
        self.lock    = threading.Lock()
        self.engine  = None
        self._thread = None
        self._running = False
        self._audio_ok = False
        
        # DMA Hardware Music Controller
        # dma_ptr: 24-bit address in physical RAM where frame data starts
        # dma_len: number of frames remaining to play (each frame = 25 bytes)
        # dma_ctrl: control register (2=playing, 0=stopped)
        # dma_active: True while music is streaming
        self.ram = ram
        self.dma_ptr = 0
        self.dma_len = 0
        self.dma_ctrl = 0
        self.dma_active = False

        self.record_file = record_file
        if self.record_file:
            self._wav = wave.open(self.record_file, 'wb')
            self._wav.setnchannels(2)
            self._wav.setsampwidth(2)
            self._wav.setframerate(self.chip.SAMPLE_RATE)
        else:
            self._wav = None

    def get_master_volume(self):
        return self.chip.shadow[24] & 0x0F

    def set_master_volume(self, vol):
        self.chip.set_volume(vol)

    def set_register(self, addr, val):
        offset = addr - 0xC030
        with self.lock:
            # Standard SID Registers
            if 0 <= offset <= 24:
                self.chip.shadow[offset] = val
                
            # DMA Controller Registers (SB + 32 to 38)
            elif offset == 32: self.dma_ptr = (self.dma_ptr & 0xFFFF00) | val
            elif offset == 33: self.dma_ptr = (self.dma_ptr & 0xFF00FF) | (val << 8)
            elif offset == 35: self.dma_ptr = (self.dma_ptr & 0x00FFFF) | (val << 16)
            elif offset == 36: self.dma_len = (self.dma_len & 0xFFFF00) | val
            elif offset == 37: self.dma_len = (self.dma_len & 0xFF00FF) | (val << 8)
            elif offset == 38: self.dma_len = (self.dma_len & 0x00FFFF) | (val << 16)
            elif offset == 34: 
                self.dma_ctrl = val
                if val == 2:
                    self.dma_active = True
                else:
                    self.dma_active = False
                    self.chip.reset()

    def get_register(self, addr):
        offset = addr - 0xC030
        with self.lock:
            if 0 <= offset <= 24: return self.chip.shadow[offset]
            elif offset == 32: return self.dma_ptr & 0xFF
            elif offset == 33: return (self.dma_ptr >> 8) & 0xFF
            elif offset == 35: return (self.dma_ptr >> 16) & 0xFF
            elif offset == 36: return self.dma_len & 0xFF
            elif offset == 37: return (self.dma_len >> 8) & 0xFF
            elif offset == 38: return (self.dma_len >> 16) & 0xFF
            elif offset == 34: return self.dma_ctrl
        return 0

    def start(self):
        if self._running or not NUMPY_OK or not PYGAME_OK: return
        try:
            rate = self.chip.SAMPLE_RATE
            # Increased buffer to prevent stuttering
            pygame.mixer.pre_init(frequency=rate, size=-16, channels=2, buffer=4096)
            pygame.mixer.init()
            self._audio_ok = True
        except Exception as e:
            print(f"AUDIO ERROR: {e}"); return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread: self._thread.join(timeout=1.0)
        if self._wav:
            self._wav.close()
            self._wav = None
        if PYGAME_OK and self._audio_ok:
            pygame.mixer.quit()
            self._audio_ok = False

    def _loop(self):
        channel = pygame.mixer.Channel(0)
        spf = max(1, self.chip.SAMPLE_RATE // self.FPS)
        frame_dur = 1.0 / self.FPS   
        next_t = time.perf_counter()
        
        CHUNK_FRAMES = 5
        audio_buffer = []

        while self._running:
            now = time.perf_counter()
            wait = next_t - now
            if wait > 0.001: time.sleep(wait - 0.001)
            while time.perf_counter() < next_t: pass   
            next_t += frame_dur

            with self.lock:
                if self.engine: self.engine.on_frame()
                
                # DMA Frame Fetcher — streams music from RAM
                # Copies 25 SID register bytes directly into the chip shadow.
                # Advances the pointer by 25 bytes and decrements frame count.
                # When frame count reaches 0, stops automatically.
                if self.dma_active and self.ram is not None:
                    if self.dma_len > 0:
                        # Direct Memory Access: Copy 25 bytes directly into SID shadow
                        for i in range(25):
                            self.chip.shadow[i] = self.ram[self.dma_ptr + i]
                        self.dma_ptr += 25
                        self.dma_len -= 1
                    else:
                        # Auto-Stop when frames run out
                        self.dma_active = False
                        self.dma_ctrl = 0
                        self.chip.reset()

                samples = self.chip.generate(spf)

            if samples is None: continue
            audio_buffer.append(samples)

            if len(audio_buffer) >= CHUNK_FRAMES:
                try:
                    combined = np.concatenate(audio_buffer)
                    audio_buffer = []
                    stereo = np.ascontiguousarray(np.column_stack((combined, combined)))
                    if self._wav: self._wav.writeframes(stereo.tobytes())
                    snd = pygame.sndarray.make_sound(stereo)
                    if not channel.get_busy(): channel.play(snd)
                    elif channel.get_queue() is None: channel.queue(snd)
                except Exception: pass