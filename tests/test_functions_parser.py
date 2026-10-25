"""Checked source lowering, local scopes, and bounded register allocation."""
import os
import subprocess
import tempfile
import unittest

from nox import NoxSyntaxError, compile_c, emit_c, parse
from nox.backend_c import find_compiler
from nox.dsl import _parse
from nox.isa import I64_MIN, analyze_functions, validate_ir


class TestFunctionsParser(unittest.TestCase):
    def valid(self, source, nregs=32):
        ir = parse(source, nregs=nregs, filename="source.nox")
        validate_ir(ir, nregs, 64)
        return ir

    def invalid(self, source, line, message, nregs=32):
        with self.assertRaises(NoxSyntaxError) as raised:
            parse(source, nregs=nregs, filename="source.nox")
        error = raised.exception
        self.assertEqual(error.filename, "source.nox")
        self.assertEqual(error.lineno, line)
        self.assertEqual(error.text, source.splitlines()[line - 1])
        self.assertIn(message, str(error))

    def test_legacy_ir_is_unchanged(self):
        source = """start:
r0 = -7
r1 = 2
r2 = r0 + r1
[r1] = r2
r3 = [r1]
if r3 != 0 goto start
halt
"""
        self.assertEqual(self.valid(source), _parse(source.splitlines(), 32))

    def test_forward_calls_and_function_parameter_registers(self):
        ir = self.valid("print twice(3)\nfn twice(value):\n    return value + value\n")
        call = next(instruction for instruction in ir if instruction[0] == "CALL")
        declaration = ir[call[2]]
        self.assertEqual(declaration, ("FUNC", "twice", (0,)))
        self.assertEqual(len(call[3]), 1)
        self.assertIn(("ENDFUNC",), ir)

    def test_labels_are_resolved_within_each_scope(self):
        ir = self.valid("""goto done
fn first():
    goto done
    done:
    return 1
fn second():
    goto done
    done:
    return 2
done:
print first() + second()
""")
        owners, _ = analyze_functions(ir)
        jumps = [(index, instruction) for index, instruction in enumerate(ir)
                 if instruction[0] == "JMP" and instruction[1] != len(ir)]
        self.assertEqual(len(jumps), 3)
        for index, instruction in jumps:
            self.assertEqual(owners[index], owners[instruction[1]])

    def test_named_locals_never_capture_main_or_other_function_names(self):
        self.invalid('outside = "main"\nfn read_global():\n    return outside\n',
                     3, "unknown local variable")
        self.invalid("fn first():\n    local = 1\n    return local\n"
                     "fn second():\n    return local\n", 5, "unknown local variable")

    def test_named_variables_must_be_lexically_initialized(self):
        self.invalid("print value\nvalue = 1", 1, "unknown local variable")
        self.invalid("value = value + 1", 1, "unknown local variable")
        self.invalid("fn broken():\n    print value\n    value = 1\n",
                     2, "unknown local variable")
        self.valid("read value\nvalue = value + 1\nprint value")

    def test_explicit_registers_are_reserved_but_literals_and_labels_are_not(self):
        ir = self.valid("r0 = 40\nanswer = 2\nprint r0 + answer", nregs=3)
        self.assertIn(("IMM", 0, 40), ir)
        self.assertIn(("IMM", 1, 2), ir)
        self.valid('text = "r255"\ngoto\tr255\nr255:\nprint text', nregs=1)

    def test_temporaries_are_reused_between_statements(self):
        source = 'text = ""\n' + 'text = text + "x"\n' * 200 + "print text\n"
        ir = self.valid(source, nregs=2)
        self.assertEqual(sum(instruction[0] == "ADD" for instruction in ir), 200)
        self.invalid('first = "a"\nsecond = "b"\nprint first + second',
                     3, "register file exhausted", nregs=2)

    def test_builtin_calls_and_ordinal_vs_string_literals(self):
        ir = self.valid('text = "é🙂\\0#;" # comment\n'
                        'count = len(text)\nprint concat(str(count), html.escape(text))\n'
                        "fn ordinal():\n    return '#' + 1\n")
        self.assertIn(("STR", 0, "é🙂\0#;"), ir)
        for opcode in ("SLEN", "TOSTR", "HESC", "ADD", "RET"):
            self.assertTrue(any(instruction[0] == opcode for instruction in ir), opcode)
        self.assertTrue(any(instruction[0] == "IMM" and instruction[2] == ord("#")
                            for instruction in ir))
        self.valid("fn quote():\n    return '''\nfn slash():\n    return '\\'\n")

    def test_compatibility_identifiers_are_normalized_consistently(self):
        ir = self.valid('xK = "a"\nprint xK\nfn suffixK(inputK):\n'
                        '    localK = inputK\n    return localK\nprint suffixK("b")')
        self.assertTrue(any(instruction[:2] == ("FUNC", "suffixK") for instruction in ir))

    def test_returns_and_signed_minimum_literal(self):
        ir = self.valid(f"fn empty():\n    return\nfn minimum():\n    return {I64_MIN}\n")
        self.assertTrue(any(instruction[0] == "IMM" and instruction[2] == I64_MIN
                            for instruction in ir))
        self.invalid("return 1", 1, "only valid inside a function")

    def test_function_and_call_errors_keep_original_locations(self):
        cases = (
            ("fn len(value):\n    return value", 1, "reserved"),
            ("fn f(value, value):\n    return value", 1, "duplicate parameter"),
            ("fn f():\n    return 1\nfn f():\n    return 2", 3, "duplicate function"),
            ("fn outer():\n    fn inner():\n        return 0", 2, "top-level"),
            ("fn empty():\nprint 0", 1, "indented statement"),
            ("fn f():\n    return 1\nprint f(2)", 3, "expects 0 arguments"),
            ("print len()", 1, "expects 1 arguments"),
            ("print unknown(1)", 1, "unknown function"),
            ("fn f():\n    goto missing\nmissing:\nprint f()", 2, "unknown label"),
        )
        for source, line, message in cases:
            with self.subTest(source=source):
                self.invalid(source, line, message)

    def test_ast_subset_rejects_python_execution_and_unsupported_values(self):
        for expression in ("__import__('os')", "open('x')", "(lambda: 1)()",
                           "html.other(1)", "html.escape('a', quote=False)",
                           "str(*[1])", "1.5", "True", "None", "{'x': 1}",
                           "[x for x in range(3)]", "(1 if 1 else 0)", "'long'", 'f"{1}"'):
            with self.subTest(expression=expression), self.assertRaises(NoxSyntaxError):
                self.valid("print " + expression)
        self.invalid('print "\\ud800"', 1, "valid Unicode")

    def test_deep_expressions_fail_with_a_located_source_error(self):
        self.invalid("# comment\nvalue = " + " + ".join("1" for _ in range(1500)),
                     2, "expression too complex")

    @unittest.skipUnless(find_compiler(), "no C compiler available")
    def test_nested_native_calls_and_legacy_special_character_returns(self):
        source = """fn add(left, right):
    return left + right
fn quote():
    return '''
print add(add(1, 2), add(3, 4))
putc quote()
putc 10
"""
        ir = self.valid(source, nregs=3)
        with tempfile.TemporaryDirectory() as directory:
            binary = os.path.join(directory, "program")
            compile_c(emit_c(ir, 3, 64), os.path.join(directory, "program.c"), binary)
            result = subprocess.run([binary], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"10'\n")
