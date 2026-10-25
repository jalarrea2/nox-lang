"""Carrier validation, legacy compatibility, and deterministic malformed inputs."""
import hashlib
import os
import struct
import unittest
from random import Random

from nox import NoxIntegrityError, artifact_to_ir, ir_to_artifact, parse
from nox.codec import BRAILLE0, MAGIC, decode, deserialize, encode, serialize

SEED = "compatibility-key"
PROGRAM = [("IMM", 0, -7), ("IMM", 1, 9), ("ADD", 2, 0, 1), ("OUTN", 2), ("HALT",)]


class TestStrictCarrier(unittest.TestCase):
    def setUp(self):
        self.artifact = ir_to_artifact(PROGRAM, SEED, nregs=3, memsize=8)

    def test_existing_encoder_output_is_unchanged(self):
        digest = hashlib.sha256(self.artifact.encode("utf-8")).hexdigest()
        self.assertEqual(digest, "f8e55f4c13b9cf92e05743788e56d2ff29ae14944b59837eb9851b18e7196083")

    def test_ascii_layout_whitespace_preserves_payload(self):
        for separator in (" ", "\t", "\r", "\n", "\r\n", " \t\r\n"):
            with self.subTest(separator=repr(separator)):
                wrapped = separator + separator.join(self.artifact) + separator
                self.assertEqual(artifact_to_ir(wrapped, SEED), (PROGRAM, 3, 8))

    def test_insertions_outside_the_carrier_are_rejected(self):
        invalid = ("A", "#", "\x00", "\x1b", "\x7f", "\v", "\f", "\u00a0",
                   "\u200b", "\u2028", "\u202e", "\ufeff", "\u27ff", "\u2900", "\U0001f600")
        for char in invalid:
            for index in (0, len(self.artifact) // 2, len(self.artifact)):
                with self.subTest(codepoint=ord(char), index=index):
                    text = self.artifact[:index] + char + self.artifact[index:]
                    with self.assertRaises(NoxIntegrityError) as raised:
                        decode(text, SEED)
                    self.assertEqual(
                        str(raised.exception),
                        f"line 1, column {index + 1}: "
                        f"invalid artifact character U+{ord(char):04X}",
                    )

    def test_diagnostic_tracks_lines_and_unicode_character_columns(self):
        for newline in ("\r", "\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                text = self.artifact[:4] + newline + "\t " + self.artifact[4:6] + "\x1b"
                with self.assertRaisesRegex(
                    NoxIntegrityError,
                    "^line 2, column 5: invalid artifact character U\\+001B$",
                ):
                    decode(text, SEED)

    def test_empty_and_whitespace_only_artifacts_are_rejected(self):
        for text in ("", " \t\r\n", chr(BRAILLE0) * 15):
            with self.subTest(text=repr(text)):
                with self.assertRaisesRegex(NoxIntegrityError, "too short"):
                    decode(text, SEED)

    def test_all_bundled_artifacts_remain_readable_with_layout(self):
        examples = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")
        for name in ("cat", "fib", "turnstile", "parity", "div3"):
            with self.subTest(name=name):
                with open(os.path.join(examples, name + ".nxa"), encoding="utf-8") as source:
                    artifact = source.read()
                with open(os.path.join(examples, name + ".nox"), encoding="utf-8") as source:
                    expected = parse(source.read())
                self.assertEqual(artifact_to_ir(artifact, "demo")[0], expected)
                wrapped = "\r\n".join(artifact[i:i + 20] for i in range(0, len(artifact), 20))
                self.assertEqual(artifact_to_ir(wrapped, "demo")[0], expected)


class TestMalformedCorpus(unittest.TestCase):
    def test_deterministic_authenticated_bytecode_corpus(self):
        """Exercise structural validation after successful authentication."""
        rng = Random(0x4E4F58)
        valid = serialize(PROGRAM, nregs=3, memsize=8)
        corpus = [valid, valid + b"\x00"]
        corpus.extend(valid[:cut] for cut in range(len(valid)))
        for index in range(len(valid)):
            altered = bytearray(valid)
            altered[index] ^= rng.randrange(1, 256)
            corpus.append(bytes(altered))
        for _ in range(300):
            version = rng.choice((2, 3))
            body = bytes(rng.randrange(256) for _ in range(rng.randrange(65)))
            header = struct.pack(
                "<BBII" if version == 2 else "<BHII", version,
                rng.choice((0, 1, 3, 32, 255)), rng.choice((0, 1, 8, 65536)),
                rng.choice((0, 1, len(body), len(body) + 1, 0xFFFFFFFF)),
            )
            corpus.append(MAGIC + header + body)
        accepted = rejected = 0
        for index, data in enumerate(corpus):
            with self.subTest(index=index):
                try:
                    result = artifact_to_ir(encode(data, SEED), SEED)
                except NoxIntegrityError:
                    rejected += 1
                else:
                    accepted += 1
                    ir, nregs, memsize = result
                    self.assertEqual(deserialize(serialize(ir, nregs, memsize)), result)
        self.assertGreater(accepted, 0)
        self.assertGreater(rejected, 300)

    def test_braille_mutations_fail_authentication(self):
        rng = Random(0x425241494C4C45)
        artifact = ir_to_artifact(PROGRAM, SEED, nregs=3, memsize=8)
        for _ in range(100):
            index = rng.randrange(len(artifact))
            glyph = chr(BRAILLE0 + ((ord(artifact[index]) - BRAILLE0) ^ rng.randrange(1, 256)))
            mutated = artifact[:index] + glyph + artifact[index + 1:]
            with self.subTest(index=index, glyph=ord(glyph)):
                with self.assertRaisesRegex(NoxIntegrityError, "integrity check failed"):
                    artifact_to_ir(mutated, SEED)
