"""Exercise strict JSON operations in one compiled, standalone native program."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nox.backend_c import emit_c, find_compiler
from nox.isa import I64_MAX, I64_MIN


def cases():
    result = {
        "encode_string": [("STR", 0, "ñ\x00\n\"\\"), ("JENC", 1, 0), ("OUTN", 1)],
        "encode_min": [("IMM", 0, I64_MIN), ("JENC", 1, 0), ("OUTN", 1)],
        "encode_max": [("IMM", 0, I64_MAX), ("JENC", 1, 0), ("OUTN", 1)],
        "object_fragment": [("STR", 0, '{"a\\u0000b":{"x":[true,null,1.25]}}'),
                            ("STR", 1, "a\x00b"), ("JGET", 2, 0, 1), ("OUTN", 2)],
        "array_string": [("STR", 0, '[null,"ñ\\u0000z",false]'), ("IMM", 1, 1),
                         ("JAT", 2, 0, 1), ("JSTR", 3, 2), ("OUTN", 3)],
        "int_min": [("STR", 0, str(I64_MIN)), ("JINT", 1, 0), ("OUTN", 1)],
        "int_max": [("STR", 0, str(I64_MAX)), ("JINT", 1, 0), ("OUTN", 1)],
        "depth_boundary": [("STR", 0, "[" * 128 + "0" + "]" * 128),
                           ("JVALID", 1, 0), ("OUTN", 1),
                           ("STR", 0, "[" * 129 + "0" + "]" * 129),
                           ("JVALID", 1, 0), ("OUTN", 1)],
        "input_limit": [("STR", 0, " ")]
                       + [("ADD", 0, 0, 0)] * 20
                       + [("STR", 1, "0"), ("ADD", 0, 1, 0),
                          ("JVALID", 2, 0), ("OUTN", 2)],
        "input_boundary": [("STR", 0, " " * 255)]
                          + [("ADD", 0, 0, 0)] * 12
                          + [("STR", 1, " " * 4095 + "0"), ("ADD", 0, 0, 1),
                             ("JVALID", 2, 0), ("OUTN", 2)],
        "duplicate_field": [("STR", 0, '{"x":1,"x":2}'), ("STR", 1, "x"),
                            ("JGET", 2, 0, 1)],
        "missing_key": [("STR", 0, '{"x":1}'), ("STR", 1, "x\x00"), ("JGET", 2, 0, 1)],
        "negative_index": [("STR", 0, "[1]"), ("IMM", 1, -1), ("JAT", 2, 0, 1)],
        "past_end_index": [("STR", 0, "[1]"), ("IMM", 1, 1), ("JAT", 2, 0, 1)],
        "wrong_container": [("STR", 0, "[1]"), ("STR", 1, "x"), ("JGET", 2, 0, 1)],
        "wrong_string": [("STR", 0, "null"), ("JSTR", 1, 0)],
        "wrong_nox_type": [("IMM", 0, 1), ("JVALID", 1, 0)],
        "invalid_document": [("STR", 0, "[1,]"), ("JSTR", 1, 0)],
    }
    invalid = ("", " ", "[1,]", '{"x":1,}', "/*comment*/1", "NaN", "Infinity",
               "01", "true false", '"\\ud800"', '"raw\x00null"', '{"x":1,"x":2}',
               '{"x":{"z":0,"z":1}}', '[{"x":1,"\\u0078":2}]')
    valid = ("null", "true", "false", "1.25", str(I64_MIN), str(I64_MAX),
             '{"x":[null,true,-1.5]}', '"ñ\\u0000z"')
    result["validity_corpus"] = []
    for text in (*invalid, *valid):
        result["validity_corpus"] += [("STR", 0, text), ("JVALID", 1, 0), ("OUTN", 1)]
    for index, text in enumerate((str(I64_MAX + 1), str(I64_MIN - 1), "1.0", "1e0",
                                  "true", "null", '"1"')):
        result[f"invalid_integer_{index}"] = [("STR", 0, text), ("JINT", 1, 0)]
    return result, len(invalid), len(valid)


@unittest.skipUnless(find_compiler(), "no C compiler available")
class TestNativeJSON(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="nox-json-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.programs, cls.invalid_count, cls.valid_count = cases()
        cls.commands = {name: index + 1 for index, name in enumerate(cls.programs)}
        instructions = [("IN", 31)]
        for name in cls.programs:
            instructions += [("IMM", 30, cls.commands[name]), ("EQ", 29, 31, 30),
                             ("JNZ", 29, name)]
        instructions.append(("HALT",))
        labels = {}
        for name, program in cls.programs.items():
            labels[name] = len(instructions)
            instructions += program + [("HALT",)]
        instructions = [
            ("JNZ", ins[1], labels[ins[2]]) if ins[0] == "JNZ" else ins
            for ins in instructions
        ]
        cls.binary = Path(cls.directory.name) / "program"
        source = Path(cls.directory.name) / "program.c"
        source.write_text(emit_c(instructions, 32, 8), encoding="utf-8")
        flags = ["-O3", "-std=c11", "-pedantic-errors"]
        if os.environ.get("NOX_TEST_SANITIZERS") == "1":
            flags += ["-fsanitize=undefined", "-fno-sanitize-recover=all"]
        compilation = subprocess.run([find_compiler(), *flags, str(source), "-o", str(cls.binary)],
                                     capture_output=True, timeout=90)
        if compilation.returncode:
            raise AssertionError(compilation.stderr.decode())

    def run_case(self, name, status=0):
        result = subprocess.run([str(self.binary)], input=bytes((self.commands[name],)),
                                capture_output=True, timeout=5,
                                env={"PATH": "", "UBSAN_OPTIONS": "symbolize=0"})
        self.assertEqual(result.returncode, status, result.stderr.decode())
        if status == 0:
            self.assertEqual(result.stderr, b"")
        else:
            self.assertIn(b"[nox] runtime error:", result.stderr)
        return result.stdout

    def test_stringify_preserves_unicode_nul_and_i64(self):
        self.assertEqual(json.loads(self.run_case("encode_string")), "ñ\x00\n\"\\")
        self.assertEqual(json.loads(self.run_case("encode_min")), I64_MIN)
        self.assertEqual(json.loads(self.run_case("encode_max")), I64_MAX)

    def test_get_at_preserve_fragments_and_explicit_string_lengths(self):
        self.assertEqual(json.loads(self.run_case("object_fragment")), {"x": [True, None, 1.25]})
        self.assertEqual(self.run_case("array_string"), "ñ\x00z".encode())

    def test_integer_decoding_is_exact_and_rejects_other_json_kinds(self):
        self.assertEqual(self.run_case("int_min"), str(I64_MIN).encode())
        self.assertEqual(self.run_case("int_max"), str(I64_MAX).encode())
        for index in range(7):
            with self.subTest(index=index):
                self.run_case(f"invalid_integer_{index}", status=4)

    def test_strict_syntax_and_duplicate_key_policy(self):
        expected = b"0" * self.invalid_count + b"1" * self.valid_count
        self.assertEqual(self.run_case("validity_corpus"), expected)
        self.run_case("duplicate_field", status=4)

    def test_input_and_depth_limits(self):
        self.assertEqual(self.run_case("depth_boundary"), b"10")
        self.assertEqual(self.run_case("input_boundary"), b"1")
        self.assertEqual(self.run_case("input_limit"), b"0")

    def test_schema_type_lookup_and_parse_failures_are_runtime_errors(self):
        for name in ("missing_key", "negative_index", "past_end_index", "wrong_container",
                     "wrong_string", "wrong_nox_type", "invalid_document"):
            with self.subTest(name=name):
                self.run_case(name, status=4)
