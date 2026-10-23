"""
Front-end NOX: DSL legible -> IR.

El DSL es lo ÚNICO que un humano escribe. Luego el encoder lo vuelve opaco.

Gramática (una instrucción por línea; '#' y ';' inician comentario):

  etiqueta:                 # label (identificador + ':')
  rN = <int>                # IMM        (acepta -5, 0x1F, 'A' char)
  rN = rM                   # MOV
  rN = rA + rB              # ADD   (+ - * / %)
  rN = rA < rB              # LT    (<  >  ==)
  rN = rA & rB              # AND   (& | ^ << >>)
  rN = [rA]                 # LD    (mem[rA])
  [rA] = rB                 # ST
  goto etiqueta             # JMP
  if rN == 0 goto etiqueta  # JZ
  if rN != 0 goto etiqueta  # JNZ
  read rN                   # IN
  print rN                  # OUTN  (imprime entero)
  putc rN                   # OUT   (imprime byte)
  halt                      # HALT

IR = lista de tuplas (MNEMONICO, *operandos) con labels ya resueltas a
índice absoluto de instrucción.
"""
import re

BIN = {"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD",
       "<": "LT", ">": "GT", "==": "EQ",
       "&": "AND", "|": "OR", "^": "XOR", "<<": "SHL", ">>": "SHR"}

_R = r"r(\d+)"


class NoxSyntaxError(SyntaxError):
    pass


def _reg(tok, lineno):
    m = re.fullmatch(_R, tok.strip())
    if not m:
        raise NoxSyntaxError(f"línea {lineno}: registro inválido {tok!r}")
    return int(m.group(1))


def _imm(tok, lineno):
    t = tok.strip()
    if len(t) == 3 and t[0] == "'" and t[2] == "'":
        return ord(t[1])
    try:
        return int(t, 0)  # soporta 0x.., 0b.., decimal, negativos
    except ValueError:
        raise NoxSyntaxError(f"línea {lineno}: inmediato inválido {tok!r}")


def parse(src: str):
    raw = []
    for lineno, line in enumerate(src.splitlines(), 1):
        line = re.split(r"[#;]", line, 1)[0].strip()
        if line:
            raw.append((lineno, line))

    # Paso 1: ubicar labels -> índice de instrucción
    labels, idx = {}, 0
    insns = []  # (lineno, texto) sólo instrucciones reales
    for lineno, line in raw:
        m = re.fullmatch(r"([A-Za-z_]\w*):", line)
        if m:
            name = m.group(1)
            if name in labels:
                raise NoxSyntaxError(f"línea {lineno}: label duplicada {name!r}")
            labels[name] = idx
        else:
            insns.append((lineno, line))
            idx += 1

    def tgt(name, lineno):
        if name not in labels:
            raise NoxSyntaxError(f"línea {lineno}: label desconocida {name!r}")
        return labels[name]

    # Paso 2: emitir IR
    ir = []
    for lineno, line in insns:
        ir.append(_emit(line, lineno, tgt))
    return ir


def _emit(line, lineno, tgt):
    # control de flujo / IO sin '='
    m = re.fullmatch(r"goto\s+([A-Za-z_]\w*)", line)
    if m:
        return ("JMP", tgt(m.group(1), lineno))

    m = re.fullmatch(r"if\s+" + _R + r"\s*(==|!=)\s*0\s+goto\s+([A-Za-z_]\w*)", line)
    if m:
        op = "JZ" if m.group(2) == "==" else "JNZ"
        return (op, int(m.group(1)), tgt(m.group(3), lineno))

    m = re.fullmatch(r"read\s+" + _R, line)
    if m:
        return ("IN", int(m.group(1)))
    m = re.fullmatch(r"print\s+" + _R, line)
    if m:
        return ("OUTN", int(m.group(1)))
    m = re.fullmatch(r"putc\s+" + _R, line)
    if m:
        return ("OUT", int(m.group(1)))
    if line == "halt":
        return ("HALT",)

    # store: [rA] = rB
    m = re.fullmatch(r"\[\s*" + _R + r"\s*\]\s*=\s*" + _R, line)
    if m:
        return ("ST", int(m.group(1)), int(m.group(2)))

    # todo lo demás es asignación: rN = rhs
    m = re.fullmatch(r"" + _R + r"\s*=\s*(.+)", line)
    if not m:
        raise NoxSyntaxError(f"línea {lineno}: no se reconoce {line!r}")
    rd, rhs = int(m.group(1)), m.group(2).strip()

    # load: rN = [rA]
    m = re.fullmatch(r"\[\s*" + _R + r"\s*\]", rhs)
    if m:
        return ("LD", rd, int(m.group(1)))

    # binaria: rA op rB
    m = re.fullmatch(_R + r"\s*(\+|-|\*|/|%|<<|>>|<|>|==|&|\||\^)\s*" + _R, rhs)
    if m:
        return (BIN[m.group(2)], rd, int(m.group(1)), int(m.group(3)))

    # mov: rN = rM
    m = re.fullmatch(_R, rhs)
    if m:
        return ("MOV", rd, int(m.group(1)))

    # imm
    return ("IMM", rd, _imm(rhs, lineno))
