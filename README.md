# Project Status: Retro 8-Bit BASIC Interpreter and Virtual PC(v3.0)

## 📌 Current State

A Python-based 8-bit BASIC interpreter with a Pygame-based Virtual Machine display. The system now includes a full VDP (Video Display Processor) with tilemap backgrounds, 16x16 and 32x32 sprites, hardware collision detection, four palette banks, a dedicated SID Sound Card with DMA streaming, joystick/gamepad support, and a fully playable arcade game (STORM-VDP.BAS) that validates all core systems.

## Quick Start — Dependencies

### Required
Pygame-cs
numpy
pyresidfp


```
pip install pygame-ce numpy pyresidfp
```

|Library|What it does|Without it|
|---|---|---|
|`pygame-ce`|Window, display, input, audio mixer, joystick. The Community Edition of pygame with better performance. Must be `pygame-ce` not plain `pygame`.|System won't start|
|`numpy`|Fast pixel array operations used by the VDP for single-color sprite recoloring — copies alpha channels between surfaces in bulk instead of pixel by pixel.|Single-color sprites (player ship, bullets) are invisible. Multicolor sprites and all other features still work.|
|`pyresidfp`|Hardware-accurate SID chip emulation. Produces authentic C64-style audio from register writes.|System runs silently. No sound effects or music. A warning is printed at startup.|

### Optional

|Library|What it does|Without it|
|---|---|---|
|`tkinter`|Native OS file open/save dialogs in the F12 menu for mounting disk images and folders. Usually included with Python on Windows and Mac. On Linux: `sudo apt install python3-tk`|F12 disk mount dialogs are disabled. Everything else works.|

### Tested On

- Python 3.11+ on Windows
- Python 3.14 on Windows (current development platform)
- Raspberry Pi support planned (Pi 4 or Pi 5 recommended)
```


---

## 🛠️ Recent Milestones (v3.0)

### 1. VDP — Video Display Processor (`video_chip.py`)

A dedicated compositing engine that runs on the Pygame main thread. BASIC talks to it by writing to memory-mapped registers and RAM regions. The VDP composites four layers each frame:

1. **Background tilemap** — 40×25 grid of 16×16 tiles, cached, only redraws when changed
2. **Sprites below text** — sprites with priority=0
3. **Text layer** — always uses fixed system palette, transparent background in VDP mode
4. **Sprites above text** — sprites with priority=1

### 2. Tile System

- **Tile format:** 16×16 pixels, 2 pixels per byte (nibbles). High nibble = left pixel, low nibble = right pixel. Color index 0 = transparent for sprites, solid for background tiles.
- **128 tile definitions** fit in the tile data region (128 × 128 bytes = 16KB)
- **Tile indices** are 0-based. Index 0 in the tilemap = empty/transparent slot.
- **Tile sheets** are plain binary files loadable with `DLOAD "SHEET.BIN", 10240`
- **Multiple tile sheets** can be swapped at runtime by loading a different `.BIN` to address 10240 — useful for different game levels or screens
- **Tile editor** (`tile_editor.py`) — standalone pygame tool to draw tiles and save `.BIN` files

### 3. Sprite System

Two sprite sizes, each with 64 slots:

|Type|Size|Center|Table Base|Slots|
|---|---|---|---|---|
|Small|16×16|pixel (7,7)|8192 ($2000)|64|
|Large|32×32|pixel (15,15)|8704 ($2200)|64|

**Sprite entry format (8 bytes):**

|Byte|Name|Notes|
|---|---|---|
|0|X lo|X position low byte|
|1|X hi|X position high byte (full 16-bit)|
|2|Y lo|Y position low byte|
|3|Y hi|Y position high byte|
|4|TILE|Tile index (0-based)|
|5|COLOR|Palette color 0-15 (used in single color mode)|
|6|FLAGS|See below|
|7|Reserved||

**Flags byte:**

|Bit|Name|Function|
|---|---|---|
|0|ENABLED|1 = sprite visible and active|
|1|FLIP_H|Flip horizontally|
|2|FLIP_V|Flip vertically|
|3|PRIORITY|1 = draw above text layer|
|4-5|GROUP|Collision group 1-4 (stored as 0-3)|
|6|SINGLE_COLOR|0 = multicolor (tile nibbles), 1 = single color override|

**Color mode:**

- `bit6 = 0` Multicolor — tile draws its own nibble colors, up to 15 colors per sprite
- `bit6 = 1` Single color — COLOR field overrides all opaque pixels with one palette color

**Group encoding for FLAGS:**

```
group 1 -> bits 4-5 = 00 -> add 0  to base flags
group 2 -> bits 4-5 = 01 -> add 16 to base flags
group 3 -> bits 4-5 = 10 -> add 32 to base flags
group 4 -> bits 4-5 = 11 -> add 48 to base flags
```

### 4. Hardware Collision Detection

The VDP checks bounding box overlaps every frame and writes results to RAM registers. BASIC reads them with PEEK.

**Six collision pairs:**

|Register|Dec|Pair|Hit Register|Dec|
|---|---|---|---|---|
|COLL_12|49296|Group 1 vs Group 2|HIT_12|49304|
|COLL_13|49297|Group 1 vs Group 3|HIT_13|49305|
|COLL_14|49298|Group 1 vs Group 4|HIT_14|49306|
|COLL_23|49299|Group 2 vs Group 3|HIT_23|49307|
|COLL_24|49300|Group 2 vs Group 4|HIT_24|49308|
|COLL_34|49301|Group 3 vs Group 4|HIT_34|49309|

- **COLL register** = count of collisions that frame (0 = none)
- **HIT register** = sprite index of first sprite from the higher-numbered group involved
- **HIT register + 1** = sprite index of first sprite from the lower-numbered group involved
- Bounding boxes are centered on sprite x,y position

### 5. Four Palette Banks

VDP sprites and tiles use the active palette bank. Text layer always uses the fixed system palette regardless.

```basic
POKE 49287, 0   : REM Bank 0 — Default (matches text palette)
POKE 49287, 1   : REM Bank 1 — Warm (reds, oranges, golds)
POKE 49287, 2   : REM Bank 2 — Cool (blues, greens, purples)
POKE 49287, 3   : REM Bank 3 — Muted/Earthy (RPG-friendly)
```

### 6. Performance Architecture

- **Text layer** only redraws when screen RAM changes (dirty flag)
- **Tilemap** only redraws when tilemap RAM, scroll, or palette changes (dirty flag)
- **Sprite surfaces** are cached after first render, rebuilt only when tile data changes
- **Lock scope** minimized — render holds `vm.lock` only during screen RAM read
- **BASIC thread** runs without per-statement sleep — `check_break_fn` removed unnecessary `time.sleep(0.001)`
- **PAUSE** granularity fixed to 1ms (was 50ms minimum, causing 10x slowdown)
- `pygame.SCALED` used for GPU-accelerated window scaling

---

## 📁 Core Files Overview

|File|Role|
|---|---|
|`basic.py`|BASIC interpreter, tokenizer, parser, MMU|
|`virtual_pc.py`|Pygame frontend, hardware registers, joystick, settings, render loop|
|`sound_chip.py`|Hardware-accurate SID emulator & Background DMA Controller|
|`video_chip.py`|VDP — tilemap, sprites, collision detection, palette banks|
|`tile_editor.py`|Standalone tile drawing tool, saves `.BIN` files|
|`sid_packer.py`|Converts SIDDump `.txt` files into raw `.BIN` chunks for DMA playback|
|`STORM.BAS`|Text-mode version of the game|
|`STORM-SOUND.BAS`|Text-mode game with SID music|
|`STORM-VDP.BAS`|Full sprite/tile graphics version with opening screen|
|`TILES.BIN`|Tile sheet for STORM-VDP (8 tiles: ship, meteor, bullet, saucer, explosions, stars, wall)|
|`MUSIC.BIN`|Packed SID music for title screen|
|`retrothree.cfg`|Persistent settings (auto-created)|

---

## 🗺️ System Memory Map (64KB View)

|Address (Hex)|Address (Dec)|Size|Description|
|---|---|---|---|
|`$0000-$03FF`|0-1023|1KB|System Workspace|
|`$0400-$07FF`|1024-2047|1KB|Default Screen RAM (80×50 chars)|
|`$0800-$1FFF`|2048-8191|6KB|BASIC Program Area|
|`$2000-$2007`|8192-8199|512B|VDP Small Sprite Table (64 × 8 bytes)|
|`$2200-$23FF`|8704-9215|512B|VDP Large Sprite Table (64 × 8 bytes)|
|`$2400-$27E7`|9216-10215|1000B|VDP Tilemap (40×25 tile indices)|
|`$2800-$67FF`|10240-26623|16KB|VDP Tile Data (128 tiles × 128 bytes)|
|`$6800-$7FFF`|26624-32767|6KB|BASIC Program Area (continued)|
|`$8000-$BFFF`|32768-49151|16KB|Bank Switched Window ($8000 maps to extended RAM)|
|`$C000-$C0FF`|49152-49407|256B|Hardware Registers|
|`$D800-$DBFF`|55296-56319|1KB|Color RAM|
|`$F000-$F7FF`|61440-63487|2KB|Character ROM|
|`$F800-$FFFF`|63488-65535|2KB|Reserved|

**Extended RAM:** 512KB physical RAM. Bank 4 = address 65536, used for DMA music data.

---

## 🖥️ Hardware Registers (`$C000-$C0FF`)

### System Registers

|Address (Hex)|Dec|Name|Function|
|---|---|---|---|
|`$C000`|49152|BANK_REG|Bank select for `$8000` window|
|`$C001`|49153|SCREEN_CMD|Write 1 to trigger CLS|
|`$C002`|49154|BORDER_COL|Border color (0-15)|
|`$C003`|49155|BG_COL|Background color (0-15)|
|`$C004`|49156|TEXT_COL|Text/pen color (0-15)|
|`$C010`|49168|LAST_KEY|Keyboard buffer (GET command)|
|`$C020`|49184|JOY1|Joystick port 1 state byte|
|`$C021`|49185|JOY2|Joystick port 2 state byte|

### SID Sound Registers

|Address (Hex)|Dec|Name|Function|
|---|---|---|---|
|`$C030-$C048`|49200-49224|SID_VOICES|25 standard SID registers (V1, V2, V3, Filter, Volume)|
|`$C050-$C056`|49232-49238|SID_DMA|DMA Audio Controller|

### VDP Registers

|Address (Hex)|Dec|Name|Function|
|---|---|---|---|
|`$C080`|49280|VDP_MODE|0=text only, 1=tile+sprite|
|`$C081`|49281|VDP_SCROLL_X|Tilemap scroll X (0-255)|
|`$C082`|49282|VDP_SCROLL_Y|Tilemap scroll Y (0-255)|
|`$C083`|49283|VDP_SPRITE_EN|Master sprite enable (1=on)|
|`$C084`|49284|VDP_BG_PAGE|Tilemap page select|
|`$C085`|49285|VDP_TILE_PAGE|Tile image data page select|
|`$C086`|49286|VDP_LAYER_PRI|Layer priority flags|
|`$C087`|49287|VDP_PALETTE|Active palette bank (0-3)|
|`$C090`|49296|COLL_12|Group 1 hit group 2 (count)|
|`$C091`|49297|COLL_13|Group 1 hit group 3 (count)|
|`$C092`|49298|COLL_14|Group 1 hit group 4 (count)|
|`$C093`|49299|COLL_23|Group 2 hit group 3 (count)|
|`$C094`|49300|COLL_24|Group 2 hit group 4 (count)|
|`$C095`|49301|COLL_34|Group 3 hit group 4 (count)|
|`$C098`|49304|HIT_12|First sprite index for COLL_12|
|`$C099`|49305|HIT_13|First sprite index for COLL_13|
|`$C09A`|49306|HIT_14|First sprite index for COLL_14|
|`$C09B`|49307|HIT_23|First sprite index for COLL_23|
|`$C09C`|49308|HIT_24|First sprite index for COLL_24|
|`$C09D`|49309|HIT_34|First sprite index for COLL_34|

---

## 🎨 Tile Editor (`tile_editor.py`)

A standalone pygame tool for drawing tiles and saving `.BIN` files.

**Controls:**

- Left click — paint with current color
- Right click — erase (color 0 = transparent)
- `0-9`, `A-F` — select color index 0-15
- `[` / `]` — previous / next tile slot
- `N` — clear current tile
- `S` — save all tiles to `TILES.BIN`
- `L` — load `TILES.BIN`
- `Q` / `ESC` — quit

**Usage:**

```powershell
python tile_editor.py              # opens with TILES.BIN if it exists
python tile_editor.py LEVEL2.BIN   # opens specific file
```

**Multiple tile sheets:** Save different `.BIN` files for different sets of art. Load whichever you need at runtime:

```basic
DLOAD "LEVEL1.BIN", 10240   : REM Load level 1 tiles
DLOAD "LEVEL2.BIN", 10240   : REM Load level 2 tiles (replaces level 1)
```

The VDP cache invalidates automatically when tile data is written.

**Large sprite tiles (32×32):** Large sprites use the same tile data region but read 4× as many bytes (512 bytes per tile at 32×32). To design large sprite art, draw it as four adjacent 16×16 tiles arranged in a 2×2 grid and reference the top-left tile index. The large sprite renderer reads the full 32×32 block automatically.

> Note: A future tile editor update will add tile copy/paste and a 32×32 large sprite drawing mode.

---

## 🎮 STORM-VDP Game Architecture

### Sprite Assignments

|Sprite #|Element|Tile|Flags|Group|
|---|---|---|---|---|
|0|Player ship|0|65 (enabled+single color)|1|
|1-10|Meteors|1|33 (enabled+multicolor)|3|
|11-15|Bullets|2|81 (enabled+single color)|2|
|16|Bonus saucer|3|49 (enabled+multicolor)|4|
|17-27|Explosions|4|9 (enabled+priority above text)|none|

### Collision Pairs Used

|Register|Pair|Meaning|
|---|---|---|
|COLL_13 (49297)|Player vs Meteors|Meteor hits player → lose life|
|COLL_23 (49299)|Bullets vs Meteors|Bullet hits meteor → score|
|COLL_24 (49300)|Bullets vs Saucer|Bullet hits saucer → gain life|

### Tilemap Layout

- Tile index 6 (starfield/black) fills entire 40×25 grid
- Tile index 7 (border wall) at tilemap columns 9 and 30
- Play field: pixel X 168-472, centered on 640px wide screen

---

## 🎵 Audio Architecture

### Manual Sound Effects (Pure SID)

Programs SID chip directly. `SB` = 49200.

|Offset|Purpose|Example|
|---|---|---|
|`SB+24`|Master Volume|`POKE SB+24, 15`|
|`SB+5/6`|Voice 1 ADSR|`POKE SB+5, 9 : POKE SB+6, 0`|
|`SB+0/1`|Voice 1 Freq|`POKE SB+0, 0 : POKE SB+1, 15`|
|`SB+4`|Voice 1 Control|`POKE SB+4, 129` (Noise+Gate)|

### DMA Music Playback

Music files are packed SID register dumps (25 bytes per frame at 50fps). Load to extended RAM, start DMA controller.

```basic
10 SB = 49200
20 DLOAD "MUSIC.BIN", 65536
30 POKE 49152, 4
40 POKE SB+32, 2 : POKE SB+33, 0 : POKE SB+35, 1
50 POKE SB+36, PEEK(32768) : POKE SB+37, PEEK(32769)
60 POKE 49152, 0
70 POKE SB+38, 0
80 POKE SB+34, 2
90 PRINT "PLAYING!"
```

Stop with `POKE SB+34, 0`.

**Note:** DMA music uses all 3 SID voices. Stop music before playing sound effects, or reserve voices 1-2 for SFX and voice 3 for music (requires custom SID packing).

### SID Packer Tool

Converts SIDDump output to packed `.BIN` files:

```powershell
siddump108\siddump.exe -s -l MySong.sid > MySong.txt
python sid_packer.py MySong.txt MUSIC.BIN
```

---

## 📖 BASIC Command Reference

### Variables & I/O

|Command|Description|Example|
|---|---|---|
|`PRINT` / `?`|Output to screen|`PRINT "SCORE: "; S`|
|`INPUT`|Wait for typed input|`INPUT "NAME? "; N$`|
|`GET`|Non-blocking single keypress|`GET A$ : IF A$="" THEN GOTO 20`|

### System & Memory

|Command|Description|Example|
|---|---|---|
|`CLS`|Clear screen|`CLS`|
|`CLEAR`|Wipe variables, keep program|`CLEAR`|
|`POKE`|Write byte to address|`POKE 49184, 0`|
|`PEEK(x)`|Read byte from address|`V = PEEK(49184)`|
|`DLOAD`|Load binary file to RAM|`DLOAD "TILES.BIN", 10240`|
|`PAUSE`|Sleep for N seconds|`PAUSE 0.04`|

### VDP Quick Reference

```basic
REM SWITCH VDP ON
POKE 49280, 1  : REM VDP mode 1 (tile+sprite)
POKE 49283, 1  : REM sprites enabled
POKE 49287, 0  : REM palette bank 0

REM RETURN TO TEXT MODE
POKE 49280, 0

REM SCROLL BACKGROUND
POKE 49281, X  : REM scroll X
POKE 49282, Y  : REM scroll Y

REM SET A SPRITE (small, 16x16)
REM SS=8192, each sprite = 8 bytes
O = IDX * 8
POKE SS+O+0, X AND 255 : POKE SS+O+1, INT(X/256)
POKE SS+O+2, Y AND 255 : POKE SS+O+3, INT(Y/256)
POKE SS+O+4, TILE
POKE SS+O+5, COLOR
POKE SS+O+6, FLAGS

REM SET A LARGE SPRITE (32x32)
REM LS=8704, same format
```

### Built-in Functions

**Math:** `SIN`, `COS`, `TAN`, `LOG`, `EXP`, `SQR`, `ABS`, `INT`, `SGN`, `RND`

**Strings:** `LEN`, `ASC`, `CHR$`, `VAL`, `STR$`, `LEFT$`, `RIGHT$`, `MID$`

**System:** `PEEK(addr)`, `TAB(n)`, `SPC(n)`, `TI`, `TI$`, `DATE$`, `DATETIME$`

---

## 🖥️ Display

- **Resolution:** 640×400 pixels
- **Text grid:** 80 columns × 50 rows (8×8 character cells)
- **Tile grid:** 40×25 tiles (16×16 pixel tiles)
- **Colors:** 16 fixed colors in system palette, 4 switchable VDP palette banks
- **Window scaling:** `pygame.SCALED` — SDL handles upscaling via GPU
- **Border:** 20px pygame border, color set via `POKE 49154, N`

## 🕹️ Joystick Memory Map

|Bit|Mask|Action|
|---|---|---|
|0|1|Up|
|1|2|Down|
|2|4|Left|
|3|8|Right|
|4|16|Fire A|
|5|32|Fire B|
|6|64|Fire C|
|7|128|Fire D|

```basic
J = PEEK(49184)          : REM read joystick 1
IF J AND 4 THEN ...      : REM left
IF J AND 8 THEN ...      : REM right
IF J AND 16 THEN ...     : REM fire
```
