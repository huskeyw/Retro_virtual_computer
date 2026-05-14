#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
basic.py — Steve's 8-Bit BASIC Interpreter
===========================================
A line-numbered BASIC interpreter modelled after Commodore BASIC V2.
Runs as the CPU thread inside the RetroThree virtual machine.

Architecture:
  - Tokenizer  : converts a text line into a flat token list
  - Parser     : converts tokens into an AST (Abstract Syntax Tree)
  - Interpreter: walks the AST and executes statements
  - MMU        : memory manager giving BASIC access to 512KB RAM,
                 bank-switched windows, and hardware I/O hooks

BASIC programs are stored as a dict of {line_number: [parsed_statements]}.
Execution walks sorted line numbers, advancing stmt_idx each step.
GOTO/GOSUB change self.line and set stmt_idx=0.
RETURN pops (line, stmt_idx) from self.stack.
"""

import sys
import os
import math
import random
import time
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional

# ==========================
# Non-Blocking Keyboard Input
# Used by the BASIC GET command to read a single keypress without
# blocking execution. Detects Windows (msvcrt) or Unix (tty/termios)
# at startup and uses the appropriate platform API.
# In VM mode this is bypassed — keystrokes come through the VM key queue.
# ==========================
class Keyboard:
    def __init__(self):
        self.impl = None
        if os.name == 'nt': 
            try:
                import msvcrt
                self.impl = 'win'
                self.msvcrt = msvcrt
            except ImportError: pass
        else: 
            try:
                import tty, termios, select
                self.impl = 'unix'
                self.tty = tty
                self.termios = termios
                self.select = select
            except ImportError: pass

    def get_char(self):
        if self.impl == 'win':
            if self.msvcrt.kbhit():
                ch = self.msvcrt.getch()
                try: return ch.decode('utf-8')
                except: return None
        elif self.impl == 'unix':
            fd = sys.stdin.fileno()
            old_settings = self.termios.tcgetattr(fd)
            try:
                self.tty.setraw(sys.stdin.fileno())
                r, _, _ = self.select.select([sys.stdin], [], [], 0)
                if r: return sys.stdin.read(1)
            finally:
                self.termios.tcsetattr(fd, self.termios.TCSADRAIN, old_settings)
        return None

kb = Keyboard()

# ==========================
# Exceptions
# ==========================
class BasicError(Exception):
    def __init__(self, message: str, line_number: Optional[int] = None):
        msg = message
        if line_number is not None:
            msg += f" (at line {line_number})"
        super().__init__(msg)

# ==========================
# MMU — Memory Management Unit
# ==========================
# Manages the 512KB physical RAM array and presents a 64KB address space
# to BASIC through read() and write() methods.
#
# Address space layout:
#   $0000-$BFFF  Direct RAM (lower 48KB, writable)
#   $8000-$BFFF  Bank-switched window: maps to any 16KB block in extended RAM
#                POKE $C000, N selects which bank appears in this window
#   $C000-$CFFF  Hardware I/O registers (partially intercepted by hooks)
#   $D800        Color RAM base
#   $F000        Character ROM (loaded from 'chargen' file or built-in fallback)
#
# IO hooks: attach_hook() lets virtual_pc.py intercept reads/writes to
# specific addresses, routing them to the VDP, SID, joystick etc.
# This is how POKE 49280,1 turns the VDP on without BASIC knowing about it.
class MMU:
    MEMORY_SIZE = 512 * 1024   # 512KB total physical RAM

    # Hardware register addresses in the $C000 I/O page
    ADDR_BANK_REG    = 0xC000  # Bank select: write N to map bank N into $8000 window
    ADDR_SCREEN_CMD  = 0xC001  # Write 1 to clear screen
    ADDR_BORDER_COL  = 0xC002  # Border color index (0-15)
    ADDR_BG_COL      = 0xC003  # Background color index (0-15)
    ADDR_TEXT_COL    = 0xC004  # Text/pen color index (0-15)
    ADDR_SCREEN_PTR  = 0xC005  # Screen RAM page (default 4 = $0400)
    ADDR_CHARSET_PTR = 0xC006  # Character ROM page (default 240 = $F000)

    ADDR_LAST_KEY    = 0xC010  # Last key pressed (used by GET command)
    ADDR_COLOR_RAM   = 0xD800  # Color RAM base — one byte per screen cell
    ADDR_CHAR_ROM    = 0xF000  # Character ROM base — 8 bytes per character

    # Bank-switched window: $8000-$BFFF maps to 16KB blocks in extended RAM
    BANK_WINDOW_START = 0x8000
    BANK_WINDOW_END   = 0xBFFF
    BANK_SIZE         = 0x4000  # 16KB per bank
   
    def __init__(self):
        self.physical_ram = bytearray(self.MEMORY_SIZE)
        self.active_bank = 0
        self.io_hooks = {} 
        self.read_hooks = {} 
        self.fallback_active = False 
        self.reset()

    def reset(self):
        self.physical_ram[:] = bytearray(self.MEMORY_SIZE)
        self.active_bank = 0
        self._load_internal_font()

    def _load_internal_font(self):
        """
        Load the character ROM into physical RAM at $F000.
        Searches for a 'chargen' binary file (C64-compatible 2KB or 4KB ROM).
        If not found, activates the built-in fallback font which contains
        digits 0-9, letters A-Z, and a few punctuation characters.
        The fallback is enough to run the BASIC prompt and most programs.
        """
        base = self.ADDR_CHAR_ROM
        self.fallback_active = False
        script_dir = os.path.dirname(os.path.abspath(__file__))
        search_paths = [os.getcwd(), script_dir]
        rom_files = ['chargen', 'chargen.bin', 'chargen.rom']
        
        loaded = False
        for path in search_paths:
            for name in rom_files:
                full_path = os.path.join(path, name)
                if os.path.exists(full_path):
                    try:
                        with open(full_path, 'rb') as f:
                            data = f.read()
                            length = min(len(data), 4096)
                            self.physical_ram[base : base+length] = data[:length]
                            loaded = True
                            break
                    except Exception as e:
                        print(f"SYSTEM: ERROR LOADING {full_path}: {e}")
            if loaded: break

        if loaded: return

        self.fallback_active = True
        for i in range(2048): self.physical_ram[base + i] = 0x00 
            
        font_data = {
            48:[0x3C,0x66,0x66,0x66,0x66,0x66,0x3C,0x00], 49:[0x18,0x38,0x18,0x18,0x18,0x18,0x3C,0x00],
            50:[0x3C,0x66,0x0C,0x18,0x30,0x66,0x7E,0x00], 51:[0x3C,0x66,0x0C,0x18,0x0C,0x66,0x3C,0x00],
            52:[0x0C,0x1C,0x3C,0x6C,0xFE,0x0C,0x0C,0x00], 53:[0x7E,0x60,0x7C,0x06,0x06,0x66,0x3C,0x00],
            54:[0x1C,0x30,0x60,0x7C,0x66,0x66,0x3C,0x00], 55:[0x7E,0x06,0x0C,0x18,0x30,0x30,0x30,0x00],
            56:[0x3C,0x66,0x3C,0x3C,0x66,0x66,0x3C,0x00], 57:[0x3C,0x66,0x66,0x3E,0x06,0x0C,0x38,0x00],
            65:[0x3C,0x66,0x66,0x7E,0x66,0x66,0x66,0x00], 66:[0x7C,0x66,0x66,0x7C,0x66,0x66,0x7C,0x00],
            67:[0x3C,0x66,0x60,0x60,0x60,0x66,0x3C,0x00], 68:[0x78,0x6C,0x66,0x66,0x66,0x6C,0x78,0x00],
            69:[0x7E,0x60,0x60,0x78,0x60,0x60,0x7E,0x00], 70:[0x7E,0x60,0x60,0x78,0x60,0x60,0x60,0x00],
            71:[0x3C,0x66,0x60,0x6E,0x66,0x66,0x3C,0x00], 72:[0x66,0x66,0x66,0x7E,0x66,0x66,0x66,0x00],
            73:[0x3C,0x18,0x18,0x18,0x18,0x18,0x3C,0x00], 74:[0x1E,0x0C,0x0C,0x0C,0x0C,0x6C,0x38,0x00],
            75:[0x66,0x6C,0x78,0x70,0x78,0x6C,0x66,0x00], 76:[0x60,0x60,0x60,0x60,0x60,0x60,0x7E,0x00],
            77:[0x63,0x77,0x7F,0x6B,0x63,0x63,0x63,0x00], 78:[0x63,0x67,0x6F,0x7B,0x73,0x63,0x63,0x00],
            79:[0x3C,0x66,0x66,0x66,0x66,0x66,0x3C,0x00], 80:[0x7C,0x66,0x66,0x7C,0x60,0x60,0x60,0x00],
            81:[0x3C,0x66,0x66,0x66,0x66,0x3C,0x0E,0x00], 82:[0x7C,0x66,0x66,0x7C,0x78,0x6C,0x66,0x00],
            83:[0x3C,0x66,0x60,0x3C,0x06,0x66,0x3C,0x00], 84:[0x7E,0x18,0x18,0x18,0x18,0x18,0x18,0x00],
            85:[0x66,0x66,0x66,0x66,0x66,0x66,0x3C,0x00], 86:[0x66,0x66,0x66,0x66,0x66,0x3C,0x18,0x00],
            87:[0x63,0x63,0x63,0x6B,0x7F,0x77,0x63,0x00], 88:[0x66,0x66,0x3C,0x18,0x3C,0x66,0x66,0x00],
            89:[0x66,0x66,0x66,0x3C,0x18,0x18,0x18,0x00], 90:[0x7E,0x06,0x0C,0x18,0x30,0x60,0x7E,0x00],
            45:[0x00,0x00,0x00,0x3C,0x00,0x00,0x00,0x00], 46:[0x00,0x00,0x00,0x00,0x00,0x18,0x18,0x00], 
            58:[0x00,0x18,0x18,0x00,0x18,0x18,0x00,0x00], 62:[0x00,0x60,0x30,0x18,0x30,0x60,0x00,0x00], 
        }
        for char_code, bytes_list in font_data.items():
            start_addr = base + (char_code * 8)
            for i, b in enumerate(bytes_list):
                self.physical_ram[start_addr + i] = b

    def attach_hook(self, addr, write_cb=None, read_cb=None):
        """
        Register Python callbacks for a hardware register address.
        write_cb(val) is called when BASIC does POKE addr, val.
        read_cb()     is called when BASIC does PEEK(addr).
        Used by virtual_pc.py to wire up the VDP, SID, joystick ports etc.
        """
        if write_cb: self.io_hooks[addr] = write_cb
        if read_cb:  self.read_hooks[addr] = read_cb

    def read(self, addr: int) -> int:
        """
        Read one byte from the 64KB address space.
        Checks registered read hooks first (hardware registers),
        then handles the $C000 I/O page and bank-switched window,
        then falls through to physical RAM.
        """
        addr = int(addr) & 0xFFFF
        if addr in self.read_hooks: 
            return int(self.read_hooks[addr]())
        if 0xC000 <= addr <= 0xCFFF:
            if addr == self.ADDR_BANK_REG: return self.active_bank
            return self.physical_ram[addr]
        if self.BANK_WINDOW_START <= addr <= self.BANK_WINDOW_END:
            offset = addr - self.BANK_WINDOW_START
            phys_addr = (self.active_bank * self.BANK_SIZE) + offset
            return self.physical_ram[phys_addr % self.MEMORY_SIZE]
        return self.physical_ram[addr]

    def write(self, addr: int, val: int):
        """
        Write one byte to the 64KB address space.
        Fires registered write hooks first (so hardware registers respond),
        then writes to physical RAM. The bank register ($C000) is handled
        specially — writing it switches which 16KB block appears at $8000.
        Note: virtual_pc.py replaces this method with dynamic_hook_write
        to add VDP tilemap and charset ROM monitoring.
        """
        addr = int(addr) & 0xFFFF
        val = int(val) & 0xFF
        if addr in self.io_hooks: self.io_hooks[addr](val)
        if 0xC000 <= addr <= 0xCFFF:
            if addr == self.ADDR_BANK_REG: self.active_bank = val
            self.physical_ram[addr] = val 
            return
        if self.BANK_WINDOW_START <= addr <= self.BANK_WINDOW_END:
            offset = addr - self.BANK_WINDOW_START
            phys_addr = (self.active_bank * self.BANK_SIZE) + offset
            self.physical_ram[phys_addr % self.MEMORY_SIZE] = val
            return
        self.physical_ram[addr] = val

# ==========================
# Token Types
# ==========================
# The tokenizer produces a flat list of Token objects from a BASIC line.
# TK_NUMBER  — numeric literal (float)
# TK_STRING  — quoted string literal
# TK_IDENT   — keyword or variable name (always uppercased)
# TK_OP      — operator: + - * / ^ = < > <= >= <>
# TK_SEP     — separator: , ;
# TK_PAREN   — parenthesis or bracket: ( ) [ ]
# TK_COLON   — statement separator :
# TK_EOF     — end of line sentinel
# TK_REM     — REM comment (consumes rest of line)
TK_NUMBER, TK_STRING, TK_IDENT, TK_OP, TK_SEP, TK_PAREN, TK_COLON, TK_EOF = \
    "NUMBER", "STRING", "IDENT", "OP", "SEP", "PAREN", "COLON", "EOF"
TK_REM = "REM"

# ==========================
# AST Node Types
# ==========================
# Each parsed statement becomes one of these dataclass instances.
# The interpreter's run_stmt() dispatches on isinstance() to execute them.
# Expression nodes (Num, Str, Var, BinOp etc.) are evaluated recursively
# by eval() to produce a Python float or string value.

@dataclass
class Token: kind: str; value: Any; pos: int
@dataclass 
class Statement: pass
@dataclass
class Expr: pass
@dataclass
class Num(Expr): value: float
@dataclass
class Str(Expr): value: str
@dataclass
class Var(Expr): name: str; subscripts: Optional[List[Expr]] = None
@dataclass
class BinOp(Expr): op: str; left: Expr; right: Expr
@dataclass
class UnaryOp(Expr): op: str; operand: Expr
@dataclass
class FuncCall(Expr): name: str; args: List[Expr]

@dataclass
class PrintStmt(Statement): items: List[Tuple[Expr, str]]
@dataclass
class InputStmt(Statement): prompt: Optional[Expr]; targets: List[Var]
@dataclass
class GetStmt(Statement): targets: List[Var]
@dataclass
class LetStmt(Statement): target: Var; value: Expr
@dataclass
class IfStmt(Statement): condition: Expr; then_line: Optional[int] = None; then_stmt: Optional[Statement] = None
@dataclass
class GotoStmt(Statement): line: int
@dataclass
class GosubStmt(Statement): line: int
@dataclass
class ReturnStmt(Statement): pass
@dataclass
class ForStmt(Statement): var: Var; start: Expr; end: Expr; step: Optional[Expr]
@dataclass
class NextStmt(Statement): var: Optional[str]
@dataclass
class DimStmt(Statement): dims: List[Tuple[str, List[Expr]]]
@dataclass
class RemStmt(Statement): comment: str
@dataclass
class StopStmt(Statement): pass
@dataclass
class EndStmt(Statement): pass
@dataclass
class PokeStmt(Statement): addr: Expr; value: Expr
@dataclass
class ClsStmt(Statement): pass   
@dataclass
class ClearStmt(Statement): pass 
@dataclass
class PauseStmt(Statement): duration: Expr
@dataclass
class DLoadStmt(Statement):
    """Load a binary or BASIC file from disk or host filesystem.
    With addr=None: load as BASIC source (replaces current program).
    With addr=N:    load as binary block directly into RAM at address N.
    Used for loading tile sheets, music data, and game assets."""
    filename: Expr
    addr: Optional[Expr] = None
@dataclass
class MatStmt(Statement): pass
@dataclass
class IgnoreStmt(Statement): cmd: str 
@dataclass
class RestoreStmt(Statement): line: Optional[int] = None
@dataclass
class ReadStmt(Statement): targets: List[Var]
@dataclass
class DataStmt(Statement): values: List[Any]
@dataclass
class OnGotoStmt(Statement): expr: Expr; targets: List[int]
@dataclass
class DefStmt(Statement): name: str; arg: str; expr: Expr
@dataclass
class ChdirStmt(Statement): path: Expr
@dataclass
class MkdirStmt(Statement): path: Expr

class Tokenizer:
    """
    Converts a single BASIC line (without its line number) into a token list.
    Called once per line at load time — parsed lines are cached as AST nodes,
    so tokenizing only happens once per line regardless of how many times it runs.

    Handles:
      - Numeric literals including C64-style hex ($C080)
      - Quoted strings (single or double quotes)
      - Identifiers and keywords (uppercased, may include $ suffix)
      - Operators and multi-char operators (<=, >=, <>)
      - REM comments (consume rest of line immediately)
      - Colon statement separators
    """
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.length = len(text)
        
    def peek(self): 
        return self.text[self.pos] if self.pos < self.length else ''
        
    def advance(self): 
        ch = self.peek()
        self.pos += 1
        return ch
    
    def skip_ws(self):
        safety = 0
        while self.peek() and self.peek() in ' \t\r': 
            self.advance()
            safety += 1
            if safety > 10000: raise BasicError("Freeze in skip_ws")

    def tokenize_all(self):
        tokens = []
        safety = 0
        while True:
            safety += 1
            if safety > 100000: raise BasicError("Tokenizer freeze detected")
            self.skip_ws()
            ch = self.peek()
            if not ch: 
                tokens.append(Token(TK_EOF, None, self.pos))
                break
            
            if ch in '"\'': 
                start = self.pos
                q = self.advance()
                s = ""
                loop_safety = 0
                while True:
                    c = self.peek()
                    if c == '' or c == '\n' or c == '\r': break
                    if c == q: 
                        self.advance() 
                        break
                    s += self.advance()
                    loop_safety += 1
                    if loop_safety > 1000: raise BasicError("Freeze in String Parsing")
                tokens.append(Token(TK_STRING, s, start))
            
            elif ch.isdigit() or (ch == '.' and (self.pos + 1 < self.length) and self.text[self.pos+1].isdigit()):
                start = self.pos
                s = ""
                while self.peek().isdigit() or self.peek() == '.': 
                    s += self.advance()
                tokens.append(Token(TK_NUMBER, float(s), start))

            elif ch == '$':
                start = self.pos
                self.advance() 
                s = ""
                hex_safety = 0
                while self.peek() and self.peek().upper() in '0123456789ABCDEF':
                    s += self.advance()
                    hex_safety += 1
                    if hex_safety > 20: raise BasicError("Freeze in hex literal")
                if not s: raise BasicError("Invalid hex number format")
                tokens.append(Token(TK_NUMBER, float(int(s, 16)), start))

            elif ch.isalpha() or ch == '_':
                start = self.pos
                s = ""
                while True:
                    c = self.peek()
                    if c != '' and (c.isalnum() or c in '_$'): 
                        s += self.advance()
                    else: break
                
                if s.upper() == 'REM':
                    rem_content = ""
                    while self.peek() and self.peek() not in ['\n', '\r']:
                        rem_content += self.advance()
                    tokens.append(Token(TK_REM, rem_content, start))
                    continue 

                tokens.append(Token(TK_IDENT, s.upper(), start))
            
            elif ch in ':(),;?[].!': 
                self.advance()
                val = ch
                kind = TK_SEP
                if ch == ':': kind = TK_COLON
                elif ch == '?': kind = TK_OP
                elif ch in ',;': kind = TK_SEP
                else: 
                    kind = TK_PAREN
                    if ch == '[': val = '('
                    elif ch == ']': val = ')'
                tokens.append(Token(kind, val, self.pos))
            
            else:
                op = ch
                two = self.text[self.pos:self.pos+2]
                if two in ['<=','>=','<>']: op = two
                elif ch == '#': op = '<>' 
                elif ch not in '+-*/^=<>' : raise BasicError(f"Unexpected char {ch}")
                
                tokens.append(Token(TK_OP, op, self.pos))
                if op == '<>' and ch == '#': self.pos += 1 
                else: self.pos += len(two) if (op == two and ch != op) else 1
                
        return tokens

class Parser:
    """
    Recursive descent parser. Converts a token list into a single AST Statement node.
    Called by split_stmts() which first splits colon-separated statements,
    then parses each fragment independently.

    Expression precedence (lowest to highest):
      OR -> AND -> comparison -> addition -> multiplication -> power -> unary -> primary

    parse_statement() dispatches on the first identifier token to determine
    which statement type to parse. Unknown identifiers fall through to parse_let()
    which handles both  LET X=expr  and the implicit  X=expr  form.
    """
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        
    def peek(self): return self.tokens[self.pos] if self.pos < len(self.tokens) else Token(TK_EOF, None, -1)
    def advance(self): 
        t = self.peek()
        self.pos += 1
        return t
        
    def match(self, k, v=None):
        if self.peek().kind == k and (v is None or self.peek().value == v): 
            self.advance()
            return True
        return False
        
    def expect(self, kind, value=None): 
        tok = self.peek()
        if tok.kind != kind or (value is not None and tok.value != value):
            raise BasicError(f"Expected {kind} {value if value else ''}")
        return self.advance()
    
    def parse_expr(self): return self.parse_op(['OR'], self.parse_and)
    def parse_and(self): return self.parse_op(['AND'], self.parse_cmp)
    def parse_cmp(self): return self.parse_op(['=','<>','<','>','<=','>='], self.parse_add)
    def parse_add(self): return self.parse_op(['+','-'], self.parse_mul)
    def parse_mul(self): return self.parse_op(['*','/'], self.parse_pow)
    def parse_pow(self): return self.parse_op(['^'], self.parse_unary)
    
    def parse_op(self, ops, next_fn):
        l = next_fn()
        while self.peek().kind in [TK_OP, TK_IDENT] and self.peek().value in ops:
            op = self.advance().value
            l = BinOp(op, l, next_fn())
        return l

    def parse_unary(self):
        if self.match(TK_OP, '-') or self.match(TK_IDENT, 'NOT'):
            op = self.tokens[self.pos-1].value
            return UnaryOp(op, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        t = self.peek()
        if self.match(TK_NUMBER): return Num(t.value)
        if self.match(TK_STRING): return Str(t.value)
        if self.match(TK_IDENT):
            name = self.tokens[self.pos-1].value
            if name in BASICInterpreter.RESERVED:
                 raise BasicError(f"Syntax Error: Reserved keyword '{name}' cannot be used as variable")
            
            if self.match(TK_PAREN, '('):
                args = []
                if not self.match(TK_PAREN, ')'):
                    while True:
                        args.append(self.parse_expr())
                        if not self.match(TK_SEP, ','): 
                            self.expect(TK_PAREN, ')')
                            break
                if name in BASICInterpreter.FUNCS: return FuncCall(name, args)
                if name.startswith("FN"): return FuncCall(name, args)
                return Var(name, args)
            return Var(name)
        if self.match(TK_PAREN, '('): 
            e = self.parse_expr()
            self.expect(TK_PAREN, ')')
            return e
        raise BasicError(f"Unexpected token {t.value}")

    def parse_statement(self):
        t = self.peek()
        if t.kind == TK_REM:
            self.advance()
            return RemStmt(t.value)

        if t.kind == TK_IDENT or t.value == '?':
            kw = t.value
            if kw == '?' or kw == 'PRINT': 
                self.advance()
                return self.parse_print()
            if kw == 'INPUT': 
                self.advance()
                return self.parse_input()
            if kw == 'GET':
                self.advance()
                vars = []
                while True:
                    vars.append(self.parse_primary())
                    if not self.match(TK_SEP, ','): break
                return GetStmt(vars)
            if kw == 'IF': 
                self.advance()
                return self.parse_if()
            if kw == 'GOTO': 
                self.advance()
                return GotoStmt(int(self.expect(TK_NUMBER).value))
            if kw == 'GOSUB': 
                self.advance()
                return GosubStmt(int(self.expect(TK_NUMBER).value))
            if kw == 'RETURN': 
                self.advance()
                return ReturnStmt()
            if kw == 'FOR': 
                self.advance()
                return self.parse_for()
            if kw == 'NEXT':
                self.advance()
                v=None
                if self.peek().kind==TK_IDENT: v=self.advance().value 
                return NextStmt(v)
            if kw == 'DIM': 
                self.advance()
                return self.parse_dim()
            if kw == 'REM': 
                self.advance()
                return RemStmt("")
            if kw == 'STOP': 
                self.advance()
                return StopStmt()
            if kw == 'END': 
                self.advance()
                return EndStmt()
            if kw == 'CLS': 
                self.advance()
                return ClsStmt()
            if kw == 'CLEAR': 
                self.advance()
                return ClearStmt()
            if kw == 'PAUSE': 
                self.advance()
                return PauseStmt(self.parse_expr())
            
            # ---  DLOAD Parsing ---
            if kw == 'DLOAD':
                self.advance()
                fname = self.parse_expr()
                addr = None
                if self.match(TK_SEP, ','):
                    addr = self.parse_expr()
                return DLoadStmt(fname, addr)

            if kw == 'POKE': 
                self.advance()
                a = self.parse_expr()
                self.expect(TK_SEP, ',')
                v = self.parse_expr()
                return PokeStmt(a,v)
            if kw == 'LET': 
                self.advance()
                return self.parse_let()
            if kw == 'CHDIR': 
                self.advance()
                return ChdirStmt(self.parse_expr())
            if kw == 'MKDIR': 
                self.advance()
                return MkdirStmt(self.parse_expr())
            if kw == 'RESTORE' or kw == 'RESET':
                self.advance()
                if self.peek().kind == TK_NUMBER:
                    return RestoreStmt(int(self.advance().value))
                return RestoreStmt()
            if kw == 'READ':
                self.advance()
                vars = []
                while True:
                    vars.append(self.parse_primary())
                    if not self.match(TK_SEP, ','): break
                return ReadStmt(vars)
            if kw == 'DATA':
                self.advance()
                vals = []
                while True:
                    t = self.peek()
                    if t.kind == TK_STRING: 
                        vals.append(t.value); self.advance()
                    elif t.kind == TK_NUMBER: 
                        vals.append(t.value); self.advance()
                    elif t.kind == TK_IDENT: 
                        vals.append(t.value); self.advance()
                    else: raise BasicError("Expected literal in DATA")
                    if not self.match(TK_SEP, ','): break
                return DataStmt(vals)
            if kw == 'ON':
                self.advance()
                exp = self.parse_expr()
                self.expect(TK_IDENT, 'GOTO') 
                targets = []
                while True:
                    targets.append(int(self.expect(TK_NUMBER).value))
                    if not self.match(TK_SEP, ','): break
                return OnGotoStmt(exp, targets)
            if kw == 'DEF':
                self.advance()
                fn_name = self.expect(TK_IDENT).value
                self.expect(TK_PAREN, '(')
                arg_name = self.expect(TK_IDENT).value
                self.expect(TK_PAREN, ')')
                self.expect(TK_OP, '=')
                expr = self.parse_expr()
                return DefStmt(fn_name, arg_name, expr)
            if kw == 'MAT':
                self.advance() 
                while self.peek().kind not in [TK_EOF, TK_COLON]: self.advance()
                return MatStmt()

            return self.parse_let()
        raise BasicError("Unknown Statement")

    def parse_print(self):
        items = []
        if self.peek().kind in [TK_EOF, TK_COLON]: return PrintStmt([])
        while True:
            if self.match(TK_SEP, ';') or self.match(TK_SEP, ','): 
                items.append((Str(""), self.tokens[self.pos-1].value))
            else:
                e = self.parse_expr()
                sep = ""
                if self.match(TK_SEP, ';') or self.match(TK_SEP, ','): 
                    sep = self.tokens[self.pos-1].value
                items.append((e, sep))
            if self.peek().kind in [TK_EOF, TK_COLON]: break
        return PrintStmt(items)

    def parse_input(self):
        prompt = None
        if self.peek().kind == TK_STRING: 
            prompt = Str(self.advance().value)
            if self.match(TK_SEP, ';'): pass
            elif self.match(TK_SEP, ','): pass
        vars = []
        while True:
            v = self.parse_primary()
            vars.append(v)
            if not self.match(TK_SEP, ','): break
        return InputStmt(prompt, vars)

    def parse_let(self):
        t = self.parse_primary()
        self.expect(TK_OP, '=')
        v = self.parse_expr()
        return LetStmt(t, v)

    def parse_if(self):
        c = self.parse_expr()
        self.expect(TK_IDENT, 'THEN')
        if self.peek().kind == TK_NUMBER: 
            return IfStmt(c, then_line=int(self.advance().value))
        return IfStmt(c, then_stmt=self.parse_statement())

    def parse_for(self):
        v = self.parse_primary()
        self.expect(TK_OP, '=')
        s = self.parse_expr()
        self.expect(TK_IDENT, 'TO')
        e = self.parse_expr()
        step = None
        if self.match(TK_IDENT, 'STEP'): 
            step = self.parse_expr()
        return ForStmt(v, s, e, step)

    def parse_dim(self):
        dims = []
        while True:
            n = self.expect(TK_IDENT).value
            self.expect(TK_PAREN, '(')
            args=[]
            while True:
                args.append(self.parse_expr())
                if not self.match(TK_SEP, ','): break
            self.expect(TK_PAREN, ')')
            dims.append((n, args))
            if not self.match(TK_SEP, ','): break
        return DimStmt(dims)

def split_stmts(tokens):
    """
    Split a flat token list into a list of per-statement token lists.
    BASIC allows multiple statements on one line separated by colons:
      10 X=1 : PRINT X : GOTO 10
    Each fragment gets its own EOF token appended so the Parser
    sees a clean end-of-input boundary.
    """
    parts = []
    curr = []
    for t in tokens:
        if t.kind == TK_COLON: 
            parts.append(curr)
            curr = []
        elif t.kind == TK_EOF: 
            if curr: parts.append(curr)
            curr = []
        else: 
            curr.append(t)
    if curr: parts.append(curr)
    return [p + [Token(TK_EOF,None,0)] for p in parts if p]

# ==========================
# Interpreter
# ==========================
class BASICInterpreter:
    """
    Executes a BASIC program stored as {line_number: [Statement, ...]}.

    Execution state:
      self.prog      — the program: dict of line_num -> list of AST statements
      self.line      — current line number being executed
      self.stmt_idx  — index into current line's statement list
      self.stack     — GOSUB return stack: list of (line, stmt_idx) tuples
      self.for_stack — FOR/NEXT loop stack: list of loop context dicts
      self.vars      — scalar variables: {'X': 3.0, 'A$': 'hello'}
      self.arrs      — array variables: {'A': {(0,): 1.0, (1,): 2.0}}
      self.defs      — user-defined functions from DEF FN

    The interpreter runs on its own thread in VM mode. check_break_fn
    is called before every statement to test for ESC/reset requests
    from the Pygame main thread without needing a lock.
    """

    # Built-in functions recognised by the parser as FuncCall nodes
    FUNCS = {
        'ABS','INT','RND','SGN','SQR','LEN','VAL','STR$','CHR$','ASC','PEEK',
        'SIN', 'COS', 'TAN', 'LOG', 'EXP', 'TAB', 'SPC', 'LEFT$', 'RIGHT$', 'MID$'
    }

    # Keywords that cannot be used as variable names
    RESERVED = {
        'PRINT', 'INPUT', 'GET', 'IF', 'THEN', 'GOTO', 'GOSUB', 'RETURN',
        'FOR', 'TO', 'STEP', 'NEXT', 'DIM', 'REM', 'STOP', 'END', 'CLS',
        'CLEAR', 'PAUSE', 'POKE', 'LET', 'CHDIR', 'MKDIR', 'RESTORE',
        'READ', 'DATA', 'ON', 'DEF', 'MAT', 'DLOAD'
    }

    # Maximum GOSUB nesting depth before raising out-of-memory error
    MAX_STACK_DEPTH = 200

    def __init__(self):
        self.prog = {}
        self.mmu = MMU()
        self.vars = {}
        self.arrs = {}
        self.stack = []
        self.for_stack = []
        self.running = False
        self.line = 0
        self.stmt_idx = 0
        self.check_break_fn = None 
        self.data = []
        self.data_ptr = 0
        self.defs = {} 
        self.print_handler = lambda x: print(x, end='', flush=True)
        self.input_handler = input 
        self.start_time = time.time()
        
        # Default hooks for standalone execution
        self.disk_mount_check = lambda: False
        self.disk_read = lambda fn: None

    def clear_runtime(self):
        """
        Wipe all runtime state (variables, arrays, stacks, user functions).
        Called by the CLEAR command and at the start of RUN.
        Does NOT clear the program listing — use reset() for a full wipe.
        """
        self.vars = {}
        self.arrs = {}
        self.stack = []
        self.for_stack = []
        self.defs = {}
        self.data_ptr = 0
        self.data_line_map = {}
        self.poke(MMU.ADDR_LAST_KEY, 0)

    def reset(self):
        """
        Full cold reset — clears both the program listing and all runtime state.
        Called by the NEW command and when loading a new program.
        """
        self.prog = {} 
        self.clear_runtime() 
        self.line = 0
        self.stmt_idx = 0
        self.data = []
        
    def peek(self, addr): return self.mmu.read(addr)
    def poke(self, addr, val): self.mmu.write(addr, val)

    def eval(self, e):
        """
        Recursively evaluate an expression AST node to a Python value.
        Returns float for numeric expressions, str for string expressions.
        BinOp '+' is overloaded: numeric addition or string concatenation.
        Comparison operators return -1.0 for true, 0.0 for false
        (matching C64 BASIC behaviour where TRUE = -1).
        """
        if isinstance(e, Num): return e.value
        if isinstance(e, Str): return e.value
        if isinstance(e, Var): return self.get_var(e)
        if isinstance(e, BinOp):
            l = self.eval(e.left)
            r = self.eval(e.right)
            if e.op == '+': return (str(l)+str(r)) if isinstance(l,str) else (l+r)
            if e.op == '-': return l-r
            if e.op == '*': return l*r
            if e.op == '/': return l/r
            if e.op == '^': return l**r
            if e.op in ['=','<>','<','>','<=','>=']: return self.cmp(l,r,e.op)
            if e.op == 'AND': return float(int(l) & int(r))
            if e.op == 'OR': return float(int(l) | int(r))
            return 0
        if isinstance(e, FuncCall): 
            if e.name.startswith("FN") and e.name in self.defs:
                def_obj = self.defs[e.name]
                arg_val = self.eval(e.args[0])
                old_val = self.vars.get(def_obj.arg, None)
                self.vars[def_obj.arg] = arg_val
                ret = self.eval(def_obj.expr)
                if old_val is not None: self.vars[def_obj.arg] = old_val
                else: del self.vars[def_obj.arg]
                return ret
            return self.call(e.name, e.args)
        if isinstance(e, UnaryOp):
            val = self.eval(e.operand)
            if e.op == '-': return -val
            if e.op == 'NOT': return float(~int(val))
        return 0

    def truthy(self, v): 
        return (len(v)>0 if isinstance(v,str) else v!=0)

    def cmp(self, l, r, op):
        res = False
        if op == '=': res = (l == r)
        elif op == '<>': res = (l != r)
        elif op == '<': res = (l < r)
        elif op == '>': res = (l > r)
        elif op == '<=': res = (l <= r)
        elif op == '>=': res = (l >= r)
        return -1.0 if res else 0.0

    def call(self, name, args):
        """
        Execute a built-in function call and return its value.
        All arguments are pre-evaluated before this is called.
        String functions (STR$, CHR$, LEFT$ etc.) return str.
        Math and system functions return float.
        PEEK reads a byte from the MMU address space.
        """
        vals = [self.eval(a) for a in args]
        if name == 'SIN': return math.sin(float(vals[0]))
        if name == 'COS': return math.cos(float(vals[0]))
        if name == 'TAN': return math.tan(float(vals[0]))
        if name == 'LOG': return math.log(float(vals[0]))
        if name == 'EXP': return math.exp(float(vals[0]))
        if name == 'SQR': return math.sqrt(float(vals[0]))
        if name == 'ABS': return abs(vals[0])
        if name == 'INT': return float(int(vals[0]))
        if name == 'RND': return random.random()
        if name == 'SGN': return 1.0 if vals[0]>0 else (-1.0 if vals[0]<0 else 0.0)
        if name == 'PEEK': return float(self.peek(vals[0]))
        if name == 'VAL': return float(vals[0])
        if name == 'LEN': return float(len(vals[0]))
        if name == 'CHR$': return chr(int(vals[0]))
        if name == 'ASC': return float(ord(vals[0][0]))
        if name == 'TAB' or name == 'SPC': return " " * int(vals[0])
        if name == 'STR$':
            val = vals[0]
            if isinstance(val, float) and val.is_integer(): return str(int(val))
            return str(val)
        if name == 'LEFT$': return str(vals[0])[:int(vals[1])]
        if name == 'RIGHT$': return str(vals[0])[-int(vals[1]):]
        if name == 'MID$':
            s = str(vals[0])
            start = int(vals[1]) - 1 
            if start < 0: start = 0
            if len(vals) > 2:
                length = int(vals[2])
                return s[start : start+length]
            return s[start:]
        return 0

    def get_var(self, v):
        if v.subscripts:
            k = v.name
            if k not in self.arrs: self.arrs[k] = {} 
            return self.arrs[k].get(self.idx(v), 0 if not k.endswith('$') else "")
            
        if v.name == "TI": return float(int((time.time() - self.start_time) * 60))
        if v.name == "TI$": return time.strftime("%H%M%S", time.localtime())
        if v.name == "DATE$": return time.strftime("%Y-%m-%d", time.localtime())
        if v.name == "DATETIME$": return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            
        return self.vars.get(v.name, 0 if not v.name.endswith('$') else "")

    def set_var(self, v, val):
        if v.subscripts: 
            if v.name not in self.arrs: self.arrs[v.name] = {} 
            self.arrs[v.name][self.idx(v)] = val
        else: self.vars[v.name] = val

    def idx(self, v):
        subs = [int(self.eval(s)) for s in v.subscripts]
        return tuple(subs) 

    def _check_break(self):
        if self.check_break_fn and self.check_break_fn(): raise BasicError("BREAK")

    def process_input(self, targets, raw_text):
        if not targets: return
        parts = [p.strip() for p in raw_text.split(',')]
        for i, target in enumerate(targets):
            if i < len(parts):
                val = parts[i]
                is_numeric = not target.name.endswith('$')
                if is_numeric:
                    try: val = float(val)
                    except ValueError:
                        print("?REDO FROM START")
                        val = 0.0
                self.set_var(target, val)

    def scan_data(self):
        """
        Pre-scan the program for DATA statements and build a flat data list.
        Also builds data_line_map so RESTORE N can jump to a specific line's data.
        Called once at the start of RUN and run_immediate before execution begins.
        """
        self.data = []
        self.data_ptr = 0
        self.data_line_map = {} 
        for ln in sorted(self.prog.keys()):
            start_idx = len(self.data)
            added = False
            for s in self.prog[ln]:
                if isinstance(s, DataStmt):
                    self.data.extend(s.values)
                    added = True
            if added:
                self.data_line_map[ln] = start_idx

    def format_output(self, val):
        if isinstance(val, float) and val.is_integer(): return str(int(val))
        return str(val)

    def run_stmt(self, s):
        if isinstance(s, PrintStmt):
            out = ""
            last_sep = None
            for e, sep in s.items:
                out += self.format_output(self.eval(e))
                if sep == ',': out += "\t"
                last_sep = sep
            if not s.items or (last_sep != ';' and last_sep != ','): out += "\n"
            self.print_handler(out)
            
        elif isinstance(s, InputStmt):
            if s.prompt: prompt_str = str(self.eval(s.prompt))
            else: prompt_str = "? "
            try:
                val = self.input_handler(prompt_str)
                if val is None: val = ""
                val = str(val).strip() 
            except EOFError: val = ""
            if s.targets: self.process_input(s.targets, val)
            
        elif isinstance(s, GetStmt):
            for t in s.targets:
                raw_val = self.peek(MMU.ADDR_LAST_KEY)
                if raw_val == 0:
                    k = kb.get_char()
                    if k:
                        raw_val = ord(k)
                        self.poke(MMU.ADDR_LAST_KEY, raw_val)
                if raw_val > 0:
                    char = chr(int(raw_val))
                    if t.name.endswith('$'): self.set_var(t, char)
                    else:
                        try:
                            val = float(char)
                            self.set_var(t, val)
                        except ValueError: self.set_var(t, 0.0)
                    self.poke(MMU.ADDR_LAST_KEY, 0)
                # else: buffer empty — leave variable unchanged (C64 GET behaviour)

        elif isinstance(s, GotoStmt): 
            self.goto(s.line)
            return "JUMP"
        elif isinstance(s, GosubStmt): 
            if len(self.stack) >= self.MAX_STACK_DEPTH: raise BasicError("?OUT OF MEMORY ERROR")
            self.stack.append((self.line, self.stmt_idx + 1))
            self.goto(s.line)
            return "JUMP"
        elif isinstance(s, ReturnStmt): 
            if not self.stack: raise BasicError("?RETURN WITHOUT GOSUB ERROR")
            l, i = self.stack.pop()
            self.line = l
            self.stmt_idx = i
            return "JUMP"
        elif isinstance(s, IfStmt):
            if self.truthy(self.eval(s.condition)):
                if s.then_line: 
                    self.goto(s.then_line)
                    return "JUMP"
                if s.then_stmt: 
                    return self.run_stmt(s.then_stmt)
            else: return "SKIP_LINE"
        elif isinstance(s, ForStmt):
            if len(self.for_stack) >= self.MAX_STACK_DEPTH: raise BasicError("?OUT OF MEMORY ERROR")
            self.set_var(s.var, self.eval(s.start))
            self.for_stack.append({
                'v':s.var, 'to':self.eval(s.end), 
                'step':self.eval(s.step) if s.step else 1, 
                'l':self.line, 'i':self.stmt_idx + 1
            })
        elif isinstance(s, NextStmt):
            if not self.for_stack: raise BasicError("NEXT without FOR")
            ctx = self.for_stack[-1]
            val = self.get_var(ctx['v']) + ctx['step']
            self.set_var(ctx['v'], val)
            loop_continue = False
            if ctx['step'] > 0 and val <= ctx['to']: loop_continue = True
            elif ctx['step'] < 0 and val >= ctx['to']: loop_continue = True
            if loop_continue:
                self.line = ctx['l']
                self.stmt_idx = ctx['i']
                return "JUMP"
            self.for_stack.pop()
        
        elif isinstance(s, PokeStmt): self.poke(self.eval(s.addr), self.eval(s.value))
        
        # ---  UNIFIED DLOAD EXECUTION ---
        elif isinstance(s, DLoadStmt): 
            fname = str(self.eval(s.filename)).strip('"').upper()
            addr = None
            if s.addr is not None:
                addr = int(self.eval(s.addr))

            if not fname.endswith(".BAS") and not fname.endswith(".BIN"):
                fname += ".BIN" if addr is not None else ".BAS"

            content = None

            # 1. Try Virtual Disk (JSON Image)
            if self.disk_mount_check():
                content = self.disk_read(fname)

            # 2. Try Mounted Folder (Host OS)
            if content is None:
                if os.path.exists(fname):
                    with open(fname, 'rb') as f:
                        content = f.read()
                else:
                    # Case-insensitive fallback for host files
                    for host_f in os.listdir('.'):
                        if host_f.upper() == fname:
                            with open(host_f, 'rb') as f:
                                content = f.read()
                            break

            if content is None:
                raise BasicError(f"?FILE NOT FOUND: {fname}")

            # Execute the Load
            if addr is not None:
                try:
                    if isinstance(content, str):
                        if content.startswith('['): byte_data = bytes(json.loads(content))
                        else: byte_data = content.encode('latin-1')
                    else:
                        byte_data = bytes(content)

                    end_addr = addr + len(byte_data)
                    if end_addr <= self.mmu.MEMORY_SIZE:
                        # FAST BLOCK COPY
                        self.mmu.physical_ram[addr:end_addr] = byte_data
                    else:
                        raise BasicError("?OUT OF MEMORY ERROR (FILE TOO LARGE)")
                except Exception as e:
                    raise BasicError(f"?MEMORY LOAD ERROR: {e}")
            else:
                self.reset()
                if isinstance(content, bytes): content = content.decode('latin-1')
                for line_d in content.splitlines():
                    if not line_d.strip(): continue
                    parts_l = line_d.strip().split(maxsplit=1)
                    if parts_l and parts_l[0].isdigit():
                        self.load(parts_l[1] if len(parts_l) > 1 else "", int(parts_l[0]))

        elif isinstance(s, PauseStmt):
            dur = float(self.eval(s.duration))
            end = time.time() + dur
            while time.time() < end:
                self._check_break()
                time.sleep(0.001)
        elif isinstance(s, DimStmt):
            for n, args in s.dims: 
                [self.eval(a) for a in args]
                self.arrs[n] = {} 
        elif isinstance(s, ClsStmt): self.poke(49153, 1) 
        elif isinstance(s, ClearStmt): self.clear_runtime() 
        elif isinstance(s, LetStmt): self.set_var(s.target, self.eval(s.value))
        elif isinstance(s, MatStmt): pass
        elif isinstance(s, IgnoreStmt): pass 
        elif isinstance(s, DefStmt): self.defs[s.name] = s
        elif isinstance(s, DataStmt): pass 
        elif isinstance(s, RestoreStmt): 
            if s.line is None:
                self.data_ptr = 0
            else:
                found = False
                for ln in sorted(self.data_line_map.keys()):
                    if ln >= s.line:
                        self.data_ptr = self.data_line_map[ln]
                        found = True
                        break
                if not found:
                    self.data_ptr = len(self.data) 
        elif isinstance(s, ReadStmt):
            for t in s.targets:
                if self.data_ptr >= len(self.data): raise BasicError("?OUT OF DATA ERROR")
                raw = self.data[self.data_ptr]
                self.data_ptr += 1
                if not t.name.endswith('$'):
                    try: raw = float(raw)
                    except: raw = 0.0
                self.set_var(t, raw)
        elif isinstance(s, OnGotoStmt):
            idx = int(self.eval(s.expr))
            if 1 <= idx <= len(s.targets):
                self.goto(s.targets[idx-1])
                return "JUMP"
        elif isinstance(s, ChdirStmt):
            path = str(self.eval(s.path))
            try:
                os.chdir(path)
                self.print_handler(f"DIR CHANGED: {os.getcwd().upper()}\n")
            except Exception as e: self.print_handler(f"?DIR ERROR: {e}\n")
        elif isinstance(s, MkdirStmt):
            path = str(self.eval(s.path))
            try:
                os.mkdir(path)
                self.print_handler("DIR CREATED.\n")
            except Exception as e: self.print_handler(f"?DIR ERROR: {e}\n")
        elif isinstance(s, EndStmt) or isinstance(s, StopStmt): self.running = False

    def goto(self, ln):
        if ln in self.prog: 
            self.line = ln
            self.stmt_idx = 0 
        else: raise BasicError(f"Line {ln} not found")

    def load(self, text, line_num):
        ts = Tokenizer(text).tokenize_all()
        stmts = split_stmts(ts)
        parsed = [Parser(s).parse_statement() for s in stmts]
        self.prog[line_num] = parsed

    def load_file(self, filename):
        if os.path.exists(filename): real_name = filename
        else:
            real_name = None
            try:
                for f in os.listdir('.'):
                    if f.lower() == filename.lower():
                        real_name = f
                        break
            except: pass
        if not real_name:
            print("FILE NOT FOUND.")
            return
        self.reset()
        try:
            with open(real_name, 'r') as f:
                current_ln = None
                current_text = ""
                for line in f:
                    clean_line = line.rstrip('\r\n') 
                    if not clean_line: continue
                    stripped = clean_line.lstrip()
                    parts = stripped.split(maxsplit=1)
                    if parts and parts[0].isdigit():
                        if current_ln is not None:
                            try: self.load(current_text, current_ln)
                            except Exception as e: raise BasicError(f"{e} IN LINE {current_ln}")
                        current_ln = int(parts[0])
                        current_text = parts[1] if len(parts) > 1 else ""
                    else:
                        if current_ln is not None: current_text += clean_line 
                if current_ln is not None:
                    try: self.load(current_text, current_ln)
                    except Exception as e: raise BasicError(f"{e} IN LINE {current_ln}")
            print(f"LOADED {real_name.upper()}")
        except Exception as e: print(f"LOAD ERROR: {e}")

    def run(self):
        """
        Execute the loaded program from its first line number.
        Clears runtime state first (variables, stacks) but keeps the program.
        The main execution loop:
          1. Get the statement list for self.line
          2. Execute stmts[self.stmt_idx]
          3. If result is JUMP (GOTO/GOSUB/RETURN changed self.line), restart loop
          4. If result is SKIP_LINE (IF condition was false), advance to next line
          5. Otherwise increment stmt_idx
        Runs until self.running is False (END/STOP) or lines are exhausted.
        """
        self.clear_runtime()
        lines = sorted(self.prog.keys())
        if not lines: return
        self.line = lines[0]; self.stmt_idx = 0; self.running = True
        self.scan_data()
        while self.running:
            stmts = self.prog.get(self.line)
            if not stmts or self.stmt_idx >= len(stmts):
                try: 
                    idx = lines.index(self.line) + 1
                    if idx >= len(lines): break
                    self.line = lines[idx]; self.stmt_idx = 0; continue
                except: break
            current_idx = self.stmt_idx
            try:
                self._check_break()
                res = self.run_stmt(stmts[current_idx])
            except KeyboardInterrupt: raise BasicError(f"BREAK IN LINE {self.line}")
            except BasicError as e:
                if "IN LINE" not in str(e): raise BasicError(f"{e} IN LINE {self.line}")
                raise e
            except Exception as e: raise BasicError(f"{e} IN LINE {self.line}")
            if res == "JUMP": pass
            elif res == "SKIP_LINE": self.stmt_idx = len(stmts)
            else: self.stmt_idx += 1

    def run_immediate(self, stmts_list):
        """
        Execute a line of BASIC typed directly at the prompt (no line number).
        Temporarily stores the parsed statements at line 0 and runs them.
        Can include GOTO/GOSUB which will jump into the stored program.
        The line 0 slot is restored or removed when execution finishes.
        """
        source = stmts_list[0] if isinstance(stmts_list, list) else stmts_list
        try:
            tokens = Tokenizer(source).tokenize_all()
            stmts_lists = split_stmts(tokens)
        except BasicError as e:
            print(f"?SYNTAX ERROR: {e}")
            raise e 
        parsed_stmts = []
        try:
            for stmts in stmts_lists:
                parsed_stmts.append(Parser(stmts).parse_statement())
        except BasicError as e:
             print(f"?SYNTAX ERROR: {e}")
             raise e 
             return
        if 0 in self.prog: temp_backup = self.prog[0]
        else: temp_backup = None
        try:
            self.prog[0] = parsed_stmts
            self.line = 0; self.stmt_idx = 0; self.running = True
            self.scan_data()
            while self.running:
                if self.line == 0 and self.stmt_idx >= len(self.prog[0]): break
                cur_stmts = self.prog.get(self.line)
                if not cur_stmts or self.stmt_idx >= len(cur_stmts): break
                try:
                    self._check_break()
                    res = self.run_stmt(cur_stmts[self.stmt_idx])
                except KeyboardInterrupt: raise BasicError("BREAK")
                except BasicError as e:
                    if self.line > 0 and "IN LINE" not in str(e): raise BasicError(f"{e} IN LINE {self.line}")
                    raise e
                except Exception as e:
                    if self.line > 0: raise BasicError(f"{e} IN LINE {self.line}")
                    raise BasicError(str(e))
                if res == "JUMP": pass
                elif res == "SKIP_LINE": self.stmt_idx = len(cur_stmts)
                else: self.stmt_idx += 1
        finally:
            if temp_backup: self.prog[0] = temp_backup
            elif 0 in self.prog: del self.prog[0]

    def reconstruct(self, stmts):
        """
        Convert a list of AST statement nodes back to BASIC source text.
        Used by LIST command and SAVE to regenerate readable program lines.
        Multiple statements are joined with ':' separators.
        """
        out = []
        for s in stmts:
            if isinstance(s, PrintStmt):
                p = ["PRINT"]
                for e, sep in s.items: p.append(self.expr_str(e) + sep)
                out.append(" ".join(p))
            elif isinstance(s, GotoStmt): out.append(f"GOTO {s.line}")
            elif isinstance(s, GosubStmt): out.append(f"GOSUB {s.line}")
            elif isinstance(s, ReturnStmt): out.append("RETURN")
            elif isinstance(s, ClsStmt): out.append("CLS")
            elif isinstance(s, ClearStmt): out.append("CLEAR")
            elif isinstance(s, EndStmt): out.append("END")
            elif isinstance(s, StopStmt): out.append("STOP")
            elif isinstance(s, PauseStmt): out.append(f"PAUSE {self.expr_str(s.duration)}")
            
            elif isinstance(s, DLoadStmt):
                addr_str = f",{self.expr_str(s.addr)}" if s.addr else ""
                out.append(f"DLOAD {self.expr_str(s.filename)}{addr_str}")
            
            elif isinstance(s, IfStmt):
                c = self.expr_str(s.condition)
                if s.then_line: out.append(f"IF {c} THEN {s.then_line}")
                else: out.append(f"IF {c} THEN " + self.reconstruct([s.then_stmt]))
            elif isinstance(s, ForStmt):
                st = f" STEP {self.expr_str(s.step)}" if s.step else ""
                out.append(f"FOR {s.var.name}={self.expr_str(s.start)} TO {self.expr_str(s.end)}{st}")
            elif isinstance(s, NextStmt): out.append(f"NEXT {s.var if s.var else ''}")
            elif isinstance(s, LetStmt): out.append(f"{self.expr_str(s.target)}={self.expr_str(s.value)}")
            elif isinstance(s, PokeStmt): out.append(f"POKE {self.expr_str(s.addr)},{self.expr_str(s.value)}")
            elif isinstance(s, RemStmt): out.append(f"REM {s.comment}")
            elif isinstance(s, GetStmt):
                vars = [self.expr_str(v) for v in s.targets]
                out.append(f"GET {','.join(vars)}")
            elif isinstance(s, DimStmt):
                d_strs = []
                for name, exprs in s.dims:
                    arg_strs = [self.expr_str(a) for a in exprs]
                    d_strs.append(f"{name}({','.join(arg_strs)})")
                out.append(f"DIM {','.join(d_strs)}")
            elif isinstance(s, InputStmt):
                parts = ["INPUT"]
                if s.prompt: parts.append(f'{self.expr_str(s.prompt)};')
                if s.targets:
                    t_strs = [self.expr_str(t) for t in s.targets]
                    parts.append(",".join(t_strs))
                out.append(" ".join(parts).replace("; ", ";"))
            elif isinstance(s, RestoreStmt):
                if s.line is not None: out.append(f"RESTORE {s.line}")
                else: out.append("RESTORE")
            elif isinstance(s, DataStmt): out.append(f"DATA {','.join(map(str, s.values))}")
            elif isinstance(s, ReadStmt):
                t_strs = [self.expr_str(t) for t in s.targets]
                out.append(f"READ {','.join(t_strs)}")
            elif isinstance(s, OnGotoStmt):
                t_strs = [str(l) for l in s.targets]
                out.append(f"ON {self.expr_str(s.expr)} GOTO {','.join(t_strs)}")
            elif isinstance(s, DefStmt):
                out.append(f"DEF {s.name}({s.arg})={self.expr_str(s.expr)}")
            elif isinstance(s, MatStmt): out.append("MAT")
            elif isinstance(s, ChdirStmt): out.append(f"CHDIR {self.expr_str(s.path)}")
            elif isinstance(s, MkdirStmt): out.append(f"MKDIR {self.expr_str(s.path)}")
            elif isinstance(s, IgnoreStmt): out.append(f"{s.cmd} ...") 
            else: out.append("REM (unknown)")
        return ":".join(out)

    def expr_str(self, e):
        if isinstance(e, Num):
            if e.value == int(e.value): return str(int(e.value))
            return str(e.value)
        if isinstance(e, Str): return f'"{e.value}"'
        if isinstance(e, Var): 
            if e.subscripts:
                args = [self.expr_str(a) for a in e.subscripts]
                return f"{e.name}({','.join(args)})"
            return e.name
        if isinstance(e, BinOp):
            l = self.expr_str(e.left)
            r = self.expr_str(e.right)
            return f"{l} {e.op} {r}"
        if isinstance(e, UnaryOp):
            val = self.expr_str(e.operand)
            return f"{e.op} {val}"
        if isinstance(e, FuncCall):
             args = [self.expr_str(a) for a in e.args]
             return f"{e.name}({','.join(args)})"
        return "?"

    def list_prog(self, start=None, end=None):
        for l in sorted(self.prog.keys()):
            if start is not None and l < start: continue
            if end is not None and l > end: continue
            print(f"{l} {self.reconstruct(self.prog[l])}")

    def save_file(self, filename):
        try:
            with open(filename, 'w') as f:
                for ln in sorted(self.prog.keys()):
                    f.write(f"{ln} {self.reconstruct(self.prog[ln])}\n")
            print(f"SAVED TO {filename}")
        except Exception as e: print(f"SAVE ERROR: {e}")

    def catalog(self):
        print("FILES:")
        try:
            items = os.listdir('.')
            dirs = []
            files = []
            for i in items:
                if os.path.isdir(i):
                    if not i.startswith('.'): dirs.append(i)
                elif i.lower().endswith('.bas'):
                    files.append(i)
            if not dirs and not files:
                print("  (EMPTY DIRECTORY)")
            else:
                for d in sorted(dirs): print(f'  <DIR> {d.upper()}')
                for f in sorted(files): print(f'        "{f.upper()}"')
        except Exception as e: print(f"ERROR: {e}")

    def show_help(self):
        print("--- STEVE'S 8-BIT BASIC HELP ---")
        print("COMMANDS: RUN, LIST, NEW, CLEAR, CLS, SAVE \"FILE\", LOAD \"FILE\", DIR, EXIT")
        print("FILESYS:  CHDIR \"PATH\", MKDIR \"PATH\"")
        print("KEYWORDS: PRINT, INPUT, LET, IF/THEN, FOR/NEXT/STEP, GOTO, GOSUB/RETURN,")
        print("          DIM, REM, STOP, END, POKE, PAUSE <SEC>, GET <VAR>, DLOAD") 
        print("DATA:     READ, DATA, RESTORE, RESET")
        print("MATH:     SIN, COS, TAN, ATN, EXP, LOG, SQR, ABS, SGN, INT, RND")
        print("STRINGS:  LEN, ASC, CHR$, VAL, STR$, LEFT$, RIGHT$, MID$")
        print("SYSTEM:   PEEK(ADDR)")

if __name__ == "__main__":
    interp = BASICInterpreter()
    print(f"STEVE'S 8-BIT BASIC (STANDALONE)")
    if interp.mmu.fallback_active: print("* WARNING: USING INTERNAL FALLBACK FONT *")
    print("TYPE 'EXIT' TO QUIT.")
    while True:
        try:
            try:
                line = input("] ")
            except KeyboardInterrupt:
                print("\nBREAK")
                continue
            
            if not line.strip(): continue
            upper_line = line.strip().upper()
            if upper_line == "EXIT": break
            if upper_line == "NEW": interp.reset(); continue
            if upper_line == "CLS": print("\033c", end=""); continue
            if upper_line == "CLEAR": interp.clear_runtime(); continue
            if upper_line == "DIR": interp.catalog(); continue
            if upper_line == "HELP": interp.show_help(); continue
            if upper_line.startswith("RUN"):
                interp.run()
                print("")
                continue
            if upper_line.startswith("LIST"):
                parts = upper_line.split()
                s, e = None, None
                if len(parts) > 1: s = int(parts[1])
                if len(parts) > 2: e = int(parts[2])
                interp.list_prog(s, e)
                continue
            if upper_line.startswith("SAVE"):
                raw = line.strip()[4:].strip()
                if raw.startswith('"') and raw.endswith('"'): raw = raw[1:-1]
                interp.save_file(raw)
                continue
            if upper_line.startswith("LOAD"):
                raw = line.strip()[4:].strip()
                if raw.startswith('"') and raw.endswith('"'): raw = raw[1:-1]
                interp.load_file(raw)
                continue
            parts = line.split(maxsplit=1)
            if parts and parts[0].isdigit():
                ln = int(parts[0])
                interp.load(parts[1] if len(parts)>1 else "", ln)
            else:
                interp.run_immediate([line])
        except Exception as e:
            print(f"?ERROR: {e}")