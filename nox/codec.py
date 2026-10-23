"""
Codec meta de NOX: IR <-> bytecode <-> artefacto opaco.

Lo que hace al código "incomprensible pero ejecutable":

  1. IR -> bytecode compacto (header + instrucciones serializadas).
  2. bytecode XOR keystream(seed)      -> cifrado (SHA-256 en modo CTR).
  3. byte -> glifo Braille permutado(seed)  -> texto opaco (U+2800..U+28FF).

El diccionario byte->glifo y el keystream se DERIVAN de la seed por programa:
dos programas con seeds distintas producen alfabetos y cifrados distintos, así
que el artefacto no se puede "aprender" leyéndolo. Todo se resuelve en tiempo
de compilación; el binario final no lleva nada de esto.

Sin la seed correcta, el header mágico no valida -> se detecta artefacto
corrupto o clave errónea.
"""
import hashlib
import struct
from random import Random

from .isa import OPCODES, BY_BYTE, NREGS_DEFAULT, MEMSIZE_DEFAULT

MAGIC = b"NOX1"
BRAILLE0 = 0x2800  # el bloque Braille tiene EXACTAMENTE 256 code points


# --------------------------------------------------------------------------
# IR <-> bytecode
# --------------------------------------------------------------------------
def serialize(ir, nregs=NREGS_DEFAULT, memsize=MEMSIZE_DEFAULT) -> bytes:
    body = bytearray()
    for ins in ir:
        name, operands = ins[0], ins[1:]
        opcode, sig = OPCODES[name]
        if len(operands) != len(sig):
            raise ValueError(f"{name}: operandos {operands} != firma {sig}")
        body.append(opcode)
        for kind, val in zip(sig, operands):
            if kind == "R":
                if not 0 <= val < nregs:
                    raise ValueError(f"{name}: registro r{val} fuera de rango")
                body.append(val & 0xFF)
            elif kind == "I":
                body += struct.pack("<q", val)
            elif kind == "T":
                body += struct.pack("<I", val)
    header = MAGIC + struct.pack("<BBII", 1, nregs, memsize, len(ir))
    return bytes(header + body)


def deserialize(data: bytes):
    if data[:4] != MAGIC:
        raise ValueError("artefacto corrupto o seed incorrecta (magic inválido)")
    version, nregs, memsize, count = struct.unpack("<BBII", data[4:14])
    pos, ir = 14, []
    for _ in range(count):
        opcode = data[pos]; pos += 1
        name, sig = BY_BYTE[opcode]
        operands = []
        for kind in sig:
            if kind == "R":
                operands.append(data[pos]); pos += 1
            elif kind == "I":
                operands.append(struct.unpack("<q", data[pos:pos + 8])[0]); pos += 8
            elif kind == "T":
                operands.append(struct.unpack("<I", data[pos:pos + 4])[0]); pos += 4
        ir.append((name, *operands))
    return ir, nregs, memsize


# --------------------------------------------------------------------------
# Derivación desde la seed
# --------------------------------------------------------------------------
def _key(seed: str) -> bytes:
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _keystream(key: bytes, n: int) -> bytes:
    out, ctr = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + b"cs" + struct.pack("<Q", ctr)).digest()
        ctr += 1
    return bytes(out[:n])


def _perm(key: bytes):
    rng = Random(int.from_bytes(hashlib.sha256(key + b"perm").digest(), "big"))
    p = list(range(256))
    rng.shuffle(p)
    inv = [0] * 256
    for i, v in enumerate(p):
        inv[v] = i
    return p, inv


# --------------------------------------------------------------------------
# bytecode <-> artefacto opaco (texto Braille)
# --------------------------------------------------------------------------
def encode(bytecode: bytes, seed: str) -> str:
    key = _key(seed)
    ks = _keystream(key, len(bytecode))
    ct = bytes(b ^ k for b, k in zip(bytecode, ks))
    p, _ = _perm(key)
    return "".join(chr(BRAILLE0 + p[b]) for b in ct)


def decode(text: str, seed: str) -> bytes:
    key = _key(seed)
    _, inv = _perm(key)
    glyphs = [ord(ch) - BRAILLE0 for ch in text if BRAILLE0 <= ord(ch) <= BRAILLE0 + 255]
    ks = _keystream(key, len(glyphs))
    return bytes(inv[g] ^ k for g, k in zip(glyphs, ks))


# --------------------------------------------------------------------------
# atajos de alto nivel
# --------------------------------------------------------------------------
def ir_to_artifact(ir, seed, nregs=NREGS_DEFAULT, memsize=MEMSIZE_DEFAULT) -> str:
    return encode(serialize(ir, nregs, memsize), seed)


def artifact_to_ir(text, seed):
    return deserialize(decode(text, seed))
