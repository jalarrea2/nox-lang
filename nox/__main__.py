"""
NOX CLI.

  python -m nox compile prog.nox  -o prog.nxa  --seed KEY   # DSL       -> artifact
  python -m nox build   prog.nxa  -o prog      --seed KEY   # artifact  -> binary
  python -m nox run     prog.nxa               --seed KEY   # compile to tmp and run
  python -m nox emit    prog.nxa               --seed KEY   # show the generated C
  python -m nox disasm  prog.nxa               --seed KEY   # disassemble to IR (debug)

The artifact (.nxa) is unreadable on its own; the seed is the key that opens it.
"""
import argparse
import os
import subprocess
import sys
import tempfile

from . import (parse, ir_to_artifact, artifact_to_ir, emit_c, compile_c,
               NoxSyntaxError, NoxIntegrityError)
from .isa import NREGS_DEFAULT, MEMSIZE_DEFAULT


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def cmd_compile(a):
    ir = parse(_read(a.src))
    art = ir_to_artifact(ir, a.seed, nregs=a.regs, memsize=a.mem)
    out = a.out or (os.path.splitext(a.src)[0] + ".nxa")
    with open(out, "w", encoding="utf-8") as f:
        f.write(art)
    print(f"[nox] {len(ir)} instructions -> {out}  ({len(art)} glyphs)", file=sys.stderr)


def cmd_build(a):
    ir, nregs, memsize = artifact_to_ir(_read(a.src), a.seed)
    out = a.out or os.path.splitext(a.src)[0]
    with tempfile.TemporaryDirectory() as d:
        cc, _ = compile_c(emit_c(ir, nregs, memsize),
                          os.path.join(d, "p.c"), out, opt=a.opt)
    print(f"[nox] {os.path.basename(cc)} {a.opt} -> {out}", file=sys.stderr)


def cmd_run(a):
    ir, nregs, memsize = artifact_to_ir(_read(a.src), a.seed)
    with tempfile.TemporaryDirectory() as d:
        binp = os.path.join(d, "p")
        compile_c(emit_c(ir, nregs, memsize), os.path.join(d, "p.c"), binp, opt=a.opt)
        sys.stdout.flush()
        return subprocess.run([binp]).returncode


def cmd_emit(a):
    ir, nregs, memsize = artifact_to_ir(_read(a.src), a.seed)
    sys.stdout.write(emit_c(ir, nregs, memsize))


def cmd_disasm(a):
    ir, nregs, memsize = artifact_to_ir(_read(a.src), a.seed)
    print(f"; nregs={nregs} memsize={memsize} instructions={len(ir)}")
    for i, ins in enumerate(ir):
        print(f"{i:4d}  {ins[0]:5s} {', '.join(map(str, ins[1:]))}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="nox", description="NOX opaque metalanguage")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, need_out=False, compile_opts=False):
        sp = sub.add_parser(name)
        sp.add_argument("src")
        sp.add_argument("--seed", required=True)
        sp.add_argument("--opt", default="-O3", help="C compiler optimization flag")
        if need_out:
            sp.add_argument("-o", "--out")
        if compile_opts:
            sp.add_argument("--regs", type=int, default=NREGS_DEFAULT,
                            help=f"number of registers (default {NREGS_DEFAULT})")
            sp.add_argument("--mem", type=int, default=MEMSIZE_DEFAULT,
                            help=f"linear memory cells (default {MEMSIZE_DEFAULT})")
        sp.set_defaults(fn=fn)
        return sp

    add("compile", cmd_compile, need_out=True, compile_opts=True)
    add("build", cmd_build, need_out=True)
    add("run", cmd_run)
    add("emit", cmd_emit)
    add("disasm", cmd_disasm)

    a = p.parse_args(argv)
    try:
        rc = a.fn(a)
    except NoxSyntaxError as e:
        print(f"[nox] syntax error: {e}", file=sys.stderr)
        return 2
    except NoxIntegrityError as e:
        print(f"[nox] integrity error: {e}", file=sys.stderr)
        return 3
    except (ValueError, subprocess.CalledProcessError, RuntimeError, OSError) as e:
        print(f"[nox] error: {e}", file=sys.stderr)
        return 1
    return rc or 0


if __name__ == "__main__":
    raise SystemExit(main())
