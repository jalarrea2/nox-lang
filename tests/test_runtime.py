"""Compare native arithmetic with a Python reference and exercise memory traps.

Set NOX_TEST_SANITIZERS=1 to compile these cases with undefined-behavior checks.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from random import Random

from nox import artifact_to_ir, emit_c, ir_to_artifact, parse
from nox.isa import I64_MAX, I64_MIN

CC = shutil.which("clang") or shutil.which("gcc") or shutil.which("cc")
MASK = (1 << 64) - 1


def signed(value):
    value &= MASK
    return value if value <= I64_MAX else value - (1 << 64)


def quotient(a, b):
    if b == 0:
        return 0
    result = abs(a) // abs(b)
    return -result if (a < 0) != (b < 0) else result


@unittest.skipUnless(CC, "no C compiler available")
class TestRuntime(unittest.TestCase):
    def _run(self, source, memsize=8, nregs=32, opt="-O3"):
        ir, nregs, memsize = artifact_to_ir(
            ir_to_artifact(parse(source), "key", nregs, memsize), "key",
        )
        with tempfile.TemporaryDirectory() as directory:
            cpath = os.path.join(directory, "program.c")
            binary = os.path.join(directory, "program")
            with open(cpath, "w", encoding="utf-8") as f:
                f.write(emit_c(ir, nregs, memsize))
            flags = [opt, "-std=c11", "-pedantic-errors"]
            if os.environ.get("NOX_TEST_SANITIZERS") == "1":
                flags += ["-fsanitize=undefined", "-fno-sanitize-recover=all"]
            subprocess.run([CC, *flags, cpath, "-o", binary],
                           capture_output=True, check=True, timeout=45)
            return subprocess.run([binary], capture_output=True, timeout=5)

    def test_arithmetic_matches_reference_at_O0_and_O3(self):
        operations = {
            "+": lambda a, b: signed(a + b),
            "-": lambda a, b: signed(a - b),
            "*": lambda a, b: signed(a * b),
            "/": lambda a, b: signed(quotient(a, b)),
            "%": lambda a, b: a - quotient(a, b) * b if b else 0,
            "&": lambda a, b: a & b,
            "|": lambda a, b: a | b,
            "^": lambda a, b: a ^ b,
            "<<": lambda a, b: signed(a << (b & 63)),
            ">>": lambda a, b: a >> (b & 63),
            "<": lambda a, b: int(a < b),
            ">": lambda a, b: int(a > b),
            "==": lambda a, b: int(a == b),
        }
        pairs = [
            (I64_MIN, -1), (I64_MIN, 0), (I64_MIN, 1), (I64_MIN, 63),
            (I64_MIN, 64), (I64_MIN, -65), (I64_MAX, 1), (I64_MAX, I64_MAX),
            (0, -1), (-1, 1), (-1, 63), (-1, 64), (-7, 3), (7, -3),
            (1, 64), (1, -1), (1, I64_MIN), (I64_MAX, 2), (I64_MAX, 63),
            (I64_MIN, I64_MIN), (3, 2), (0, 0), (I64_MIN, 2),
        ]
        rng = Random(42)
        pairs += [(rng.randint(I64_MIN, I64_MAX), rng.randint(I64_MIN, I64_MAX))
                  for _ in range(16)]
        source, expected = ["r3 = 10"], []
        for a, b in pairs:
            source += [f"r0 = {a}", f"r1 = {b}"]
            for operator, reference in operations.items():
                source += [f"r2 = r0 {operator} r1", "print r2", "putc r3"]
                expected.append(str(reference(a, b)))
        for opt in ("-O0", "-O3"):
            with self.subTest(opt=opt):
                result = self._run("\n".join(source), opt=opt)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(result.stdout.decode().splitlines(), expected)

    def test_memory_traps_for_loads_and_stores(self):
        for address in (-1, 8, I64_MIN, I64_MAX):
            for instruction in ("r1 = [r0]", "[r0] = r1"):
                with self.subTest(address=address, instruction=instruction):
                    result = self._run(f"r0 = {address}\n{instruction}")
                    self.assertEqual(result.returncode, 4, result.stderr)
                    self.assertEqual(result.stdout, b"")
                    self.assertEqual(
                        result.stderr.decode(),
                        f"[nox] runtime error: memory address {address} out of bounds (size 8)\n",
                    )

    def test_memory_boundaries_and_register_aliasing(self):
        source = """
        r255 = 42
        r0 = 0
        [r0] = r255
        r0 = [r0]
        print r0
        r1 = 7
        [r1] = r0
        r1 = [r1]
        print r1
        r1 = r1 + r1
        print r1
        """
        result = self._run(source, nregs=256)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"424284")

    def test_cli_propagates_memory_trap(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = os.path.join(directory, "invalid_address.nxa")
            with open(artifact, "w", encoding="utf-8") as f:
                f.write(ir_to_artifact(parse("r0 = -1\nr1 = [r0]"), "key"))
            result = subprocess.run(
                [sys.executable, "-m", "nox", "run", artifact, "--seed", "key"],
                capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn(b"memory address -1 out of bounds", result.stderr)

    def test_jumps_to_end_and_empty_program(self):
        for source in ("", "goto end\nprint r0\nend:",
                       "if r0 == 0 goto end\nprint r0\nend:",
                       "r0 = 1\nif r0 != 0 goto end\nprint r0\nend:"):
            with self.subTest(source=source):
                result = self._run(source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"")
