"""JSON expression lowering and static native MCP tool registrations."""
import json
import unittest

from nox import NoxSyntaxError, parse
from nox.codec import deserialize, serialize
from nox.isa import JSON_OPCODES, validate_ir


class TestMCPFrontend(unittest.TestCase):
    def valid(self, source, nregs=32):
        ir = parse(source, nregs=nregs, filename="server.nox")
        validate_ir(ir, nregs, 64)
        self.assertEqual(deserialize(serialize(ir, nregs, 64)), (ir, nregs, 64))
        return ir

    def invalid(self, source, line, message):
        with self.assertRaises(NoxSyntaxError) as raised:
            parse(source, nregs=32, filename="server.nox")
        error = raised.exception
        self.assertEqual(error.filename, "server.nox")
        self.assertEqual(error.lineno, line)
        self.assertEqual(error.text, source.splitlines()[line - 1])
        self.assertIn(message, str(error))

    def test_json_builtin_expressions_lower_to_all_json_opcodes(self):
        ir = self.valid('text = json.stringify("é\\0")\n'
                        'fragment = json.get(text, "field")\n'
                        'item = json.at(fragment, 0)\n'
                        'print json.string(item)\nprint json.int(item)\nprint json.valid(text)')
        self.assertEqual({instruction[0] for instruction in ir} & JSON_OPCODES, JSON_OPCODES)
        self.assertEqual(serialize(ir, 32, 64)[4], 5)

    def test_json_builtins_work_inside_user_functions_and_nested_expressions(self):
        ir = self.valid('fn extract(text):\n    return json.int(json.get(text, "number"))\n'
                        'print extract("{\\"number\\":42}")')
        self.assertTrue(any(instruction[0] == "JGET" for instruction in ir))
        self.assertTrue(any(instruction[0] == "JINT" for instruction in ir))

    def test_forward_registration_derives_parameters_and_has_no_runtime_argument_values(self):
        ir = self.valid('mcp.tool("saludar", saludar, "Saluda por nombre")\n'
                        'mcp.serve_stdio()\nfn saludar(nombre):\n    return "Hola, " + nombre')
        registration = ir[0]
        self.assertEqual(registration[0:2], ("MCPTOOL", "saludar"))
        self.assertEqual(ir[registration[2]][:2], ("FUNC", "saludar"))
        self.assertEqual(json.loads(registration[3]), ["nombre"])
        self.assertEqual(registration[4], "Saluda por nombre")
        self.assertEqual(ir[1], ("MCPSERVE",))

    def test_default_description_zero_arguments_and_normalized_parameter_names(self):
        ir = self.valid('fn ping():\n    return "pong"\n'
                        'fn echoK(valueK):\n    return valueK\n'
                        'mcp.tool("ping", ping)\nmcp.tool("echo", echoK)\nmcp.serve_stdio()')
        registrations = [instruction for instruction in ir if instruction[0] == "MCPTOOL"]
        self.assertEqual(registrations[0][3:], ("[]", ""))
        self.assertEqual(registrations[1][3:], ('["valueK"]', ""))

    def test_metadata_literals_do_not_reserve_registers(self):
        self.valid('fn answer():\n    return 42\n'
                   'mcp.tool("r255", answer, "r999")\nmcp.serve_stdio()', nregs=1)

    def test_module_names_cannot_be_shadowed(self):
        for name in ("json", "mcp", "jsｏn"):
            for source in (f"{name} = 1", f"fn {name}():\n    return 1",
                           f"fn f({name}):\n    return 1"):
                with self.subTest(source=source):
                    self.invalid(source, 1, "reserved")

    def test_json_builtin_arity_and_call_subset_are_checked(self):
        cases = (("json.stringify", "", 1), ("json.get", '"{}"', 2),
                 ("json.at", '"[]", 0, 1', 2), ("json.string", "", 1),
                 ("json.int", '"1", 2', 1), ("json.valid", "", 1))
        for name, arguments, arity in cases:
            with self.subTest(name=name):
                self.invalid(f"print {name}({arguments})", 1, f"expects {arity} arguments")
        self.invalid('print json.get("{}", key="x")', 1, "positional arguments only")
        self.invalid('print json.loads("{}")', 1, "unknown function")

    def test_mcp_operations_are_standalone_and_main_only(self):
        function = "fn target():\n    return 0\n"
        for statement in ('value = mcp.tool("tool", target)', "print mcp.serve_stdio()"):
            with self.subTest(statement=statement):
                self.invalid(function + statement, 3, "standalone main-scope statement")
        self.invalid('fn target():\n    mcp.serve_stdio()\n', 2, "main scope")
        self.invalid('fn target():\n    mcp.tool("tool", target)\n', 2, "main scope")

    def test_registration_requires_literal_metadata_and_a_function_reference(self):
        function = "fn target():\n    return 0\n"
        cases = (
            ('mcp.tool("tool", "target")', "declared function"),
            ('mcp.tool("tool", missing)', "declared function"),
            ('mcp.tool("tool", target())', "declared function"),
            ('mcp.tool(str(1), target)', "string literals"),
            ('mcp.tool("tool", target, str(1))', "string literals"),
            ('mcp.tool("tool")', "expects 2 or 3 arguments"),
            ('mcp.tool("tool", target, description="x")', "positional arguments only"),
            ('mcp.serve_stdio(1)', "expects 0 arguments"),
            ('mcp.unknown()', "unknown MCP operation"),
        )
        for statement, message in cases:
            with self.subTest(statement=statement):
                self.invalid(function + statement, 3, message)

    def test_invalid_registration_names_descriptions_and_duplicates_are_located(self):
        function = "fn target():\n    return 0\n"
        for name in ("", "a" * 129, "bad name", "é", "has\x00suffix"):
            with self.subTest(name=repr(name)):
                self.invalid(function + f"mcp.tool({json.dumps(name)}, target)",
                             3, "MCP tool name")
        self.invalid(function + 'mcp.tool("tool", target, "bad\\0description")', 3, "NUL")
        self.invalid(function + 'mcp.tool("tool", target, "\\ud800")', 3, "encode")
        self.invalid(function + 'mcp.tool("tool", target)\nmcp.tool("tool", target)',
                     4, "duplicate MCP tool name")
