"""Explicit compiler configuration must never silently select another compiler."""
import os
import unittest
from unittest.mock import patch

from nox.backend_c import find_compiler


class TestCompilerSelection(unittest.TestCase):
    def test_explicit_compiler_is_selected(self):
        with patch.dict(os.environ, {"CC": "custom-cc"}):
            with patch("nox.backend_c.shutil.which", return_value="/tools/custom-cc") as which:
                self.assertEqual(find_compiler(), "/tools/custom-cc")
                which.assert_called_once_with("custom-cc")

    def test_missing_explicit_compiler_does_not_fall_back(self):
        with patch.dict(os.environ, {"CC": "missing-cc"}):
            with patch("nox.backend_c.shutil.which", return_value=None) as which:
                with self.assertRaisesRegex(RuntimeError, "specified by CC not found"):
                    find_compiler()
                which.assert_called_once_with("missing-cc")
