"""Regression tests for parser, IR boundaries, artifact versions, and CLI errors."""
import contextlib
import io
import os
import struct
import tempfile
import unittest

from nox import NoxIntegrityError, NoxSyntaxError, artifact_to_ir, emit_c, ir_to_artifact, parse
from nox.__main__ import main
from nox.codec import MAGIC, VERSION, decode, deserialize, encode, serialize
from nox.isa import EXTENDED_OPCODES, I64_MAX, I64_MIN, OPCODES


def bytecode(body, nregs=32, memsize=8, count=1, version=VERSION):
    fmt = "<BBII" if version == 2 else "<BHII"
    return MAGIC + struct.pack(fmt, version, nregs, memsize, count) + body


class TestParserLimits(unittest.TestCase):
    def test_comments_preserve_character_literals(self):
        for char in ("#", ";", "'", " ", "A"):
            for comment in ("", " # comment", " ; comment"):
                with self.subTest(char=char, comment=comment):
                    self.assertEqual(parse(f"r0 = '{char}'{comment}"), [("IMM", 0, ord(char))])
        self.assertEqual(parse("# ignored\n; ignored\nhalt # '"), [("HALT",)])

    def test_i64_limits(self):
        for value in (I64_MIN, I64_MAX):
            self.assertEqual(parse(f"r0 = {value}"), [("IMM", 0, value)])
        for value in (I64_MIN - 1, I64_MAX + 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(NoxSyntaxError, "line 2:.*out of i64 range"):
                    parse(f"# comment\nr0 = {value}")


class TestIRValidation(unittest.TestCase):
    def test_both_ir_consumers_reject_invalid_instructions(self):
        cases = [
            [()], [None], [("NOPE",)], [([],)], [("HALT", 0)], [("IMM", 0)],
            [("IMM", -1, 0)], [("IMM", 32, 0)], [("IMM", 0, I64_MIN - 1)],
            [("IMM", 0, I64_MAX + 1)], [("IMM", 0, 1.0)], [("IMM", True, 0)],
            [("MOV", 0, "1")], [("JMP", -1)], [("JMP", 2)], [("JMP", 1.0)],
        ]
        for ir in cases:
            for consumer in (serialize, emit_c):
                with self.subTest(ir=ir, consumer=consumer.__name__):
                    with self.assertRaises(ValueError):
                        consumer(ir, 32, 8)

    def test_both_ir_consumers_reject_invalid_dimensions(self):
        for nregs, memsize in ((0, 8), (257, 8), (1.0, 8), (True, 8),
                               (32, 0), (32, -1), (32, 1 << 32), (32, 1.0)):
            for consumer in (serialize, emit_c):
                with self.subTest(nregs=nregs, memsize=memsize, consumer=consumer.__name__):
                    with self.assertRaises(ValueError):
                        consumer([], nregs, memsize)

    def test_all_opcodes_round_trip(self):
        legacy = {name: spec for name, spec in OPCODES.items() if name not in EXTENDED_OPCODES}
        values = {"R": 255, "I": I64_MIN, "T": len(legacy)}
        ir = [(name, *(values[kind] for kind in sig)) for name, (_, sig) in legacy.items()]
        self.assertEqual(deserialize(serialize(ir, 256, 1)), (ir, 256, 1))

    def test_empty_program_and_end_target_are_valid(self):
        for ir in ([], [("JMP", 1)]):
            self.assertEqual(deserialize(serialize(ir, 1, 1)), (ir, 1, 1))
            self.assertIn(f"I{len(ir)}:;", emit_c(ir, 1, 1))


class TestBytecodeValidation(unittest.TestCase):
    def test_v3_supports_256_registers(self):
        ir = [("IMM", 255, I64_MAX), ("OUTN", 255)]
        artifact = ir_to_artifact(ir, "key", nregs=256)
        self.assertEqual(decode(artifact, "key")[4], 3)
        self.assertEqual(artifact_to_ir(artifact, "key"), (ir, 256, 65536))

    def test_v2_artifacts_remain_readable(self):
        data = bytecode(b"\x01\x00" + struct.pack("<q", I64_MIN) + b"\x70",
                        nregs=255, count=2, version=2)
        self.assertEqual(artifact_to_ir(encode(data, "key"), "key"),
                         ([("IMM", 0, I64_MIN), ("HALT",)], 255, 8))

    def test_bundled_v2_examples_remain_readable(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for name in ("fib", "cat"):
            with self.subTest(name=name):
                with open(os.path.join(root, "examples", name + ".nxa"), encoding="utf-8") as f:
                    artifact = f.read()
                with open(os.path.join(root, "examples", name + ".nox"), encoding="utf-8") as f:
                    source = f.read()
                self.assertEqual(decode(artifact, "demo")[4], 2)
                self.assertEqual(artifact_to_ir(artifact, "demo")[0], parse(source))

    def test_every_truncation_is_an_integrity_error(self):
        body = b"\x01\x00" + struct.pack("<q", I64_MIN) + b"\x50" + struct.pack("<I", 2)
        for version in (2, 3):
            data = bytecode(body, count=2, version=version)
            for cut in range(len(data)):
                with self.subTest(version=version, cut=cut):
                    with self.assertRaises(NoxIntegrityError):
                        deserialize(data[:cut])

    def test_authenticated_but_invalid_bytecode_is_rejected(self):
        for version in (2, 3):
            cases = [
                bytecode(b"\x70", nregs=0, version=version),
                bytecode(b"\x70", memsize=0, version=version),
                bytecode(b"\x60\xff", nregs=1, version=version),
                bytecode(b"\x50" + struct.pack("<I", 2), version=version),
                bytecode(b"\x70ignored", version=version),
                bytecode(b"\xff", version=version),
                bytecode(b"\x70", count=0, version=version),
                bytecode(b"\x70", count=0xFFFFFFFF, version=version),
            ]
            for data in cases:
                with self.subTest(version=version, data=data):
                    with self.assertRaises(NoxIntegrityError):
                        artifact_to_ir(encode(data, "key"), "key")
        with self.assertRaises(NoxIntegrityError):
            deserialize(bytecode(b"\x70", nregs=257))
        with self.assertRaisesRegex(NoxIntegrityError, "unsupported artifact version"):
            deserialize(MAGIC + b"\xff")


class TestCLIValidation(unittest.TestCase):
    def test_maximum_register_count_compiles(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "program.nox")
            with open(source, "w", encoding="utf-8") as f:
                f.write("r255 = 1\nhalt\n")
            with contextlib.redirect_stderr(io.StringIO()):
                result = main(["compile", source, "--seed", "key", "--regs", "256"])
            self.assertEqual(result, 0)
            with open(os.path.join(directory, "program.nxa"), encoding="utf-8") as f:
                self.assertEqual(artifact_to_ir(f.read(), "key")[1], 256)

    def test_invalid_inputs_return_documented_exit_codes(self):
        cases = [
            ("compile", f"r0 = {I64_MAX + 1}", [], 2, "line 1"),
            ("compile", "halt", ["--mem", "0"], 1, "memsize"),
            ("emit", encode(bytecode(b"\x60"), "key"), [], 3, "truncated"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "program")
            for command, text, options, expected, message in cases:
                with self.subTest(command=command, text=text):
                    with open(source, "w", encoding="utf-8") as f:
                        f.write(text)
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        result = main([command, source, "--seed", "key"] + options)
                    self.assertEqual(result, expected)
                    self.assertIn(message, stderr.getvalue())
