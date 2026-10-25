"""Source locations, configured register bounds, and CLI version regressions."""
import contextlib
import io
import os
import tempfile
import unittest

from nox import NoxSyntaxError, __version__, artifact_to_ir, parse
from nox.__main__ import main


class TestSourceDiagnostics(unittest.TestCase):
    def test_register_error_retains_original_source_location(self):
        source = "# heading\n\nstart:\n    r32 = 1 # register overflow\n"
        with self.assertRaises(NoxSyntaxError) as raised:
            parse(source, nregs=32, filename="program.nox")
        error = raised.exception
        self.assertEqual(error.filename, "program.nox")
        self.assertEqual(error.lineno, 4)
        self.assertEqual(error.text, "    r32 = 1 # register overflow")
        self.assertEqual(
            str(error), "program.nox: line 4: register r32 out of range (expected r0..r31)",
        )

    def test_bounds_apply_to_every_register_operand(self):
        statements = (
            "r2 = 1", "r2 = r0", "r0 = r2",
            "r2 = r0 + r1", "r0 = r2 + r1", "r0 = r1 + r2",
            "r2 = [r0]", "r0 = [r2]", "[r2] = r0", "[r0] = r2",
            "read r2", "print r2", "putc r2",
            "if r2 == 0 goto end", "if r2 != 0 goto end",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(NoxSyntaxError, "line 3: register r2 out of range"):
                    parse(f"# comment\n\n{statement}\nend:\n", nregs=2)

    def test_default_and_configured_register_limits(self):
        self.assertEqual(parse("r255 = 1"), [("IMM", 255, 1)])
        self.assertEqual(parse("r255 = 1", nregs=256), [("IMM", 255, 1)])
        self.assertEqual(parse("r0 = 1", nregs=1), [("IMM", 0, 1)])
        with self.assertRaisesRegex(NoxSyntaxError, "line 1: register r256 out of range"):
            parse("r256 = 1")
        for nregs in (0, 257, True, 1.5):
            with self.subTest(nregs=nregs), self.assertRaises(ValueError):
                parse("halt", nregs=nregs)

    def test_large_register_indices_retain_bounded_source_diagnostics(self):
        for digits in ("9" * 5000, "\u0669" * 5000):
            for statement in (f"r{digits} = 1", f"r0 = r{digits}",
                              f"[r{digits}] = r0", f"read r{digits}",
                              f"if r{digits} == 0 goto end"):
                with self.subTest(prefix=statement[:12]):
                    with self.assertRaises(NoxSyntaxError) as raised:
                        parse(f"# comment\n\n{statement}\nend:", filename="large.nox")
                    error = raised.exception
                    self.assertEqual(error.lineno, 3)
                    self.assertEqual(error.filename, "large.nox")
                    self.assertIn("register index out of range", str(error))
                    self.assertLess(len(str(error)), 100)

    def test_long_leading_zero_and_unicode_registers_remain_valid(self):
        for digits in ("0" * 5000 + "255", "\u0660" * 5000 + "\u0662\u0665\u0665"):
            with self.subTest(prefix=digits[:1]):
                self.assertEqual(parse(f"r{digits} = 1"), [("IMM", 255, 1)])

    def test_existing_syntax_errors_also_retain_source_locations(self):
        cases = (
            ("# comment\ngoto missing", 2, "unknown label"),
            ("start:\n\nstart:", 3, "duplicate label"),
            ("\n\nr0 = 9223372036854775808", 3, "out of i64 range"),
            ("\ninvalid statement", 2, "unrecognized statement"),
        )
        for source, line, message in cases:
            with self.subTest(source=source):
                with self.assertRaises(NoxSyntaxError) as raised:
                    parse(source, filename="input.nox")
                error = raised.exception
                self.assertEqual(error.lineno, line)
                self.assertEqual(error.text, source.splitlines()[line - 1])
                self.assertIn(f"input.nox: line {line}:", str(error))
                self.assertIn(message, str(error))


class TestCLIDiagnostics(unittest.TestCase):
    def test_large_register_is_a_located_source_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "large.nox")
            with open(source_path, "w", encoding="utf-8") as source:
                source.write("# comment\n\nr" + "9" * 5000 + " = 1\n")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["compile", source_path, "--seed", "key"])
            self.assertEqual(result, 2)
            self.assertIn(f"{source_path}: line 3: register index out of range", stderr.getvalue())
            self.assertNotIn("integer string conversion", stderr.getvalue())
            self.assertNotIn("9" * 200, stderr.getvalue())
            self.assertTrue(stderr.getvalue().rstrip().endswith("..."))

    def test_compile_reports_filename_and_source_line_without_writing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "program.nox")
            output_path = os.path.join(directory, "program.nxa")
            with open(source_path, "w", encoding="utf-8") as source:
                source.write("# comment\n\nstart:\n    r32 = 1\n")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["compile", source_path, "--seed", "key"])
            self.assertEqual(result, 2)
            self.assertIn(f"{source_path}: line 4: register r32 out of range", stderr.getvalue())
            self.assertIn("    r32 = 1", stderr.getvalue())
            self.assertFalse(os.path.exists(output_path))

    def test_compile_honors_custom_register_count(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "program.nox")
            with open(source_path, "w", encoding="utf-8") as source:
                source.write("r255 = 1\nprint r255\nhalt\n")
            with contextlib.redirect_stderr(io.StringIO()):
                result = main(["compile", source_path, "--seed", "key", "--regs", "256"])
            self.assertEqual(result, 0)
            with open(os.path.join(directory, "program.nxa"), encoding="utf-8") as artifact:
                ir, nregs, _ = artifact_to_ir(artifact.read(), "key")
            self.assertEqual(nregs, 256)
            self.assertEqual(ir, [("IMM", 255, 1), ("OUTN", 255), ("HALT",)])

    def test_version_is_available_without_a_subcommand_or_seed(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), f"nox {__version__}\n")
