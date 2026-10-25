"""Execute extended IR as native C, including ownership and failure boundaries."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nox.backend_c import emit_c, find_compiler
from nox.isa import I64_MAX, I64_MIN

CC = find_compiler()


def assemble(instructions):
    """Resolve symbolic test labels without using the language frontend."""
    labels, ir = {}, []
    for instruction in instructions:
        if instruction[0] == "LABEL":
            labels[instruction[1]] = len(ir)
        else:
            if instruction[0] == "FUNC":
                labels[instruction[1]] = len(ir)
            ir.append(instruction)
    result = []
    for instruction in ir:
        instruction = list(instruction)
        position = {"CALL": 2, "JMP": 1, "JZ": 2, "JNZ": 2}.get(instruction[0])
        if position is not None and isinstance(instruction[position], str):
            instruction[position] = labels[instruction[position]]
        result.append(tuple(instruction))
    return result


@unittest.skipUnless(CC, "no C compiler available")
class TestNativeValues(unittest.TestCase):
    def run_ir(self, instructions, opt="-O3", flags=(), stdin=b"", nregs=32, memsize=8):
        generated = emit_c(assemble(instructions), nregs, memsize)
        with tempfile.TemporaryDirectory(prefix="nox-values-") as directory:
            source = Path(directory) / "program.c"
            binary = Path(directory) / "program"
            source.write_text(generated, encoding="utf-8")
            options = [opt, "-std=c11", "-pedantic-errors", *flags]
            if os.environ.get("NOX_TEST_SANITIZERS") == "1":
                options += ["-fsanitize=undefined", "-fno-sanitize-recover=all"]
            compilation = subprocess.run([CC, *options, str(source), "-o", str(binary)],
                                         capture_output=True, timeout=45)
            self.assertEqual(compilation.returncode, 0, compilation.stderr.decode())
            return subprocess.run([str(binary)], input=stdin, capture_output=True, timeout=5)

    def assert_output(self, instructions, expected, **kwargs):
        for opt in ("-O0", "-O3"):
            with self.subTest(opt=opt):
                result = self.run_ir(instructions, opt=opt, **kwargs)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(result.stdout, expected)

    def test_functions_are_native_and_arguments_are_local(self):
        instructions = [
            ("STR", 0, "NOX"), ("CALL", 1, "greet", (0,)), ("OUTN", 1), ("OUTN", 0),
            ("FUNC", "greet", (5,)), ("STR", 0, "Hello, "), ("ADD", 1, 0, 5),
            ("RET", 1), ("ENDFUNC",),
        ]
        generated = emit_c(assemble(instructions), 32, 8)
        self.assertIn("static nox_value nox_function_4(nox_value argument_0)", generated)
        self.assertIn("r[1] = nox_function_4(r[0]);", generated)
        self.assert_output(instructions, b"Hello, NOXNOX")

    def test_recursion_preserves_each_invocations_registers(self):
        self.assert_output([
            ("IMM", 0, 6), ("CALL", 1, "factorial", (0,)), ("OUTN", 1), ("OUTN", 0),
            ("FUNC", "factorial", (0,)), ("IMM", 1, 1), ("GT", 2, 0, 1),
            ("JZ", 2, "base"), ("SUB", 3, 0, 1), ("CALL", 4, "factorial", (3,)),
            ("MUL", 5, 0, 4), ("RET", 5), ("LABEL", "base"), ("RET", 1), ("ENDFUNC",),
        ], b"7206")

    def test_mutual_recursion_and_forward_declarations(self):
        self.assert_output([
            ("IMM", 0, 9), ("CALL", 1, "even", (0,)), ("OUTN", 1),
            ("IMM", 0, 10), ("CALL", 1, "even", (0,)), ("OUTN", 1),
            ("FUNC", "even", (0,)), ("JZ", 0, "even_base"), ("IMM", 1, 1),
            ("SUB", 0, 0, 1), ("CALL", 2, "odd", (0,)), ("RET", 2),
            ("LABEL", "even_base"), ("IMM", 2, 1), ("RET", 2), ("ENDFUNC",),
            ("FUNC", "odd", (0,)), ("JZ", 0, "odd_base"), ("IMM", 1, 1),
            ("SUB", 0, 0, 1), ("CALL", 2, "even", (0,)), ("RET", 2),
            ("LABEL", "odd_base"), ("IMM", 2, 0), ("RET", 2), ("ENDFUNC",),
        ], b"01")

    def test_all_256_registers_can_be_function_parameters(self):
        registers = tuple(range(256))
        self.assert_output([
            ("IMM", 255, 42), ("CALL", 0, "last", registers), ("OUTN", 0),
            ("FUNC", "last", registers), ("RET", 255), ("ENDFUNC",),
        ], b"42", nregs=256)

    def test_implicit_return_end_jump_and_initial_registers(self):
        self.assert_output([
            ("CALL", 0, "implicit", ()), ("OUTN", 0),
            ("CALL", 1, "fresh", ()), ("OUTN", 1),
            ("FUNC", "implicit", ()), ("JMP", "implicit_end"), ("IMM", 0, 99),
            ("LABEL", "implicit_end"), ("ENDFUNC",),
            ("FUNC", "fresh", ()), ("RET", 31), ("ENDFUNC",),
        ], b"00")

    def test_shared_memory_preserves_string_handles(self):
        self.assert_output([
            ("STR", 0, "before"), ("IMM", 1, 0), ("ST", 1, 0),
            ("CALL", 2, "replace", ()), ("OUTN", 2), ("LD", 3, 1), ("OUTN", 3),
            ("OUTN", 0),
            ("FUNC", "replace", ()), ("LD", 2, 0), ("STR", 1, "after"),
            ("ST", 0, 1), ("RET", 2), ("ENDFUNC",),
        ], b"beforeafterbefore")

    def test_unicode_nul_html_escape_length_and_conversion(self):
        text = "ñ\x00<&>\"'"
        escaped = "ñ\x00&lt;&amp;&gt;&quot;&#39;"
        self.assert_output([
            ("STR", 0, text), ("HESC", 1, 0), ("OUTN", 1), ("SLEN", 2, 0),
            ("TOSTR", 3, 2), ("ADD", 4, 1, 3), ("OUTN", 4),
            ("IMM", 5, I64_MIN), ("TOSTR", 6, 5), ("OUTN", 6),
            ("TOSTR", 7, 0), ("OUTN", 7),
        ], (escaped + escaped + str(len(text.encode())) + str(I64_MIN) + text).encode())

    def test_empty_strings_and_value_equality(self):
        self.assert_output([
            ("STR", 0, ""), ("STR", 1, ""), ("ADD", 2, 0, 1), ("OUTN", 2),
            ("EQ", 3, 0, 2), ("OUTN", 3), ("IMM", 4, 0), ("EQ", 3, 0, 4), ("OUTN", 3),
            ("STR", 0, "a\x00b"), ("STR", 1, "a\x00b"), ("EQ", 3, 0, 1), ("OUTN", 3),
            ("STR", 1, "a\x00c"), ("EQ", 3, 0, 1), ("OUTN", 3),
            ("STR", 1, "a"), ("EQ", 3, 0, 1), ("OUTN", 3),
        ], b"10100")

    def test_tagged_arithmetic_preserves_i64_rules(self):
        instructions = [("STR", 31, "select tagged backend"), ("IMM", 0, -7), ("IMM", 1, 3)]
        expected = []
        for op, result in (("ADD", -4), ("SUB", -10), ("MUL", -21), ("DIV", -2),
                           ("MOD", -1), ("LT", 1), ("GT", 0), ("EQ", 0), ("AND", 1),
                           ("OR", -5), ("XOR", -6), ("SHL", -56), ("SHR", -1)):
            instructions += [(op, 2, 0, 1), ("OUTN", 2)]
            expected.append(str(result))
        instructions += [("IMM", 0, I64_MIN), ("IMM", 1, -1)]
        for op, result in (("ADD", I64_MAX), ("SUB", I64_MIN + 1), ("MUL", I64_MIN),
                           ("DIV", I64_MIN), ("MOD", 0), ("SHL", 0), ("SHR", -1)):
            instructions += [(op, 2, 0, 1), ("OUTN", 2)]
            expected.append(str(result))
        self.assert_output(instructions, "".join(expected).encode())

    def test_tagged_byte_input_output_and_eof(self):
        self.assert_output([
            ("STR", 31, "select tagged backend"), ("IN", 0), ("OUT", 0),
            ("IN", 0), ("OUT", 0), ("IN", 0), ("OUTN", 0),
        ], b"\x00\xff-1", stdin=b"\x00\xff")

    def test_wrong_value_types_are_runtime_errors(self):
        cases = [
            ("ADD", 2, 0, 1), ("ADD", 2, 1, 0), ("SUB", 2, 0, 1), ("MUL", 2, 1, 0),
            ("DIV", 2, 0, 1), ("MOD", 2, 1, 0), ("LT", 2, 0, 1), ("GT", 2, 1, 0),
            ("AND", 2, 0, 1), ("OR", 2, 1, 0), ("XOR", 2, 0, 1), ("SHL", 2, 1, 0),
            ("SHR", 2, 0, 1), ("LD", 2, 0), ("ST", 0, 1), ("OUT", 0),
            ("SLEN", 2, 1), ("HESC", 2, 1), ("JZ", 0, 3), ("JNZ", 0, 3),
        ]
        for instruction in cases:
            with self.subTest(instruction=instruction):
                result = self.run_ir([("STR", 0, "text"), ("IMM", 1, 1), instruction])
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertIn(b"[nox] runtime error:", result.stderr)
                self.assertIn(instruction[0].encode(), result.stderr)

    def test_tagged_memory_bounds_still_trap(self):
        for instruction in (("LD", 2, 1), ("ST", 1, 0)):
            result = self.run_ir([("STR", 0, "text"), ("IMM", 1, -1), instruction])
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn(b"memory address -1 out of bounds", result.stderr)

    def test_call_depth_limit_traps_before_unbounded_recursion(self):
        instructions = [("CALL", 0, "recurse", ()), ("FUNC", "recurse", ()),
                        ("CALL", 0, "recurse", ()), ("RET", 0), ("ENDFUNC",)]
        for opt in ("-O0", "-O3"):
            result = self.run_ir(instructions, opt=opt)
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn(b"call depth limit exceeded", result.stderr)

    def test_string_arena_counts_metadata_and_cumulative_allocations(self):
        result = self.run_ir([("STR", 0, "")], flags=("-DNOX_STRING_LIMIT=1",))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(b"string arena limit exceeded", result.stderr)
        result = self.run_ir([("STR", 0, "small"), ("JMP", 0)],
                             flags=("-DNOX_STRING_LIMIT=128",))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(b"string arena limit exceeded", result.stderr)

    def test_halt_inside_function_terminates_main(self):
        self.assert_output([
            ("CALL", 0, "stop", ()), ("IMM", 1, 99), ("OUTN", 1),
            ("FUNC", "stop", ()), ("STR", 0, "done"), ("OUTN", 0),
            ("HALT",), ("ENDFUNC",),
        ], b"done")
