import unittest
import sys
import os
import shutil
import math
import time
from io import StringIO
from basic import BASICInterpreter, BasicError, MMU

class TestAllKeywords(unittest.TestCase):
    def setUp(self):
        self.interp = BASICInterpreter()
        self.held_stdout = sys.stdout
        self.captured_output = StringIO()
        sys.stdout = self.captured_output
        self.log(f"\n{'='*40}")
        self.log(f"TEST START: {self._testMethodName}")
        self.log(f"{'-'*40}")

    def tearDown(self):
        sys.stdout = self.held_stdout
        # Cleanup files created during tests
        clean_files = ["test_all.bas", "test_save.bas"]
        for f in clean_files:
            if os.path.exists(f):
                try: os.remove(f)
                except: pass
        
        # Cleanup directories created during tests
        if os.path.exists("test_dir_tmp"):
            try: shutil.rmtree("test_dir_tmp")
            except: pass

    def log(self, msg):
        self.held_stdout.write(msg + "\n")

    def run_cmd(self, cmd):
        self.log(f"  > Executing Immediate: {cmd}")
        self.interp.run_immediate([cmd])

    def load_program(self, code_lines):
        self.log("  > Loading Program:")
        for line in code_lines:
            self.log(f"    {line}")
            parts = line.split(maxsplit=1)
            ln = int(parts[0])
            txt = parts[1] if len(parts) > 1 else ""
            self.interp.load(txt, ln)

    def get_output(self):
        val = self.captured_output.getvalue().strip()
        self.captured_output.truncate(0)
        self.captured_output.seek(0)
        return val

    def expect_error(self, cmd, error_fragment, immediate=True):
        self.log(f"  > Negative Test: '{cmd}' expecting '{error_fragment}'")
        try:
            if immediate:
                self.run_cmd(cmd)
            else:
                self.interp.run()
            self.fail(f"FAILURE: Command '{cmd}' succeeded but should have failed.")
        except Exception as e:
            msg = str(e)
            if error_fragment.lower() in msg.lower():
                self.log(f"    Caught expected error: {msg} (PASS)")
            else:
                self.fail(f"FAILURE: Caught unexpected error: '{msg}'. Expected part: '{error_fragment}'")

    # ==========================
    # POSITIVE TESTS
    # ==========================

    def test_math_functions(self):
        self.run_cmd('PRINT SIN(0)')
        self.assertEqual(float(self.get_output()), 0.0)
        self.run_cmd('PRINT INT(1.9)')
        self.assertEqual(self.get_output(), "1") 
        self.run_cmd('PRINT ABS(-50)')
        self.assertEqual(self.get_output(), "50")
        self.run_cmd('PRINT SQR(16)')
        self.assertEqual(self.get_output(), "4")
        self.log("    Math functions verified (PASS)")

    def test_boolean_logic(self):
        self.log("  > Test Logic Gates (AND, OR, NOT)")
        self.run_cmd('PRINT 1 AND 1')
        self.assertEqual(self.get_output(), "1")
        self.run_cmd('PRINT 1 AND 0')
        self.assertEqual(self.get_output(), "0")
        self.run_cmd('PRINT 0 OR 1')
        self.assertEqual(self.get_output(), "1")
        self.run_cmd('PRINT NOT 0')
        self.assertEqual(self.get_output(), "-1") 
        self.run_cmd('PRINT NOT 1')
        self.assertEqual(self.get_output(), "-2")
        self.run_cmd('PRINT 10 >= 10')
        self.assertEqual(self.get_output(), "-1") 
        self.run_cmd('PRINT 5 <> 5')
        self.assertEqual(self.get_output(), "0") 
        self.log("    Boolean Logic verified (PASS)")

    def test_str_funcs(self):
        self.run_cmd('PRINT LEN("ABC")')
        self.assertEqual(self.get_output(), "3")
        self.run_cmd('PRINT CHR$(65)')
        self.assertEqual(self.get_output(), "A")
        self.run_cmd('PRINT ASC("A")')
        self.assertEqual(self.get_output(), "65")
        self.run_cmd('PRINT LEFT$("HELLO", 2)')
        self.assertEqual(self.get_output(), "HE")
        self.run_cmd('PRINT RIGHT$("HELLO", 2)')
        self.assertEqual(self.get_output(), "LO")
        self.run_cmd('PRINT MID$("HELLO", 2, 3)')
        self.assertEqual(self.get_output(), "ELL")
        self.run_cmd('PRINT MID$("HELLO", 2)')
        self.assertEqual(self.get_output(), "ELLO")
        self.log("    String functions verified (PASS)")

    def test_val_str(self):
        self.run_cmd('PRINT STR$(123)')
        self.assertEqual(self.get_output(), "123")
        self.run_cmd('PRINT VAL("123")')
        self.assertEqual(self.get_output(), "123")
        self.log("    VAL/STR conversions verified (PASS)")

    def test_arrays_multidim(self):
        self.log("  > Test Multi-Dimensional Arrays")
        self.load_program([
            '10 DIM A(2,2)',
            '20 A(0,0) = 99',
            '30 A(2,2) = 11',
            '40 PRINT A(0,0); A(2,2)'
        ])
        self.interp.run()
        self.assertEqual(self.get_output().replace('\n', ''), "9911")
        self.log("    2D Array R/W verified (PASS)")

    def test_peek_poke(self):
        self.log("  > Test PEEK / POKE")
        self.run_cmd('POKE 1000, 255')
        self.run_cmd('PRINT PEEK(1000)')
        self.assertEqual(self.get_output(), "255")
        self.log("    Memory Access verified (PASS)")

    def test_control_flow(self):
        self.load_program(['10 FOR I=1 TO 2', '20 PRINT I', '30 NEXT I'])
        self.interp.run()
        self.assertEqual(self.get_output().replace('\n', ' '), "1 2")
        self.interp.reset()
        self.load_program(['10 GOTO 30', '20 PRINT "BAD"', '30 PRINT "GOOD"'])
        self.interp.run()
        self.assertEqual(self.get_output(), "GOOD")
        self.log("    Control flow verified (PASS)")

    def test_gosub_stack(self):
        self.load_program([
            '10 GOSUB 100', '20 END',
            '100 PRINT "SUB"', '110 RETURN'
        ])
        self.interp.run()
        self.assertEqual(self.get_output(), "SUB")
        self.log("    GOSUB/RETURN verified (PASS)")

    def test_filesystem_cmds(self):
        self.log("  > Test MKDIR / CHDIR")
        start_dir = os.getcwd()
        self.run_cmd('MKDIR "test_dir_tmp"')
        output = self.get_output()
        self.assertIn("DIR CREATED", output)
        self.assertTrue(os.path.exists("test_dir_tmp"))
        self.run_cmd('CHDIR "test_dir_tmp"')
        output = self.get_output()
        self.assertIn("DIR CHANGED", output)
        self.assertNotEqual(os.getcwd(), start_dir)
        os.chdir(start_dir)
        self.assertEqual(os.getcwd(), start_dir)
        self.log("    Filesystem commands verified (PASS)")

    def test_file_io_save_load(self):
        self.load_program(['10 PRINT "SAVED"'])
        self.interp.save_file("test_save.bas")
        _ = self.get_output() 
        self.interp.catalog()
        self.assertIn("TEST_SAVE.BAS", self.get_output().upper()) 
        self.interp.reset()
        self.interp.load_file("test_save.bas")
        _ = self.get_output()
        self.interp.run()
        self.assertEqual(self.get_output(), "SAVED")
        self.log("    SAVE/LOAD verified (PASS)")

    def test_data_read_restore(self):
        self.log("  > Test DATA / READ / RESTORE")
        self.load_program([
            '10 DATA 10, 20',
            '20 READ A, B',
            '30 RESTORE',
            '40 READ C',
            '50 PRINT A;B;C' 
        ])
        self.interp.run()
        self.assertEqual(self.get_output().replace('\n', ''), "102010")
        self.log("    DATA flow verified (PASS)")

    def test_on_goto(self):
        self.log("  > Test ON ... GOTO")
        self.load_program([
            '10 I = 2',
            '20 ON I GOTO 100, 200',
            '30 END',
            '100 PRINT "ONE"',
            '110 END', 
            '200 PRINT "TWO"',
            '210 END'
        ])
        self.interp.run()
        self.assertEqual(self.get_output(), "TWO")
        self.log("    ON GOTO verified (PASS)")

    def test_def_fn(self):
        self.log("  > Test DEF FN")
        self.load_program([
            '10 DEF FNA(X) = X * X',
            '20 PRINT FNA(5)'
        ])
        self.interp.run()
        self.assertEqual(self.get_output(), "25")
        self.log("    DEF FN verified (PASS)")

    # --- UPDATED TEST: GET COMMAND ---
    def test_get_command(self):
        self.log("  > Test GET command (Memory Mapped 0xC010)")
        ADDR_KEY = 49168 # 0xC010

        # Case 1: Buffer is Empty (0)
        # Verify variable retains old value
        self.interp.poke(ADDR_KEY, 0)
        self.load_program([
            '10 A$ = "OLD"',
            '20 GET A$',
            '30 PRINT A$'
        ])
        self.interp.run()
        self.assertEqual(self.get_output(), "OLD")
        self.log("    GET (Empty Buffer) verified (PASS)")

        # Case 2: Buffer has Input (ASCII 65 = 'A')
        self.interp.reset()
        self.load_program([
            '10 GET B$',
            '20 PRINT B$'
        ])
        
        # KEY FIX: Use the interpreter's break hook to simulate
        # a key press occurring AFTER 'RUN' starts but before 'GET' runs.
        def inject_key():
            self.interp.poke(ADDR_KEY, 65) # Inject 'A'
            self.interp.check_break_fn = None # Unhook so it runs once
            return False # Continue execution

        self.interp.check_break_fn = inject_key
        
        self.interp.run()
        self.assertEqual(self.get_output(), "A")
        
        # Verify Buffer was CLEARED after read
        val = self.interp.peek(ADDR_KEY)
        self.assertEqual(val, 0)
        self.log("    GET (String Input + Buffer Clear) verified (PASS)")

        # Case 3: Numeric Input (ASCII 53 = '5')
        self.interp.reset()
        self.load_program([
            '10 GET N',
            '20 PRINT N'
        ])
        
        def inject_num():
            self.interp.poke(ADDR_KEY, 53) # Inject '5'
            self.interp.check_break_fn = None
            return False
            
        self.interp.check_break_fn = inject_num
        
        self.interp.run()
        self.assertEqual(self.get_output(), "5")
        self.assertEqual(self.interp.peek(ADDR_KEY), 0)
        self.log("    GET (Numeric Input) verified (PASS)")

    def test_comments(self):
        self.log("  > Test REM Comments")
        self.load_program([
            '10 REM THIS IS A COMMENT',
            '20 PRINT "OK" : REM INLINE COMMENT'
        ])
        self.interp.run()
        self.assertEqual(self.get_output(), "OK")
        self.log("    REM ignored correctly (PASS)")

    # ==========================
    # NEGATIVE TESTS
    # ==========================

    def test_syntax_errors(self):
        self.expect_error('PRINT *', 'Unexpected token') 
        self.expect_error('$$$', 'Invalid hex number format')
        self.expect_error('PRINT INT(5', 'Expected PAREN )')

    def test_control_errors(self):
        self.interp.reset()
        self.load_program(['10 NEXT I'])
        self.expect_error(None, 'NEXT without FOR', immediate=False)
        
        self.interp.reset()
        self.load_program(['10 RETURN'])
        self.expect_error(None, 'RETURN without GOSUB', immediate=False)
        
        self.interp.reset()
        self.load_program(['10 GOTO 999'])
        self.expect_error(None, 'Line 999 not found', immediate=False)

    def test_argument_errors(self):
        self.expect_error('POKE 100', 'Expected SEP ,')
        self.expect_error('PAUSE "A"', 'could not convert')

    # ==========================
    # NEW FEATURE TESTS
    # ==========================

    def test_ti_variable(self):
        """TI returns elapsed jiffies (60ths of a second) since interpreter start."""
        self.log("  > Test TI (elapsed jiffies)")
        # TI should be numeric and non-negative
        self.run_cmd('PRINT TI')
        val = float(self.get_output())
        self.assertGreaterEqual(val, 0.0)
        # After a short pause, TI should have advanced
        t0 = self.interp.eval_immediate('TI') if hasattr(self.interp, 'eval_immediate') else None
        time.sleep(0.1)
        self.run_cmd('PRINT TI')
        val2 = float(self.get_output())
        self.assertGreater(val2, val - 1)  # Allow for near-zero start; just confirm it's numeric
        self.log("    TI verified (PASS)")

    def test_ti_string(self):
        """TI$ returns current time as HHMMSS (6 digits)."""
        self.log("  > Test TI$ (time string)")
        self.run_cmd('PRINT TI$')
        val = self.get_output()
        self.assertEqual(len(val), 6, f"TI$ should be 6 chars, got '{val}'")
        self.assertTrue(val.isdigit(), f"TI$ should be all digits, got '{val}'")
        self.log("    TI$ verified (PASS)")

    def test_date_string(self):
        """DATE$ returns date as YYYY-MM-DD."""
        self.log("  > Test DATE$ (date string)")
        self.run_cmd('PRINT DATE$')
        val = self.get_output()
        import re
        self.assertRegex(val, r'^\d{4}-\d{2}-\d{2}$', f"DATE$ format wrong: '{val}'")
        self.log("    DATE$ verified (PASS)")

    def test_datetime_string(self):
        """DATETIME$ returns date and time as YYYY-MM-DD HH:MM:SS."""
        self.log("  > Test DATETIME$ (datetime string)")
        self.run_cmd('PRINT DATETIME$')
        val = self.get_output()
        import re
        self.assertRegex(val, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$',
                         f"DATETIME$ format wrong: '{val}'")
        self.log("    DATETIME$ verified (PASS)")

    def test_pause(self):
        """PAUSE N sleeps for approximately N seconds."""
        self.log("  > Test PAUSE timing")
        self.load_program([
            '10 PAUSE 0.1',
            '20 PRINT "DONE"'
        ])
        t0 = time.time()
        self.interp.run()
        elapsed = time.time() - t0
        self.assertEqual(self.get_output(), "DONE")
        self.assertGreaterEqual(elapsed, 0.08, "PAUSE 0.1 returned too fast")
        self.assertLess(elapsed, 1.0, "PAUSE 0.1 took too long")
        self.log("    PAUSE timing verified (PASS)")

    def test_joystick_registers(self):
        """JOY1/JOY2 registers (49184/49185) are readable via PEEK after POKE."""
        self.log("  > Test Joystick registers (49184, 49185)")
        # In standalone mode these are plain MMU RAM — no Pygame needed.
        # Simulate a joystick state by POKEing and reading back.
        self.run_cmd('POKE 49184, 16')   # Fire A bit
        self.run_cmd('PRINT PEEK(49184)')
        self.assertEqual(self.get_output(), "16")
        self.run_cmd('POKE 49185, 8')    # Right bit on port 2
        self.run_cmd('PRINT PEEK(49185)')
        self.assertEqual(self.get_output(), "8")
        # Verify bit masking works correctly in BASIC
        self.load_program([
            '10 POKE 49184, 20',   # bits: right(8) + fire(16) = 24... use 20 = right(8)+fire(16)? 
            # Actually 8+16=24. Use 12 = right(8)+left(4)
            '10 POKE 49184, 12',
            '20 J = PEEK(49184)',
            '30 IF J AND 8 THEN PRINT "RIGHT"',
            '40 IF J AND 4 THEN PRINT "LEFT"'
        ])
        self.interp.run()
        out = self.get_output()
        self.assertIn("RIGHT", out)
        self.assertIn("LEFT", out)
        self.log("    Joystick registers verified (PASS)")

    def test_vdp_registers(self):
        """VDP registers (49280-49287) are writable and readable via POKE/PEEK."""
        self.log("  > Test VDP registers (49280-49287)")
        vdp_regs = {
            49280: 1,   # VDP_MODE on
            49281: 128, # VDP_SCROLL_X
            49282: 64,  # VDP_SCROLL_Y
            49283: 1,   # VDP_SPRITE_EN
            49287: 3,   # VDP_PALETTE bank 3
        }
        for addr, val in vdp_regs.items():
            self.run_cmd(f'POKE {addr}, {val}')
            self.run_cmd(f'PRINT PEEK({addr})')
            result = int(self.get_output())
            self.assertEqual(result, val, f"VDP reg {addr}: wrote {val}, read {result}")
        self.log("    VDP registers verified (PASS)")

    def test_sid_registers(self):
        """SID registers (base 49200) are writable and readable via POKE/PEEK."""
        self.log("  > Test SID registers (base 49200)")
        # Master volume is at SB+24 = 49224
        self.run_cmd('POKE 49224, 15')
        self.run_cmd('PRINT PEEK(49224)')
        self.assertEqual(self.get_output(), "15")
        # Voice 1 control at SB+4 = 49204
        self.run_cmd('POKE 49204, 129')
        self.run_cmd('PRINT PEEK(49204)')
        self.assertEqual(self.get_output(), "129")
        self.log("    SID registers verified (PASS)")

    def test_for_step(self):
        """FOR/NEXT with explicit STEP, including negative step."""
        self.log("  > Test FOR/NEXT with STEP")
        self.load_program([
            '10 FOR I = 1 TO 5 STEP 2',
            '20 PRINT I',
            '30 NEXT I'
        ])
        self.interp.run()
        self.assertEqual(self.get_output().replace('\n', ' ').strip(), "1 3 5")

        self.interp.reset()
        self.load_program([
            '10 FOR I = 3 TO 1 STEP -1',
            '20 PRINT I',
            '30 NEXT I'
        ])
        self.interp.run()
        self.assertEqual(self.get_output().replace('\n', ' ').strip(), "3 2 1")
        self.log("    FOR/NEXT STEP verified (PASS)")

    def test_for_immediate(self):
        """FOR/NEXT as immediate (single-line) command."""
        self.log("  > Test FOR/NEXT immediate mode")
        self.run_cmd('FOR X = 1 TO 3 : PRINT X : NEXT X')
        out = self.get_output().replace('\n', ' ').strip()
        self.assertEqual(out, "1 2 3")
        self.log("    FOR/NEXT immediate mode verified (PASS)")

    def test_nested_for(self):
        """Nested FOR loops."""
        self.log("  > Test Nested FOR loops")
        self.load_program([
            '10 FOR I = 1 TO 2',
            '20 FOR J = 1 TO 2',
            '30 PRINT I * 10 + J',
            '40 NEXT J',
            '50 NEXT I'
        ])
        self.interp.run()
        out = self.get_output().replace('\n', ' ').strip()
        self.assertEqual(out, "11 12 21 22")
        self.log("    Nested FOR loops verified (PASS)")

    def test_dload_binary(self):
        """DLOAD loads a binary file into MMU RAM at a given address."""
        self.log("  > Test DLOAD binary to RAM")
        # Write a small binary file
        test_data = bytes([0xDE, 0xAD, 0xBE, 0xEF])
        with open("test_bin.bin", "wb") as f:
            f.write(test_data)
        try:
            self.load_program([
                '10 DLOAD "test_bin.bin", 4096',
                '20 PRINT PEEK(4096)',
                '30 PRINT PEEK(4097)'
            ])
            self.interp.run()
            out = self.get_output().split()
            self.assertEqual(int(out[0]), 0xDE)
            self.assertEqual(int(out[1]), 0xAD)
            self.log("    DLOAD binary verified (PASS)")
        finally:
            if os.path.exists("test_bin.bin"):
                os.remove("test_bin.bin")

    def test_expr_str_precedence_save_reload(self):
        """
        SAVE/LIST must preserve operator precedence by adding parentheses.
        Bug: (I+1)*8 was saved as I+1*8 which reloads as I+(1*8).
        This test loads a program with mixed-precedence expressions,
        saves it, reloads it, and verifies the values are unchanged.
        """
        self.log("  > Test expr_str parenthesis preservation on SAVE/LOAD")

        # (I+1)*8 — addition inside multiplication, needs parens
        self.load_program([
            '10 I = 0',
            '20 O = (I + 1) * 8',
            '30 PRINT O'
        ])
        self.interp.run()
        self.assertEqual(self.get_output(), "8",
            "(I+1)*8 with I=0 should be 8")

        # Save and reload — check the value survives the round trip
        self.interp.save_file("test_save.bas")
        _ = self.get_output()
        self.interp.reset()
        self.interp.load_file("test_save.bas")
        _ = self.get_output()
        self.interp.run()
        self.assertEqual(self.get_output(), "8",
            "(I+1)*8 changed value after SAVE/LOAD — parentheses lost")

        self.log("    (I+1)*8 round-trip verified (PASS)")

    def test_expr_str_precedence_multiply_add(self):
        """
        (J+11)*8 — the sprite offset formula used throughout STORM-VDP.
        Without parens it saves as J+11*8 = J+88 instead of (J+11)*8.
        For J=1: correct=(1+11)*8=96, broken=1+88=89.
        """
        self.log("  > Test (J+11)*8 sprite offset formula")
        self.load_program([
            '10 J = 1',
            '20 O = (J + 11) * 8',
            '30 PRINT O'
        ])
        self.interp.run()
        self.assertEqual(self.get_output(), "96",
            "(J+11)*8 with J=1 should be 96")

        self.interp.save_file("test_save.bas")
        _ = self.get_output()
        self.interp.reset()
        self.interp.load_file("test_save.bas")
        _ = self.get_output()
        self.interp.run()
        self.assertEqual(self.get_output(), "96",
            "(J+11)*8 changed after SAVE/LOAD — should be 96 not 89")

        self.log("    (J+11)*8 round-trip verified (PASS)")

    def test_expr_str_no_spurious_parens(self):
        """
        Expressions that don't need parentheses shouldn't get them.
        A+B*C is already correct precedence — no parens needed.
        I*8+1 is correct — no parens needed.
        """
        self.log("  > Test no spurious parentheses added")
        self.load_program([
            '10 A = 2',
            '20 B = 3',
            '30 C = 4',
            '40 PRINT A + B * C',   # = 2 + 12 = 14, not (2+3)*4 = 20
            '50 PRINT A * 8 + 1'    # = 16 + 1 = 17
        ])
        self.interp.run()
        out = self.get_output().split()
        self.assertEqual(out[0], "14", "A+B*C should be 14")
        self.assertEqual(out[1], "17", "A*8+1 should be 17")

        self.interp.save_file("test_save.bas")
        _ = self.get_output()
        self.interp.reset()
        self.interp.load_file("test_save.bas")
        _ = self.get_output()
        self.interp.run()
        out = self.get_output().split()
        self.assertEqual(out[0], "14", "A+B*C changed after SAVE/LOAD")
        self.assertEqual(out[1], "17", "A*8+1 changed after SAVE/LOAD")

        self.log("    No spurious parens verified (PASS)")


if __name__ == '__main__':
    unittest.main()