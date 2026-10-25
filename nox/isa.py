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
  S  -> UTF-8 string             (u32 byte length + bytes)
  A  -> register list            (u16 count + u8 register indices)
"""
import json
import re

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
    "FUNC": (0x80, ("S", "A")),
    "ENDFUNC": (0x81, ()),
    "CALL": (0x82, ("R", "T", "A")),
    "RET": (0x83, ("R",)),
    "STR": (0x84, ("R", "S")),
    "SLEN": (0x85, ("R", "R")),
    "TOSTR": (0x86, ("R", "R")),
    "HESC": (0x87, ("R", "R")),
    "JENC": (0x90, ("R", "R")),
    "JGET": (0x91, ("R", "R", "R")),
    "JAT": (0x92, ("R", "R", "R")),
    "JSTR": (0x93, ("R", "R")),
    "JINT": (0x94, ("R", "R")),
    "JVALID": (0x95, ("R", "R")),
    "MCPTOOL": (0x98, ("S", "T", "S", "S")),
    "MCPSERVE": (0x99, ()),
}

V4_OPCODES = frozenset(("FUNC", "ENDFUNC", "CALL", "RET", "STR", "SLEN", "TOSTR", "HESC"))
JSON_OPCODES = frozenset(("JENC", "JGET", "JAT", "JSTR", "JINT", "JVALID"))
MCP_OPCODES = frozenset(("MCPTOOL", "MCPSERVE"))
V5_OPCODES = JSON_OPCODES | MCP_OPCODES
EXTENDED_OPCODES = V4_OPCODES | V5_OPCODES

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
            if kind == "S":
                if not isinstance(value, str):
                    raise ValueError(f"instruction {index}: {name} requires a string operand")
                try:
                    encoded = value.encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    raise ValueError(
                        f"instruction {index}: {name} string is not valid UTF-8"
                    ) from None
                if len(encoded) > 0xFFFFFFFF:
                    raise ValueError(f"instruction {index}: {name} string exceeds u32 byte length")
                continue
            if kind == "A":
                if not isinstance(value, (list, tuple)) or len(value) > NREGS_MAX:
                    raise ValueError(f"instruction {index}: {name} requires at most 256 registers")
                if any(type(reg) is not int or not 0 <= reg < nregs for reg in value):
                    raise ValueError(f"instruction {index}: {name} register list is out of range")
                continue
            if type(value) is not int:
                raise ValueError(f"instruction {index}: {name} operands must be integers")
            if kind == "R" and not 0 <= value < nregs:
                raise ValueError(f"instruction {index}: register r{value} out of range")
            if kind == "I" and not I64_MIN <= value <= I64_MAX:
                raise ValueError(f"instruction {index}: immediate {value} out of i64 range")
            if kind == "T" and not 0 <= value <= len(ir):
                raise ValueError(f"instruction {index}: jump target {value} out of range")

    owners, functions = analyze_functions(ir)
    tools = set()
    for index, ins in enumerate(ir):
        name = ins[0]
        if name == "RET" and owners[index] is None:
            raise ValueError(f"instruction {index}: RET is only valid inside a function")
        if name == "CALL":
            target, arguments = ins[2], ins[3]
            if target not in functions:
                raise ValueError(f"instruction {index}: CALL target must be a FUNC instruction")
            if len(arguments) != len(functions[target]["params"]):
                raise ValueError(
                    f"instruction {index}: CALL argument count does not match parameters"
                )
        if name in MCP_OPCODES and owners[index] is not None:
            raise ValueError(f"instruction {index}: {name} is only valid in main scope")
        if name == "MCPTOOL":
            tool_name, target, parameters, description = ins[1:]
            if target not in functions:
                raise ValueError(f"instruction {index}: MCPTOOL target must be a FUNC instruction")
            try:
                validate_tool_metadata(
                    tool_name, parameters, description, len(functions[target]["params"]),
                )
            except ValueError as error:
                raise ValueError(f"instruction {index}: {error}") from None
            if tool_name in tools:
                raise ValueError(f"instruction {index}: duplicate MCP tool name {tool_name!r}")
            tools.add(tool_name)
        if name in ("JMP", "JZ", "JNZ"):
            target = ins[-1]
            if target == len(ir):
                target_owner = None
            else:
                target_owner = owners[target]
                if ir[target][0] == "FUNC":
                    raise ValueError(f"instruction {index}: jump target cannot be FUNC metadata")
            if target_owner != owners[index]:
                raise ValueError(f"instruction {index}: jump target crosses a function boundary")


def validate_tool_metadata(tool_name, parameters, description, arity):
    """Validate static MCP names and JSON parameter metadata; return the names."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool_name):
        raise ValueError("MCP tool name must contain 1..128 ASCII letters, digits, _, ., or -")
    if "\x00" in description:
        raise ValueError("MCP tool description cannot contain NUL")
    try:
        names = json.loads(parameters)
    except (ValueError, RecursionError):
        raise ValueError("MCP parameter metadata must be a JSON list of names") from None
    if (not isinstance(names, list) or len(names) > NREGS_MAX
            or any(not isinstance(name, str) or "\x00" in name for name in names)):
        raise ValueError("MCP parameter metadata must be a list of at most 256 NUL-free strings")
    if len(set(names)) != len(names):
        raise ValueError("MCP parameter names must be unique")
    if len(names) != arity:
        raise ValueError("MCP parameter name count must match function parameters")
    for name in names:
        try:
            name.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("MCP parameter names must be valid UTF-8") from None
    return names


def analyze_functions(ir):
    """Return instruction owners and function metadata for structurally valid IR.

    Owners are FUNC instruction indices, or None for main. Function metadata
    maps those indices to dictionaries containing name, params, and end.
    FUNC and ENDFUNC markers belong to their function. Operand bounds are
    checked separately by validate_ir; this helper checks declaration shape.
    """
    owners, functions, names = [], {}, set()
    current = None
    for index, ins in enumerate(ir):
        if not isinstance(ins, (list, tuple)) or not ins:
            raise ValueError(f"instruction {index}: expected a nonempty instruction")
        name = ins[0]
        if name == "FUNC":
            if current is not None:
                raise ValueError(
                    f"instruction {index}: nested function declarations are prohibited"
                )
            if (len(ins) != 3 or not isinstance(ins[1], str)
                    or not isinstance(ins[2], (list, tuple))):
                raise ValueError(f"instruction {index}: invalid FUNC declaration")
            if ins[1] in names:
                raise ValueError(f"instruction {index}: duplicate function name {ins[1]!r}")
            if any(type(reg) is not int for reg in ins[2]) or len(set(ins[2])) != len(ins[2]):
                raise ValueError(
                    f"instruction {index}: function parameters must be unique registers"
                )
            names.add(ins[1])
            current = index
            functions[index] = {"name": ins[1], "params": tuple(ins[2]), "end": None}
        owners.append(current)
        if name == "ENDFUNC":
            if current is None:
                raise ValueError(f"instruction {index}: ENDFUNC has no matching FUNC")
            functions[current]["end"] = index
            current = None
    if current is not None:
        raise ValueError(f"instruction {current}: FUNC has no matching ENDFUNC")
    return owners, functions
