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

Integrity:
  * A wrong seed yields a different permutation/keystream, so the magic header
    fails to validate -> the artifact is rejected.
  * A correct seed but a tampered artifact fails the HMAC check -> rejected.
    This distinguishes a corrupted/edited artifact from a wrong key.
"""
import hashlib
import hmac
import struct
from random import Random

from .isa import OPCODES, BY_BYTE, NREGS_DEFAULT, NREGS_MAX, MEMSIZE_DEFAULT

MAGIC = b"NOX1"
VERSION = 2            # bumped when HMAC integrity was added
TAG_LEN = 16          # truncated HMAC-SHA256 tag
BRAILLE0 = 0x2800     # the Braille block has EXACTLY 256 code points
BRAILLE_LAST = BRAILLE0 + 0xFF


class NoxIntegrityError(ValueError):
    """Raised when an artifact fails to decode: wrong seed or tampering."""


# --------------------------------------------------------------------------
# IR <-> bytecode
# --------------------------------------------------------------------------
def serialize(ir, nregs=NREGS_DEFAULT, memsize=MEMSIZE_DEFAULT) -> bytes:
    if not 1 <= nregs <= NREGS_MAX:
        raise ValueError(f"nregs must be in 1..{NREGS_MAX}, got {nregs}")
    if not 1 <= memsize <= 0xFFFFFFFF:
        raise ValueError(f"memsize out of range: {memsize}")

    body = bytearray()
    for ins in ir:
        name, operands = ins[0], ins[1:]
        if name not in OPCODES:
            raise ValueError(f"unknown opcode: {name}")
        opcode, sig = OPCODES[name]
        if len(operands) != len(sig):
            raise ValueError(f"{name}: operands {operands} != signature {sig}")
        body.append(opcode)
        for kind, val in zip(sig, operands):
            if kind == "R":
                if not 0 <= val < nregs:
                    raise ValueError(f"{name}: register r{val} out of range (nregs={nregs})")
                body.append(val)
            elif kind == "I":
                body += struct.pack("<q", val)
            elif kind == "T":
                body += struct.pack("<I", val)
    header = MAGIC + struct.pack("<BBII", VERSION, nregs, memsize, len(ir))
    return bytes(header + body)


def deserialize(data: bytes):
    if data[:4] != MAGIC:
        raise NoxIntegrityError("invalid magic: corrupt artifact or wrong seed")
    if len(data) < 14:
        raise NoxIntegrityError("truncated artifact header")
    version, nregs, memsize, count = struct.unpack("<BBII", data[4:14])
    if version != VERSION:
        raise NoxIntegrityError(f"unsupported artifact version {version} (expected {VERSION})")

    pos, ir = 14, []
    for _ in range(count):
        if pos >= len(data):
            raise NoxIntegrityError("truncated artifact body")
        opcode = data[pos]; pos += 1
        if opcode not in BY_BYTE:
            raise NoxIntegrityError(f"unknown opcode byte 0x{opcode:02X}")
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
