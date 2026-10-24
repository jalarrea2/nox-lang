"""
NOX ISA — register machine, i64, linear memory, I/O.
Turing-complete: arithmetic + addressable memory + conditional jumps.

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
