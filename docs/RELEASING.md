# Preparing a release

Package version 0.5.0 and bytecode format versions are separate contracts.
JSON/MCP programs emit v5; functions/strings emit v4; pure legacy programs emit v3, and v2 artifacts
remain readable. Keep `project.version` in
`pyproject.toml`, `nox.__version__`, and the changelog consistent.

## Local verification

Use a virtual environment and install the development tools:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
make lint PYTHON=python
make test PYTHON=python
NOX_TEST_SANITIZERS=1 python -m unittest discover -s tests -p 'test_runtime.py'
NOX_TEST_SANITIZERS=1 python -m unittest discover -s tests -p 'test_native_values.py'
make dist PYTHON=python
make check-dist PYTHON=python
```

On Python 3.10+, also install `.[mcp-test]` and run
`python scripts/check_mcp_sdk.py` to verify the native server with the official
MCP client. This check launches the server with an empty PATH.

Start with an empty `dist` directory for a new version so old distributions
cannot be mistaken for the candidate. `make dist` uses PyPA build isolation,
builds the wheel from the sdist, and checks distribution metadata with Twine.
In an environment where build requirements are already installed, an offline
alternative is `python -m build --no-isolation`, followed by
`python -m twine check --strict dist/*` and `make check-dist PYTHON=python`.

The package check extracts the sdist, verifies required documentation and
examples, and rebuilds a wheel without using a package index. It installs both
the original wheel and the rebuilt wheel into separate fresh virtual
environments. Checks run outside the checkout, verify that imports come from
the installed package, and exercise the console entry point, module entry
point, version metadata, compile/build/run pipeline, native functions/UTF-8/NUL
strings, a native MCP handshake/tool call, and legacy Fibonacci and cat artifacts. A C compiler
is required; native checks must not be skipped for release preparation.

`CC` selects one C compiler executable by name or path. CI covers Linux with
Clang and GCC and macOS with Clang, including the minimum Python version and
Python 3.14. Local checks only validate the Python/compiler/platform on which
they run; a candidate should also pass the complete CI matrix.

## Downloadable artifacts

The `Package distributions` workflow runs as part of CI, on manual dispatch,
and for tags matching `v*`. It verifies distributions on Python 3.8 and 3.14,
checks tag/version agreement for tagged runs, and uploads the resulting wheel
and sdist as workflow artifacts. The workflow has read-only repository
permissions and does not publish to PyPI or create a GitHub release.

After reviewing the candidate and passing CI, a maintainer can create a
matching tag such as `v0.5.0`, then use its verified artifacts for the chosen
publication destination. Public registry and release publication remain
separate maintainer actions.
