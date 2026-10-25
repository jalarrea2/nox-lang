# NOX — opaque metalanguage compiled to native code

NOX is an experimental language with a readable register-based DSL and an
**opaque distribution format**: a `.nxa` artifact is a string of Braille glyphs
decoded with a **seed**. The toolchain lowers the decoded instructions to C and
compiles them to a native executable with `clang -O3` (or GCC/cc).

Artifact decoding happens at build time, so the executable has no decoder or
opcode-dispatch loop. Native performance depends on the program and compiler;
the project does not yet provide benchmarks against hand-written C. The
executable can be analyzed without the seed. See the
[security model](docs/SECURITY.md) for the limits of artifact obfuscation.

## Why it is "meta"

The readable DSL has fixed syntax. Its opaque carrier has no visible keywords:
the `byte → glyph` dictionary and the keystream are **derived from the seed**.
Different seeds produce different mappings; reusing a seed reuses both the
mapping and keystream and exposes patterns between artifacts.

## Pipeline

```
 readable DSL (.nox)
        │  python -m nox compile … --seed K
        ▼
 opaque artifact (.nxa)   ← seed-based obfuscation for distribution
        │  python -m nox build/run … --seed K
        ▼
 decode(seed) → IR → C → clang -O3
        ▼
 native binary
```

- **Layer 0 (carrier):** Braille block U+2800–U+28FF (exactly 256 glyphs).
- **Layer 1 (meta):** byte↔glyph permutation + XOR keystream, both `f(seed)`.
- **Layer 2 (compute):** i64 register machine, linear memory, I/O. Turing-complete.
- **Layer 3 (runtime):** emit C + `clang -O3` (falls back to gcc/cc).
- **Integrity:** an HMAC-SHA256 tag rejects tampered artifacts and incorrect seeds
  before the bytecode is parsed.

## Usage

Requires Python 3.8+ and a C compiler (`clang`, `gcc`, or `cc`) for native
builds. Install from a checkout in a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
nox --version
```

Set `CC=gcc` or `CC=/path/to/clang` to select a compiler explicitly. `CC` must
name a single executable; pass optimization through `--opt=-O0` or
`--opt=-O3`. Without `CC`, the toolchain tries clang, gcc, then cc.

```bash
python -m nox compile examples/fib.nox -o fib.nxa --seed KEY   # DSL      -> artifact
python -m nox build   fib.nxa          -o fib     --seed KEY   # artifact -> binary
python -m nox run     fib.nxa                     --seed KEY   # compile to tmp and run
python -m nox emit    fib.nxa                     --seed KEY   # show the generated C
python -m nox disasm  fib.nxa                     --seed KEY   # show the IR (debug)
```

The bundled examples are compiled with the seed `demo`:

```bash
python -m nox run examples/fib.nxa --seed demo    # -> 0 1 1 2 3 5 8 13 21 34 55 89 144 233 377
echo hello | python -m nox run examples/cat.nxa --seed demo
```

(To rebuild an artifact from source: `python -m nox compile examples/fib.nox -o examples/fib.nxa --seed demo`.)

### Finite state machines

A finite state machine maps naturally onto NOX: **each state is a label**, and a
transition is `read` followed by a conditional `goto`. The bundled examples:

| Example           | What it is                                                        | Try it |
|-------------------|-------------------------------------------------------------------|--------|
| `turnstile.nox`   | Classic 2-state turnstile: `'c'` = coin, `'p'` = push             | `printf cpcp \| python -m nox run examples/turnstile.nxa --seed demo` → `ULUL` |
| `parity.nox`      | DFA for the parity of `'1'` characters (even/odd)                 | `printf 1011 \| python -m nox run examples/parity.nxa --seed demo` → `1` |
| `div3.nox`        | DFA accepting binary numbers divisible by 3 (remainder mod 3)     | `printf 110 \| python -m nox run examples/div3.nxa --seed demo` → `Y` |

`compile` also accepts `--regs N` and `--mem N` to size the register file and
linear memory. Both a wrong seed and a tampered payload fail the HMAC check.
Pure register programs use format v3, which supports all 256 registers. Programs
with functions or strings use format v4. Existing v2 artifacts, including the
original bundled examples, remain readable.
JSON/MCP programs use v5; the decoder accepts all four versions.

Artifacts accept only Braille glyphs and ASCII space, tab, CR, and LF. Other
characters are rejected with a line, column, and Unicode codepoint diagnostic.
Whitespace is ignored and is not covered by payload authentication.

## DSL

NOX also supports native functions, named locals, and strings:

```text
fn heading(name):
    return "<h1>Hello, " + html.escape(name) + "</h1>"

print heading("María & <agents>") + "\n"
```

Functions compile to native calls with independent locals, parameters, and
return values. Strings support UTF-8, concatenation with `+`, byte length with
`len`, formatting with `str`, and HTML escaping. The executable runs without
Python. See [`docs/LANGUAGE.md`](docs/LANGUAGE.md) for syntax, recursion, typing,
and limits, or try `examples/html.nox` and `examples/functions.nox`.

The original register syntax remains available:

```
label:                    # label
rN = <int>                # IMM (accepts -5, 0x1F, 'A')
rN = rM                   # MOV
rN = rA + rB              # ADD  (+ - * / %)
rN = rA < rB              # LT   (<  >  ==)
rN = rA & rB              # AND  (& | ^ << >>)
rN = [rA]                 # LD  mem[rA]
[rA] = rB                 # ST
goto label                # JMP
if rN == 0 goto label     # JZ
if rN != 0 goto label     # JNZ
read rN                   # IN   (-1 on EOF)
print rN                  # OUTN (integer)
putc rN                   # OUT  (byte)
halt                      # HALT
```

`#` and `;` start comments outside character literals; `r0 = '#'` and
`r0 = ';'` are valid assignments. Integer immediates must fit signed i64;
out-of-range values report a syntax error with the source line number.
Register errors also retain the source filename and original line, including
when comments or labels precede the instruction. `compile --regs N` checks
every register operand against that configured count and returns exit code 2
for source errors. For example, using `r32` with the default 32 registers:

```text
[nox] syntax error: program.nox: line 4: register r32 out of range (expected r0..r31)
    r32 = 1
```

## ISA

`IMM MOV ADD SUB MUL DIV MOD LT EQ GT AND OR XOR SHL SHR LD ST JMP JZ JNZ IN OUT OUTN HALT`
(up to 256 i64 registers, linear memory of i64 cells; both configurable,
defaults 32 registers / 65536 cells).

Execution rules are independent of C compiler optimization:

- `ADD`, `SUB`, `MUL`, and `SHL` wrap modulo 2^64, interpreted as signed i64.
- `DIV` truncates toward zero; `MOD` has the dividend's sign. A zero divisor
  returns `0`. `INT64_MIN / -1` wraps to `INT64_MIN`; its remainder is `0`.
- Shift counts use their low six bits, including negative counts (`-1` means
  63). `SHR` is an arithmetic right shift that preserves the sign.
- Memory starts at zero. `LD` and `ST` require addresses in `0..memsize-1`;
  invalid addresses print a diagnostic to stderr and exit with code `4`.
- Registers start at zero. Jump targets may point just past the final
  instruction to exit successfully; other out-of-range targets are rejected.

Format v4 adds `FUNC ENDFUNC CALL RET STR SLEN TOSTR HESC`. Functions have their
own registers; memory remains shared. Extended programs use native tagged
integer/string values, with runtime type checks and bounded string allocation
and call depth. See the language guide for the full contract.

## Project layout

```
nox/
  isa.py        opcodes + signatures
  dsl.py        DSL parser -> IR
  codec.py      IR <-> bytecode <-> artifact (seed-based cipher/carrier + HMAC)
  backend_c.py  IR -> C -> clang -O3
  frontend.py   functions, expressions, named locals, and strings -> IR
  backend_values.py  native function/string C generation and runtime
  __main__.py   CLI
examples/       fib, cat, turnstile, parity, div3 (.nox + compiled .nxa, seed "demo")
tests/          parser, codec, native runtime, diagnostics, and compiler regressions
scripts/        installed-package validation
```

## Tests

```bash
make test            # or: python -m unittest discover -s tests
```

The native-output tests are skipped automatically when no C compiler is found.
To also check the native regression cases with UndefinedBehaviorSanitizer:

```bash
NOX_TEST_SANITIZERS=1 python -m unittest discover -s tests -p 'test_runtime.py'
NOX_TEST_SANITIZERS=1 python -m unittest discover -s tests -p 'test_native_values.py'
```

## Native MCP and JSON

```text
fn saludar(nombre):
    return "Hola, " + nombre

mcp.tool("saludar", saludar, "Saluda por nombre")
mcp.serve_stdio()
```

Build `examples/mcp.nox` into an executable and point your agent host at that
binary. It serves MCP tools over stdio without a Python runtime. Parameter
schemas derive from function parameters; values are strings or signed i64.
Native JSON helpers include `json.stringify`, `json.get`, `json.at`,
`json.string`, `json.int`, and `json.valid`. See [`docs/MCP.md`](docs/MCP.md)
for protocol coverage, configuration, limits, and the official-client check.

## Documentation & contributing

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — pipeline, artifact format, and CI governance.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup and the pre-merge checks.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.
- [`docs/SECURITY.md`](docs/SECURITY.md) — threat model and format limitations.
- [`docs/RELEASING.md`](docs/RELEASING.md) — build and verify installable packages.
- [`docs/LANGUAGE.md`](docs/LANGUAGE.md) — native functions and strings.
- [`docs/MCP.md`](docs/MCP.md) — native MCP tool servers and JSON.

## Roadmap

- A reference interpreter for whole-program differential tests, then tracing
  and register/memory inspection with source maps.
- Modules, named constants, native HTTP/MCP client support, and Streamable HTTP
  transport for servers.
- A separately versioned authenticated-encryption envelope with per-artifact
  randomness and password derivation, if confidentiality becomes a project goal.
- Reproducible performance and compilation-size benchmarks before choosing
  LLVM/WASM backends or a Rust toolchain port.
