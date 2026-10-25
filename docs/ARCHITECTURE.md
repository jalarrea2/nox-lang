# NOX architecture

This document describes how a readable `.nox` program becomes an opaque `.nxa`
artifact and then a native binary, the on-disk artifact format, and how the
repository is governed for community contributions.

## Pipeline overview

```
 .nox (DSL)  ──parse──▶  IR  ──serialize──▶  bytecode
                                                │
                                   encrypt (XOR keystream) + HMAC
                                                │
                                       permute byte→glyph
                                                ▼
                                         .nxa (Braille)
 .nxa  ──decode(seed)──▶  IR  ──emit_c──▶  C  ──clang -O3──▶  native binary
```

Each stage maps to one module:

| Module            | Responsibility                                            |
|-------------------|-----------------------------------------------------------|
| `nox/isa.py`      | Opcode table and operand signatures (the ISA).            |
| `nox/dsl.py`      | Parse the DSL into IR, resolving labels to indices.       |
| `nox/frontend.py` | Lower functions, expressions, strings, and scoped locals. |
| `nox/codec.py`    | IR ↔ bytecode ↔ opaque artifact (cipher, carrier, HMAC).  |
| `nox/backend_c.py`| Lower IR to C and invoke the C compiler.                  |
| `nox/backend_values.py` | Emit native functions and tagged-value helpers.     |
| `nox/backend_json.py` | Inline the vendored JSON parser and value helpers.    |
| `nox/backend_mcp.py` | Emit native POSIX stdio MCP server and tool isolation. |
| `nox/__main__.py` | CLI (`compile`, `build`, `run`, `emit`, `disasm`).        |

## Intermediate representation (IR)

The IR is a flat list of tuples `(MNEMONIC, *operands)`. Labels are resolved to
absolute instruction indices during parsing, so jump targets are plain integers
by the time they reach the codec or the back-end. Operand kinds:

- `R` — register index (1 byte, so up to 256 registers).
- `I` — signed i64 immediate (8 bytes, little-endian).
- `T` — jump target: instruction index (4 bytes, little-endian, unsigned).
- `S` — strict UTF-8 string (u32 little-endian byte length followed by bytes).
- `A` — register list (u16 little-endian count followed by u8 register indices).

`parse(src, nregs=256, filename=None)` returns the same tuple IR and retains
source locations in `NoxSyntaxError` attributes (`filename`, `lineno`, `text`).
The default accepts every ISA register; the CLI supplies its configured count
(32 by default). Every register operand is checked before its source location
is discarded. These diagnostics do not add a source map to serialized artifacts.

## Artifact format (`.nxa`, versions 3 through 5)

The byte layout **before** encryption and carrier encoding:

```
┌───────────────────────── bytecode ─────────────────────────┐
│ "NOX1" │ ver │ nregs │ memsize │ count │ instruction bytes… │  + HMAC tag
│  4 B   │ 1 B │  2 B  │   4 B    │  4 B  │        …           │   16 B
└─────────────────────────────────────────────────────────────┘
```

1. **Bytecode** — the header (`MAGIC`, version, register count, memory size,
   instruction count) followed by serialized instructions. The header uses
   little-endian `<BHII` after the magic. The register count is an unsigned
   16-bit value, validated to 1..256; register operands remain one byte.
2. **Integrity tag** — `HMAC-SHA256(key + "mac", bytecode)` truncated to 16 bytes,
   appended to the bytecode.
3. **Cipher** — the whole payload is XORed with a keystream
   `SHA-256(key + "cs" + counter_le64)` for successive counters starting at zero.
4. **Carrier** — each ciphertext byte is mapped through a seed-derived
   permutation and emitted as one Braille glyph (`U+2800 + perm[byte]`). The
   Braille block is exactly 256 code points, so the mapping is a clean bijection
   onto a visually uniform alphabet.

The encoder writes v5 for JSON/MCP instructions, v4 for function/string instructions, and
v3 for pure legacy IR, preserving its previous encoding exactly. Both versions
use the displayed header. The decoder also accepts v2's `<BBII` header, where
the register count occupies one byte (1..255). This preserves the bundled
v2 artifacts. Other versions are rejected explicitly. All versions undergo
the same structural and operand validation. Extended opcodes in a v2/v3
container are rejected, as are v5 instructions inside a v4 container. Register-list lengths and string byte lengths are
checked before reading their payloads; invalid UTF-8 is an integrity error.

### Seed derivation

The seed is encoded as UTF-8 and hashed with SHA-256. Separate suffixes are
used for the keystream, permutation, and HMAC key:

- keystream: `SHA-256(key + "cs" + counter)`
- permutation: seeded `random.Random(SHA-256(key + "perm"))`
- HMAC key: `key + "mac"`

### Decoding and failure modes

`decode()` inverts carrier → cipher → tag check. An incorrect seed or an edited
payload fails the HMAC check with `NoxIntegrityError`; these causes cannot be
distinguished by this check. Authentication happens before bytecode parsing.

Before decoding, the carrier parser accepts only Braille and ASCII space,
tab, CR, and LF. It rejects other characters with one-based line/column and
Unicode codepoint diagnostics; CRLF is counted as one line break. Layout
whitespace does not alter the decoded payload and is not authenticated. The
stricter parser preserves all encoder output and bundled v2/v3 artifacts.

`deserialize()` checks the magic and version, header dimensions, every operand's
available bytes, register and jump bounds, instruction count, and exact body
consumption. Malformed bytecode raises `NoxIntegrityError`, including when it
has a valid HMAC. `serialize()` and `emit_c()` use the shared ISA validator to
reject invalid IR with `ValueError` before encoding or generating C. A main-scope
jump to instruction `len(ir)` is a valid successful exit; function branches
must remain inside their own body or target its closing boundary.

The same seed reuses the permutation and keystream, exposing patterns between
artifacts. There is no nonce, salt, or password stretching. Anyone holding the
seed can reconstruct the IR with `disasm` and create authenticated artifacts.
Native binaries can be analyzed without the seed. See
[`SECURITY.md`](SECURITY.md) for the full threat model.

## Back-end

For pure legacy IR, the backend emits a single `main()` with `int64_t` local
registers. Each IR instruction gets a C label (`I0:`, `I1:`, …) with `goto`
for control flow. `clang -O3` then does register allocation, dead-label elimination, and
CFG construction, producing code close to hand-written C.

Arithmetic helpers use unsigned 64-bit operations and explicit conversion to
signed i64 so addition, subtraction, multiplication, and left shifts wrap
without signed C overflow. Shift counts are masked to six bits, and arithmetic
right shift is explicit rather than dependent on signed C shifts. Division
and modulo return `0` for a zero divisor and handle `INT64_MIN / -1` explicitly
(wrapped quotient `INT64_MIN`, remainder `0`). The minimum i64 immediate is
emitted as `INT64_MIN` to avoid an out-of-range C integer literal.

Memory accesses go through a bounds check before indexing. Addresses below
zero or at/above the declared cell count produce a stderr diagnostic and
exit `4`; `nox run` propagates this code. Memory and registers start at zero.
The native regression suite compares all binary operations against a Python
reference at `-O0` and `-O3`, checks memory boundary failures, and can enable
UndefinedBehaviorSanitizer with `NOX_TEST_SANITIZERS=1`.

`CC` selects an explicit compiler executable by name or path. A missing
explicit compiler is an error; no other compiler is substituted. Without `CC`,
the backend tries clang, gcc, then cc. Native tests use the same selection so
the CI compiler matrix exercises the intended compiler.

### Native functions and strings

Extended IR adds `FUNC`, `ENDFUNC`, `CALL`, `RET`, `STR`, `SLEN`, `TOSTR`, and
`HESC`. Function markers delimit body instruction ranges. `analyze_functions`
maps each instruction to its owner and indexes parameter registers and closing
boundaries. IR validation checks unique declarations/parameters, call targets
and arity, returns, and branch ownership before C generation. Jumps cannot
enter function metadata or cross scopes.

The extended backend emits prototypes and direct C functions, so forward and
recursive calls need no runtime interpreter. Each invocation gets independent
registers, with parameters copied by value; the memory array remains shared.
Values are tagged integers or immutable byte-string handles. Arithmetic uses
the same numeric helpers after type checks, and addition also concatenates
two strings. Equality compares typed values; output uses explicit lengths.

String allocations use a 64 MiB arena including metadata, released through
`atexit` on normal exit and errors. Native calls are guarded at depth 256.
Overwritten strings are retained until process exit. These are runtime bounds,
not a sandbox or a CPU budget. See [`LANGUAGE.md`](LANGUAGE.md) for semantics.

### Native JSON and MCP

JSON helpers operate on validated fragment strings, retaining objects, arrays,
real numbers, null, and booleans without extending the NOX value representation.
The generator inlines pinned yyjson C/header assets and native conversion/query
helpers only for JSON/MCP programs. Ordinary legacy and v4 programs remain
independent of those assets. Vendored source, hashes, and MIT license are
distributed in both source packages and wheels.

`MCPTOOL` carries a literal name, function target, JSON parameter-name metadata,
and description. The generator emits a direct C adapter for that function.
`MCPSERVE` enters a POSIX stdio JSON-RPC loop with lifecycle negotiation, tools,
bounded framing and responses, and per-call child-process isolation. Return
values cross a private pipe; stdout is exclusively MCP protocol output. User
prints go to stderr, and user reads return EOF. Schema validation and runtime
errors remain distinct protocol/tool errors. See [`MCP.md`](MCP.md).

## Community & CI governance

The repository is set up so contributions from a group of maintainers
("subscribers") are reviewed and tested automatically **before merge**.

### What lives in the repo

- `.github/workflows/ci.yml` — runs lint and tests on Linux with Python 3.8,
  3.12, and 3.14 and on macOS with Python 3.14. Linux covers Clang and GCC;
  Clang also runs native UBSan regressions.
- `.github/workflows/release.yml` — builds and verifies the wheel and sdist,
  then uploads downloadable workflow artifacts. It is reusable from CI and
  runs on manual dispatch or version tags. It does not publish packages.
- `scripts/check_package.py` — verifies metadata, clean installed imports,
  CLI behavior, and native output for both wheel and rebuilt sdist packages.
- `.github/CODEOWNERS` — maps paths to the people/teams asked to review.
- `.github/pull_request_template.md` — the PR checklist.

### What must be configured in GitHub settings (one time, by an admin)

These cannot be committed to the repo; set them under
**Settings → Branches → Branch protection rules** for `main`:

1. **Require a pull request before merging** (disallow direct pushes to `main`).
2. **Require status checks to pass before merging** → select the `test` jobs
   from the CI workflow. This makes CI a hard gate.
3. **Require review from Code Owners** (and/or N approving reviews).
4. Optionally **Require branches to be up to date before merging**.

Repository files define checks and review ownership; they do not by themselves
enable branch protection. Check the actual repository settings before treating
passing CI and owner approval as enforced merge gates.

### Letting a community of contributors in

- Create a **GitHub Team** (e.g. `@your-org/nox-maintainers`) and reference it
  in `CODEOWNERS` so review load is shared across subscribers.
- Add outside contributors via **fork + pull request** (they need no write
  access; CI still runs on their PRs).
- Use **GitHub Discussions** / issues for proposals, and label
  `good first issue` to onboard newcomers.
