"""
NOX front-end: readable DSL -> IR.

The DSL is the ONLY thing a human writes; the encoder then makes it opaque.

Grammar (one instruction per line; '#' and ';' start a comment):

  label:                    # label (identifier + ':')
  rN = <int>                # IMM        (accepts -5, 0x1F, 'A' char)
  rN = rM                   # MOV
  rN = rA + rB              # ADD   (+ - * / %)
  rN = rA < rB              # LT    (<  >  ==)
  rN = rA & rB              # AND   (& | ^ << >>)
  rN = [rA]                 # LD    (mem[rA])
  [rA] = rB                 # ST
  goto label                # JMP
  if rN == 0 goto label     # JZ
  if rN != 0 goto label     # JNZ
  read rN                   # IN
  print rN                  # OUTN  (prints integer)
  putc rN                   # OUT   (prints byte)
  halt                      # HALT

IR = list of tuples (MNEMONIC, *operands) with labels already resolved to
absolute instruction indices.
"""
import re

from .isa import I64_MAX, I64_MIN

BIN = {"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
       "<": "LT", ">": "GT", "==": "EQ",
       "&": "AND", "|": "OR", "^": "XOR", "<<": "SHL", ">>": "SHR"}

_R = r"r(\d+)"
_LABEL = r"[A-Za-z_]\w*"


class NoxSyntaxError(SyntaxError):
    pass


def _imm(tok, lineno):
    t = tok.strip()
    if len(t) == 3 and t[0] == "'" and t[2] == "'":
        return ord(t[1])
    try:
        value = int(t, 0)  # supports 0x.., 0b.., decimal, negatives
    except ValueError:
        raise NoxSyntaxError(f"line {lineno}: invalid immediate {tok!r}") from None
    if not I64_MIN <= value <= I64_MAX:
        raise NoxSyntaxError(f"line {lineno}: immediate {tok!r} out of i64 range")
    return value


def _strip_comment(line):
    # A character literal consists of exactly three characters, including quotes.
    pos = 0
    while pos < len(line):
        if line[pos] == "'" and pos + 2 < len(line) and line[pos + 2] == "'":
            pos += 3
            continue
        if line[pos] in "#;":
            return line[:pos]
        pos += 1
    return line


def parse(src: str):
    raw = []
    for lineno, line in enumerate(src.splitlines(), 1):
        line = _strip_comment(line).strip()
        if line:
            raw.append((lineno, line))

    # Pass 1: map labels -> instruction index
    labels, idx = {}, 0
    insns = []  # (lineno, text) for real instructions only
    for lineno, line in raw:
        m = re.fullmatch(rf"({_LABEL}):", line)
        if m:
            name = m.group(1)
            if name in labels:
                raise NoxSyntaxError(f"line {lineno}: duplicate label {name!r}")
            labels[name] = idx
        else:
            insns.append((lineno, line))
            idx += 1

    def tgt(name, lineno):
        if name not in labels:
            raise NoxSyntaxError(f"line {lineno}: unknown label {name!r}")
        return labels[name]

    # Pass 2: emit IR
    return [_emit(line, lineno, tgt) for lineno, line in insns]


def _emit(line, lineno, tgt):
    # control flow / IO without '='
    m = re.fullmatch(rf"goto\s+({_LABEL})", line)
    if m:
        return ("JMP", tgt(m.group(1), lineno))

    m = re.fullmatch(rf"if\s+{_R}\s*(==|!=)\s*0\s+goto\s+({_LABEL})", line)
    if m:
        op = "JZ" if m.group(2) == "==" else "JNZ"
        return (op, int(m.group(1)), tgt(m.group(3), lineno))

    m = re.fullmatch(rf"read\s+{_R}", line)
    if m:
        return ("IN", int(m.group(1)))
    m = re.fullmatch(rf"print\s+{_R}", line)
    if m:
        return ("OUTN", int(m.group(1)))
    m = re.fullmatch(rf"putc\s+{_R}", line)
    if m:
        return ("OUT", int(m.group(1)))
    if line == "halt":
        return ("HALT",)

    # store: [rA] = rB
    m = re.fullmatch(rf"\[\s*{_R}\s*\]\s*=\s*{_R}", line)
    if m:
        return ("ST", int(m.group(1)), int(m.group(2)))

    # everything else is an assignment: rN = rhs
    m = re.fullmatch(rf"{_R}\s*=\s*(.+)", line)
    if not m:
        raise NoxSyntaxError(f"line {lineno}: unrecognized statement {line!r}")
    rd, rhs = int(m.group(1)), m.group(2).strip()

    # load: rN = [rA]
    m = re.fullmatch(rf"\[\s*{_R}\s*\]", rhs)
    if m:
        return ("LD", rd, int(m.group(1)))

    # binary: rA op rB
    m = re.fullmatch(rf"{_R}\s*(\+|-|\*|/|%|<<|>>|<|>|==|&|\||\^)\s*{_R}", rhs)
    if m:
        return (BIN[m.group(2)], rd, int(m.group(1)), int(m.group(3)))

    # mov: rN = rM
    m = re.fullmatch(_R, rhs)
    if m:
        return ("MOV", rd, int(m.group(1)))

    # imm
    return ("IMM", rd, _imm(rhs, lineno))
