"""
Back-end NOX: IR -> C -> binario nativo (clang -O3).

Diseño para eficiencia:
  * Máquina de REGISTROS (no de pila): r[] son locals i64 que el optimizador
    de LLVM asigna a registros físicos. Sin boxing, sin despacho de opcodes
    en runtime (la ofuscación ya se resolvió al compilar).
  * Cada instrucción lleva una etiqueta C (I0:, I1:, ...); los saltos son
    `goto`. clang -O3 elimina las etiquetas muertas y hace su propio CFG,
    register allocation y, cuando aplica, vectorización.
  * I/O mínima vía getchar/putchar/printf: sin runtime pesado.

El resultado es prácticamente indistinguible de C escrito a mano.
"""
import shutil
import subprocess

from .isa import OPCODES

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
    w("")
    for i, ins in enumerate(ir):
        w(f"I{i}:;")
        w("    " + _stmt(ins, i))
    # destino de fallthrough / saltos al final del programa
    w(f"I{len(ir)}:;")
    w("    return 0;")
    w("}")
    return "\n".join(L) + "\n"


def _stmt(ins, i):
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
    raise ValueError(f"opcode no soportado en back-end: {op}")


def compile_c(c_src: str, c_path: str, out_path: str, opt="-O3"):
    with open(c_path, "w") as f:
        f.write(c_src)
    cc = shutil.which("clang") or shutil.which("gcc") or shutil.which("cc")
    if not cc:
        raise RuntimeError("no se encontró clang/gcc/cc en PATH")
    cmd = [cc, opt, "-std=c11", "-w", c_path, "-o", out_path]
    subprocess.run(cmd, check=True)
    return cc, cmd
