# -*- coding: utf-8 -*-
"""
Created on Sat Jan  3 12:27:26 2026

@author: shuskey
"""

# PETSCII to Unicode Mapping
# This maps Commodore byte values to the Unicode equivalent
# supported by C64 Pro Mono and other KreativeKorp fonts.

PETSCII_TO_UNICODE = {
    # --- Control Codes (Mapped to harmless spaces or specific symbols) ---
    # We map these to prevent crashes, though the VM might handle them separately
    13:  "\n",     # Return
    147: "",       # CLR/HOME (Handled by VM logic usually)
    
    # --- Standard Graphics (Range 96-127) ---
    96:  "\u2500", # Horizontal Line
    97:  "\u2660", # Spade
    98:  "\u2502", # Vertical Line
    99:  "\u2500", # Horizontal Line
    100: "\u2500", # Horizontal Line
    101: "\u2500", # Horizontal Line
    102: "\u2500", # Horizontal Line
    103: "\u2500", # Horizontal Line
    104: "\u2500", # Horizontal Line
    105: "\u256E", # Top-Right Corner
    106: "\u2570", # Bottom-Left Corner
    107: "\u256D", # Top-Left Corner
    108: "\u256F", # Bottom-Right Corner
    109: "\u2572", # Diagonal \
    110: "\u2571", # Diagonal /
    111: "\u2500", # Horizontal Line
    112: "\u2500", # Horizontal Line
    113: "\u25CF", # Circle (Ball)
    114: "\u2500", # Horizontal Line
    115: "\u2665", # Heart
    116: "\u2500", # Horizontal Line
    117: "\u256D", # Top-Left Corner
    118: "\u2573", # X Cross
    119: "\u25CB", # Circle Outline
    120: "\u2663", # Club
    121: "\u2500", # Horizontal Line
    122: "\u2666", # Diamond
    123: "\u253C", # Cross (Plus)
    124: "\u2500", # Horizontal Line
    125: "\u2502", # Vertical Line
    126: "\u03C0", # PI Symbol
    127: "\u25E4", # Triangle Upper-Left

    # --- Shifted Graphics / Block Graphics (Range 160-255) ---
    160: "\u00A0", # Hard Space
    
    # Box Drawing (Common subset)
    224: "\u00A0", # Space
    225: "\u258C", # Left Half Block
    226: "\u2584", # Lower Half Block
    227: "\u2594", # Upper 1/8 Block
    228: "\u2581", # Lower 1/8 Block
    229: "\u258F", # Left 1/8 Block
    230: "\u2592", # Checkerboard
    231: "\u2595", # Right 1/8 Block
    232: "\u25E4", # Inverted Triangle
    233: "\u258A", # Left 3/4 Block
    234: "\u258E", # Left 1/4 Block
    235: "\u2596", # Quadrant Lower Left
    236: "\u259D", # Quadrant Upper Right
    237: "\u2598", # Quadrant Upper Left
    238: "\u2597", # Quadrant Lower Right
    239: "\u2582", # Lower 1/4 Block
    240: "\u2586", # Lower 3/4 Block
    241: "\u2580", # Upper Half Block
    242: "\u259A", # Quadrant Upper Left/Lower Right
    243: "\u259E", # Quadrant Upper Right/Lower Left
}