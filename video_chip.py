"""
video_chip.py — VDP Video Display Processor for RetroThree
===========================================================
Composites the game display from four layers each frame.
Called from the Pygame main thread — all Surface operations stay
on the thread that owns the display context.

Compositing order (bottom to top):
  1. Background tilemap  — opaque, cached, redraws only when dirty
  2. Sprites (below text) — priority=0 sprites, SRCALPHA
  3. Text layer          — transparent bg, colorkey masked, always on top of bg
  4. Sprites (above text) — priority=1 sprites, for explosions etc.

BASIC communicates with the VDP entirely through memory:
  - POKE to VDP register addresses ($C080-$C09F) via MMU hooks
  - POKE to sprite table RAM ($2000-$23FF) directly
  - POKE to tilemap RAM ($2400-$27E7) directly
  - DLOAD to tile data RAM ($2800-$67FF) for loading tile sheets

Sprite entry format (8 bytes, full 16-bit X and Y):
  Byte 0  X lo      X position low byte
  Byte 1  X hi      X position high byte (full 16-bit, 0-65535)
  Byte 2  Y lo      Y position low byte
  Byte 3  Y hi      Y position high byte
  Byte 4  TILE      Tile index (0-based) into tile data region
  Byte 5  COLOR     Palette color index 0-15 (used in single-color mode)
  Byte 6  FLAGS     bit0=enabled  bit1=flipH  bit2=flipV  bit3=priority
                    bit4-5=collision group (0=none, stored as group-1)
                    bit6=single-color mode (0=multicolor, 1=color override)
  Byte 7  Reserved

Sprite positioning: x,y is the CENTER of the sprite.
Collision bounding boxes are centered on x,y to match rendering.

Tile format: 16x16 pixels, 2 pixels per byte (nibbles).
  High nibble = left pixel, low nibble = right pixel.
  Color index 0 = transparent for sprites, opaque for background tiles.
  128 bytes per tile. Load tile sheets with DLOAD "FILE.BIN", 10240.

Collision detection:
  Each frame _run_collision() checks bounding box overlaps between groups.
  Results are written to both self.ram and self.shadow so PEEK() works
  regardless of whether it reads through the MMU hook or raw RAM.
  HIT register stores the group-B sprite index (the thing being hit).
  HIT+1 register stores the group-A sprite index (the thing doing the hitting).
"""

import threading
import time

# ==========================
# VDP Hardware Register Addresses
# ==========================

# Control
VDP_MODE        = 0xC080  # 49280  0=text-only 1=tile+sprite 2=bitmap(future)
VDP_SCROLL_X    = 0xC081  # 49281  tilemap scroll X (0-255)
VDP_SCROLL_Y    = 0xC082  # 49282  tilemap scroll Y (0-255)
VDP_SPRITE_EN   = 0xC083  # 49283  master sprite enable (1=on)
VDP_BG_PAGE     = 0xC084  # 49284  tilemap page select (matches extended RAM bank)
VDP_TILE_PAGE   = 0xC085  # 49285  tile image data page select
VDP_LAYER_PRI   = 0xC086  # 49286  bit0=bg behind text, bit1=sprites behind text
VDP_PALETTE     = 0xC087  # 49287  active palette bank (0-3)

# Collision result registers
COLL_12         = 0xC090  # 49296
COLL_13         = 0xC091  # 49297
COLL_14         = 0xC092  # 49298
COLL_23         = 0xC093  # 49299
COLL_24         = 0xC094  # 49300
COLL_34         = 0xC095  # 49301

# Hit list — index of first sprite involved in each collision pair
HIT_12          = 0xC098  # 49304
HIT_13          = 0xC099  # 49305
HIT_14          = 0xC09A  # 49306
HIT_23          = 0xC09B  # 49307
HIT_24          = 0xC09C  # 49308
HIT_34          = 0xC09D  # 49309

VDP_REG_BASE    = VDP_MODE
VDP_REG_END     = 0xC09F  # 49311

# Range constants for virtual_pc.py register routing
ADDR_VDP_BASE   = VDP_MODE    # 0xC080 = 49280
ADDR_VDP_END    = VDP_REG_END # 0xC09F = 49311

# ==========================
# VDP Data Regions — all within 64KB, in the free space $2000-$6800
# Safe: above screen RAM end ($1080), below IO registers ($C000)
# ==========================
VDP_SMALL_SPR_BASE  = 0x2000   # 8192    64 * 8 = 512 bytes
VDP_LARGE_SPR_BASE  = 0x2200   # 8704    64 * 8 = 512 bytes
VDP_TILEMAP_BASE    = 0x2400   # 9216    40*25  = 1000 bytes
VDP_TILE_DATA_BASE  = 0x2800   # 10240   128 tiles * 128 bytes = 16384 bytes

SMALL_SPR_COUNT = 64
LARGE_SPR_COUNT = 64
SMALL_SPR_SIZE  = 16
LARGE_SPR_SIZE  = 32
SPR_ENTRY_BYTES = 8

TILE_W          = 16
TILE_H          = 16
TILE_BYTES      = (TILE_W * TILE_H) // 2   # 128 — 2 pixels per byte (nibbles)
MAX_TILES       = 128

TILEMAP_COLS    = 40
TILEMAP_ROWS    = 25

# Sprite flag bits
SPR_ENABLED       = 0x01
SPR_FLIP_H        = 0x02
SPR_FLIP_V        = 0x04
SPR_PRIORITY      = 0x08   # 1 = draw above text layer
SPR_GROUP_MASK    = 0x30   # bits 4-5 = group 0-3 (stored as 0-3, displayed as 1-4)
SPR_GROUP_SHIFT   = 4
SPR_SINGLE_COLOR  = 0x40   # bit 6: 0=multicolor (use tile nibbles), 1=single color (color field overrides)

# ==========================
# Palette Banks
# All four banks share index 0 = transparent/black.
# Text layer always uses TEXT_PALETTE (fixed, never changes).
# ==========================

TEXT_PALETTE = [
    (0,   0,   0),      # 0  Black
    (255, 255, 255),    # 1  White
    (136, 0,   0),      # 2  Red
    (170, 255, 238),    # 3  Cyan
    (204, 68,  204),    # 4  Purple
    (0,   204, 85),     # 5  Green
    (0,   0,   170),    # 6  Blue
    (238, 238, 119),    # 7  Yellow
    (221, 136, 85),     # 8  Orange
    (102, 68,  0),      # 9  Brown
    (205, 170, 125),    # 10 Tan
    (51,  51,  51),     # 11 Dark Grey
    (119, 119, 119),    # 12 Mid Grey
    (170, 255, 102),    # 13 Light Green
    (0,   136, 255),    # 14 Light Blue
    (187, 187, 187),    # 15 Light Grey
]

VDP_PALETTES = [
    # Bank 0 — Default (matches text palette, familiar starting point)
    TEXT_PALETTE[:],

    # Bank 1 — Warm (reds, oranges, golds)
    [
        (0,   0,   0),      # 0  Black
        (255, 248, 220),    # 1  Cream
        (180, 20,  20),     # 2  Deep Red
        (255, 200, 160),    # 3  Peach
        (200, 50,  150),    # 4  Magenta
        (180, 200, 50),     # 5  Yellow Green
        (60,  0,   160),    # 6  Indigo
        (255, 200, 0),      # 7  Gold
        (200, 90,  20),     # 8  Burnt Orange
        (80,  40,  0),      # 9  Dark Brown
        (220, 190, 140),    # 10 Sand
        (40,  30,  20),     # 11 Charcoal
        (140, 120, 100),    # 12 Warm Grey
        (200, 255, 50),     # 13 Lime
        (100, 180, 255),    # 14 Sky Blue
        (210, 200, 170),    # 15 Buff
    ],

    # Bank 2 — Cool (blues, greens, purples)
    [
        (0,   0,   0),      # 0  Black
        (240, 248, 255),    # 1  Ice White
        (160, 0,   60),     # 2  Crimson
        (100, 255, 220),    # 3  Aqua
        (140, 60,  220),    # 4  Violet
        (0,   180, 100),    # 5  Forest Green
        (0,   40,  200),    # 6  Deep Blue
        (200, 240, 80),     # 7  Chartreuse
        (180, 120, 60),     # 8  Copper
        (60,  40,  20),     # 9  Espresso
        (160, 200, 180),    # 10 Sage
        (20,  30,  50),     # 11 Midnight
        (80,  110, 140),    # 12 Steel Blue
        (80,  255, 160),    # 13 Mint
        (40,  100, 255),    # 14 Cobalt
        (180, 200, 220),    # 15 Pale Blue
    ],

    # Bank 3 — Muted / Earthy (natural, RPG-friendly)
    [
        (0,   0,   0),      # 0  Black
        (235, 225, 210),    # 1  Parchment
        (120, 40,  30),     # 2  Brick
        (180, 210, 190),    # 3  Mist
        (130, 70,  130),    # 4  Dusty Purple
        (80,  140, 60),     # 5  Olive Green
        (50,  70,  130),    # 6  Denim
        (200, 180, 80),     # 7  Straw
        (180, 110, 50),     # 8  Clay
        (90,  60,  30),     # 9  Umber
        (190, 160, 120),    # 10 Khaki
        (45,  40,  35),     # 11 Near Black
        (110, 100, 90),     # 12 Slate
        (130, 170, 100),    # 13 Fern
        (90,  130, 180),    # 14 Cornflower
        (200, 195, 185),    # 15 Stone
    ],
]

# Collision pair definitions: (group_a, group_b, coll_reg, hit_reg)
COLLISION_PAIRS = [
    (1, 2, COLL_12, HIT_12),
    (1, 3, COLL_13, HIT_13),
    (1, 4, COLL_14, HIT_14),
    (2, 3, COLL_23, HIT_23),
    (2, 4, COLL_24, HIT_24),
    (3, 4, COLL_34, HIT_34),
]


# ==========================
# VDP
# ==========================
class VDP:
    """
    Video Display Processor.
    Composites tile background + small sprites + large sprites + text layer.
    Runs collision detection each frame and writes results to RAM registers.
    The main render() method is called from the Pygame main thread — this keeps
    all Surface operations on the same thread that owns the display.
    The VDP owns its own pygame Surfaces; the VM just blits the result.
    """

    FPS = 60

    def __init__(self, ram, screen_width_px, screen_height_px):
        self.ram    = ram
        self.sw     = screen_width_px
        self.sh     = screen_height_px
        self.lock   = threading.Lock()

        # Shadow registers — VDP reads from these, BASIC writes via set_register
        self.shadow = bytearray(VDP_REG_END - VDP_REG_BASE + 1)
        self.shadow[VDP_MODE      - VDP_REG_BASE] = 0   # text-only on boot
        self.shadow[VDP_SPRITE_EN - VDP_REG_BASE] = 1
        self.shadow[VDP_PALETTE   - VDP_REG_BASE] = 0

        # Compositing surfaces
        import pygame
        # bg_surface is opaque — tiles fill every pixel, no transparency needed
        self.bg_surface  = pygame.Surface((screen_width_px, screen_height_px))
        # spr_surface is SRCALPHA — sprites have transparent pixels (color 0)
        self.spr_surface = pygame.Surface((screen_width_px, screen_height_px),
                                           pygame.SRCALPHA)
        # result surface reused every frame — avoids allocation per frame
        self.result_surface = pygame.Surface((screen_width_px, screen_height_px))
        self.tile_cache  = {}
        self.spr_cache   = {}

        # Tilemap dirty flag — only redraw bg when tilemap/palette/scroll changes
        self.bg_dirty       = True
        self._last_scroll_x = -1
        self._last_scroll_y = -1
        self._last_palette  = -1

    # ------------------------------------------------------------------
    # Register access  (called from VM set_reg / get_reg)
    # ------------------------------------------------------------------
    def set_register(self, addr, val):
        offset = addr - VDP_REG_BASE
        if 0 <= offset < len(self.shadow):
            with self.lock:
                self.shadow[offset] = val & 0xFF
                # Mark tilemap dirty if scroll or palette changed
                if addr in (VDP_SCROLL_X, VDP_SCROLL_Y, VDP_PALETTE):
                    self.bg_dirty = True

    def get_register(self, addr):
        offset = addr - VDP_REG_BASE
        if 0 <= offset < len(self.shadow):
            with self.lock:
                return self.shadow[offset]
        return 0

    # ------------------------------------------------------------------
    # Convenience shadow reads
    # ------------------------------------------------------------------
    def _reg(self, addr):
        return self.shadow[addr - VDP_REG_BASE]

    # ------------------------------------------------------------------
    # Sprite table helpers
    # Sprite entry byte layout:
    #   0: X low   1: X high  (full 16-bit, supports 0-65535)
    #   2: Y low   3: Y high
    #   4: tile    5: color   6: flags   7: reserved
    # ------------------------------------------------------------------
    def _read_sprite(self, is_large, idx):
        base = VDP_LARGE_SPR_BASE if is_large else VDP_SMALL_SPR_BASE
        off  = base + idx * SPR_ENTRY_BYTES
        r    = self.ram
        x    = r[off+0] | (r[off+1] << 8)
        y    = r[off+2] | (r[off+3] << 8)
        tile = r[off+4]
        col  = r[off+5] & 0x0F
        fl   = r[off+6]
        return x, y, tile, col, fl

    # ------------------------------------------------------------------
    # Tile surface builder
    # is_sprite=False  -> opaque surface, color 0 draws as palette[0] (black)
    # is_sprite=True   -> SRCALPHA surface, color 0 = transparent
    # ------------------------------------------------------------------
    def _get_tile_surface(self, tile_idx, palette_bank, w=TILE_W, h=TILE_H,
                          is_sprite=False):
        import pygame
        key = (tile_idx, palette_bank, w, h, is_sprite)
        if key in self.tile_cache:
            return self.tile_cache[key]

        pal  = VDP_PALETTES[palette_bank & 3]
        base = VDP_TILE_DATA_BASE + tile_idx * TILE_BYTES

        if is_sprite:
            surf = pygame.Surface((w, h), pygame.SRCALPHA)
            surf.fill((0, 0, 0, 0))
        else:
            surf = pygame.Surface((w, h))
            surf.fill(pal[0])   # fill with color 0 as default background

        px_count = w * h
        for i in range(px_count // 2):
            byte   = self.ram[base + i] if (base + i) < len(self.ram) else 0
            hi     = (byte >> 4) & 0x0F
            lo     =  byte       & 0x0F
            px     = i * 2
            cx0, cy0 = px % w,      px // w
            cx1, cy1 = (px+1) % w, (px+1) // w
            # For sprites skip color 0 (transparent). For tiles always draw.
            if not is_sprite or hi != 0:
                surf.set_at((cx0, cy0), pal[hi])
            if not is_sprite or lo != 0:
                surf.set_at((cx1, cy1), pal[lo])

        self.tile_cache[key] = surf
        return surf

    # ------------------------------------------------------------------
    # Sprite surface builder (with flip support)
    # ------------------------------------------------------------------
    def _get_sprite_surface(self, tile_idx, w, h, flip_h, flip_v,
                             palette_bank, color_override=0, single_color=False):
        """
        single_color=False  multicolor mode: tile nibbles draw their own palette colors.
        single_color=True   single color mode: all opaque pixels become color_override.
        color_override=0 in single color mode draws nothing visible (transparent).
        """
        import pygame
        key = (tile_idx, w, h, flip_h, flip_v, palette_bank,
               color_override if single_color else 0, single_color)
        if key in self.spr_cache:
            return self.spr_cache[key]

        base_surf = self._get_tile_surface(tile_idx, palette_bank, w, h,
                                           is_sprite=True)

        if single_color and color_override > 0:
            # Recolor using fill + alpha mask from base surface
            import numpy as np
            pal   = VDP_PALETTES[palette_bank & 3]
            color = pal[color_override & 0x0F]
            # Create solid color surface, then apply alpha mask from base
            solid = pygame.Surface((w, h), pygame.SRCALPHA)
            solid.fill((color[0], color[1], color[2], 255))
            # Get alpha from base sprite (0=transparent, 255=opaque)
            base_alpha = pygame.surfarray.array_alpha(base_surf)
            # Apply as mask to solid surface
            solid_alpha = pygame.surfarray.pixels_alpha(solid)
            solid_alpha[:] = base_alpha
            del solid_alpha
            surf = solid
        else:
            # Multicolor — use tile's own pixel colors as-is
            surf = base_surf.copy()

        if flip_h or flip_v:
            surf = pygame.transform.flip(surf, flip_h, flip_v)

        self.spr_cache[key] = surf
        return surf

    # ------------------------------------------------------------------
    # Invalidate caches when tile data changes (called on POKE to tile region)
    # ------------------------------------------------------------------
    def invalidate_tile(self, tile_idx):
        keys = [k for k in self.tile_cache if k[0] == tile_idx]
        for k in keys: del self.tile_cache[k]
        keys = [k for k in self.spr_cache  if k[0] == tile_idx]
        for k in keys: del self.spr_cache[k]
        self.bg_dirty = True

    def invalidate_all(self):
        self.tile_cache.clear()
        self.spr_cache.clear()
        self.bg_dirty = True

    # ------------------------------------------------------------------
    # Collision detection
    # Groups: encoded in flags bits 4-5 as 0-3 meaning groups 1-4.
    # We collect bounding boxes per group, then test pairs.
    # ------------------------------------------------------------------
    def _collect_bboxes(self):
        """Returns dict: group (1-4) -> list of (x, y, w, h, global_idx)
        Sprites are drawn centered on their x,y position, so bbox top-left
        is x - size//2, y - size//2 to match what the player sees."""
        groups = {1: [], 2: [], 3: [], 4: []}

        for i in range(SMALL_SPR_COUNT):
            base = VDP_SMALL_SPR_BASE + i * SPR_ENTRY_BYTES
            fl   = self.ram[base + 6]
            if not (fl & SPR_ENABLED): continue
            g = ((fl & SPR_GROUP_MASK) >> SPR_GROUP_SHIFT) + 1
            if g < 1 or g > 4: continue
            cx = self.ram[base+0] | (self.ram[base+1] << 8)
            cy = self.ram[base+2] | (self.ram[base+3] << 8)
            half = SMALL_SPR_SIZE // 2
            groups[g].append((cx - half, cy - half,
                               SMALL_SPR_SIZE, SMALL_SPR_SIZE, i))

        for i in range(LARGE_SPR_COUNT):
            base = VDP_LARGE_SPR_BASE + i * SPR_ENTRY_BYTES
            fl   = self.ram[base + 6]
            if not (fl & SPR_ENABLED): continue
            g = ((fl & SPR_GROUP_MASK) >> SPR_GROUP_SHIFT) + 1
            if g < 1 or g > 4: continue
            cx = self.ram[base+0] | (self.ram[base+1] << 8)
            cy = self.ram[base+2] | (self.ram[base+3] << 8)
            half = LARGE_SPR_SIZE // 2
            groups[g].append((cx - half, cy - half,
                               LARGE_SPR_SIZE, LARGE_SPR_SIZE, 64 + i))

        return groups

    def _boxes_overlap(self, ax, ay, aw, ah, bx, by, bw, bh):
        return (ax < bx + bw and ax + aw > bx and
                ay < by + bh and ay + ah > by)

    def _run_collision(self):
        groups = self._collect_bboxes()
        for ga, gb, creg, hreg in COLLISION_PAIRS:
            count    = 0
            hit_a    = 255   # first sprite from group a involved
            hit_b    = 255   # first sprite from group b involved
            list_a   = groups.get(ga, [])
            list_b   = groups.get(gb, [])
            for ax, ay, aw, ah, ai in list_a:
                for bx, by, bw, bh, bi in list_b:
                    if self._boxes_overlap(ax, ay, aw, ah, bx, by, bw, bh):
                        count += 1
                        if hit_a == 255:
                            hit_a = ai
                            hit_b = bi
            result_c = min(count, 255)
            # HIT reg stores group_b sprite index (the thing being hit)
            # HIT reg + 1 stores group_a sprite index (the thing doing the hitting)
            result_h = hit_b if hit_b != 255 else 255
            self.ram[creg]  = result_c
            self.ram[hreg]  = result_h
            # Also store group_a index in hreg+1 if in range
            if hreg + 1 <= VDP_REG_END:
                self.ram[hreg + 1] = hit_a if hit_a != 255 else 255
            off_c = creg - VDP_REG_BASE
            off_h = hreg - VDP_REG_BASE
            if 0 <= off_c < len(self.shadow): self.shadow[off_c] = result_c
            if 0 <= off_h < len(self.shadow): self.shadow[off_h] = result_h
            if 0 <= off_h + 1 < len(self.shadow):
                self.shadow[off_h + 1] = hit_a if hit_a != 255 else 255

    # ------------------------------------------------------------------
    # Tilemap render
    # ------------------------------------------------------------------
    def _render_tilemap(self, palette_bank):
        import pygame
        scroll_x = self._reg(VDP_SCROLL_X)
        scroll_y = self._reg(VDP_SCROLL_Y)

        # Only redraw if something changed
        if (not self.bg_dirty and
                scroll_x == self._last_scroll_x and
                scroll_y == self._last_scroll_y and
                palette_bank == self._last_palette):
            return

        self._last_scroll_x = scroll_x
        self._last_scroll_y = scroll_y
        self._last_palette  = palette_bank
        self.bg_dirty       = False

        pal    = VDP_PALETTES[palette_bank & 3]
        tile_w = TILE_W
        tile_h = TILE_H
        self.bg_surface.fill(pal[0])

        for row in range(TILEMAP_ROWS + 1):
            for col in range(TILEMAP_COLS + 1):
                map_col  = (col + scroll_x // tile_w) % TILEMAP_COLS
                map_row  = (row + scroll_y // tile_h) % TILEMAP_ROWS
                map_idx  = map_row * TILEMAP_COLS + map_col
                addr     = VDP_TILEMAP_BASE + map_idx
                tile_idx = self.ram[addr] if addr < len(self.ram) else 0
                if tile_idx == 0: continue

                surf = self._get_tile_surface(tile_idx, palette_bank,
                                              is_sprite=False)
                dx = col * tile_w - (scroll_x % tile_w)
                dy = row * tile_h - (scroll_y % tile_h)
                self.bg_surface.blit(surf, (dx, dy))

    # ------------------------------------------------------------------
    # Sprite render — returns list of (surface, rect, priority, global_idx)
    # ------------------------------------------------------------------
    def _render_sprites(self, palette_bank):
        import pygame
        sprites_below = []   # priority=0, drawn below text
        sprites_above = []   # priority=1, drawn above text

        def add_sprites(is_large, base_idx):
            count = LARGE_SPR_COUNT if is_large else SMALL_SPR_COUNT
            size  = LARGE_SPR_SIZE  if is_large else SMALL_SPR_SIZE
            for i in range(count):
                x, y, tile, col, fl = self._read_sprite(is_large, i)
                if not (fl & SPR_ENABLED): continue
                flip_h       = bool(fl & SPR_FLIP_H)
                flip_v       = bool(fl & SPR_FLIP_V)
                priority     = bool(fl & SPR_PRIORITY)
                single_color = bool(fl & SPR_SINGLE_COLOR)
                surf = self._get_sprite_surface(tile, size, size,
                                                flip_h, flip_v, palette_bank,
                                                color_override=col,
                                                single_color=single_color)
                # Center the sprite on its x,y coordinate
                cx   = size // 2
                cy   = size // 2
                rect = pygame.Rect(x - cx, y - cy, size, size)
                entry = (surf, rect, base_idx + i)
                if priority: sprites_above.append(entry)
                else:        sprites_below.append(entry)

        add_sprites(False, 0)    # small sprites, indices 0-63
        add_sprites(True,  64)   # large sprites,  indices 64-127
        return sprites_below, sprites_above

    # ------------------------------------------------------------------
    # Main composite — called from Pygame main thread each frame
    # Returns a Surface to blit onto the canvas, or None if text-only mode
    # ------------------------------------------------------------------
    def composite(self, text_surface, palette_bank=None):
        import pygame

        mode        = self._reg(VDP_MODE)
        if palette_bank is None:
            palette_bank = self._reg(VDP_PALETTE) & 3
        sprite_en   = self._reg(VDP_SPRITE_EN)

        if mode == 0:
            return text_surface

        # Collision detection
        self._run_collision()

        # Tilemap (cached, only redraws when dirty)
        self._render_tilemap(palette_bank)

        # Sprites
        sprites_below, sprites_above = self._render_sprites(palette_bank) \
            if sprite_en else ([], [])

        # Composite onto reused result surface
        result = self.result_surface
        result.blit(self.bg_surface, (0, 0))

        self.spr_surface.fill((0, 0, 0, 0))
        for surf, rect, _ in sprites_below:
            self.spr_surface.blit(surf, rect)
        result.blit(self.spr_surface, (0, 0))

        result.blit(text_surface, (0, 0))

        self.spr_surface.fill((0, 0, 0, 0))
        for surf, rect, _ in sprites_above:
            self.spr_surface.blit(surf, rect)
        result.blit(self.spr_surface, (0, 0))

        return result


# ==========================
# BASIC command helpers
# These are convenience functions the BASIC interpreter can call
# instead of manually poking every sprite byte.
# They write directly into RAM — identical to what a POKE sequence would do.
# ==========================

def cmd_sprite(ram, idx, x, y, tile, color, flags, is_large=False):
    """
    Set a sprite entry in RAM. Full 16-bit X and Y position.
    """
    base = VDP_LARGE_SPR_BASE if is_large else VDP_SMALL_SPR_BASE
    off  = base + idx * SPR_ENTRY_BYTES
    ram[off+0] = x & 0xFF
    ram[off+1] = (x >> 8) & 0xFF
    ram[off+2] = y & 0xFF
    ram[off+3] = (y >> 8) & 0xFF
    ram[off+4] = tile & 0xFF
    ram[off+5] = color & 0x0F
    ram[off+6] = flags & 0xFF
    ram[off+7] = 0


def cmd_sprite_pos(ram, idx, x, y, is_large=False):
    """Update only the position of an existing sprite entry."""
    base = VDP_LARGE_SPR_BASE if is_large else VDP_SMALL_SPR_BASE
    off  = base + idx * SPR_ENTRY_BYTES
    ram[off+0] = x & 0xFF
    ram[off+1] = (x >> 8) & 0xFF
    ram[off+2] = y & 0xFF
    ram[off+3] = (y >> 8) & 0xFF


def cmd_sprite_hide(ram, idx, is_large=False):
    """Clear the enabled bit of a sprite."""
    base = VDP_LARGE_SPR_BASE if is_large else VDP_SMALL_SPR_BASE
    off  = base + idx * SPR_ENTRY_BYTES
    ram[off+6] &= ~SPR_ENABLED


def cmd_tile(ram, tilemap_col, tilemap_row, tile_idx):
    """Write a tile index into the tilemap."""
    if 0 <= tilemap_col < TILEMAP_COLS and 0 <= tilemap_row < TILEMAP_ROWS:
        addr = VDP_TILEMAP_BASE + tilemap_row * TILEMAP_COLS + tilemap_col
        if addr < len(ram):
            ram[addr] = tile_idx & 0xFF


def cmd_fill_tiles(ram, tile_idx):
    """Fill entire tilemap with one tile (e.g. background wash)."""
    for i in range(TILEMAP_COLS * TILEMAP_ROWS):
        addr = VDP_TILEMAP_BASE + i
        if addr < len(ram):
            ram[addr] = tile_idx & 0xFF


def flags_from(enabled=True, flip_h=False, flip_v=False,
               priority=False, group=0, single_color=False):
    """
    Build a flags byte for a sprite entry.
    group:        0=no collision group, 1-4=collision group
    single_color: True  = color field overrides all pixels (C64-style)
                  False = tile draws its own nibble colors (multicolor)
    """
    f  = SPR_ENABLED      if enabled      else 0
    f |= SPR_FLIP_H       if flip_h       else 0
    f |= SPR_FLIP_V       if flip_v       else 0
    f |= SPR_PRIORITY     if priority     else 0
    f |= SPR_SINGLE_COLOR if single_color else 0
    g  = max(0, min(4, group))
    if g > 0:
        f |= ((g - 1) & 0x03) << SPR_GROUP_SHIFT
    return f