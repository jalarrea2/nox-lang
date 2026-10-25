"""Verify a native NOX MCP server with the official Python MCP client SDK.

The SDK is a test dependency, not a server runtime dependency. Install the
mcp-test extra on Python 3.10+, then run this script from a source checkout.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path


async def check(binary):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(command=str(binary), env={"PATH": ""})
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=15) as client:
            initialized = await client.initialize()
            assert initialized.server_info.name == "nox", initialized
            assert initialized.server_info.version == "0.5.0", initialized
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {"saludar", "sumar", "heading", "json_nombre"}, names
            name = "María\0🙂"
            greeting = await client.call_tool("saludar", {"nombre": name})
            assert not greeting.is_error and greeting.content[0].text == "Hola, " + name, greeting
            total = await client.call_tool("sumar", {"a": 9223372036854775806, "b": 1})
            assert not total.is_error and total.content[0].text == "9223372036854775807", total
            html = await client.call_tool("heading", {"nombre": "<Ana & NOX>"})
            expected = "<h1>&lt;Ana &amp; NOX&gt;</h1>"
            assert not html.is_error and html.content[0].text == expected, html
            extracted = await client.call_tool("json_nombre", {"documento": '{"nombre":"Agente"}'})
            assert not extracted.is_error and extracted.content[0].text == "Agente", extracted
            failure = await client.call_tool("sumar", {"a": "text", "b": 1})
            assert failure.is_error, failure
            after = await client.call_tool("saludar", {"nombre": "después del error"})
            assert not after.is_error and after.content[0].text == "Hola, después del error", after
            return {"server": initialized.server_info.name,
                    "version": initialized.server_info.version,
                    "protocol": initialized.protocol_version, "tools": sorted(names),
                    "checks": ["official-sdk", "native-empty-PATH", "unicode-nul", "i64",
                               "html", "json", "runtime-error-recovery"]}


def build(directory):
    root = Path(__file__).resolve().parents[1]
    source = root / "examples" / "mcp.nox"
    artifact = directory / "mcp.nxa"
    binary = directory / "nox-mcp"
    for arguments in (("compile", source, "-o", artifact), ("build", artifact, "-o", binary)):
        subprocess.run([sys.executable, "-m", "nox", *map(str, arguments), "--seed", "demo"],
                       cwd=root, check=True, timeout=120)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="check an already-built native server")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="nox-sdk-check-") as temporary:
        binary = args.binary.resolve() if args.binary else build(Path(temporary))
        result = asyncio.run(check(binary))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
