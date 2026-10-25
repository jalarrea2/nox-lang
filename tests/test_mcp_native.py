"""Drive one compiled native MCP server through its newline JSON-RPC transport."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from nox.backend_c import emit_c, find_compiler

CC = find_compiler()


def request(identifier, method, parameters=None):
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if parameters is not None:
        message["params"] = parameters
    return message


def initialize(version="2025-11-25"):
    return request("init", "initialize", {
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "native-test", "version": "1"},
    })


READY = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"}]


def tool_call(identifier, name, arguments=None):
    parameters = {"name": name}
    if arguments is not None:
        parameters["arguments"] = arguments
    return request(identifier, "tools/call", parameters)


def fixture_ir():
    declarations = [
        ("greet", (0,), ["name"], [
            ("STR", 1, "Hola, "), ("ADD", 2, 1, 0), ("RET", 2),
        ]),
        ("echo", (0,), ["value"], [("RET", 0)]),
        ("bad", (), [], [("STR", 0, "x"), ("IMM", 1, 1), ("SUB", 2, 0, 1), ("RET", 2)]),
        ("halt", (), [], [("HALT",)]),
        ("loop", (), [], [("LABEL", "forever"), ("JMP", "forever")]),
        ("memory", (), [], [
            ("IMM", 0, 0), ("LD", 1, 0), ("IMM", 2, 1),
            ("ADD", 1, 1, 2), ("ST", 0, 1), ("RET", 1),
        ]),
        ("noisy", (), [], [("STR", 0, "tool-log"), ("OUTN", 0), ("IMM", 0, 7), ("RET", 0)]),
        ("read", (), [], [("IN", 0), ("RET", 0)]),
    ]
    instructions = [("STR", 0, "startup-log"), ("OUTN", 0)]
    for name, _, parameters, _ in declarations:
        instructions.append(("MCPTOOL", name, name, json.dumps(parameters), "Description " + name))
    instructions.append(("MCPSERVE",))
    for name, parameters, _, body in declarations:
        instructions += [("FUNC", name, parameters), *body, ("ENDFUNC",)]
    labels, ir = {}, []
    for instruction in instructions:
        if instruction[0] == "LABEL":
            labels[instruction[1]] = len(ir)
        else:
            if instruction[0] == "FUNC":
                labels[instruction[1]] = len(ir)
            ir.append(instruction)
    for index, instruction in enumerate(ir):
        if instruction[0] in ("MCPTOOL", "JMP"):
            position = 2 if instruction[0] == "MCPTOOL" else 1
            mutable = list(instruction)
            mutable[position] = labels[mutable[position]]
            ir[index] = tuple(mutable)
    return ir


@unittest.skipUnless(CC and os.name == "posix", "native MCP requires a POSIX C compiler")
class TestMCPNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="nox-mcp-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = Path(cls.directory.name) / "server"
        source = Path(cls.directory.name) / "server.c"
        source.write_text(emit_c(fixture_ir(), 32, 8), encoding="utf-8")
        flags = ["-O1", "-std=c11", "-pedantic-errors", "-DNOX_MCP_TIMEOUT_MS=100",
                 "-DNOX_MCP_OUTPUT_LIMIT=8192", "-DNOX_STRING_LIMIT=65536"]
        if os.environ.get("NOX_TEST_SANITIZERS") == "1":
            flags += ["-fsanitize=undefined", "-fno-sanitize-recover=all"]
        result = subprocess.run([CC, *flags, str(source), "-o", str(cls.binary)],
                                capture_output=True, timeout=90)
        if result.returncode:
            raise AssertionError(result.stderr.decode(errors="replace"))

    def exchange(self, messages, raw=False):
        if raw:
            data = messages
        else:
            data = b"".join(json.dumps(item, ensure_ascii=False).encode() + b"\n"
                            for item in messages)
        environment = os.environ.copy()
        environment["PATH"] = ""
        result = subprocess.run([str(self.binary)], input=data, capture_output=True,
                                timeout=10, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        for reply in replies:
            self.assertEqual(reply["jsonrpc"], "2.0")
        return replies, result.stderr

    def test_lifecycle_and_derived_schemas(self):
        replies, diagnostics = self.exchange([
            request(0, "ping"), request(1, "tools/list"), initialize(),
            request(2, "tools/list"), READY[1], request(3, "tools/list"),
        ])
        self.assertEqual(replies[0]["result"], {})
        self.assertEqual(replies[1]["error"]["code"], -32002)
        self.assertEqual(replies[2]["result"]["serverInfo"], {"name": "nox", "version": "0.5.0"})
        self.assertEqual(replies[2]["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(replies[3]["error"]["code"], -32002)
        tools = {tool["name"]: tool for tool in replies[4]["result"]["tools"]}
        self.assertEqual(len(tools), 8)
        self.assertEqual(tools["greet"]["description"], "Description greet")
        schema = tools["greet"]["inputSchema"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["name"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["name"]["type"], ["integer", "string"])
        self.assertEqual(schema["properties"]["name"]["minimum"], -(1 << 63))
        self.assertEqual(schema["properties"]["name"]["maximum"], (1 << 63) - 1)
        self.assertEqual(tools["halt"]["inputSchema"]["properties"], {})
        self.assertIn(b"startup-log", diagnostics)

    def test_protocol_negotiation(self):
        for requested in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "future"):
            with self.subTest(version=requested):
                replies, _ = self.exchange([initialize(requested)])
                self.assertEqual(replies[0]["result"]["protocolVersion"],
                                 "2025-11-25" if requested == "future" else requested)

    def test_unicode_nul_and_i64_tool_results(self):
        values = ["á\x00<&🙂", -(1 << 63), (1 << 63) - 1, ""]
        replies, _ = self.exchange(READY + [
            tool_call(index, "echo", {"value": value}) for index, value in enumerate(values)
        ] + [tool_call("hello\x00id", "greet", {"name": "NOX"})])
        for response, expected in zip(replies[1:], values + ["Hola, NOX"]):
            self.assertFalse(response["result"]["isError"])
            self.assertEqual(response["result"]["content"],
                             [{"type": "text", "text": str(expected)}])
        self.assertEqual(replies[-1]["id"], "hello\x00id")

    def test_callback_errors_timeout_halt_and_output_limit_are_isolated(self):
        started = time.monotonic()
        replies, _ = self.exchange(READY + [
            tool_call(1, "bad"), tool_call(2, "halt"), tool_call(3, "loop"),
            tool_call(4, "echo", {"value": "x" * 8192}), request(5, "ping"),
            tool_call(6, "greet", {"name": "still alive"}),
        ])
        self.assertLess(time.monotonic() - started, 5)
        for reply in replies[1:5]:
            self.assertTrue(reply["result"]["isError"])
        self.assertIn("timed out", replies[3]["result"]["content"][0]["text"])
        self.assertEqual(replies[5]["result"], {})
        self.assertEqual(replies[6]["result"]["content"][0]["text"], "Hola, still alive")

    def test_child_memory_does_not_persist_and_stdio_is_reserved(self):
        replies, diagnostics = self.exchange(READY + [
            tool_call(1, "memory"), tool_call(2, "memory", {}), tool_call(3, "noisy"),
            tool_call(4, "read"), request(5, "ping"),
        ])
        self.assertEqual([reply["result"]["content"][0]["text"] for reply in replies[1:5]],
                         ["1", "1", "7", "-1"])
        self.assertIn(b"tool-log", diagnostics)
        self.assertEqual(replies[5]["result"], {})

    def test_invalid_envelopes_and_parse_errors_recover(self):
        raw = b'not-json\n[]\nnull\n{"jsonrpc":"1.0","id":1,"method":"ping"}\n'
        for identifier in (True, None, {}, []):
            raw += json.dumps(request(identifier, "ping")).encode() + b"\n"
        raw += b'{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"method":"ping"}\n'
        raw += json.dumps(request(9, "ping")).encode() + b"\n"
        replies, _ = self.exchange(raw, raw=True)
        self.assertEqual([reply["error"]["code"] for reply in replies[:-1]],
                         [-32700] + [-32600] * 8)
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_strict_tool_argument_types_names_and_parameters(self):
        calls = [tool_call(1, "missing"), tool_call(2, "echo"),
                 tool_call(3, "echo", {"value": "x", "extra": 1}),
                 tool_call(4, "echo\x00suffix", {"value": "x"}),
                 tool_call(5, "echo", {"value\x00suffix": "x"}),
                 request(6, "tools/list", {"cursor": "nonempty"})]
        for value in (True, None, [], {}, 1.5, 1 << 63, -(1 << 63) - 1):
            calls.append(tool_call(len(calls) + 1, "echo", {"value": value}))
        replies, _ = self.exchange(READY + calls)
        for reply in replies[1:]:
            self.assertEqual(reply["error"]["code"], -32602)

    def test_duplicate_arguments_are_invalid_params(self):
        raw = b"".join(json.dumps(item).encode() + b"\n" for item in READY)
        raw += (b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                b'"params":{"name":"echo","arguments":{"value":1,"value":2}}}\n')
        raw += (b'{"jsonrpc":"2.0","id":2,"method":"tools/call",'
                b'"params":{"name":"echo","name":"echo","arguments":{"value":1}}}\n')
        replies, _ = self.exchange(raw, raw=True)
        self.assertEqual([reply["error"]["code"] for reply in replies[1:]], [-32602, -32602])

    def test_notifications_unknown_methods_and_no_final_newline(self):
        messages = [
            {"jsonrpc": "2.0", "method": "notifications/anything"},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "halt"}},
            request("nul", "ping\x00suffix"), request(2, "unknown/method"), request(3, "ping"),
        ]
        raw = b"\n".join(json.dumps(item).encode() for item in messages)
        replies, _ = self.exchange(raw, raw=True)
        self.assertEqual(len(replies), 3)
        self.assertEqual([reply["error"]["code"] for reply in replies[:2]], [-32601, -32601])
        self.assertEqual(replies[-1]["result"], {})

    def test_oversized_line_is_drained_before_next_request(self):
        raw = b"x" * (1024 * 1024 + 1) + b"\n" + json.dumps(request(1, "ping")).encode() + b"\n"
        replies, _ = self.exchange(raw, raw=True)
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"], {})

    def test_parent_string_arena_does_not_grow_per_call(self):
        replies, _ = self.exchange(READY + [
            tool_call(index, "echo", {"value": "x" * 1024}) for index in range(96)
        ])
        self.assertEqual(len(replies), 97)
        for reply in replies[1:]:
            self.assertFalse(reply["result"]["isError"])
            self.assertEqual(reply["result"]["content"][0]["text"], "x" * 1024)
