"""Native function/string IR validation and version 4 encoding regressions."""
import struct
import unittest
from random import Random

from nox import NoxIntegrityError, artifact_to_ir, ir_to_artifact
from nox.codec import MAGIC, decode, deserialize, encode, serialize
from nox.isa import OPCODES, V4_OPCODES, analyze_functions, validate_ir


def header(body, count=1, version=4, nregs=8):
    fmt = "<BBII" if version == 2 else "<BHII"
    return MAGIC + struct.pack(fmt, version, nregs, 8, count) + body


FUNCTION_PROGRAM = [
    ("CALL", 0, 3, (1, 2)),
    ("STR", 1, "h\u00e9llo \U0001f30e\x00"),
    ("HALT",),
    ("FUNC", "merge", (0, 1)),
    ("STR", 2, ""),
    ("SLEN", 3, 0),
    ("TOSTR", 4, 3),
    ("HESC", 5, 1),
    ("RET", 5),
    ("ENDFUNC",),
]


class TestExtendedCodec(unittest.TestCase):
    def test_all_extended_opcodes_round_trip(self):
        self.assertEqual({ins[0] for ins in FUNCTION_PROGRAM} & V4_OPCODES, V4_OPCODES)
        artifact = ir_to_artifact(FUNCTION_PROGRAM, "key", nregs=8, memsize=8)
        self.assertEqual(decode(artifact, "key")[4], 4)
        self.assertEqual(artifact_to_ir(artifact, "key"), (FUNCTION_PROGRAM, 8, 8))

    def test_unicode_strings_use_utf8_byte_length_and_allow_nul(self):
        text = "\u00e9\x00\U0001f30e"
        data = serialize([("STR", 0, text)], nregs=1, memsize=1)
        self.assertEqual(data[15:], b"\x84\x00\x07\x00\x00\x00" + text.encode("utf-8"))
        self.assertEqual(deserialize(data), ([("STR", 0, text)], 1, 1))

    def test_register_arrays_normalize_to_tuples(self):
        ir = [("CALL", 0, 1, [0, 1]), ("FUNC", "copy", [0, 1]),
              ("RET", 0), ("ENDFUNC",)]
        decoded, _, _ = deserialize(serialize(ir, 2, 1))
        self.assertEqual(decoded[0], ("CALL", 0, 1, (0, 1)))
        self.assertEqual(decoded[1], ("FUNC", "copy", (0, 1)))

    def test_register_arrays_support_all_256_registers(self):
        registers = tuple(range(256))
        ir = [("CALL", 255, 1, registers), ("FUNC", "all", registers), ("ENDFUNC",)]
        self.assertEqual(deserialize(serialize(ir, 256, 1)), (ir, 256, 1))

    def test_legacy_programs_still_write_v3(self):
        for ir in ([], [("HALT",)], [("IMM", 255, 1)]):
            with self.subTest(ir=ir):
                self.assertEqual(serialize(ir, 256, 1)[4], 3)

    def test_extended_instructions_are_rejected_in_legacy_formats(self):
        for version in (2, 3):
            for name in V4_OPCODES:
                with self.subTest(version=version, name=name):
                    body = bytes((OPCODES[name][0],))
                    with self.assertRaisesRegex(NoxIntegrityError, "requires artifact version 4"):
                        deserialize(header(body, version=version))

    def test_every_v4_truncation_is_an_integrity_error(self):
        data = serialize(FUNCTION_PROGRAM, 8, 8)
        for cut in range(len(data)):
            with self.subTest(cut=cut):
                with self.assertRaises(NoxIntegrityError):
                    artifact_to_ir(encode(data[:cut], "key"), "key")

    def test_malformed_variable_operands_are_rejected(self):
        cases = [
            b"\x84\x00" + struct.pack("<I", 0xFFFFFFFF),
            b"\x84\x00" + struct.pack("<I", 1) + b"\xff",
            b"\x84\x00" + struct.pack("<I", 3) + b"\xed\xa0\x80",
            b"\x80" + struct.pack("<I", 0) + struct.pack("<H", 257),
            b"\x80" + struct.pack("<I", 0) + struct.pack("<H", 2) + b"\x00",
            b"\x82\x00" + struct.pack("<I", 0) + struct.pack("<H", 65535),
        ]
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(NoxIntegrityError):
                    artifact_to_ir(encode(header(body), "key"), "key")

    def test_mutated_v4_bytecode_never_leaks_structural_exceptions(self):
        rng = Random(0x5634)
        valid = serialize(FUNCTION_PROGRAM, 8, 8)
        rejected = 0
        for _ in range(300):
            data = bytearray(valid)
            index = rng.randrange(len(data))
            data[index] ^= rng.randrange(1, 256)
            with self.subTest(index=index):
                try:
                    result = artifact_to_ir(encode(bytes(data), "key"), "key")
                except NoxIntegrityError:
                    rejected += 1
                else:
                    self.assertEqual(deserialize(serialize(*result)), result)
        self.assertGreater(rejected, 100)


class TestFunctionValidation(unittest.TestCase):
    def test_function_ownership_and_end_targets(self):
        ir = [("JMP", 5), ("FUNC", "identity", (0,)), ("JMP", 3),
              ("ENDFUNC",), ("CALL", 0, 1, (1,))]
        validate_ir(ir, 2, 8)
        owners, functions = analyze_functions(ir)
        self.assertEqual(owners, [None, 1, 1, 1, None])
        self.assertEqual(functions, {1: {"name": "identity", "params": (0,), "end": 3}})

    def test_forward_recursive_and_mutually_recursive_calls_are_valid(self):
        ir = [("CALL", 0, 2, (1,)), ("HALT",),
              ("FUNC", "first", (0,)), ("CALL", 0, 7, (0,)),
              ("CALL", 0, 2, (0,)), ("RET", 0), ("ENDFUNC",),
              ("FUNC", "second", (1,)), ("CALL", 1, 2, (1,)), ("ENDFUNC",)]
        validate_ir(ir, 2, 8)
        self.assertEqual(deserialize(serialize(ir, 2, 8)), (ir, 2, 8))

    def test_invalid_function_structure_and_scope(self):
        cases = [
            [("RET", 0)],
            [("ENDFUNC",)],
            [("FUNC", "open", ())],
            [("FUNC", "outer", ()), ("FUNC", "inner", ()), ("ENDFUNC",), ("ENDFUNC",)],
            [("FUNC", "same", ()), ("ENDFUNC",), ("FUNC", "same", ()), ("ENDFUNC",)],
            [("FUNC", "dupe", (0, 0)), ("ENDFUNC",)],
            [("CALL", 0, 1, ()), ("HALT",)],
            [("CALL", 0, 2, ()), ("HALT",)],
            [("CALL", 0, 1, ()), ("FUNC", "args", (0,)), ("ENDFUNC",)],
            [("JMP", 1), ("FUNC", "f", ()), ("ENDFUNC",)],
            [("JMP", 2), ("FUNC", "f", ()), ("ENDFUNC",)],
            [("FUNC", "f", ()), ("JMP", 3), ("ENDFUNC",)],
            [("FUNC", "f", ()), ("JZ", 0, 4), ("ENDFUNC",), ("HALT",)],
            [("FUNC", "f", ()), ("JMP", 4), ("ENDFUNC",),
             ("FUNC", "g", ()), ("ENDFUNC",)],
        ]
        for ir in cases:
            with self.subTest(ir=ir):
                with self.assertRaises(ValueError):
                    validate_ir(ir, 8, 8)

    def test_invalid_extended_operand_shapes(self):
        cases = [
            [("STR", 0, b"bytes")], [("STR", 0, "\ud800")], [("STR", True, "text")],
            [("FUNC", "f", "not a register list"), ("ENDFUNC",)],
            [("FUNC", "f", (True,)), ("ENDFUNC",)],
            [("FUNC", "f", (8,)), ("ENDFUNC",)],
            [("FUNC", "f", tuple(range(257))), ("ENDFUNC",)],
            [("CALL", 0, 1, ("0",)), ("FUNC", "f", (0,)), ("ENDFUNC",)],
            [("SLEN", 0, 8)], [("TOSTR", 0)], [("HESC", 0, -1)],
        ]
        for ir in cases:
            with self.subTest(ir=ir):
                with self.assertRaises(ValueError):
                    serialize(ir, 8, 8)
