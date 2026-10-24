"""
Test suite for NOX: parsing, round-trip codec, integrity, and native output.

Run with:  python -m pytest -q      (or: python -m unittest)
The native-output tests are skipped automatically when no C compiler is found.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nox import (
    NoxIntegrityError,
    NoxSyntaxError,
    artifact_to_ir,
    compile_c,
    emit_c,
    ir_to_artifact,
    parse,
)
from nox.codec import BRAILLE0, BRAILLE_LAST, serialize

HAVE_CC = bool(shutil.which("clang") or shutil.which("gcc") or shutil.which("cc"))
EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")

FIB = """
r0 = 0
r1 = 1
r2 = 0
r3 = 15
loop:
r4 = r2 < r3
if r4 == 0 goto end
print r0
r5 = 32
putc r5
r6 = r0 + r1
r0 = r1
r1 = r6
r7 = 1
r2 = r2 + r7
goto loop
end:
r8 = 10
putc r8
halt
"""


class TestParser(unittest.TestCase):
    def test_all_forms(self):
        ir = parse(
            "a:\n r0 = 5\n r1 = 0x1F\n r2 = 'A'\n r3 = -7\n"
            "r4 = r0\n r5 = r0 + r1\n r6 = r0 < r1\n r7 = r0 & r1\n"
            "r8 = [r0]\n [r0] = r1\n goto a\n if r0 == 0 goto a\n"
            "if r0 != 0 goto a\n read r0\n print r0\n putc r0\n halt\n"
        )
        self.assertEqual(ir[0], ("IMM", 0, 5))
        self.assertEqual(ir[1], ("IMM", 1, 0x1F))
        self.assertEqual(ir[2], ("IMM", 2, ord("A")))
        self.assertEqual(ir[3], ("IMM", 3, -7))
        self.assertEqual(ir[4], ("MOV", 4, 0))
        self.assertEqual(ir[5], ("ADD", 5, 0, 1))
        self.assertIn(("JMP", 0), ir)
        self.assertIn(("HALT",), ir)

    def test_label_resolution(self):
        ir = parse("start:\n goto start\n")
        self.assertEqual(ir, [("JMP", 0)])

    def test_errors(self):
        for bad in ("r0 = r1 +", "goto missing", "nonsense", "x: \n x:"):
            with self.assertRaises(NoxSyntaxError):
                parse(bad)


class TestCodec(unittest.TestCase):
    def test_round_trip(self):
        ir = parse(FIB)
        art = ir_to_artifact(ir, "secret")
        back, nregs, memsize = artifact_to_ir(art, "secret")
        self.assertEqual(back, ir)
        self.assertEqual((nregs, memsize), (32, 1 << 16))

    def test_artifact_is_pure_braille(self):
        art = ir_to_artifact(parse(FIB), "s")
        self.assertTrue(all(BRAILLE0 <= ord(c) <= BRAILLE_LAST for c in art))

    def test_seed_changes_output(self):
        ir = parse(FIB)
        self.assertNotEqual(ir_to_artifact(ir, "a"), ir_to_artifact(ir, "b"))

    def test_wrong_seed_rejected(self):
        art = ir_to_artifact(parse(FIB), "right")
        with self.assertRaises(NoxIntegrityError):
            artifact_to_ir(art, "wrong")

    def test_tamper_detected(self):
        art = ir_to_artifact(parse(FIB), "k")
        glyphs = list(art)
        # flip one glyph to a different Braille code point
        i = len(glyphs) // 2
        glyphs[i] = chr(BRAILLE0 + ((ord(glyphs[i]) - BRAILLE0) ^ 1))
        with self.assertRaises(NoxIntegrityError):
            artifact_to_ir("".join(glyphs), "k")

    def test_serialize_validates_registers(self):
        with self.assertRaises(ValueError):
            serialize([("IMM", 99, 0)], nregs=32)


@unittest.skipUnless(HAVE_CC, "no C compiler available")
class TestNative(unittest.TestCase):
    def _run(self, src, stdin=b""):
        ir, nregs, memsize = artifact_to_ir(ir_to_artifact(parse(src), "k"), "k")
        with tempfile.TemporaryDirectory() as d:
            binp = os.path.join(d, "p")
            compile_c(emit_c(ir, nregs, memsize), os.path.join(d, "p.c"), binp)
            return subprocess.run([binp], input=stdin, capture_output=True).stdout

    def test_fib_output(self):
        out = self._run(FIB).decode()
        self.assertEqual(out, "0 1 1 2 3 5 8 13 21 34 55 89 144 233 377 \n")

    def test_cat_roundtrips_stdin(self):
        cat = (
            "r1 = -1\n"
            "loop:\n"
            "read r0\n"
            "r2 = r0 == r1\n"
            "if r2 != 0 goto end\n"
            "putc r0\n"
            "goto loop\n"
            "end:\n"
            "halt\n"
        )
        self.assertEqual(self._run(cat, b"hello world"), b"hello world")


@unittest.skipUnless(HAVE_CC, "no C compiler available")
class TestExamples(TestNative):
    """Compile and run the bundled finite-state-machine examples."""

    def _run_example(self, name, stdin=b""):
        with open(os.path.join(EXAMPLES_DIR, name), encoding="utf-8") as f:
            return self._run(f.read(), stdin)

    def test_turnstile(self):
        # coin, push, coin, push -> unlocked, locked, unlocked, locked
        self.assertEqual(self._run_example("turnstile.nox", b"cpcp"), b"ULUL\n")

    def test_parity(self):
        self.assertEqual(self._run_example("parity.nox", b"1011"), b"1\n")  # three 1s -> odd
        self.assertEqual(self._run_example("parity.nox", b"1010"), b"0\n")  # two 1s -> even

    def test_div3(self):
        cases = [(b"110", b"Y\n"), (b"101", b"N\n"), (b"1001", b"Y\n"), (b"", b"Y\n")]
        for stdin, want in cases:
            self.assertEqual(self._run_example("div3.nox", stdin), want)


if __name__ == "__main__":
    unittest.main()
