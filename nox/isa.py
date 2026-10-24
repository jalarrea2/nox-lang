"""
NOX ISA — register machine, i64, linear memory, I/O.
Turing-complete: arithmetic + addressable memory + conditional jumps.

Arithmetic wraps modulo 2^64. Shifts mask counts to six bits; SHR sign-extends.
DIV/MOD return 0 for a zero divisor; MIN/-1 wraps (remainder 0).
LD/ST trap with exit code 4 when an address is outside the declared memory.

Each instruction = (MNEMONIC, *operands). The signatures define how operands
are serialized into bytecode and how they are lowered to C.

Operand kinds:
  R  -> register index           (1 byte)
  I  -> i64 immediate            (8 bytes LE, signed)
  T  -> target: instruction index (4 bytes LE, unsigned)
"""

# name -> (opcode_byte, signature)
OPCODES = {
    "IMM":  (0x01, ("R", "I")),   # rd = imm
    "MOV":  (0x02, ("R", "R")),   # rd = rs
    "ADD":  (0x10, ("R", "R", "R")),
    "SUB":  (0x11, ("R", "R", "R")),
    "MUL":  (0x12, ("R", "R", "R")),
    "DIV":  (0x13, ("R", "R", "R")),   # integer div, /0 -> 0
    "MOD":  (0x14, ("R", "R", "R")),   # %0 -> 0
    "LT":   (0x20, ("R", "R", "R")),   # rd = (ra <  rb) ? 1 : 0
    "EQ":   (0x21, ("R", "R", "R")),   # rd = (ra == rb) ? 1 : 0
    "GT":   (0x22, ("R", "R", "R")),   # rd = (ra >  rb) ? 1 : 0
    "AND":  (0x30, ("R", "R", "R")),   # bitwise
    "OR":   (0x31, ("R", "R", "R")),
    "XOR":  (0x32, ("R", "R", "R")),
    "SHL":  (0x33, ("R", "R", "R")),
    "SHR":  (0x34, ("R", "R", "R")),
    "LD":   (0x40, ("R", "R")),        # rd = mem[ra]
    "ST":   (0x41, ("R", "R")),        # mem[ra] = rb
    "JMP":  (0x50, ("T",)),
    "JZ":   (0x51, ("R", "T")),        # if rs == 0 goto T
    "JNZ":  (0x52, ("R", "T")),        # if rs != 0 goto T
    "IN":   (0x60, ("R",)),            # rd = getchar() (-1 on EOF)
    "OUT":  (0x61, ("R",)),            # putchar(rs & 0xFF)
    "OUTN": (0x62, ("R",)),            # printf("%lld", rs)
    "HALT": (0x70, ()),
}

# opcode_byte -> (name, signature)   (inverse, used when disassembling)
BY_BYTE = {b: (name, sig) for name, (b, sig) in OPCODES.items()}

NREGS_DEFAULT = 32
NREGS_MAX = 256            # register index is a single byte
MEMSIZE_DEFAULT = 1 << 16  # 65536 i64 cells
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1


def validate_sizes(nregs, memsize):
    """Validate machine dimensions before serialization or C generation."""
    if type(nregs) is not int or not 1 <= nregs <= NREGS_MAX:
        raise ValueError(f"nregs must be in 1..{NREGS_MAX}, got {nregs!r}")
    if type(memsize) is not int or not 1 <= memsize <= 0xFFFFFFFF:
        raise ValueError(f"memsize out of range: {memsize!r}")


def validate_ir(ir, nregs, memsize):
    """Check instruction structure and operand bounds at every IR boundary."""
    validate_sizes(nregs, memsize)
    if not isinstance(ir, (list, tuple)) or len(ir) > 0xFFFFFFFF:
        raise ValueError("IR must be a sequence of at most 4294967295 instructions")
    for index, ins in enumerate(ir):
        if not isinstance(ins, (list, tuple)) or not ins:
            raise ValueError(f"instruction {index}: expected a nonempty instruction")
        name, operands = ins[0], ins[1:]
        if not isinstance(name, str) or name not in OPCODES:
            raise ValueError(f"instruction {index}: unknown opcode {name!r}")
        _, sig = OPCODES[name]
        if len(operands) != len(sig):
            raise ValueError(f"instruction {index}: {name} expects {len(sig)} operands")
        for kind, value in zip(sig, operands):
            if type(value) is not int:
                raise ValueError(f"instruction {index}: {name} operands must be integers")
            if kind == "R" and not 0 <= value < nregs:
                raise ValueError(f"instruction {index}: register r{value} out of range")
            if kind == "I" and not I64_MIN <= value <= I64_MAX:
                raise ValueError(f"instruction {index}: immediate {value} out of i64 range")
            if kind == "T" and not 0 <= value <= len(ir):
                raise ValueError(f"instruction {index}: jump target {value} out of range")
