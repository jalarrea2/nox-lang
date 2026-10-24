"""
NOX meta-codec: IR <-> bytecode <-> opaque artifact.

What makes the code "unreadable yet executable":

  1. IR -> compact bytecode (header + serialized instructions).
  2. bytecode + HMAC tag -> XOR keystream(seed)  -> ciphertext (SHA-256 CTR).
  3. byte -> seed-permuted Braille glyph          -> opaque text (U+2800..U+28FF).

The byte->glyph dictionary, the keystream and the HMAC key are all DERIVED
from the seed, per program: two programs with different seeds produce different
alphabets, ciphers and tags, so the artifact cannot be "learned" by reading it.
Everything is resolved at compile time; the final binary carries none of it.

Integrity: a wrong seed or a tampered payload fails the HMAC check before
bytecode parsing. Authentication does not replace structural IR validation.
"""
import hashlib
import hmac
import struct
from random import Random

from .isa import BY_BYTE, MEMSIZE_DEFAULT, NREGS_DEFAULT, OPCODES, validate_ir, validate_sizes

MAGIC = b"NOX1"
VERSION = 3           # u16 register count; the decoder also accepts v2
TAG_LEN = 16          # truncated HMAC-SHA256 tag
BRAILLE0 = 0x2800     # the Braille block has EXACTLY 256 code points
BRAILLE_LAST = BRAILLE0 + 0xFF
_HEADERS = {2: struct.Struct("<BBII"), 3: struct.Struct("<BHII")}
_OPERANDS = {"R": struct.Struct("<B"), "I": struct.Struct("<q"), "T": struct.Struct("<I")}


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
            body += _OPERANDS[kind].pack(val)
    header = MAGIC + _HEADERS[VERSION].pack(VERSION, nregs, memsize, len(ir))
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
        operands = []
        for kind in sig:
            operand = _OPERANDS[kind]
            if pos + operand.size > len(data):
                raise NoxIntegrityError(f"truncated {name} operand")
            operands.append(operand.unpack_from(data, pos)[0])
            pos += operand.size
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
    key = _key(seed)
    _, inv = _perm(key)
    glyphs = [ord(ch) - BRAILLE0 for ch in text if BRAILLE0 <= ord(ch) <= BRAILLE_LAST]
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
