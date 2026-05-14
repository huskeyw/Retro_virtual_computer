# -*- coding: utf-8 -*-
"""
Created on Sun Jan  4 17:34:38 2026

@author: shuskey
"""

import pygame
import os

# --- Configuration ---
ROM_FILE = "chargen" # Make sure you have the 'chargen' file in your directory
CHAR_WIDTH = 8
CHAR_HEIGHT = 8
CHARS_PER_SET = 256
ROM_SIZE = CHARS_PER_SET * CHAR_HEIGHT # 2048 bytes (2KB)

# --- Pygame Initialization ---
pygame.init()
screen_width = 320
screen_height = 200
screen = pygame.display.set_mode((screen_width, screen_height))
pygame.display.set_caption("C64 Character ROM Viewer")

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# --- 1. Read the Character ROM data ---
def read_c64_rom(filename):
    try:
        with open(filename, 'rb') as f:
            rom_data = f.read()
        if len(rom_data) != ROM_SIZE and len(rom_data) != 4096:
             print(f"Warning: ROM file size is {len(rom_data)} bytes, expected 2048 or 4096.")
        return rom_data
    except FileNotFoundError:
        print(f"Error: {filename} not found. Please provide the C64 'chargen' file.")
        pygame.quit()
        exit()

# --- 2. Parse ROM data into Pygame Surfaces ---
def create_c64_surfaces(rom_data):
    character_surfaces = []
    # Iterate through all 256 characters
    for char_index in range(CHARS_PER_SET):
        char_data_start = char_index * CHAR_HEIGHT
        char_data = rom_data[char_data_start : char_data_start + CHAR_HEIGHT]
        
        # Create a new 8x8 Pygame surface for the character
        surface = pygame.Surface((CHAR_WIDTH, CHAR_HEIGHT))
        surface.fill(BLACK) # Background color
        
        # Draw pixels
        for row_index, byte_val in enumerate(char_data):
            for col_index in range(CHAR_WIDTH):
                # Check if the corresponding bit is set (1)
                if (byte_val >> (7 - col_index)) & 1:
                    surface.set_at((col_index, row_index), WHITE)
        character_surfaces.append(surface)
    return character_surfaces

# --- Main logic ---
rom_data = read_c64_rom(ROM_FILE)
char_surfaces = create_c64_surfaces(rom_data)

# --- 3. Display characters in Pygame loop ---
running = True
while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

    screen.fill(BLACK)

    # Display all 256 characters in a grid (e.g., 16x16 grid)
    chars_per_row = 16
    for i, char_surface in enumerate(char_surfaces):
        x = (i % chars_per_row) * (CHAR_WIDTH + 2) # +2 for spacing
        y = (i // chars_per_row) * (CHAR_HEIGHT + 2)
        screen.blit(char_surface, (x, y))

    pygame.display.flip()

pygame.quit()
