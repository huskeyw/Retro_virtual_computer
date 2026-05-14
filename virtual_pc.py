"""
virtual_pc.py — RetroThree Virtual Machine Frontend
=====================================================
The Pygame-based display and hardware layer for the RetroThree system.
Owns the window, the shared RAM bytearray, and all hardware subsystems.

Threading model:
  Main thread  — Pygame event loop, rendering, joystick polling (60fps)
  BASIC thread — runs the interpreter, pokes RAM, reads registers
  Audio thread — SID chip emulation and DMA music streaming (50fps)

The shared RAM bytearray is the communication bus between all threads.
BASIC writes sprite positions, the VDP reads them. BASIC writes SID
registers, the audio thread reads them. No explicit synchronisation is
needed for game-speed data because single byte writes are atomic in Python.

VirtualMachine   — owns the window, RAM, VDP, SoundSystem, settings
SystemInterpreter — subclass of BASICInterpreter wired to the VM
                    overrides print/input handlers to write to the virtual screen
                    attaches MMU hooks for all hardware register ranges
"""

import pygame
import threading
import queue
import time
import sys
import os
import io
import json
from pathlib import Path
from contextlib import redirect_stdout

# Tkinter is used only for native file open/save dialogs in the F12 menu
try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:
    print("WARNING: Tkinter not found. File dialogs disabled.")

from basic import BASICInterpreter, Tokenizer, Parser, split_stmts, BasicError, MMU
from sound_chip import SoundSystem
from video_chip import (VDP, TEXT_PALETTE,
                        VDP_REG_BASE, VDP_REG_END,
                        VDP_MODE, VDP_TILE_DATA_BASE, VDP_TILEMAP_BASE,
                        ADDR_VDP_BASE, ADDR_VDP_END,
                        MAX_TILES, TILE_BYTES)

# ==========================
# Display Configuration
# ==========================
# Screen is 640x400 pixels at native resolution.
# Text grid: 80 columns x 50 rows (8x8 pixel characters).
# Tile grid: 40 columns x 25 rows (16x16 pixel tiles) — exact fit.
# pygame.SCALED lets SDL handle window upscaling via GPU.
VISIBLE_COLS  = 80
VISIBLE_ROWS  = 50
SCALE_FACTOR  = 2    # Used for fallback software scaling if SCALED mode unavailable
BORDER_SIZE   = 20   # Pygame window border in pixels (decorative, not play area)
SCREEN_WIDTH_PX  = VISIBLE_COLS * 8   # 640
SCREEN_HEIGHT_PX = VISIBLE_ROWS * 8   # 400

# Default RAM pages for screen and character data
DEFAULT_SCREEN_PAGE  = 4    # Screen RAM at $0400 (address 1024)
DEFAULT_CHARSET_PAGE = 240  # Character ROM at $F000 (address 61440)

# Cursor position registers in zero page — written by POKE to move cursor
ADDR_CURSOR_COL = 211
ADDR_CURSOR_ROW = 214

# ==========================
# Hardware Register Ranges
# ==========================
# Each range is routed to its subsystem in set_reg() / get_reg().
ADDR_JOY1     = 0xC020  # 49184 — Joystick port 1 state byte
ADDR_JOY2     = 0xC021  # 49185 — Joystick port 2 state byte
ADDR_SND_BASE = 0xC030  # 49200 — SID chip registers start
ADDR_SND_END  = 0xC061  # 49249 — SID chip + DMA registers end
ADDR_VDP_BASE = 0xC080  # 49280 — VDP control registers start
ADDR_VDP_END  = 0xC09F  # 49311 — VDP collision registers end

# Joystick state byte bit masks — OR these together to build the state byte
# BASIC reads: J = PEEK(49184) then tests individual bits with AND
JOY_UP    = 0x01   # bit 0
JOY_DOWN  = 0x02   # bit 1
JOY_LEFT  = 0x04   # bit 2
JOY_RIGHT = 0x08   # bit 3
JOY_FIREA = 0x10   # bit 4 — primary fire
JOY_FIREB = 0x20   # bit 5
JOY_FIREC = 0x40   # bit 6
JOY_FIRED = 0x80   # bit 7

# Text layer uses the fixed system palette from the VDP module.
# COLORS kept as alias so all existing code works unchanged.
COLORS = TEXT_PALETTE

COLOR_CODES = {
    144: 0, 5: 1, 28: 2, 159: 3, 156: 4, 30: 5, 31: 6, 158: 7,
    129: 8, 149: 9, 150: 10, 151: 11, 152: 12, 153: 13, 154: 14, 155: 15
}

# Default key mappings
DEFAULT_SETTINGS = {
    "joystick1": {
        "up":     "UP",
        "down":   "DOWN",
        "left":   "LEFT",
        "right":  "RIGHT",
        "fire_a": "SPACE",
        "fire_b": "Z",
        "fire_c": "X",
        "fire_d": "C"
    },
    "joystick2": {
        "up":     "W",
        "down":   "S",
        "left":   "A",
        "right":  "D",
        "fire_a": "F",
        "fire_b": "G",
        "fire_c": "H",
        "fire_d": "J"
    },
    "display": {
        "border_color": 2,
        "bg_color":     0,
        "text_color":   1
    },
    "sound": {
        "enabled":      True,
        "master_volume": 15
    }
}

# Map friendly key names to pygame key constants
KEY_NAME_TO_PYGAME = {
    "UP":       pygame.K_UP,
    "DOWN":     pygame.K_DOWN,
    "LEFT":     pygame.K_LEFT,
    "RIGHT":    pygame.K_RIGHT,
    "SPACE":    pygame.K_SPACE,
    "RETURN":   pygame.K_RETURN,
    "LSHIFT":   pygame.K_LSHIFT,
    "RSHIFT":   pygame.K_RSHIFT,
    "LCTRL":    pygame.K_LCTRL,
    "RCTRL":    pygame.K_RCTRL,
    "LALT":     pygame.K_LALT,
    "RALT":     pygame.K_RALT,
    "TAB":      pygame.K_TAB,
    "ESCAPE":   pygame.K_ESCAPE,
    "BACKSPACE":pygame.K_BACKSPACE,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KEY_NAME_TO_PYGAME[_c] = getattr(pygame, f"K_{_c.lower()}")
for _n in range(10):
    KEY_NAME_TO_PYGAME[str(_n)] = getattr(pygame, f"K_{_n}")
for _f in range(1, 13):
    KEY_NAME_TO_PYGAME[f"F{_f}"] = getattr(pygame, f"K_F{_f}")

def pygame_key_to_name(key):
    for name, k in KEY_NAME_TO_PYGAME.items():
        if k == key: return name
    return None

SETTINGS_FILE = "retrothree.cfg"

class VirtualMachine:
    """
    The hardware layer of the RetroThree system.

    Owns and manages:
      - The shared RAM bytearray (512KB, used by BASIC, VDP, and SID)
      - The Pygame window and render loop
      - Screen RAM and color RAM (virtual text display)
      - The VDP (Video Display Processor) — tiles, sprites, collision
      - The SoundSystem — SID chip and DMA music streaming
      - Joystick/keyboard state, updated every Pygame frame
      - The F12 settings menu (joystick mapping, display, sound, disk)
      - Virtual disk image mounting (.dsk JSON files)
      - Persistent settings via retrothree.cfg

    set_reg(addr, val) / get_reg(addr) route hardware register accesses
    to the appropriate subsystem (VDP, SID, joystick, display registers).

    render() is called 60fps from the main loop. It builds the text layer
    (only when screen RAM has changed), then calls vdp.composite() to merge
    all layers, then blits the result to the window.

    Text dirty tracking: text_dirty flag is set whenever screen RAM or
    color RAM is written. The render loop skips the 4000-cell text rebuild
    when nothing has changed, which is most frames during gameplay.
    """
    def __init__(self):
        self.running = True
        self.lock = threading.RLock()
        self.break_request = False
        self.caps_lock = True
        self.editor_mode = True
        self.insert_mode = False
        self.input_mode = False
        self.input_queue = queue.Queue()
        self.input_ready = threading.Event()
        self.keyboard_buffer = ""

        self.key_queue = queue.Queue()
        self.held_keys = set()
        self.joystick_devices = []

        self.menu_active = False
        self.menu_state = "MAIN"   
        self.menu_idx = 0
        self.capture_target = None  
        self.capture_for_gamepad = False

        self.menu_options_main = [
            "CONFIGURE JOYSTICK 1",
            "CONFIGURE JOYSTICK 2",
            "DISPLAY SETTINGS",
            "SOUND SETTINGS",
            "CREATE NEW DISK",
            "MOUNT DISK IMAGE",
            "UNMOUNT DISK",
            "MOUNT FOLDER (CD)",
            "SOFT RESET",
            "HARD RESET",
            "EXIT MENU"
        ]
        self.joy_actions = ["up", "down", "left", "right", "fire_a", "fire_b", "fire_c", "fire_d"]
        self.joy_labels  = ["UP", "DOWN", "LEFT", "RIGHT", "FIRE A", "FIRE B", "FIRE C", "FIRE D"]

        self.soft_reset = False
        self.hard_reset = False
        self.current_folder = os.getcwd()

        self.disk_mounted = False
        self.disk_path = ""
        self.disk_data = {}

        self.settings = self.load_settings()

        try:
            self.tk_root = tk.Tk()
            self.tk_root.withdraw()
        except:
            self.tk_root = None

        pygame.mixer.pre_init(frequency=48000, size=-16, channels=2, buffer=4096)
        pygame.init()

        pygame.joystick.init()
        for i in range(pygame.joystick.get_count()):
            js = pygame.joystick.Joystick(i)
            js.init()
            self.joystick_devices.append(js)

        self.canvas = pygame.Surface((SCREEN_WIDTH_PX, SCREEN_HEIGHT_PX))
        # Use SCALED flag — pygame/SDL handles upscaling via GPU, no software scale needed
        # Render at native 640x400, display fills the window automatically
        win_w = (SCREEN_WIDTH_PX * SCALE_FACTOR) + (BORDER_SIZE * 2)
        win_h = (SCREEN_HEIGHT_PX * SCALE_FACTOR) + (BORDER_SIZE * 2)
        try:
            self.window = pygame.display.set_mode(
                (SCREEN_WIDTH_PX + BORDER_SIZE * 2, SCREEN_HEIGHT_PX + BORDER_SIZE * 2),
                pygame.SCALED)
            self._use_scaled = True
        except Exception:
            self.window = pygame.display.set_mode((win_w, win_h))
            self._use_scaled = False
        pygame.display.set_caption("RetroThree.14 8-Bit System v1.5")

        disp = self.settings.get("display", {})
        self.fg_color_idx  = disp.get("text_color",   1)
        self.bg_color_idx  = disp.get("bg_color",      0)
        self.border_color  = disp.get("border_color",  2)
        self.screen_page   = DEFAULT_SCREEN_PAGE
        self.charset_page  = DEFAULT_CHARSET_PAGE

        self.ram = bytearray(MMU.MEMORY_SIZE)
        self.cols = VISIBLE_COLS
        self.rows = VISIBLE_ROWS
        self.cursor_x = 0
        self.cursor_y = 0
        self.cursor_visible = True
        self.last_blink = time.time()
        self.cls()

        self.char_cache  = {}
        self.dirty_chars = set()

        # Connect the upgraded SoundSystem to RAM
        snd = self.settings.get("sound", {})
        self.sound = SoundSystem(self.ram)
        self.sound.set_master_volume(snd.get("master_volume", 15))
        if snd.get("enabled", True):
            self.sound.start()

        # --- VDP Video Display Processor ---
        self.vdp = VDP(self.ram, SCREEN_WIDTH_PX, SCREEN_HEIGHT_PX)
        # Text surface uses colorkey transparency — faster than SRCALPHA
        # Magic transparent color: pure magenta (not used in any palette)
        self._text_transparent = (255, 0, 255)
        self.text_surface = pygame.Surface((SCREEN_WIDTH_PX, SCREEN_HEIGHT_PX))
        self.text_surface.set_colorkey(self._text_transparent)
        self.text_dirty   = True
        self._last_screen_hash = -1

    def load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r') as f:
                    data = json.load(f)
                merged = json.loads(json.dumps(DEFAULT_SETTINGS))
                for section in merged:
                    if section in data:
                        merged[section].update(data[section])
                return merged
            except Exception as e:
                print(f"SETTINGS LOAD ERROR: {e}")
        return json.loads(json.dumps(DEFAULT_SETTINGS))

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            print(f"SETTINGS SAVE ERROR: {e}")

    def update_joystick(self):
        for port_num, port_key in [(1, "joystick1"), (2, "joystick2")]:
            addr = ADDR_JOY1 if port_num == 1 else ADDR_JOY2
            mapping = self.settings.get(port_key, {})
            state = 0
            bit_map = {
                "up":     JOY_UP,
                "down":   JOY_DOWN,
                "left":   JOY_LEFT,
                "right":  JOY_RIGHT,
                "fire_a": JOY_FIREA,
                "fire_b": JOY_FIREB,
                "fire_c": JOY_FIREC,
                "fire_d": JOY_FIRED,
            }
            for action, bit in bit_map.items():
                key_name = mapping.get(action, "")
                pygame_key = KEY_NAME_TO_PYGAME.get(key_name)
                if pygame_key and pygame_key in self.held_keys:
                    state |= bit

            js_idx = port_num - 1
            if js_idx < len(self.joystick_devices):
                js = self.joystick_devices[js_idx]
                try:
                    ax = js.get_axis(0)
                    ay = js.get_axis(1)
                    if ax < -0.5: state |= JOY_LEFT
                    if ax >  0.5: state |= JOY_RIGHT
                    if ay < -0.5: state |= JOY_UP
                    if ay >  0.5: state |= JOY_DOWN
                    for btn_idx, bit in enumerate([JOY_FIREA, JOY_FIREB, JOY_FIREC, JOY_FIRED]):
                        if btn_idx < js.get_numbuttons() and js.get_button(btn_idx):
                            state |= bit
                except:
                    pass

            self.ram[addr] = state

    def mount_disk_image(self, path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
                self.disk_data = data.get('files', {})
                self.disk_path = path
                self.disk_mounted = True
                return True
        except Exception as e:
            print(f"DISK ERROR: {e}")
            return False

    def save_disk_image(self):
        if not self.disk_mounted or not self.disk_path: return
        try:
            with open(self.disk_path, 'w') as f:
                json.dump({'files': self.disk_data}, f, indent=2)
        except Exception as e:
            print(f"DISK SAVE ERROR: {e}")

    def create_disk_image(self, path):
        if not path.lower().endswith(".dsk"): path += ".dsk"
        try:
            with open(path, 'w') as f:
                json.dump({'files': {}}, f, indent=2)
            return self.mount_disk_image(path)
        except:
            return False

    def set_reg(self, addr, val):
        with self.lock:
            val = val & 0xFF
            if addr == MMU.ADDR_BORDER_COL:
                self.border_color = val & 0x0F
                self.settings["display"]["border_color"] = self.border_color
            elif addr == MMU.ADDR_BG_COL:
                self.bg_color_idx = val & 0x0F
                self.char_cache = {}
                self.settings["display"]["bg_color"] = self.bg_color_idx
            elif addr == MMU.ADDR_TEXT_COL:
                self.fg_color_idx = val & 0x0F
                self.settings["display"]["text_color"] = self.fg_color_idx
            elif addr == MMU.ADDR_SCREEN_PTR:
                self.screen_page = val
            elif addr == MMU.ADDR_CHARSET_PTR:
                self.charset_page = val
                self.char_cache = {}
            elif addr == MMU.ADDR_LAST_KEY:
                self.ram[addr] = val
            elif addr in (ADDR_JOY1, ADDR_JOY2):
                self.ram[addr] = val
            # Forward sound hardware memory maps to the SID/DMA controller
            elif ADDR_SND_BASE <= addr <= ADDR_SND_END:
                self.sound.set_register(addr, val)
                self.ram[addr] = val
            # Forward VDP register writes
            elif ADDR_VDP_BASE <= addr <= ADDR_VDP_END:
                self.vdp.set_register(addr, val)
                self.ram[addr] = val

    def get_reg(self, addr):
        with self.lock:
            if addr == MMU.ADDR_BORDER_COL:  return self.border_color
            elif addr == MMU.ADDR_BG_COL:    return self.bg_color_idx
            elif addr == MMU.ADDR_TEXT_COL:  return self.fg_color_idx
            elif addr == MMU.ADDR_SCREEN_PTR: return self.screen_page
            elif addr == MMU.ADDR_CHARSET_PTR: return self.charset_page
            elif addr == MMU.ADDR_LAST_KEY:  return self.ram[addr]
            elif addr in (ADDR_JOY1, ADDR_JOY2): return self.ram[addr]
            elif ADDR_SND_BASE <= addr <= ADDR_SND_END:
                return self.sound.get_register(addr)
            elif ADDR_VDP_BASE <= addr <= ADDR_VDP_END:
                return self.vdp.get_register(addr)
            return 0

    def set_cursor_x(self, val):
        with self.lock: self.cursor_x = max(0, min(int(val), self.cols - 1))

    def set_cursor_y(self, val):
        with self.lock: self.cursor_y = max(0, min(int(val), self.rows - 1))

    def get_cursor(self):
        with self.lock: return self.cursor_x, self.cursor_y

    def poke_char_rom(self, offset, val):
        with self.lock:
            char_idx = offset // 8
            self.dirty_chars.add(char_idx)

    def cls(self):
        with self.lock:
            screen_base = self.screen_page * 256
            screen_size = self.cols * self.rows
            for i in range(screen_size):
                self.ram[screen_base + i] = 32
                self.ram[MMU.ADDR_COLOR_RAM + i] = self.fg_color_idx
            self.cursor_x = 0
            self.cursor_y = 0
            self.text_dirty = True

    def write_char(self, char):
        with self.lock:
            self.text_dirty = True
            code = ord(char)
            if code in COLOR_CODES:
                self.fg_color_idx = COLOR_CODES[code]
                return
            if char == '\n':
                self.cursor_x = 0
                self.cursor_y += 1
            elif char == '\r':
                self.cursor_x = 0
            elif char == '\b':
                self.backspace()
            elif char == '\t':
                steps = 4 - (self.cursor_x % 4)
                for _ in range(steps): self._write_internal(' ')
            else:
                self._write_internal(char)
            self.check_scroll()

    def _write_internal(self, char):
        if self.cursor_y >= self.rows: return
        screen_base = self.screen_page * 256
        row_start_idx = self.cursor_y * self.cols
        row_end_idx   = row_start_idx + self.cols
        curr_offset   = row_start_idx + self.cursor_x
        should_insert = self.insert_mode and (self.editor_mode or self.input_mode)
        if should_insert:
            for i in range(row_end_idx - 1, curr_offset, -1):
                self.ram[screen_base + i] = self.ram[screen_base + i - 1]
                self.ram[MMU.ADDR_COLOR_RAM + i] = self.ram[MMU.ADDR_COLOR_RAM + i - 1]
        sc = self.ascii_to_screen_code(char)
        self.ram[screen_base + curr_offset] = sc
        self.ram[MMU.ADDR_COLOR_RAM + curr_offset] = self.fg_color_idx
        self.cursor_x += 1
        if self.cursor_x >= self.cols:
            self.cursor_x = 0
            self.cursor_y += 1

    def backspace(self):
        if self.cursor_x > 0:
            if not self.editor_mode and len(self.keyboard_buffer) > 0:
                self.keyboard_buffer = self.keyboard_buffer[:-1]
            self.cursor_x -= 1
            self.delete()

    def delete(self):
        if self.cursor_y >= self.rows: return
        screen_base   = self.screen_page * 256
        row_start_idx = self.cursor_y * self.cols
        curr_offset   = row_start_idx + self.cursor_x
        row_end_idx   = row_start_idx + self.cols
        for i in range(curr_offset, row_end_idx - 1):
            self.ram[screen_base + i] = self.ram[screen_base + i + 1]
            self.ram[MMU.ADDR_COLOR_RAM + i] = self.ram[MMU.ADDR_COLOR_RAM + i + 1]
        self.ram[screen_base + row_end_idx - 1] = 32
        self.ram[MMU.ADDR_COLOR_RAM + row_end_idx - 1] = self.fg_color_idx

    def check_scroll(self):
        if self.cursor_y >= self.rows:
            screen_base = self.screen_page * 256
            total_chars = self.cols * self.rows
            row_len     = self.cols
            self.ram[screen_base : screen_base + total_chars - row_len] = \
                self.ram[screen_base + row_len : screen_base + total_chars]
            self.ram[MMU.ADDR_COLOR_RAM : MMU.ADDR_COLOR_RAM + total_chars - row_len] = \
                self.ram[MMU.ADDR_COLOR_RAM + row_len : MMU.ADDR_COLOR_RAM + total_chars]
            bottom_start = screen_base + total_chars - row_len
            self.ram[bottom_start : bottom_start + row_len] = b'\x20' * row_len
            col_bottom = MMU.ADDR_COLOR_RAM + total_chars - row_len
            for i in range(row_len): self.ram[col_bottom + i] = self.fg_color_idx
            self.cursor_y = self.rows - 1

    def ascii_to_screen_code(self, char):
        c = ord(char)
        if 32 <= c <= 63:  return c
        if 64 <= c <= 95:  return c - 64
        if 97 <= c <= 122: return c
        if c >= 128:       return (c - 128) & 0xFF
        return 32

    def screen_code_to_ascii(self, code):
        if 0  <= code <= 31:  return chr(code + 64)
        if 32 <= code <= 63:  return chr(code)
        if 64 <= code <= 127: return chr(code)
        if 128 <= code <= 255: return chr(code)
        return ' '

    def get_line_text(self, y):
        screen_base = self.screen_page * 256
        start = screen_base + (y * self.cols)
        end   = start + self.cols
        codes = self.ram[start:end]
        return "".join([self.screen_code_to_ascii(c) for c in codes])

    def get_logical_line(self):
        screen_base = self.screen_page * 256
        start_y = self.cursor_y
        while start_y > 0:
            offset_above = ((start_y - 1) * self.cols) + (self.cols - 1)
            if self.ram[screen_base + offset_above] != 32: start_y -= 1
            else: break
        text   = ""
        curr_y = start_y
        while curr_y < self.rows:
            start     = screen_base + (curr_y * self.cols)
            end       = start + self.cols
            row_codes = self.ram[start:end]
            row_str   = "".join([self.screen_code_to_ascii(c) for c in row_codes])
            is_full   = (self.ram[screen_base + (curr_y * self.cols) + (self.cols - 1)] != 32)
            if is_full and curr_y < self.rows - 1: text += row_str
            else: text += row_str.rstrip(); break
            curr_y += 1
            if len(text) > 256: break
        return text[:256]

    def handle_enter(self):
        with self.lock:
            data_to_send = ""
            if self.editor_mode:
                raw = self.get_logical_line()
                if "READY>" in raw: raw = raw.replace("READY>", "", 1).strip()
                data_to_send = raw
            else:
                data_to_send = self.keyboard_buffer
                self.keyboard_buffer = ""
            self.cursor_x = 0
            self.cursor_y += 1
            self.check_scroll()
            if self.input_mode:
                self.input_queue.put(data_to_send)
                self.input_ready.set()

    def get_char_surface(self, char_code, color_idx):
        if char_code in self.dirty_chars:
            to_del = [k for k in self.char_cache if k[0] == char_code]
            for k in to_del: del self.char_cache[k]
            self.dirty_chars.discard(char_code)
        key = (char_code, color_idx, self.bg_color_idx, self.charset_page)
        if key in self.char_cache: return self.char_cache[key]
        surf = pygame.Surface((8, 8))
        surf.fill(COLORS[self.bg_color_idx])
        fg = COLORS[color_idx]
        base_addr = (self.charset_page * 256) + (char_code * 8)
        for r in range(8):
            byte = self.ram[base_addr + r]
            for c in range(8):
                if (byte >> (7 - c)) & 1: surf.set_at((c, r), fg)
        self.char_cache[key] = surf
        return surf

    def draw_menu(self):
        menu_w = 480
        menu_h = 400
        x = (self.window.get_width()  - menu_w) // 2
        y = (self.window.get_height() - menu_h) // 2

        shadow = pygame.Surface((menu_w, menu_h))
        shadow.set_alpha(128)
        shadow.fill((0, 0, 0))
        self.window.blit(shadow, (x + 10, y + 10))
        pygame.draw.rect(self.window, (150, 0, 0), (x, y, menu_w, menu_h))
        pygame.draw.rect(self.window, (255, 255, 255), (x, y, menu_w, menu_h), 4)

        title_font = pygame.font.SysFont("Courier", 22, bold=True)
        opt_font   = pygame.font.SysFont("Courier", 16, bold=True)
        info_font  = pygame.font.SysFont("Courier", 12)

        if self.menu_state == "MAIN":
            title = title_font.render("SYSTEM HOST MENU", True, (255, 255, 0))
            self.window.blit(title, (x + (menu_w - title.get_width()) // 2, y + 10))
            for i, opt in enumerate(self.menu_options_main):
                col    = (0, 255, 0) if i == self.menu_idx else (255, 255, 255)
                prefix = "> " if i == self.menu_idx else "  "
                txt = opt_font.render(prefix + opt, True, col)
                self.window.blit(txt, (x + 20, y + 45 + i * 28))
            disk_font = pygame.font.SysFont("Courier", 12)
            f_txt = disk_font.render(f"HD: {self.current_folder[-40:]}", True, (200, 200, 200))
            self.window.blit(f_txt, (x + 10, y + menu_h - 40))
            status = "MOUNTED" if self.disk_mounted else "EMPTY"
            fname  = os.path.basename(self.disk_path) if self.disk_mounted else ""
            d_txt  = disk_font.render(f"FD: {status} {fname}", True, (200, 200, 200))
            self.window.blit(d_txt, (x + 10, y + menu_h - 20))

        elif self.menu_state in ("JOY1", "JOY2"):
            port_key = "joystick1" if self.menu_state == "JOY1" else "joystick2"
            port_num = "1" if self.menu_state == "JOY1" else "2"
            title = title_font.render(f"JOYSTICK {port_num} CONFIG", True, (255, 255, 0))
            self.window.blit(title, (x + (menu_w - title.get_width()) // 2, y + 10))
            mapping = self.settings.get(port_key, {})
            for i, (action, label) in enumerate(zip(self.joy_actions, self.joy_labels)):
                col    = (0, 255, 0) if i == self.menu_idx else (255, 255, 255)
                prefix = "> " if i == self.menu_idx else "  "
                current = mapping.get(action, "---")
                txt = opt_font.render(f"{prefix}{label:<8} : {current}", True, col)
                self.window.blit(txt, (x + 20, y + 50 + i * 30))
            back = opt_font.render("  BACK", True, (0, 255, 0) if self.menu_idx == 8 else (255, 255, 255))
            if self.menu_idx == 8:
                back = opt_font.render("> BACK", True, (0, 255, 0))
            self.window.blit(back, (x + 20, y + 50 + 8 * 30))
            hint = info_font.render("ENTER=REMAP KEY  G=REMAP GAMEPAD BTN", True, (180, 180, 180))
            self.window.blit(hint, (x + 10, y + menu_h - 20))

        elif self.menu_state == "CAPTURE":
            title = title_font.render("PRESS A KEY...", True, (255, 255, 0))
            if self.capture_for_gamepad:
                title = title_font.render("PRESS GAMEPAD BTN...", True, (255, 255, 0))
            self.window.blit(title, (x + (menu_w - title.get_width()) // 2, y + menu_h // 2 - 20))
            hint = info_font.render("ESC TO CANCEL", True, (180, 180, 180))
            self.window.blit(hint, (x + (menu_w - hint.get_width()) // 2, y + menu_h // 2 + 20))

        elif self.menu_state == "DISPLAY":
            title = title_font.render("DISPLAY SETTINGS", True, (255, 255, 0))
            self.window.blit(title, (x + (menu_w - title.get_width()) // 2, y + 10))
            disp    = self.settings.get("display", {})
            options = [
                f"BORDER COLOR : {disp.get('border_color', 2)}",
                f"BG COLOR     : {disp.get('bg_color', 0)}",
                f"TEXT COLOR   : {disp.get('text_color', 1)}",
                "BACK"
            ]
            for i, opt in enumerate(options):
                col    = (0, 255, 0) if i == self.menu_idx else (255, 255, 255)
                prefix = "> " if i == self.menu_idx else "  "
                txt = opt_font.render(prefix + opt, True, col)
                self.window.blit(txt, (x + 20, y + 60 + i * 40))
            hint = info_font.render("LEFT/RIGHT TO CHANGE VALUE", True, (180, 180, 180))
            self.window.blit(hint, (x + 10, y + menu_h - 20))

        # --- UPDATED SOUND MENU FOR DEDICATED SID CHIP ---
        elif self.menu_state == "SOUND":
            title = title_font.render("SOUND SETTINGS", True, (255, 255, 0))
            self.window.blit(title, (x + (menu_w - title.get_width()) // 2, y + 10))
            snd        = self.settings.get("sound", {})
            vol        = self.sound.get_master_volume()
            enabled    = snd.get("enabled", True)
            options = [
                f"ENABLED      : {'YES' if enabled else 'NO'}",
                f"MASTER VOL   : {vol}",
                "BACK"
            ]
            for i, opt in enumerate(options):
                col    = (0, 255, 0) if i == self.menu_idx else (255, 255, 255)
                prefix = "> " if i == self.menu_idx else "  "
                txt = opt_font.render(prefix + opt, True, col)
                self.window.blit(txt, (x + 20, y + 45 + i * 26))
            
            chip_txt = info_font.render(
                f"ACTIVE: SID @ {self.sound.chip.SAMPLE_RATE}HZ (DMA READY)",
                True, (180, 255, 180))
            self.window.blit(chip_txt, (x + 10, y + menu_h - 20))

    def _handle_menu_input(self, event):
        if event.type != pygame.KEYDOWN: return False

        if self.menu_state == "CAPTURE":
            if event.key == pygame.K_ESCAPE:
                self.menu_state = "JOY1" if self.capture_target[0] == "joystick1" else "JOY2"
                self.capture_target = None
                return True
            key_name = pygame_key_to_name(event.key)
            if key_name:
                port_key, action = self.capture_target
                self.settings[port_key][action] = key_name
                self.save_settings()
                self.menu_state = "JOY1" if port_key == "joystick1" else "JOY2"
                self.capture_target = None
            return True

        if self.menu_state == "MAIN":
            if event.key == pygame.K_ESCAPE:
                self.menu_active = False
                return True
            if event.key == pygame.K_UP:
                self.menu_idx = (self.menu_idx - 1) % len(self.menu_options_main)
                return True
            if event.key == pygame.K_DOWN:
                self.menu_idx = (self.menu_idx + 1) % len(self.menu_options_main)
                return True
            if event.key == pygame.K_RETURN:
                sel = self.menu_options_main[self.menu_idx]
                if sel == "EXIT MENU":
                    self.menu_active = False
                elif sel == "CONFIGURE JOYSTICK 1":
                    self.menu_state = "JOY1"
                    self.menu_idx   = 0
                elif sel == "CONFIGURE JOYSTICK 2":
                    self.menu_state = "JOY2"
                    self.menu_idx   = 0
                elif sel == "DISPLAY SETTINGS":
                    self.menu_state = "DISPLAY"
                    self.menu_idx   = 0
                elif sel == "SOUND SETTINGS":
                    self.menu_state = "SOUND"
                    self.menu_idx   = 0
                elif sel == "SOFT RESET":
                    self.soft_reset    = True
                    self.break_request = True
                    self.menu_active   = False
                    self.input_queue.put("")
                    self.input_ready.set()
                elif sel == "HARD RESET":
                    self.hard_reset    = True
                    self.break_request = True
                    self.menu_active   = False
                    self.input_queue.put("")
                    self.input_ready.set()
                elif sel == "MOUNT FOLDER (CD)":
                    if self.tk_root:
                        path = filedialog.askdirectory(initialdir=self.current_folder, title="Select Hard Drive Folder")
                        if path:
                            self.current_folder = path
                            os.chdir(path)
                            self.menu_active = False
                elif sel == "MOUNT DISK IMAGE":
                    if self.tk_root:
                        path = filedialog.askopenfilename(initialdir=self.current_folder, title="Mount Disk Image", filetypes=[("Disk Image", "*.dsk")])
                        if path:
                            if self.mount_disk_image(path): self.menu_active = False
                elif sel == "CREATE NEW DISK":
                    if self.tk_root:
                        path = filedialog.asksaveasfilename(initialdir=self.current_folder, title="Create New Disk", defaultextension=".dsk", filetypes=[("Disk Image", "*.dsk")])
                        if path:
                            if self.create_disk_image(path): self.menu_active = False
                elif sel == "UNMOUNT DISK":
                    self.disk_mounted = False
                    self.disk_path    = ""
                    self.disk_data    = {}
                return True

        if self.menu_state in ("JOY1", "JOY2"):
            port_key   = "joystick1" if self.menu_state == "JOY1" else "joystick2"
            num_opts   = len(self.joy_actions) + 1  
            if event.key == pygame.K_ESCAPE:
                self.menu_state = "MAIN"
                self.menu_idx   = 0
                return True
            if event.key == pygame.K_UP:
                self.menu_idx = (self.menu_idx - 1) % num_opts
                return True
            if event.key == pygame.K_DOWN:
                self.menu_idx = (self.menu_idx + 1) % num_opts
                return True
            if event.key == pygame.K_RETURN:
                if self.menu_idx == len(self.joy_actions):
                    self.menu_state = "MAIN"
                    self.menu_idx   = 0
                else:
                    action = self.joy_actions[self.menu_idx]
                    self.capture_target    = (port_key, action)
                    self.capture_for_gamepad = False
                    self.menu_state        = "CAPTURE"
                return True
            if event.key == pygame.K_g:
                if self.menu_idx < len(self.joy_actions):
                    action = self.joy_actions[self.menu_idx]
                    self.capture_target      = (port_key, action)
                    self.capture_for_gamepad = True
                    self.menu_state          = "CAPTURE"
                return True
            return True

        if self.menu_state == "DISPLAY":
            disp_keys = ["border_color", "bg_color", "text_color"]
            if event.key == pygame.K_ESCAPE:
                self.menu_state = "MAIN"
                self.menu_idx   = 0
                return True
            if event.key == pygame.K_UP:
                self.menu_idx = (self.menu_idx - 1) % 4
                return True
            if event.key == pygame.K_DOWN:
                self.menu_idx = (self.menu_idx + 1) % 4
                return True
            if event.key == pygame.K_RETURN and self.menu_idx == 3:
                self.menu_state = "MAIN"
                self.menu_idx   = 0
                return True
            if event.key in (pygame.K_LEFT, pygame.K_RIGHT) and self.menu_idx < 3:
                key  = disp_keys[self.menu_idx]
                val  = self.settings["display"].get(key, 0)
                val += 1 if event.key == pygame.K_RIGHT else -1
                val  = max(0, min(15, val))
                self.settings["display"][key] = val
                if key == "border_color": self.border_color = val
                elif key == "bg_color":   self.bg_color_idx = val; self.char_cache = {}
                elif key == "text_color": self.fg_color_idx = val
                self.save_settings()
                return True
            return True

        # --- UPDATED SOUND MENU INPUT ---
        if self.menu_state == "SOUND":
            num_opts = 3
            if event.key == pygame.K_ESCAPE:
                self.menu_state = "MAIN"
                self.menu_idx   = 0
                return True
            if event.key == pygame.K_UP:
                self.menu_idx = (self.menu_idx - 1) % num_opts
                return True
            if event.key == pygame.K_DOWN:
                self.menu_idx = (self.menu_idx + 1) % num_opts
                return True
            snd = self.settings.setdefault("sound", {})
            if event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                delta = 1 if event.key == pygame.K_RIGHT else -1
                if self.menu_idx == 0:
                    enabled = not snd.get("enabled", True)
                    snd["enabled"] = enabled
                    if enabled: self.sound.start()
                    else:       self.sound.stop()
                    self.save_settings()
                elif self.menu_idx == 1:
                    vol = self.sound.get_master_volume()
                    vol = max(0, min(15, vol + delta))
                    self.sound.set_master_volume(vol)
                    snd["master_volume"] = vol
                    self.save_settings()
                return True
            if event.key == pygame.K_RETURN:
                if self.menu_idx == 2:
                    self.menu_state = "MAIN"
                    self.menu_idx   = 0
                return True
            return True

        return False
    def render(self):
        # Only hold lock briefly to snapshot what we need
        with self.lock:
            border_col   = self.border_color
            bg_col       = self.bg_color_idx
            fg_col       = self.fg_color_idx
            vdp_mode     = self.vdp._reg(VDP_MODE)
            text_dirty   = self.text_dirty
            editor_mode  = self.editor_mode
            input_mode   = self.input_mode
            cursor_x     = self.cursor_x
            cursor_y     = self.cursor_y
            insert_mode  = self.insert_mode
            menu_active  = self.menu_active

            # Rebuild text surface while holding lock (reads screen RAM)
            if text_dirty or editor_mode or input_mode:
                self.text_dirty = False
                self.text_surface.fill(self._text_transparent)
                if vdp_mode == 0:
                    pygame.draw.rect(self.text_surface,
                                     COLORS[bg_col],
                                     (0, 0, SCREEN_WIDTH_PX, SCREEN_HEIGHT_PX))
                screen_base = self.screen_page * 256
                for y in range(self.rows):
                    for x in range(self.cols):
                        offset  = (y * self.cols) + x
                        code    = self.ram[screen_base + offset]
                        col_idx = self.ram[MMU.ADDR_COLOR_RAM + offset] & 0x0F
                        if code == 32: continue
                        tile = self.get_char_surface(code, col_idx)
                        self.text_surface.blit(tile, (x * 8, y * 8))

        # Cursor blink — outside lock
        if time.time() - self.last_blink > 0.5:
            self.cursor_visible = not self.cursor_visible
            self.last_blink = time.time()

        # Composite — outside lock, VDP reads RAM directly
        self.window.fill(COLORS[border_col])
        final = self.vdp.composite(self.text_surface)

        # Cursor on top
        if self.cursor_visible and (editor_mode or input_mode) and not menu_active:
            if insert_mode:
                rect = (cursor_x * 8, (cursor_y * 8) + 4, 8, 4)
            else:
                rect = (cursor_x * 8, cursor_y * 8, 8, 8)
            pygame.draw.rect(final, COLORS[fg_col], rect)

        # Blit to window
        if self._use_scaled:
            self.window.blit(final, (BORDER_SIZE, BORDER_SIZE))
        else:
            scaled = pygame.transform.scale(
                final,
                (SCREEN_WIDTH_PX * SCALE_FACTOR, SCREEN_HEIGHT_PX * SCALE_FACTOR))
            self.window.blit(scaled, (BORDER_SIZE, BORDER_SIZE))

        if menu_active:
            self.draw_menu()

        pygame.display.flip()

    def process_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                self.held_keys.add(event.key)
            elif event.type == pygame.KEYUP:
                self.held_keys.discard(event.key)

            if event.type == pygame.KEYDOWN:
                if self.menu_active:
                    self._handle_menu_input(event)
                    continue

                if event.key == pygame.K_F12:
                    self.menu_active = True
                    self.menu_state  = "MAIN"
                    self.menu_idx    = 0
                elif event.key == pygame.K_ESCAPE:
                    self.break_request = True
                elif event.key == pygame.K_RETURN:
                    self.handle_enter()
                elif event.key == pygame.K_INSERT:
                    self.insert_mode = not self.insert_mode
                elif event.key == pygame.K_DELETE:
                    with self.lock: self.delete()
                elif event.key == pygame.K_BACKSPACE:
                    with self.lock: self.backspace()
                elif event.key == pygame.K_UP:
                    with self.lock:
                        if self.cursor_y > 0: self.cursor_y -= 1
                elif event.key == pygame.K_DOWN:
                    with self.lock:
                        if self.cursor_y < self.rows - 1: self.cursor_y += 1
                elif event.key == pygame.K_LEFT:
                    with self.lock:
                        if self.cursor_x > 0: self.cursor_x -= 1
                elif event.key == pygame.K_RIGHT:
                    with self.lock:
                        if self.cursor_x < self.cols - 1: self.cursor_x += 1
                elif event.key == pygame.K_HOME:
                    with self.lock: self.cursor_x = 0; self.cursor_y = 0
                elif event.key == pygame.K_v and (event.mod & pygame.KMOD_CTRL or event.mod & pygame.KMOD_META):
                    if self.tk_root:
                        try:
                            clip_text = self.tk_root.clipboard_get()
                            clip_text = clip_text.replace('\r\n', '\n').replace('\r', '\n')
                            with self.lock:
                                for char in clip_text:
                                    if char == '\n': self.handle_enter()
                                    elif char.isprintable():
                                        c = char.upper() if self.caps_lock else char
                                        self.write_char(c)
                        except Exception: pass
                else:
                    if len(event.unicode) > 0 and event.unicode.isprintable():
                        char = event.unicode
                        if self.caps_lock: char = char.upper()
                        if event.mod & pygame.KMOD_ALT:
                            try:
                                code = ord(char.upper())
                                if 32 <= code <= 126: char = chr(code + 128)
                            except: pass
                        self.key_queue.put(char)
                        if self.editor_mode or self.input_mode:
                            if self.input_mode and not self.editor_mode:
                                self.keyboard_buffer += char
                            self.write_char(char)

        self.update_joystick()


class SystemInterpreter(BASICInterpreter):
    """
    BASICInterpreter subclass wired into the VirtualMachine.

    Key differences from the standalone interpreter:
      - self.mmu.physical_ram points to vm.ram so BASIC and all hardware
        subsystems share the same bytearray
      - print_handler writes characters to the virtual screen via vm.write_char()
        instead of printing to the terminal
      - input_handler blocks waiting on vm.input_queue (filled by Pygame key events)
        instead of calling Python's input()
      - check_break_fn is called before every BASIC statement to test for ESC,
        soft reset, or hard reset requests from the Pygame thread
      - MMU hooks route all hardware register addresses to the VM's set_reg/get_reg
      - dynamic_hook_write extends mmu.write() to monitor charset ROM writes
        (invalidates character cache) and tilemap/tile data writes (marks VDP dirty)

    The REPL loop (repl()) handles the READY> prompt and all immediate-mode
    commands (RUN, LIST, LOAD, SAVE, DSAVE, DDIR etc.).
    """
    def __init__(self, vm_ref):
        super().__init__()
        self.vm = vm_ref

        self.mmu.physical_ram = self.vm.ram
        self.mmu._load_internal_font()
        self.vm.cls()

        self.print_handler = self._virtual_print
        self.input_handler = self._virtual_input

        self.disk_mount_check = lambda: self.vm.disk_mounted
        self.disk_read        = lambda fn: self.vm.disk_data.get(fn)

        def write_disk_hook(fn, content):
            self.vm.disk_data[fn] = content
            self.vm.save_disk_image()
        self.disk_write = write_disk_hook
        self.disk_list  = lambda: list(self.vm.disk_data.keys())

        def check_interruption_and_sync():
            """
            Called by the BASIC interpreter before every statement.
            Transfers any pending keyboard character into the LAST_KEY register
            so the GET command can read it without polling.
            Returns True to signal BREAK if ESC was pressed or a reset was requested.
            No sleep here — adding sleep on every statement kills BASIC performance.
            """
            current_val = self.vm.get_reg(MMU.ADDR_LAST_KEY)
            if current_val == 0 and not self.vm.key_queue.empty():
                try:
                    char = self.vm.key_queue.get_nowait()
                    self.vm.set_reg(MMU.ADDR_LAST_KEY, ord(char))
                except: pass
            if self.vm.soft_reset or self.vm.hard_reset: return True
            if self.vm.break_request:
                self.vm.sound.dma_active = False
                self.vm.sound.chip.reset()
                return True
            return self.vm.break_request

        self.check_break_fn = check_interruption_and_sync

        # MMU hooks — intercept POKE/PEEK for all hardware register addresses.
        # Each hook routes to the VM's set_reg/get_reg which dispatches to the
        # correct subsystem (VDP, SID, display registers, joystick ports).
        self.mmu.attach_hook(MMU.ADDR_SCREEN_CMD,  write_cb=self._on_cls_cmd)
        self.mmu.attach_hook(MMU.ADDR_BORDER_COL,  write_cb=lambda v: self.vm.set_reg(MMU.ADDR_BORDER_COL, v),  read_cb=lambda: self.vm.get_reg(MMU.ADDR_BORDER_COL))
        self.mmu.attach_hook(MMU.ADDR_BG_COL,      write_cb=lambda v: self.vm.set_reg(MMU.ADDR_BG_COL, v),      read_cb=lambda: self.vm.get_reg(MMU.ADDR_BG_COL))
        self.mmu.attach_hook(MMU.ADDR_TEXT_COL,    write_cb=lambda v: self.vm.set_reg(MMU.ADDR_TEXT_COL, v),    read_cb=lambda: self.vm.get_reg(MMU.ADDR_TEXT_COL))
        self.mmu.attach_hook(MMU.ADDR_SCREEN_PTR,  write_cb=lambda v: self.vm.set_reg(MMU.ADDR_SCREEN_PTR, v),  read_cb=lambda: self.vm.get_reg(MMU.ADDR_SCREEN_PTR))
        self.mmu.attach_hook(MMU.ADDR_CHARSET_PTR, write_cb=lambda v: self.vm.set_reg(MMU.ADDR_CHARSET_PTR, v), read_cb=lambda: self.vm.get_reg(MMU.ADDR_CHARSET_PTR))
        self.mmu.attach_hook(ADDR_CURSOR_COL,      write_cb=lambda v: self.vm.set_cursor_x(v))
        self.mmu.attach_hook(ADDR_CURSOR_ROW,      write_cb=lambda v: self.vm.set_cursor_y(v))
        self.mmu.attach_hook(MMU.ADDR_LAST_KEY,
                             write_cb=lambda v: self.vm.set_reg(MMU.ADDR_LAST_KEY, v),
                             read_cb=lambda: self.vm.get_reg(MMU.ADDR_LAST_KEY))
        self.mmu.attach_hook(ADDR_JOY1,
                             write_cb=lambda v: self.vm.set_reg(ADDR_JOY1, v),
                             read_cb=lambda: self.vm.get_reg(ADDR_JOY1))
        self.mmu.attach_hook(ADDR_JOY2,
                             write_cb=lambda v: self.vm.set_reg(ADDR_JOY2, v),
                             read_cb=lambda: self.vm.get_reg(ADDR_JOY2))

        # SID chip hooks — all 50 registers from $C030 to $C061
        for _snd_addr in range(ADDR_SND_BASE, ADDR_SND_END + 1):
            _a = _snd_addr
            self.mmu.attach_hook(
                _a,
                write_cb=lambda v, a=_a: self.vm.set_reg(a, v),
                read_cb=lambda  a=_a:    self.vm.get_reg(a)
            )

        # VDP hooks — control and collision registers $C080 to $C09F
        for _vdp_addr in range(ADDR_VDP_BASE, ADDR_VDP_END + 1):
            _a = _vdp_addr
            self.mmu.attach_hook(
                _a,
                write_cb=lambda v, a=_a: self.vm.set_reg(a, v),
                read_cb =lambda   a=_a:  self.vm.get_reg(a)
            )

        # Replace mmu.write with an extended version that monitors data regions.
        # This is needed because sprite tables, tilemap, and tile data live in
        # normal RAM (not the I/O page) so they don't trigger the hook mechanism.
        # We watch for writes to those regions and trigger the appropriate cache
        # invalidation or dirty flags.
        original_write = self.mmu.write
        def dynamic_hook_write(addr, val):
            # Charset ROM — invalidate the character surface cache for that char
            start = self.vm.charset_page * 256
            if start <= addr < start + 2048:
                self.vm.poke_char_rom(addr - start, val)
            # VDP tile data — invalidate cached tile/sprite surfaces for that tile
            if VDP_TILE_DATA_BASE <= addr < VDP_TILE_DATA_BASE + MAX_TILES * TILE_BYTES:
                tile_idx = (addr - VDP_TILE_DATA_BASE) // TILE_BYTES
                self.vm.vdp.invalidate_tile(tile_idx)
            # VDP tilemap — mark background as needing redraw
            if VDP_TILEMAP_BASE <= addr < VDP_TILEMAP_BASE + 1000:
                self.vm.vdp.bg_dirty = True
            # Screen RAM or color RAM — mark text layer as needing redraw
            screen_base = self.vm.screen_page * 256
            if screen_base <= addr < screen_base + self.vm.cols * self.vm.rows:
                self.vm.text_dirty = True
            elif MMU.ADDR_COLOR_RAM <= addr < MMU.ADDR_COLOR_RAM + self.vm.cols * self.vm.rows:
                self.vm.text_dirty = True
            original_write(addr, val)
        self.mmu.write = dynamic_hook_write

    def _on_cls_cmd(self, val):
        if val == 1: self.vm.cls()

    def _virtual_print(self, text):
        with self.vm.lock:
            for c in text: self.vm.write_char(c)

    def _virtual_input(self, prompt):
        with self.vm.lock:
            for c in prompt: self.vm.write_char(c)
        self.vm.editor_mode    = False
        self.vm.keyboard_buffer = ""
        self.vm.input_mode     = True
        if self.vm.input_queue.empty():
            self.vm.input_ready.clear()
            self.vm.input_ready.wait()
        if not self.vm.running: raise KeyboardInterrupt("VM KILLED")
        data = self.vm.input_queue.get()
        self.vm.input_mode = False
        return data

    def repl(self):
        msg = "\n                        RETRO-THREE.14 8-BIT SYSTEM V1.5\n                             512KB RAM SYSTEM READY\n                            TYPE 'HELP' FOR COMMANDS\n"
        for c in msg: self.vm.write_char(c)
        if self.mmu.fallback_active:
            warn = "\n* WARNING: 'CHARGEN' ROM NOT FOUND *\n* USING INTERNAL FALLBACK FONT *\n"
            for c in warn: self.vm.write_char(c)

        suppress_prompt = False
        while self.vm.running:

            if self.vm.get_reg(MMU.ADDR_LAST_KEY) == 0 and not self.vm.key_queue.empty():
                try:
                    char = self.vm.key_queue.get_nowait()
                    self.vm.set_reg(MMU.ADDR_LAST_KEY, ord(char))
                except: pass

            if self.vm.hard_reset:
                self.vm.hard_reset     = False
                self.vm.break_request  = False
                self.vm.ram[:]         = bytearray(MMU.MEMORY_SIZE)
                self.mmu.reset()
                self.vm.set_reg(MMU.ADDR_CHARSET_PTR, DEFAULT_CHARSET_PAGE)
                self.vm.set_reg(MMU.ADDR_SCREEN_PTR,  DEFAULT_SCREEN_PAGE)
                self.vm.set_reg(MMU.ADDR_BORDER_COL,  self.vm.settings["display"]["border_color"])
                self.vm.set_reg(MMU.ADDR_BG_COL,      self.vm.settings["display"]["bg_color"])
                self.vm.set_reg(MMU.ADDR_TEXT_COL,    self.vm.settings["display"]["text_color"])
                # Reset VDP — turn off mode, sprites, clear caches
                self.vm.vdp.shadow[VDP_MODE - VDP_REG_BASE] = 0
                self.vm.vdp.shadow[0xC083 - VDP_REG_BASE]   = 0  # VDP_SPRITE_EN off
                self.vm.vdp.invalidate_all()
                self.vm.vdp.bg_dirty = True
                self.vm.cls()
                self.reset()
                with self.vm.key_queue.mutex: self.vm.key_queue.queue.clear()
                self.vm.set_reg(MMU.ADDR_LAST_KEY, 0)
                boot_msg = "\n                        RETRO-THREE.14 8-BIT SYSTEM V1.5\n                             512KB RAM SYSTEM READY\n                            TYPE 'HELP' FOR COMMANDS\n"
                for c in boot_msg: self.vm.write_char(c)
                continue

            if self.vm.soft_reset:
                self.vm.soft_reset    = False
                self.vm.break_request = False
                # Reset VDP — turn off mode and sprites, clear caches
                self.vm.vdp.shadow[VDP_MODE - VDP_REG_BASE] = 0
                self.vm.vdp.shadow[0xC083 - VDP_REG_BASE]   = 0  # VDP_SPRITE_EN off
                self.vm.vdp.invalidate_all()
                self.vm.vdp.bg_dirty = True
                self.vm.cls()
                with self.vm.key_queue.mutex: self.vm.key_queue.queue.clear()
                self.vm.set_reg(MMU.ADDR_LAST_KEY, 0)
                for c in "\n*** WARM RESET ***\nREADY.\n": self.vm.write_char(c)
                continue

            try:
                self.vm.break_request = False
                if not suppress_prompt:
                    cx, cy = self.vm.get_cursor()
                    if cx > 0: self.vm.write_char('\n')
                    for c in 'READY>\n': self.vm.write_char(c)

                self.vm.editor_mode = True
                self.vm.input_mode  = True
                if self.vm.input_queue.empty():
                    self.vm.input_ready.clear()
                    self.vm.input_ready.wait()
                if not self.vm.running: break

                line = self.vm.input_queue.get()
                self.vm.input_mode  = False
                self.vm.editor_mode = False

                if not line.strip(): continue
                if line.strip()[0].isdigit(): suppress_prompt = True
                else: suppress_prompt = False

                f = io.StringIO()
                with redirect_stdout(f):
                    parts = line.strip().split(maxsplit=1)
                    cmd   = parts[0].upper()
                    args  = parts[1] if len(parts) > 1 else ""

                    if cmd == 'RUN':
                        with self.vm.key_queue.mutex: self.vm.key_queue.queue.clear()
                        self.vm.set_reg(MMU.ADDR_LAST_KEY, 0)
                        self.run()
                    elif cmd == 'LIST':
                        s, e = None, None
                        if args:
                            args = args.strip()
                            if '-' in args:
                                p = args.split('-')
                                if p[0].strip(): s = int(p[0])
                                if p[1].strip(): e = int(p[1])
                            else:
                                s = int(args); e = s
                        self.list_prog(s, e)
                    elif cmd == 'NEW':
                        with self.vm.key_queue.mutex: self.vm.key_queue.queue.clear()
                        self.vm.set_reg(MMU.ADDR_LAST_KEY, 0)
                        self.reset()
                    elif cmd == 'CLS':
                        self.vm.cls()
                    elif cmd in ('EXIT', 'QUIT', 'BYE'):
                        self.vm.running = False; return
                    elif cmd in ('DIR', 'FILES'):
                        self.catalog()
                    elif cmd == 'HELP':
                        self.show_help()
                    elif cmd == 'DDIR':
                        if not self.vm.disk_mounted: print("?DISK NOT MOUNTED")
                        else:
                            print("DISK IMAGE FILES:")
                            if not self.vm.disk_data: print("  (EMPTY)")
                            else:
                                for file in sorted(self.vm.disk_data.keys()): print(f"  \"{file}\"")
                    elif cmd == 'DSAVE':
                        if not self.vm.disk_mounted: print("?DISK NOT MOUNTED")
                        elif args:
                            fname = args.strip().strip('"').upper()
                            if not fname.endswith(".BAS"): fname += ".BAS"
                            content = ""
                            for ln in sorted(self.prog.keys()):
                                content += f"{ln} {self.reconstruct(self.prog[ln])}\n"
                            self.disk_write(fname, content)
                            print(f"SAVED \"{fname}\" TO DISK IMAGE")
                        else: print("USAGE: DSAVE \"FILENAME\"")
                    elif cmd in ('CHDIR', 'CD'):
                        try:
                            os.chdir(args.strip().strip('"'))
                            print(f"DIR CHANGED: {Path(os.fspath(os.getcwd())).as_posix().upper()}")
                        except Exception as ex: print(f"ERROR: {str(ex).upper()}")
                    elif cmd == 'DEL':
                        if not args: print("USAGE: DEL \"HOST_FILENAME\"")
                        else:
                            fname = args.strip().strip('"')
                            try:
                                os.remove(fname)
                                print(f"DELETED HOST FILE: \"{fname.upper()}\"")
                            except Exception as e: print(f"ERROR: {str(e).upper()}")
                    elif cmd == 'DDEL':
                        if not self.vm.disk_mounted: print("?DISK NOT MOUNTED")
                        elif args:
                            fname = args.strip().strip('"').upper()
                            if not fname.endswith(".BAS") and not fname.endswith(".BIN"): fname += ".BAS"
                            if fname in self.vm.disk_data:
                                del self.vm.disk_data[fname]
                                self.vm.save_disk_image()
                                print(f"DELETED \"{fname}\" FROM DISK IMAGE")
                            else: print(f"?FILE '{fname}' NOT FOUND ON DISK")
                        else: print("USAGE: DDEL \"FILENAME\"")
                    
                    elif cmd == 'LOAD':
                        if not args: print("USAGE: LOAD \"HOST_FILENAME\"")
                        else:
                            fname = args.strip().strip('"')
                            try:
                                with open(fname, 'r') as load_file:
                                    raw_lines = load_file.readlines()
                                self.reset()
                                for line_l in raw_lines:
                                    line_l = line_l.strip()
                                    if not line_l: continue
                                    parts_l = line_l.split(maxsplit=1)
                                    if parts_l and parts_l[0].isdigit():
                                        self.load(parts_l[1] if len(parts_l) > 1 else "", int(parts_l[0]))
                                print(f"LOADED \"{fname.upper()}\" FROM HOST")
                            except FileNotFoundError: print("ERROR: FILE NOT FOUND ON HOST")
                            except Exception as e:    print(f"ERROR: {str(e).upper()}")
                    elif cmd == 'SAVE':
                        if not args: print("USAGE: SAVE \"HOST_FILENAME\"")
                        else:
                            fname = args.strip().strip('"')
                            try:
                                with open(fname, 'w') as save_file:
                                    for ln in sorted(self.prog.keys()):
                                        save_file.write(f"{ln} {self.reconstruct(self.prog[ln])}\n")
                                print(f"SAVED \"{fname.upper()}\" TO HOST SYSTEM")
                            except Exception as e: print(f"ERROR: {str(e).upper()}")
                    elif cmd == 'IMPORT':
                        if not self.vm.disk_mounted: print("?DISK NOT MOUNTED")
                        elif not args: print("USAGE: IMPORT \"HOST_FILENAME\"")
                        else:
                            host_fname = args.strip().strip('"')
                            if not os.path.exists(host_fname): print(f"ERROR: HOST FILE '{host_fname.upper()}' NOT FOUND")
                            else:
                                try:
                                    with open(host_fname, 'rb') as import_file: content = import_file.read()
                                    disk_fname = os.path.basename(host_fname).upper()
                                    
                                    # Write binary lists directly to disk json
                                    if disk_fname.endswith('.BIN'):
                                        self.disk_write(disk_fname, list(content))
                                    else:
                                        self.disk_write(disk_fname, content.decode('latin-1'))
                                        
                                    print(f"IMPORTED \"{host_fname.upper()}\" -> \"{disk_fname}\" ON DISK")
                                except Exception as e: print(f"IMPORT ERROR: {str(e).upper()}")
                    elif line.strip()[0].isdigit():
                        ln   = int(parts[0])
                        rest = args.strip()
                        if rest == "":
                            if ln in self.prog: del self.prog[ln]
                        else: self.load(rest, ln)
                    else:
                        self.run_immediate(line)

                out = f.getvalue()
                for c in out: self.vm.write_char(c)

            except Exception as e:
                err = f"\nERROR: {str(e).upper()}\n"
                for c in err: self.vm.write_char(c)
                suppress_prompt = False


def run_cpu(vm):
    time.sleep(0.5)
    interp = SystemInterpreter(vm)
    interp.repl()
    vm.running = False


def main():
    vm    = VirtualMachine()
    t     = threading.Thread(target=run_cpu, args=(vm,), daemon=True)
    t.start()
    clock = pygame.time.Clock()
    while vm.running:
        vm.process_events()
        vm.render()
        clock.tick(60)
    vm.sound.stop()
    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()