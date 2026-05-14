# -*- coding: utf-8 -*-
"""
Created on Sat Jan  3 10:39:47 2026

@author: shuskey
"""

import sys
import unittest
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Dict
import random

# ==========================
# DIAGNOSTIC BASIC INTERPRETER
# ==========================

class BasicError(Exception):
    pass

# --- Token Definitions ---
TK_NUMBER = "NUMBER"
TK_STRING = "STRING"
TK_IDENT  = "IDENT"
TK_OP     = "OP"
TK_SEP    = "SEP"
TK_PAREN  = "PAREN"
TK_COLON  = "COLON"
TK_EOF    = "EOF"

@dataclass
class Token:
    kind: str
    value: Any
    pos: int

# --- AST Nodes ---
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

# --- Statements ---
@dataclass
class PrintStmt(Statement): items: List[Tuple[Expr, str]]
@dataclass
class InputStmt(Statement): prompt: Optional[Expr]; targets: List[Var]
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

# ==========================
# TOKENIZER (WITH SAFETY VALVE)
# ==========================
class Tokenizer:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.length = len(text)

    def peek(self):
        if self.pos < self.length:
            return self.text[self.pos]
        return ''

    def advance(self):
        ch = self.peek()
        self.pos += 1
        return ch

    def skip_ws(self):
        safety = 0
        while self.peek() != '' and self.peek() in ' \t\r':
            self.advance()
            safety += 1
            if safety > 10000:
                raise BasicError("Freeze detected in skip_ws")

    def tokenize_all(self):
        tokens = []
        safety_outer = 0
        
        while True:
            safety_outer += 1
            if safety_outer > 100000: raise BasicError("Freeze detected in tokenize_all main loop")
            
            self.skip_ws()
            ch = self.peek()
            
            if ch == '':
                tokens.append(Token(TK_EOF, None, self.pos))
                break
            
            # String
            if ch in '"\'': 
                start = self.pos
                q = self.advance()
                s = ""
                loop_safety = 0
                while True:
                    c = self.peek()
                    if c == '': raise BasicError("Unterminated string")
                    if c == q: self.advance(); break
                    s += self.advance()
                    loop_safety += 1
                    if loop_safety > 1000: raise BasicError("Freeze in String Parsing")
                tokens.append(Token(TK_STRING, s, start))
            
            # Number
            elif ch.isdigit() or (ch == '.' and (self.pos + 1 < self.length) and self.text[self.pos+1].isdigit()):
                start = self.pos
                s = ""
                loop_safety = 0
                while self.peek().isdigit() or self.peek() == '.':
                    s += self.advance()
                    loop_safety += 1
                    if loop_safety > 1000: raise BasicError("Freeze in Number Parsing")
                tokens.append(Token(TK_NUMBER, float(s), start))
            
            # Identifier (The suspected freeze location)
            elif ch.isalpha() or ch == '_':
                start = self.pos
                s = ""
                loop_safety = 0
                while True:
                    c = self.peek()
                    if c.isalnum() or c in '_$':
                        s += self.advance()
                    else:
                        break
                    loop_safety += 1
                    if loop_safety > 1000: 
                        # Debug info to see what triggered the loop
                        raise BasicError(f"Freeze in Identifier Parsing. Current String: '{s}' Next Char code: {ord(c)}")
                tokens.append(Token(TK_IDENT, s.upper(), start))
            
            # Symbols
            elif ch in ':(),;?':
                kind = TK_COLON if ch==':' else (TK_OP if ch=='?' else (TK_SEP if ch in ',;' else TK_PAREN))
                tokens.append(Token(kind, ch, self.pos))
                self.advance()
            
            # Operators
            else:
                op = ch
                two = self.text[self.pos:self.pos+2]
                if two in ['<=','>=','<>']:
                    op = two
                elif ch not in '+-*/^=<>' :
                    raise BasicError(f"Unexpected char '{ch}'")
                
                tokens.append(Token(TK_OP, op, self.pos))
                self.pos += len(op)
                
        return tokens

# ==========================
# PARSER
# ==========================
class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens): return self.tokens[self.pos]
        return Token(TK_EOF, None, -1)

    def advance(self):
        t = self.peek()
        self.pos += 1
        return t

    def match(self, k, v=None):
        if self.peek().kind == k and (v is None or self.peek().value == v):
            self.advance(); return True
        return False

    def expect(self, kind, value=None): 
        tok = self.peek()
        if tok.kind != kind or (value is not None and tok.value != value):
            raise BasicError(f"Expected {kind} {value if value else ''}")
        return self.advance()
    
    # Simple Expression Parser
    def parse_expr(self): return self.parse_op(['OR'], self.parse_and)
    def parse_and(self): return self.parse_op(['AND'], self.parse_cmp)
    def parse_cmp(self): return self.parse_op(['=','<>','<','>','<=','>='], self.parse_add)
    def parse_add(self): return self.parse_op(['+','-'], self.parse_mul)
    def parse_mul(self): return self.parse_op(['*','/'], self.parse_pow)
    def parse_pow(self): return self.parse_op(['^'], self.parse_unary)
    
    def parse_op(self, ops, next_fn):
        l = next_fn()
        while self.peek().kind in [TK_OP, TK_IDENT] and self.peek().value in ops:
            op = self.advance().value; l = BinOp(op, l, next_fn())
        return l

    def parse_unary(self):
        if self.match(TK_OP, '-') or self.match(TK_IDENT, 'NOT'):
            op = self.tokens[self.pos-1].value; return UnaryOp(op, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        t = self.peek()
        if self.match(TK_NUMBER): return Num(t.value)
        if self.match(TK_STRING): return Str(t.value)
        if self.match(TK_IDENT):
            name = self.tokens[self.pos-1].value
            if self.match(TK_PAREN, '('):
                args = []
                if not self.match(TK_PAREN, ')'):
                    while True:
                        args.append(self.parse_expr())
                        if not self.match(TK_SEP, ','): self.expect(TK_PAREN, ')'); break
                return Var(name, args) # Simplified for diagnostics
            return Var(name)
        if self.match(TK_PAREN, '('): e = self.parse_expr(); self.expect(TK_PAREN, ')'); return e
        raise BasicError(f"Unexpected token {t.value}")

    def parse_statement(self):
        t = self.peek()
        if t.kind == TK_IDENT or t.value == '?':
            kw = t.value
            if kw == '?' or kw == 'PRINT': self.advance(); return self.parse_print()
            if kw == 'LET': self.advance(); return self.parse_let()
            if kw == 'CLS' or kw == 'CLEAR': self.advance(); return ClsStmt()
            if kw == 'FOR': self.advance(); return self.parse_for()
            if kw == 'NEXT': self.advance(); v=None; 
            if self.peek().kind==TK_IDENT: v=self.advance().value; 
            return NextStmt(v)
            if kw == 'GOTO': self.advance(); return GotoStmt(int(self.expect(TK_NUMBER).value))
            # Implicit Let
            return self.parse_let()
        raise BasicError("Unknown Statement")

    def parse_print(self):
        items = []
        if self.peek().kind in [TK_EOF, TK_COLON]: return PrintStmt([])
        while True:
            if self.match(TK_SEP, ';') or self.match(TK_SEP, ','): items.append((Str(""), self.tokens[self.pos-1].value))
            else:
                e = self.parse_expr(); sep = ""
                if self.match(TK_SEP, ';') or self.match(TK_SEP, ','): sep = self.tokens[self.pos-1].value
                items.append((e, sep))
            if self.peek().kind in [TK_EOF, TK_COLON]: break
        return PrintStmt(items)

    def parse_let(self):
        t = self.parse_primary(); self.expect(TK_OP, '='); v = self.parse_expr(); return LetStmt(t, v)

    def parse_for(self):
        v = self.parse_primary(); self.expect(TK_OP, '='); s = self.parse_expr(); self.expect(TK_IDENT, 'TO'); e = self.parse_expr(); step = None
        if self.match(TK_IDENT, 'STEP'): step = self.parse_expr()
        return ForStmt(v, s, e, step)

def split_stmts(tokens):
    parts = []; curr = []
    for t in tokens:
        if t.kind == TK_COLON: parts.append(curr); curr = []
        elif t.kind == TK_EOF: 
            if curr: parts.append(curr)
            curr = []
        else: curr.append(t)
    if curr: parts.append(curr)
    return [p + [Token(TK_EOF,None,0)] for p in parts if p]

# ==========================
# INTERPRETER CORE
# ==========================
class BASICInterpreter:
    def __init__(self):
        self.vars = {}
        self.prog = {}
        self.running = False
        self.stmt_idx = 0
        self.line = 0

    def reset(self):
        self.vars = {}
        self.prog = {}

    def eval(self, e):
        if isinstance(e, Num): return e.value
        if isinstance(e, Str): return e.value
        if isinstance(e, Var): return self.vars.get(e.name, 0)
        if isinstance(e, BinOp):
            l = self.eval(e.left); r = self.eval(e.right)
            if e.op == '+': return l+r
            if e.op == '*': return l*r
        return 0

    def run_stmt(self, s):
        if isinstance(s, PrintStmt):
            out = []
            for e, sep in s.items: out.append(str(self.eval(e)))
            print("".join(out))
        elif isinstance(s, LetStmt): self.vars[s.target.name] = self.eval(s.value)
        elif isinstance(s, ClsStmt): print("[CLS CMD EXECUTED]")
        elif isinstance(s, GotoStmt): pass # stub for test

    def load(self, text, ln):
        ts = Tokenizer(text).tokenize_all()
        stmts = split_stmts(ts)
        self.prog[ln] = [Parser(s).parse_statement() for s in stmts][0]

# ==========================
# UNIT TESTS
# ==========================
class TestSystem(unittest.TestCase):
    def setUp(self):
        self.interp = BASICInterpreter()

    def test_01_tokenizer_simple(self):
        print("\nTesting Tokenizer Simple...")
        t = Tokenizer('PRINT "HELLO"').tokenize_all()
        self.assertEqual(t[0].kind, TK_IDENT)
        self.assertEqual(t[1].kind, TK_STRING)

    def test_02_tokenizer_loop_safety(self):
        print("Testing Tokenizer Safety...")
        # This string caused freezes before
        t = Tokenizer('CLEAR').tokenize_all()
        self.assertEqual(t[0].kind, TK_IDENT)
        self.assertEqual(t[0].value, "CLEAR")

    def test_03_parser_let(self):
        print("Testing Parser LET...")
        self.interp.vars['A'] = 0
        ts = Tokenizer('LET A = 100').tokenize_all()
        stmt = Parser(split_stmts(ts)[0]).parse_statement()
        self.interp.run_stmt(stmt)
        self.assertEqual(self.interp.vars['A'], 100)

    def test_04_parser_cls(self):
        print("Testing Parser CLS...")
        # If this freezes, the safety valve will catch it
        ts = Tokenizer('CLS').tokenize_all()
        stmt = Parser(split_stmts(ts)[0]).parse_statement()
        self.assertIsInstance(stmt, ClsStmt)

    def test_05_eof_safety(self):
        print("Testing EOF Safety...")
        # Ensure trailing spaces don't freeze
        t = Tokenizer('PRINT A   ').tokenize_all()
        self.assertEqual(len(t), 3) # PRINT, A, EOF

if __name__ == '__main__':
    unittest.main()