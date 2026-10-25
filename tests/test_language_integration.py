"""Public CLI and artifacts must preserve native functions/string behavior."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nox.backend_c import find_compiler

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(find_compiler(), "no C compiler available")
class TestLanguagePipeline(unittest.TestCase):
    def cli(self, *arguments, **kwargs):
        return subprocess.run([sys.executable, "-m", "nox", *map(str, arguments)],
                              capture_output=True, timeout=45, **kwargs)

    def compile_source(self, directory, source):
        path = Path(directory) / "program.nox"
        artifact = Path(directory) / "program.nxa"
        path.write_text(source, encoding="utf-8")
        result = self.cli("compile", path, "-o", artifact, "--seed", "integration-key")
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return artifact

    def run_source(self, source):
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.compile_source(directory, source)
            return self.cli("run", artifact, "--seed", "integration-key")

    def test_native_html_executable_runs_without_python_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.compile_source(
                directory, (ROOT / "examples" / "html.nox").read_text(encoding="utf-8"),
            )
            binary = Path(directory) / "page"
            result = self.cli("build", artifact, "-o", binary, "--seed", "integration-key")
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], env={"PATH": ""},
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                        '<title>NOX native</title></head><body>'
                        '<h1>Hello, María &amp; &lt;agents&gt;</h1></body></html>\n')
            self.assertEqual(result.stdout, expected.encode("utf-8"))

    def test_mutual_recursion_and_local_variables(self):
        source = '''
fn even(n):
    if n == 0 goto yes
    return odd(n - 1)
    yes:
    return 1

fn odd(n):
    if n == 0 goto no
    return even(n - 1)
    no:
    return 0

fn factorial(n):
    if n == 0 goto base
    saved = n
    result = factorial(n - 1)
    return saved * result
    base:
    return 1

saved = "main"
print str(factorial(10)) + ":" + str(even(11)) + ":" + str(odd(11)) + ":" + saved
'''
        result = self.run_source(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"3628800:0:1:main")

    def test_utf8_nul_and_comment_characters_survive_artifact_and_disassembly(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.compile_source(directory, '''
fn decorate(value):
    return "#; " + value + "\\0" + "\\n"

text = decorate("é🙂")
print text
print len(text)
''')
            result = self.cli("run", artifact, "--seed", "integration-key")
            expected = "#; é🙂\0\n".encode()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected + str(len(expected)).encode("ascii"))
            result = self.cli("disasm", artifact, "--seed", "integration-key")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"\\x00", result.stdout)
            self.assertIn(b"FUNC", result.stdout)

    def test_type_errors_propagate_through_public_run(self):
        result = self.run_source('text = "NOX"\nprint text - 1\n')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(b"[nox] runtime error:", result.stderr)

    def test_string_comparisons_with_zero_branch_on_the_comparison_result(self):
        result = self.run_source('''
text = "NOX"
if text == 0 goto wrong
if text != 0 goto right
wrong:
print "wrong"
halt
right:
r0 = "NOX"
if r0 == 0 goto wrong
if r0 != 0 goto done
goto wrong
done:
print "ok"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"ok")

    def test_call_depth_exhaustion_is_a_defined_runtime_error(self):
        result = self.run_source("fn forever():\n    return forever()\n\nprint forever()\n")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(b"depth", result.stderr.lower())

    def test_source_error_keeps_location_and_does_not_create_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.nox"
            artifact = path.with_suffix(".nxa")
            path.write_text('fn greet(name):\n    return "Hi, " + missing\n\nprint greet("x")\n',
                            encoding="utf-8")
            result = self.cli("compile", path, "--seed", "integration-key")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(os.fsencode(path), result.stderr)
            self.assertIn(b"line 2", result.stderr)
            self.assertFalse(artifact.exists())
