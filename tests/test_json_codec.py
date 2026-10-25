"""Versioned JSON/MCP operands, metadata validation, and legacy wire fixtures."""
import json
import struct
import unittest
from random import Random

from nox import NoxIntegrityError, artifact_to_ir, ir_to_artifact
from nox.codec import MAGIC, VERSION, decode, deserialize, encode, serialize
from nox.isa import (
    EXTENDED_OPCODES,
    JSON_OPCODES,
    MCP_OPCODES,
    OPCODES,
    V4_OPCODES,
    V5_OPCODES,
    validate_ir,
)

PROGRAM = [
    ("STR", 0, "{}"), ("STR", 1, "name"), ("IMM", 2, 0),
    ("JENC", 3, 1), ("JGET", 3, 0, 1), ("JAT", 3, 0, 2),
    ("JSTR", 3, 1), ("JINT", 3, 1), ("JVALID", 3, 0),
    ("MCPTOOL", "echo", 12, '["value"]', "Return a value"),
    ("MCPSERVE",), ("HALT",), ("FUNC", "echo", (0,)), ("RET", 0), ("ENDFUNC",),
]


def metadata_program(name="echo", parameters='["value"]', description="Return a value"):
    return [("MCPTOOL", name, 1, parameters, description),
            ("FUNC", "echo", (0,)), ("RET", 0), ("ENDFUNC",)]


class TestJSONMCPCodec(unittest.TestCase):
    def test_opcode_groups_and_v5_round_trip(self):
        self.assertEqual(VERSION, 5)
        self.assertEqual(V5_OPCODES, JSON_OPCODES | MCP_OPCODES)
        self.assertEqual(EXTENDED_OPCODES, V4_OPCODES | V5_OPCODES)
        self.assertEqual({instruction[0] for instruction in PROGRAM} & V5_OPCODES, V5_OPCODES)
        artifact = ir_to_artifact(PROGRAM, "v5-key", 4, 8)
        self.assertEqual(decode(artifact, "v5-key")[4], 5)
        self.assertEqual(artifact_to_ir(artifact, "v5-key"), (PROGRAM, 4, 8))

    def test_older_programs_keep_their_original_container_versions(self):
        legacy = [("IMM", 0, 42), ("OUTN", 0)]
        legacy_wire = MAGIC + struct.pack("<BHII", 3, 1, 8, 2)
        legacy_wire += b"\x01\x00" + struct.pack("<q", 42) + b"\x62\x00"
        self.assertEqual(serialize(legacy, 1, 8), legacy_wire)
        extended = [("STR", 0, "é\x00"), ("FUNC", "id", (0,)), ("RET", 0), ("ENDFUNC",)]
        extended_wire = MAGIC + struct.pack("<BHII", 4, 1, 8, 4)
        extended_wire += b"\x84\x00\x03\x00\x00\x00" + "é\x00".encode()
        extended_wire += b"\x80\x02\x00\x00\x00id\x01\x00\x00\x83\x00\x81"
        self.assertEqual(serialize(extended, 1, 8), extended_wire)
        self.assertEqual(deserialize(extended_wire), (extended, 1, 8))
        newer_container = extended_wire[:4] + b"\x05" + extended_wire[5:]
        self.assertEqual(deserialize(newer_container), (extended, 1, 8))

    def test_v5_instructions_are_rejected_before_operand_parsing_in_older_containers(self):
        for version in (2, 3, 4):
            for name in V5_OPCODES:
                with self.subTest(version=version, name=name):
                    fmt = "<BBII" if version == 2 else "<BHII"
                    wire = MAGIC + struct.pack(fmt, version, 4, 8, 1) + bytes((OPCODES[name][0],))
                    with self.assertRaisesRegex(NoxIntegrityError, "requires artifact version 5"):
                        deserialize(wire)

    def test_every_authenticated_v5_truncation_is_rejected(self):
        wire = serialize(PROGRAM, 4, 8)
        for cut in range(len(wire)):
            with self.subTest(cut=cut), self.assertRaises(NoxIntegrityError):
                artifact_to_ir(encode(wire[:cut], "key"), "key")

    def test_v5_bytecode_mutations_have_defined_failures(self):
        rng = Random(0x5635)
        original = serialize(PROGRAM, 4, 8)
        rejected = 0
        for _ in range(200):
            wire = bytearray(original)
            wire[rng.randrange(len(wire))] ^= rng.randrange(1, 256)
            try:
                result = deserialize(bytes(wire))
            except NoxIntegrityError:
                rejected += 1
            else:
                self.assertEqual(deserialize(serialize(*result)), result)
        self.assertGreater(rejected, 80)


class TestMCPMetadataValidation(unittest.TestCase):
    def test_names_are_bounded_ascii_and_metadata_names_are_utf8(self):
        for name in ("a", "_.-09AZaz", "a" * 128):
            with self.subTest(name=name):
                validate_ir(metadata_program(name, '["nombŕe"]'), 1, 8)
        ir = metadata_program(description="Saluda a María 🙂")
        self.assertEqual(deserialize(serialize(ir, 1, 8)), (ir, 1, 8))

    def test_invalid_names_descriptions_and_parameter_metadata_are_rejected(self):
        cases = [metadata_program(name=name) for name in
                 ("", "a" * 129, "with space", "é", "tool/child", "name\x00suffix")]
        cases += [metadata_program(parameters=parameters) for parameters in (
            "not json", "{}", "1", "null", "[1]", '["x","x"]',
            '["x\\u0000suffix"]', '["\\ud800"]', "[]", '["x","y"]',
            json.dumps([str(index) for index in range(257)]), "[" * 1500 + "]" * 1500,
        )]
        cases += [metadata_program(description="has\x00NUL"),
                  metadata_program(description="\ud800")]
        for ir in cases:
            with self.subTest(prefix=repr(ir)[:120]), self.assertRaises(ValueError):
                validate_ir(ir, 1, 8)

    def test_registration_target_and_scope_are_checked(self):
        cases = [
            [("MCPTOOL", "echo", 1, "[]", ""), ("HALT",)],
            [("FUNC", "echo", ()), ("MCPTOOL", "echo", 0, "[]", ""), ("ENDFUNC",)],
            [("FUNC", "echo", ()), ("MCPSERVE",), ("ENDFUNC",)],
            [("MCPTOOL", "echo", 2, "[]", ""), ("MCPTOOL", "echo", 2, "[]", ""),
             ("FUNC", "echo", ()), ("ENDFUNC",)],
        ]
        for ir in cases:
            with self.subTest(ir=ir), self.assertRaises(ValueError):
                validate_ir(ir, 1, 8)

    def test_authenticated_metadata_failures_become_integrity_errors(self):
        wire = serialize(metadata_program(), 1, 8)
        for old, replacement in ((b"echo", b"bad!"), (b"value", b"v\x00lue")):
            with self.subTest(replacement=replacement), self.assertRaises(NoxIntegrityError):
                artifact_to_ir(encode(wire.replace(old, replacement, 1), "key"), "key")

    def test_json_register_operands_and_mcp_operand_shapes_are_validated(self):
        for name in JSON_OPCODES:
            _, signature = OPCODES[name]
            for operands in ([-1] * len(signature), [True] * len(signature), [1] * len(signature)):
                with self.subTest(name=name, operands=operands), self.assertRaises(ValueError):
                    validate_ir([(name, *operands)], 1, 8)
        for instruction in (("MCPSERVE", 0), ("MCPTOOL", "name", True, "[]", ""),
                            ("MCPTOOL", b"name", 1, "[]", "")):
            with self.subTest(instruction=instruction), self.assertRaises(ValueError):
                validate_ir([instruction, ("FUNC", "f", ()), ("ENDFUNC",)], 1, 8)
