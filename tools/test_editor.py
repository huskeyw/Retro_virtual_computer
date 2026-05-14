# -*- coding: utf-8 -*-
"""
Created on Sat Jan  3 13:25:16 2026

@author: shuskey
"""

import unittest
import sys
from unittest.mock import MagicMock

# --- 1. MOCK PYGAME BEFORE IMPORTING ---
# This tricks Python into thinking pygame exists, 
# so we can test the logic without opening a window.
sys.modules["pygame"] = MagicMock()
sys.modules["pygame.font"] = MagicMock()
sys.modules["pygame.display"] = MagicMock()
sys.modules["pygame.time"] = MagicMock()

# Now we can import the module safely
import virtual_pc

class TestEditorLogic(unittest.TestCase):
    def setUp(self):
        self.vm = virtual_pc.VirtualMachine()
        self.vm.cls() # Start clean

    def get_line(self, row):
        """Helper to read a line from the text buffer as a string."""
        return "".join(self.vm.text_buffer[row]).rstrip()

    def test_basic_typing(self):
        print("\n--- Test: Basic Typing ---")
        for char in "HELLO":
            self.vm.write_char(char)
        
        result = self.get_line(0)
        self.assertEqual(result, "HELLO")
        self.assertEqual(self.vm.cursor_x, 5)
        print("  > Typed 'HELLO' successfully.")

    def test_overwrite_mode(self):
        print("\n--- Test: Overwrite Mode (Default) ---")
        # Type "HELLO"
        for char in "HELLO": self.vm.write_char(char)
        
        # Move Cursor back to 'E' (index 1)
        self.vm.cursor_x = 1
        
        # Type 'A' (Should replace 'E')
        self.vm.write_char('A')
        
        result = self.get_line(0)
        self.assertEqual(result, "HALLO")
        print("  > 'HELLO' became 'HALLO' (Overwrite works).")

    def test_insert_mode(self):
        print("\n--- Test: Insert Mode ---")
        # Type "HLO"
        for char in "HLO": self.vm.write_char(char)
        
        # Move cursor to 'L'
        self.vm.cursor_x = 1
        
        # Enable Insert Mode
        self.vm.insert_mode = True
        
        # Type 'E' (Should insert, making "HELO")
        self.vm.write_char('E')
        self.assertEqual(self.get_line(0), "HELO")
        
        # Type 'L' (Should insert, making "HELLO")
        self.vm.write_char('L')
        self.assertEqual(self.get_line(0), "HELLO")
        
        print("  > 'HLO' became 'HELLO' (Insert works).")

    def test_backspace(self):
        print("\n--- Test: Backspace ---")
        # Type "TEST"
        for char in "TEST": self.vm.write_char(char)
        
        # Backspace twice
        self.vm.write_char('\b') # Deletes 'T'
        self.vm.write_char('\b') # Deletes 'S'
        
        result = self.get_line(0)
        self.assertEqual(result, "TE")
        self.assertEqual(self.vm.cursor_x, 2)
        print("  > 'TEST' became 'TE'.")

    def test_mid_line_backspace(self):
        print("\n--- Test: Middle-of-line Backspace ---")
        # Type "APPLE"
        for char in "APPLE": self.vm.write_char(char)
        
        # Move to the second 'P' (index 2) and delete the first 'P'
        self.vm.cursor_x = 2
        self.vm.write_char('\b') 
        
        # Should pull 'PLE' to the left -> "APLE"
        result = self.get_line(0)
        self.assertEqual(result, "APLE")
        print("  > 'APPLE' became 'APLE' (Shift left worked).")

    def test_enter_scrapes_screen(self):
        print("\n--- Test: Enter Key (Screen Scraping) ---")
        # Manually inject text onto the screen (simulating a LIST command output)
        line_text = "10 PRINT \"EDITED\""
        for i, char in enumerate(line_text):
            self.vm.text_buffer[5][i] = char
            
        # Place cursor on that line
        self.vm.cursor_y = 5
        self.vm.cursor_x = 5 # arbitrary position
        self.vm.input_mode = True # Pretend we are ready for input
        
        # Hit Enter
        self.vm.handle_enter()
        
        # Verify the VM captured the line
        captured = self.vm.input_queue.get()
        self.assertEqual(captured, "10 PRINT \"EDITED\"")
        print("  > Successfully scraped '10 PRINT \"EDITED\"' from line 5.")

if __name__ == '__main__':
    unittest.main()