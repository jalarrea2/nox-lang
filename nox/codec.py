"""
NOX meta-codec: IR <-> bytecode <-> opaque artifact.

How the opaque artifact is encoded:

  1. IR -> compact bytecode (header + serialized instructions).
  2. bytecode + HMAC tag -> XOR keystream(seed)  -> ciphertext (SHA-256 CTR).
  3. byte -> seed-permuted Braille glyph          -> opaque text (U+2800..U+28FF).

The byte->glyph dictionary, the keystream and the HMAC key are all DERIVED
from the seed: different seeds produce different alphabets, ciphers, and tags.
Reusing a seed reuses the permutation and keystream, exposing equality patterns.
Everything is resolved at compile time; the final binary carries none of it.

Integrity: a wrong seed or a tampered payload fails the HMAC check before
bytecode parsing. Authentication does not replace structural IR validation.
"""
import hashlib
import hmac
import struct
from random import Random

from .isa import (
    BY_BYTE,
    EXTENDED_OPCODES,
    MEMSIZE_DEFAULT,
    NREGS_DEFAULT,
    OPCODES,
    V5_OPCODES,
    validate_ir,
    validate_sizes,
)

MAGIC = b"NOX1"
VERSION = 5           # maximum format; legacy and function/string IR retain v3/v4
TAG_LEN = 16          # truncated HMAC-SHA256 tag
BRAILLE0 = 0x2800     # the Braille block has EXACTLY 256 code points
BRAILLE_LAST = BRAILLE0 + 0xFF
_HEADERS = {2: struct.Struct("<BBII")}
_HEADERS.update({version: struct.Struct("<BHII") for version in (3, 4, 5)})
_OPERANDS = {"R": struct.Struct("<B"), "I": struct.Struct("<q"), "T": struct.Struct("<I")}
_STRING_LENGTH = struct.Struct("<I")
_REGISTER_COUNT = struct.Struct("<H")


class NoxIntegrityError(ValueError):
    """Raised when an artifact fails to decode: wrong seed or tampering."""


# --------------------------------------------------------------------------
# IR <-> bytecode
# --------------------------------------------------------------------------
def serialize(ir, nregs=NREGS_DEFAULT, memsize=MEMSIZE_DEFAULT) -> bytes:
    validate_ir(ir, nregs, memsize)
    body = bytearray()
    for ins in ir:
        name, operands = ins[0], ins[1:]
        opcode, sig = OPCODES[name]
        body.append(opcode)
        for kind, val in zip(sig, operands):
            if kind == "S":
                encoded = val.encode("utf-8")
                body += _STRING_LENGTH.pack(len(encoded)) + encoded
            elif kind == "A":
                body += _REGISTER_COUNT.pack(len(val)) + bytes(val)
            else:
                body += _OPERANDS[kind].pack(val)
    names = {ins[0] for ins in ir}
    version = 5 if names & V5_OPCODES else 4 if names & EXTENDED_OPCODES else 3
    header = MAGIC + _HEADERS[version].pack(version, nregs, memsize, len(ir))
    return bytes(header + body)


def deserialize(data: bytes):
    if data[:4] != MAGIC:
        raise NoxIntegrityError("invalid magic: corrupt artifact or wrong seed")
    if len(data) < 5:
        raise NoxIntegrityError("truncated artifact header")
    version = data[4]
    if version not in _HEADERS:
        raise NoxIntegrityError(f"unsupported artifact version {version}")
    header = _HEADERS[version]
    pos = len(MAGIC) + header.size
    if len(data) < pos:
        raise NoxIntegrityError("truncated artifact header")
    _, nregs, memsize, count = header.unpack_from(data, len(MAGIC))
    try:
        validate_sizes(nregs, memsize)
    except ValueError as e:
        raise NoxIntegrityError(str(e)) from None
    if count > len(data) - pos:
        raise NoxIntegrityError("truncated artifact body")

    ir = []
    for _ in range(count):
        if pos >= len(data):
            raise NoxIntegrityError("truncated artifact body")
        opcode = data[pos]
        pos += 1
        if opcode not in BY_BYTE:
            raise NoxIntegrityError(f"unknown opcode byte 0x{opcode:02X}")
        name, sig = BY_BYTE[opcode]
        required = 5 if name in V5_OPCODES else 4 if name in EXTENDED_OPCODES else 2
        if version < required:
            raise NoxIntegrityError(f"{name} requires artifact version {required}")
        operands = []
        for kind in sig:
            operand = (_STRING_LENGTH if kind == "S" else
                       _REGISTER_COUNT if kind == "A" else _OPERANDS[kind])
            if pos + operand.size > len(data):
                raise NoxIntegrityError(f"truncated {name} operand")
            value = operand.unpack_from(data, pos)[0]
            pos += operand.size
            if kind in ("S", "A"):
                if kind == "A" and value > 256:
                    raise NoxIntegrityError(f"{name} register count exceeds 256")
                if value > len(data) - pos:
                    raise NoxIntegrityError(f"truncated {name} operand")
                raw = data[pos:pos + value]
                pos += value
                if kind == "S":
                    try:
                        value = raw.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        raise NoxIntegrityError(f"invalid UTF-8 in {name} operand") from None
                else:
                    value = tuple(raw)
            operands.append(value)
        ir.append((name, *operands))
    if pos != len(data):
        raise NoxIntegrityError("trailing bytes after artifact body")
    try:
        validate_ir(ir, nregs, memsize)
    except ValueError as e:
        raise NoxIntegrityError(str(e)) from None
    return ir, nregs, memsize


# --------------------------------------------------------------------------
# Seed derivation
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


def _tag(key: bytes, payload: bytes) -> bytes:
    mac = hmac.new(key + b"mac", payload, hashlib.sha256).digest()
    return mac[:TAG_LEN]


# --------------------------------------------------------------------------
# bytecode <-> opaque artifact (Braille text)
# --------------------------------------------------------------------------
def encode(bytecode: bytes, seed: str) -> str:
    key = _key(seed)
    payload = bytecode + _tag(key, bytecode)      # append integrity tag
    ks = _keystream(key, len(payload))
    ct = bytes(b ^ k for b, k in zip(payload, ks))
    p, _ = _perm(key)
    return "".join(chr(BRAILLE0 + p[b]) for b in ct)


def decode(text: str, seed: str) -> bytes:
    """Decode Braille with optional ASCII space, tab, CR, and LF separators.

    All other characters are rejected. Locations use one-based Unicode
    character columns; CRLF is treated as a single line break.
    """
    glyphs = bytearray()
    line, column, previous = 1, 1, ""
    for ch in text:
        codepoint = ord(ch)
        if BRAILLE0 <= codepoint <= BRAILLE_LAST:
            glyphs.append(codepoint - BRAILLE0)
        elif ch not in " \t\r\n":
            raise NoxIntegrityError(
                f"line {line}, column {column}: invalid artifact character U+{codepoint:04X}"
            )
        if ch == "\r" or (ch == "\n" and previous != "\r"):
            line, column = line + 1, 1
        elif ch != "\n":
            column += 1
        previous = ch

    key = _key(seed)
    _, inv = _perm(key)
    ks = _keystream(key, len(glyphs))
    payload = bytes(inv[g] ^ k for g, k in zip(glyphs, ks))
    if len(payload) < TAG_LEN:
        raise NoxIntegrityError("artifact too short to contain an integrity tag")
    bytecode, tag = payload[:-TAG_LEN], payload[-TAG_LEN:]
    if not hmac.compare_digest(tag, _tag(key, bytecode)):
        raise NoxIntegrityError("integrity check failed: tampered artifact or wrong seed")
    return bytecode


# --------------------------------------------------------------------------
# high-level shortcuts
# --------------------------------------------------------------------------
def ir_to_artifact(ir, seed, nregs=NREGS_DEFAULT, memsize=MEMSIZE_DEFAULT) -> str:
    return encode(serialize(ir, nregs, memsize), seed)


def artifact_to_ir(text, seed):
    return deserialize(decode(text, seed))
