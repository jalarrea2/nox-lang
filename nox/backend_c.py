"""
NOX back-end: IR -> C -> native binary (clang -O3).

Design for efficiency:
  * REGISTER machine: legacy programs use i64 locals. Extended programs use
    tagged values, direct C function calls, and a bounded string arena. There
    is no runtime opcode dispatch; obfuscation is resolved at compile time.
  * Each instruction gets a C label (I0:, I1:, ...); jumps are `goto`.
    clang -O3 removes dead labels and builds its own CFG, does register
    allocation and, where applicable, vectorization.
  * Minimal I/O via getchar/putchar/printf: no heavy runtime.

The resulting code is optimized by the selected C compiler. Performance against
hand-written C has not yet been benchmarked.
"""
import os
import shutil
import subprocess

from .isa import EXTENDED_OPCODES, I64_MIN, validate_ir

_BIN = {
    "ADD": "+", "SUB": "-", "MUL": "*",
    "AND": "&", "OR": "|", "XOR": "^",
}
_CMP = {"LT": "<", "EQ": "==", "GT": ">"}

_HELPERS = """
/* Convert a bit pattern without an out-of-range unsigned-to-signed cast. */
static inline int64_t nox_i64(uint64_t bits) {
    return bits <= INT64_MAX ? (int64_t)bits : -1 - (int64_t)(UINT64_MAX - bits);
}

static inline int64_t nox_shl(int64_t value, int64_t count) {
    return nox_i64((uint64_t)value << ((uint64_t)count & 63));
}

static inline int64_t nox_shr(int64_t value, int64_t count) {
    unsigned shift = (unsigned)((uint64_t)count & 63);
    uint64_t bits = (uint64_t)value;
    return nox_i64(value < 0 ? ~(~bits >> shift) : bits >> shift);
}

static inline int64_t nox_div(int64_t a, int64_t b) {
    if (!b) return 0;
    if (a == INT64_MIN && b == -1) return INT64_MIN;
    return a / b;
}

static inline int64_t nox_mod(int64_t a, int64_t b) {
    if (!b || (a == INT64_MIN && b == -1)) return 0;
    return a % b;
}

static inline size_t nox_addr(int64_t address) {
    if (address < 0 || (uint64_t)address >= sizeof mem / sizeof mem[0]) {
        fprintf(stderr,
                "[nox] runtime error: memory address %lld out of bounds (size %llu)\\n",
                (long long)address, (unsigned long long)(sizeof mem / sizeof mem[0]));
        exit(4);
    }
    return (size_t)address;
}
"""


def emit_c(ir, nregs, memsize) -> str:
    validate_ir(ir, nregs, memsize)
    if any(instruction[0] in EXTENDED_OPCODES for instruction in ir):
        from .backend_values import emit_values_c

        return emit_values_c(ir, nregs, memsize, _HELPERS)
    L = []
    w = L.append
    w("#include <stdint.h>")
    w("#include <stdio.h>")
    w("#include <stdlib.h>")
    w("")
    w(f"static int64_t mem[{memsize}];")
    w(_HELPERS)
    w("")
    w("int main(void) {")
    w(f"    int64_t r[{nregs}] = {{0}};")
    w("    int c;")
    w("    (void)c;")
    w("")
    for i, ins in enumerate(ir):
        w(f"I{i}:;")
        w("    " + _stmt(ins))
    # fallthrough / end-of-program jump target
    w(f"I{len(ir)}:;")
    w("    return 0;")
    w("}")
    return "\n".join(L) + "\n"


def _stmt(ins):
    op = ins[0]
    if op == "IMM":
        literal = "INT64_MIN" if ins[2] == I64_MIN else f"{ins[2]}LL"
        return f"r[{ins[1]}] = {literal};"
    if op == "MOV":
        return f"r[{ins[1]}] = r[{ins[2]}];"
    if op in _BIN:
        return f"r[{ins[1]}] = nox_i64((uint64_t)r[{ins[2]}] {_BIN[op]} (uint64_t)r[{ins[3]}]);"
    if op in ("SHL", "SHR", "DIV", "MOD"):
        return f"r[{ins[1]}] = nox_{op.lower()}(r[{ins[2]}], r[{ins[3]}]);"
    if op in _CMP:
        return f"r[{ins[1]}] = (r[{ins[2]}] {_CMP[op]} r[{ins[3]}]) ? 1 : 0;"
    if op == "LD":
        return f"r[{ins[1]}] = mem[nox_addr(r[{ins[2]}])];"
    if op == "ST":
        return f"mem[nox_addr(r[{ins[1]}])] = r[{ins[2]}];"
    if op == "JMP":
        return f"goto I{ins[1]};"
    if op == "JZ":
        return f"if (r[{ins[1]}] == 0) goto I{ins[2]};"
    if op == "JNZ":
        return f"if (r[{ins[1]}] != 0) goto I{ins[2]};"
    if op == "IN":
        return f"c = getchar(); r[{ins[1]}] = (c == EOF) ? -1 : c;"
    if op == "OUT":
        return f"putchar((int)(r[{ins[1]}] & 0xFF));"
    if op == "OUTN":
        return f'printf("%lld", (long long)r[{ins[1]}]);'
    if op == "HALT":
        return "return 0;"
    raise ValueError(f"opcode not supported by back-end: {op}")


def find_compiler():
    """Find a C compiler, respecting an explicit CC executable name or path."""
    requested = os.environ.get("CC")
    if requested:
        cc = shutil.which(requested)
        if not cc:
            raise RuntimeError(f"C compiler specified by CC not found: {requested!r}")
        return cc
    return shutil.which("clang") or shutil.which("gcc") or shutil.which("cc")


def compile_c(c_src: str, c_path: str, out_path: str, opt="-O3"):
    with open(c_path, "w") as f:
        f.write(c_src)
    cc = find_compiler()
    if not cc:
        raise RuntimeError("no clang/gcc/cc found on PATH")
    cmd = [cc, opt, "-std=c11", "-w", c_path, "-o", out_path]
    subprocess.run(cmd, check=True)
    return cc, cmd
