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
        self.expect_error('$$$', 'Unexpected char')
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

if __name__ == '__main__':
    unittest.main()