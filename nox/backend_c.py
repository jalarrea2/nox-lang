"""
NOX back-end: IR -> C -> native binary (clang -O3).

Design for efficiency:
  * REGISTER machine (not a stack): r[] are i64 locals that the LLVM optimizer
    assigns to physical registers. No boxing, no runtime opcode dispatch (the
    obfuscation was already resolved at compile time).
  * Each instruction gets a C label (I0:, I1:, ...); jumps are `goto`.
    clang -O3 removes dead labels and builds its own CFG, does register
    allocation and, where applicable, vectorization.
  * Minimal I/O via getchar/putchar/printf: no heavy runtime.

The result is practically indistinguishable from hand-written C.
"""
import shutil
import subprocess

_BIN = {
    "ADD": "+", "SUB": "-", "MUL": "*",
    "AND": "&", "OR": "|", "XOR": "^", "SHL": "<<", "SHR": ">>",
}
_CMP = {"LT": "<", "EQ": "==", "GT": ">"}


def emit_c(ir, nregs, memsize) -> str:
    L = []
    w = L.append
    w("#include <stdint.h>")
    w("#include <stdio.h>")
    w("")
    w(f"static int64_t mem[{memsize}];")
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
        return f"r[{ins[1]}] = {ins[2]}LL;"
    if op == "MOV":
        return f"r[{ins[1]}] = r[{ins[2]}];"
    if op in _BIN:
        return f"r[{ins[1]}] = r[{ins[2]}] {_BIN[op]} r[{ins[3]}];"
    if op in _CMP:
        return f"r[{ins[1]}] = (r[{ins[2]}] {_CMP[op]} r[{ins[3]}]) ? 1 : 0;"
    if op == "DIV":
        return f"r[{ins[1]}] = r[{ins[3]}] ? r[{ins[2]}] / r[{ins[3]}] : 0;"
    if op == "MOD":
        return f"r[{ins[1]}] = r[{ins[3]}] ? r[{ins[2]}] % r[{ins[3]}] : 0;"
    if op == "LD":
        return f"r[{ins[1]}] = mem[r[{ins[2]}]];"
    if op == "ST":
        return f"mem[r[{ins[1]}]] = r[{ins[2]}];"
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


def compile_c(c_src: str, c_path: str, out_path: str, opt="-O3"):
    with open(c_path, "w") as f:
        f.write(c_src)
    cc = shutil.which("clang") or shutil.which("gcc") or shutil.which("cc")
    if not cc:
        raise RuntimeError("no clang/gcc/cc found on PATH")
    cmd = [cc, opt, "-std=c11", "-w", c_path, "-o", out_path]
    subprocess.run(cmd, check=True)
    return cc, cmd
