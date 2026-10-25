"""Verify wheel and sdist installations without importing the source checkout.

Build distributions first with ``python -m build``. This check needs build,
setuptools, and wheel in the invoking environment and a native C compiler.
Package installation uses no package index or runtime dependencies.
"""
import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
from email.parser import Parser
from pathlib import Path


def run(args, cwd, **kwargs):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    result = subprocess.run(args, cwd=cwd, env=environment,
                            capture_output=True, timeout=120, **kwargs)
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(map(str, args))}\n"
            f"{result.stdout.decode(errors='replace')}{result.stderr.decode(errors='replace')}"
        )
    return result.stdout


def unpack_sdist(archive, destination):
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise RuntimeError(f"unsafe source archive member: {member.name!r}")
        package.extractall(destination, members=members)
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError("source archive must contain one top-level directory")
    source = roots[0]
    required = [
        "pyproject.toml", "PKG-INFO", "LICENSE", "README.md", "CHANGELOG.md", "CONTRIBUTING.md",
        "docs/ARCHITECTURE.md", "docs/SECURITY.md", "docs/RELEASING.md", "docs/LANGUAGE.md",
        "docs/MCP.md", "scripts/check_mcp_sdk.py", "nox/_vendor/yyjson/yyjson.h",
        "nox/_vendor/yyjson/yyjson.c", "nox/_vendor/yyjson/LICENSE",
        "scripts/check_package.py", "tests/test_nox.py",
    ]
    examples = ("fib", "cat", "turnstile", "parity", "div3", "html", "functions", "mcp")
    required += [f"examples/{name}.{suffix}"
                 for name in examples
                 for suffix in ("nox", "nxa")]
    for name in required:
        if not (source / name).is_file():
            raise RuntimeError(f"source archive is missing {name}")
    return source


def check_install(wheel, directory, examples, expected_version):
    environment = directory / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    bindir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    cli = bindir / ("nox.exe" if os.name == "nt" else "nox")
    sandbox = directory / "outside-checkout"
    sandbox.mkdir()
    run([str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        sandbox)
    # -I ignores PYTHONPATH and the working directory. Verify import ownership as
    # well so an editable install or another installed `nox` cannot hide a defect.
    probe = """
import importlib.metadata
import pathlib
import sys
import nox
assert pathlib.Path(sys.prefix) in pathlib.Path(nox.__file__).resolve().parents
version = importlib.metadata.version('nox-lang')
assert nox.__version__ == version, (nox.__version__, version)
print(version)
"""
    version = run([str(python), "-I", "-c", probe], sandbox).decode().strip()
    if expected_version and version != expected_version:
        raise RuntimeError(f"expected version {expected_version}, installed {version}")
    for command in ([str(python), "-I", "-m", "nox"], [str(cli)]):
        output = run(command + ["--version"], sandbox).decode().strip()
        if version not in output:
            raise RuntimeError(f"CLI version does not match metadata: {output!r}")
    source = sandbox / "answer.nox"
    artifact = sandbox / "answer.nxa"
    binary = sandbox / ("answer.exe" if os.name == "nt" else "answer")
    source.write_text("r0 = 42\nprint r0\nhalt\n", encoding="utf-8")
    run([str(cli), "compile", str(source), "-o", str(artifact), "--seed", "package-test"],
        sandbox)
    run([str(cli), "build", str(artifact), "-o", str(binary), "--seed", "package-test"],
        sandbox)
    if run([str(binary)], sandbox) != b"42":
        raise RuntimeError("installed package produced incorrect native output")
    if run([str(cli), "run", str(artifact), "--seed", "package-test"], sandbox) != b"42":
        raise RuntimeError("installed package run command produced incorrect output")
    expected = b"0 1 1 2 3 5 8 13 21 34 55 89 144 233 377 \n"
    if run([str(cli), "run", str(examples / "fib.nxa"), "--seed", "demo"], sandbox) != expected:
        raise RuntimeError("installed package could not execute the legacy Fibonacci artifact")
    data = b"package smoke test\x00\xff\n"
    if run([str(cli), "run", str(examples / "cat.nxa"), "--seed", "demo"],
           sandbox, input=data) != data:
        raise RuntimeError("installed package did not preserve byte input/output")
    extended_source = sandbox / "functions.nox"
    extended_artifact = sandbox / "functions.nxa"
    extended_source.write_text(
        'fn greet(name):\n    return "<h1>" + html.escape(name) + "</h1>"\n\n'
        'print greet("María & <NOX>") + "\\0"\n', encoding="utf-8",
    )
    run([str(cli), "compile", str(extended_source), "-o", str(extended_artifact),
         "--seed", "package-test"], sandbox)
    if run([str(cli), "run", str(extended_artifact), "--seed", "package-test"], sandbox) != (
        "<h1>María &amp; &lt;NOX&gt;</h1>\0".encode()
    ):
        raise RuntimeError("installed package could not execute native functions and UTF-8 strings")
    expected = b"10! = 3628800\n"
    result = run([str(cli), "run", str(examples / "functions.nxa"), "--seed", "demo"], sandbox)
    if result != expected:
        raise RuntimeError("installed package could not execute the recursive v4 artifact")
    mcp_binary = sandbox / "nox-mcp"
    run([str(cli), "build", str(examples / "mcp.nxa"), "-o", str(mcp_binary),
         "--seed", "demo"], sandbox)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "package-check", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "saludar", "arguments": {"nombre": "MCP"}}},
    ]
    wire = "".join(json.dumps(message) + "\n" for message in messages).encode()
    output = run([str(mcp_binary)], sandbox, input=wire)
    responses = [json.loads(line) for line in output.splitlines()]
    if len(responses) != 3 or responses[-1].get("result", {}).get("content") != [
        {"type": "text", "text": "Hola, MCP"}
    ]:
        raise RuntimeError("installed package could not build and run the native MCP server")
    print(f"Verified installed {wheel.name}: version {version}, CLI, native MCP/values, legacy I/O")
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", nargs="?", default="dist",
                        help="directory built by python -m build")
    parser.add_argument("--expected-version", help="require this release version")
    args = parser.parse_args()
    distributions = Path(args.dist).resolve()
    wheels = list(distributions.glob("nox_lang-*.whl"))
    sources = list(distributions.glob("nox_lang-*.tar.gz"))
    # Older setuptools used hyphens in sdist filenames.
    sources += list(distributions.glob("nox-lang-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error("expected exactly one nox-lang wheel and sdist; build into an empty directory")
    with tempfile.TemporaryDirectory(prefix="nox-package-") as temporary:
        root = Path(temporary)
        extracted = root / "source"
        extracted.mkdir()
        source = unpack_sdist(sources[0], extracted)
        wheel_check = root / "wheel-check"
        wheel_check.mkdir()
        version = check_install(wheels[0], wheel_check, source / "examples", args.expected_version)
        metadata = Parser().parsestr((source / "PKG-INFO").read_text(encoding="utf-8"))
        if metadata.get("Name") != "nox-lang" or metadata.get("Version") != version:
            raise RuntimeError("source distribution metadata does not match the installed wheel")
        rebuilt = root / "rebuilt"
        run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(rebuilt)],
            source)
        rebuilt_wheels = list(rebuilt.glob("*.whl"))
        if len(rebuilt_wheels) != 1:
            raise RuntimeError("building the sdist did not produce exactly one wheel")
        sdist_check = root / "sdist-check"
        sdist_check.mkdir()
        check_install(rebuilt_wheels[0], sdist_check, source / "examples", version)


if __name__ == "__main__":
    main()
