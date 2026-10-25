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

from .isa import I64_MAX, I64_MIN, MEMSIZE_DEFAULT, NREGS_MAX, OPCODES, validate_sizes

BIN = {"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
       "<": "LT", ">": "GT", "==": "EQ",
       "&": "AND", "|": "OR", "^": "XOR", "<<": "SHL", ">>": "SHR"}

_R = r"r(\d+)"
_LABEL = r"[A-Za-z_]\w*"


class NoxSyntaxError(SyntaxError):
    """A DSL diagnostic with its original source location when available."""

    def __init__(self, message, lineno=None):
        super().__init__(message)
        self.lineno = lineno

    def __str__(self):
        if self.filename is not None:
            return f"{self.filename}: line {self.lineno}: {self.msg}"
        if self.lineno is not None:
            return f"line {self.lineno}: {self.msg}"
        return str(self.msg)


def _imm(tok, lineno):
    t = tok.strip()
    if len(t) == 3 and t[0] == "'" and t[2] == "'":
        return ord(t[1])
    try:
        value = int(t, 0)  # supports 0x.., 0b.., decimal, negatives
    except ValueError:
        raise NoxSyntaxError(f"invalid immediate {tok!r}", lineno) from None
    if not I64_MIN <= value <= I64_MAX:
        raise NoxSyntaxError(f"immediate {tok!r} out of i64 range", lineno)
    return value


def _reg(tok, lineno):
    """Normalize Unicode decimal digits before converting a bounded index."""
    digits = "".join(str(int(char)) for char in tok).lstrip("0")
    if len(digits) > len(str(NREGS_MAX - 1)):
        raise NoxSyntaxError(f"register index out of range (maximum r{NREGS_MAX - 1})", lineno)
    return int(digits or "0")


def _strip_comment(line):
    # Preserve comment markers inside both string and legacy character literals.
    pos = 0
    while pos < len(line):
        if line[pos] == "'" and pos + 2 < len(line) and line[pos + 2] == "'":
            pos += 3
            continue
        if line[pos] in "\"'":
            quote = line[pos]
            pos += 1
            while pos < len(line):
                if line[pos] == "\\":
                    pos += 2
                elif line[pos] == quote:
                    pos += 1
                    break
                else:
                    pos += 1
            continue
        if line[pos] in "#;":
            return line[:pos]
        pos += 1
    return line


def parse(src: str, nregs=NREGS_MAX, filename=None):
    """Parse source, retaining locations in diagnostics and returning tuple IR.

    The default accepts every register representable by the ISA. Callers that
    configure a smaller register file can supply ``nregs``; the CLI always does.
    ``filename`` is used only in errors and does not affect the returned IR.
    """
    validate_sizes(nregs, MEMSIZE_DEFAULT)
    lines = src.splitlines()
    try:
        try:
            return _parse(lines, nregs)
        except NoxSyntaxError:
            from .frontend import compile_source

            return compile_source(lines, nregs)
    except NoxSyntaxError as error:
        error.filename = filename
        if error.lineno is not None:
            error.text = lines[error.lineno - 1]
        raise


def _parse(lines, nregs):
    raw = []
    for lineno, line in enumerate(lines, 1):
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
                raise NoxSyntaxError(f"duplicate label {name!r}", lineno)
            labels[name] = idx
        else:
            insns.append((lineno, line))
            idx += 1

    def tgt(name, lineno):
        if name not in labels:
            raise NoxSyntaxError(f"unknown label {name!r}", lineno)
        return labels[name]

    # Pass 2: emit IR
    ir = []
    for lineno, line in insns:
        instruction = _emit(line, lineno, tgt)
        _, signature = OPCODES[instruction[0]]
        for kind, value in zip(signature, instruction[1:]):
            if kind == "R" and value >= nregs:
                raise NoxSyntaxError(
                    f"register r{value} out of range (expected r0..r{nregs - 1})", lineno,
                )
        ir.append(instruction)
    return ir


def _emit(line, lineno, tgt):
    # control flow / IO without '='
    m = re.fullmatch(rf"goto\s+({_LABEL})", line)
    if m:
        return ("JMP", tgt(m.group(1), lineno))

    m = re.fullmatch(rf"if\s+{_R}\s*(==|!=)\s*0\s+goto\s+({_LABEL})", line)
    if m:
        op = "JZ" if m.group(2) == "==" else "JNZ"
        return (op, _reg(m.group(1), lineno), tgt(m.group(3), lineno))

    m = re.fullmatch(rf"read\s+{_R}", line)
    if m:
        return ("IN", _reg(m.group(1), lineno))
    m = re.fullmatch(rf"print\s+{_R}", line)
    if m:
        return ("OUTN", _reg(m.group(1), lineno))
    m = re.fullmatch(rf"putc\s+{_R}", line)
    if m:
        return ("OUT", _reg(m.group(1), lineno))
    if line == "halt":
        return ("HALT",)

    # store: [rA] = rB
    m = re.fullmatch(rf"\[\s*{_R}\s*\]\s*=\s*{_R}", line)
    if m:
        return ("ST", _reg(m.group(1), lineno), _reg(m.group(2), lineno))

    # everything else is an assignment: rN = rhs
    m = re.fullmatch(rf"{_R}\s*=\s*(.+)", line)
    if not m:
        raise NoxSyntaxError(f"unrecognized statement {line!r}", lineno)
    rd, rhs = _reg(m.group(1), lineno), m.group(2).strip()

    # load: rN = [rA]
    m = re.fullmatch(rf"\[\s*{_R}\s*\]", rhs)
    if m:
        return ("LD", rd, _reg(m.group(1), lineno))

    # binary: rA op rB
    m = re.fullmatch(rf"{_R}\s*(\+|-|\*|/|%|<<|>>|<|>|==|&|\||\^)\s*{_R}", rhs)
    if m:
        return (BIN[m.group(2)], rd, _reg(m.group(1), lineno), _reg(m.group(3), lineno))

    # mov: rN = rM
    m = re.fullmatch(_R, rhs)
    if m:
        return ("MOV", rd, _reg(m.group(1), lineno))

    # imm
    return ("IMM", rd, _imm(rhs, lineno))
