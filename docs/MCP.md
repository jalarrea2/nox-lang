# Native MCP server and JSON

NOX 0.5 compiles MCP tool servers into native executables for Linux and macOS.
Python is used to compile the source and is not used by the running server.
The server implements the standard newline-delimited JSON-RPC stdio transport.
It exposes tools; it does not implement a client, HTTP transport, resources,
prompts, progress, or task extensions. See the
[MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## Registering native functions

```text
fn saludar(nombre):
    return "Hola, " + nombre

fn sumar(a, b):
    return a + b

mcp.tool("saludar", saludar, "Saluda por nombre")
mcp.tool("sumar", sumar, "Suma dos enteros")
mcp.serve_stdio()
```

`mcp.tool` accepts a literal public name, an existing function reference, and an
optional literal description. The name must contain 1..128 ASCII letters,
digits, `_`, `-`, or `.` and must be unique. Registration and `serve_stdio`
are main-scope statements, not expressions. Functions can be declared later
in the source. The compiler derives argument names from the parameters.

Every argument is required and accepts either a string or a signed 64-bit
integer. Extra, missing, duplicate, or incompatible arguments are rejected.
The runtime currently accepts integer JSON tokens, not real/exponent tokens.
A parameterless function accepts an omitted arguments object or `{}`. Return
values are exposed as MCP text content: strings preserve Unicode and NUL,
and integers use their exact decimal representation. Invalid argument types
are protocol parameter errors; runtime errors produce `isError: true`.

## Building and connecting

```bash
python -m nox compile examples/mcp.nox -o server.nxa --seed demo
python -m nox build server.nxa -o nox-mcp --seed demo
```

Configure a compatible agent host to launch the resulting executable directly:

```json
{
  "mcpServers": {
    "nox": {
      "command": "/absolute/path/to/nox-mcp",
      "args": []
    }
  }
}
```

The exact configuration destination depends on the host. The executable does
not need Python or the artifact seed when launched. Rebuild the executable
for a different OS or CPU architecture.

The server negotiates protocol version `2025-11-25`, accepting the compatible
`2025-06-18`, `2025-03-26`, and `2024-11-05` versions when requested. It handles
`initialize`, `notifications/initialized`, `ping`, `tools/list`, and
`tools/call`. Tools become available after initialization. Its advertised
capabilities contain only tools, with no list-change notifications.
[Lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

In a program containing `mcp.serve_stdio`, NOX `print` and `putc` write to
stderr so stdout remains reserved for protocol messages. NOX `read` returns
EOF (-1); tools receive input through arguments. The server exits cleanly on
stdin EOF, and code following `serve_stdio` is not executed.

## Isolation and limits

Tool calls run serially in child processes. Each invocation inherits the
server's state when `serve_stdio` starts; changes to linear memory or other state
inside a call do not persist into subsequent invocations. The child has no
protocol stdin. A tool crash, runtime error, explicit `halt`, oversized result,
or timeout becomes an MCP tool error; the parent server remains available.
This process isolation is not an operating-system sandbox.

Defaults are 64 registered tools, 1 MiB request frames, 1 MiB tool results, and
a five-second wall-clock limit per invocation. String arena and call-depth
limits still apply. Cancellation and concurrent calls are not supported in
this version. Logs use stderr and do not become the tool return value.

## Native JSON helpers

JSON is represented as validated text so objects, arrays, booleans, null, and
real numbers can be preserved without coercion into the two NOX value types.

| Function | Result |
| --- | --- |
| `json.stringify(value)` | JSON text encoding a NOX integer or string |
| `json.get(document, key)` | JSON fragment text for an object field |
| `json.at(document, index)` | JSON fragment text for an array element |
| `json.string(fragment)` | Decoded string; requires a JSON string |
| `json.int(fragment)` | Signed i64; requires an in-range JSON integer |
| `json.valid(document)` | Integer 1 for valid supported JSON, otherwise 0 |

For example:

```text
document = "{\"nombre\":\"María\",\"cuenta\":42}"
name = json.string(json.get(document, "nombre"))
count = json.int(json.get(document, "cuenta"))
print name + ": " + str(count)
```

Missing keys, invalid indexes, invalid JSON, and incompatible conversions
produce runtime errors. JSON operations reject duplicate object keys; `valid`
returns 0 for duplicates. Parsing is strict, with a 1 MiB input bound and a
nesting bound of 128. Text uses explicit lengths for Unicode and embedded NUL.
The native parser is vendored yyjson under the MIT license; source version,
hashes, and license are included under `nox/_vendor/yyjson`.

## Official-client verification

On Python 3.10+, install test tooling and run the official SDK check:

```bash
python -m pip install -e ".[dev,mcp-test]"
python scripts/check_mcp_sdk.py
```

This compiles the example, launches the executable with an empty PATH, and
uses the official MCP client SDK to initialize, discover and call tools. It
checks Unicode/NUL, exact i64 values, HTML, JSON, and recovery after a tool
runtime error. The SDK is used only by this verification client; it is not a
server runtime dependency.
