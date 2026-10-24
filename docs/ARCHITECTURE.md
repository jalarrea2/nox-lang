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
| `nox/codec.py`    | IR ↔ bytecode ↔ opaque artifact (cipher, carrier, HMAC).  |
| `nox/backend_c.py`| Lower IR to C and invoke the C compiler.                  |
| `nox/__main__.py` | CLI (`compile`, `build`, `run`, `emit`, `disasm`).        |

## Intermediate representation (IR)

The IR is a flat list of tuples `(MNEMONIC, *operands)`. Labels are resolved to
absolute instruction indices during parsing, so jump targets are plain integers
by the time they reach the codec or the back-end. Operand kinds:

- `R` — register index (1 byte, so up to 256 registers).
- `I` — signed i64 immediate (8 bytes, little-endian).
- `T` — jump target: instruction index (4 bytes, little-endian, unsigned).

## Artifact format (`.nxa`, version 3)

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
2. **Integrity tag** — `HMAC-SHA256(key⊕"mac", bytecode)` truncated to 16 bytes,
   appended to the bytecode.
3. **Cipher** — the whole payload is XORed with a keystream
   `SHA-256(key⊕"cs" ‖ counter)` in CTR fashion.
4. **Carrier** — each ciphertext byte is mapped through a seed-derived
   permutation and emitted as one Braille glyph (`U+2800 + perm[byte]`). The
   Braille block is exactly 256 code points, so the mapping is a clean bijection
   onto a visually uniform alphabet.

The encoder writes v3. The decoder also accepts v2's `<BBII` header, where
the register count occupies one byte (1..255). This preserves the bundled
v2 artifacts. Other versions are rejected explicitly. Both versions undergo
the same structural and operand validation.

### Seed derivation

All secrets derive from `SHA-256(seed)`, domain-separated by a suffix so the
keystream, the permutation, and the HMAC key never collide:

- keystream: `SHA-256(key + "cs" + counter)`
- permutation: seeded `random.Random(SHA-256(key + "perm"))`
- HMAC key: `key + "mac"`

### Decoding and failure modes

`decode()` inverts carrier → cipher → tag check. An incorrect seed or an edited
payload fails the HMAC check with `NoxIntegrityError`; these causes cannot be
distinguished by this check. Authentication happens before bytecode parsing.

`deserialize()` checks the magic and version, header dimensions, every operand's
available bytes, register and jump bounds, instruction count, and exact body
consumption. Malformed bytecode raises `NoxIntegrityError`, including when it
has a valid HMAC. `serialize()` and `emit_c()` use the shared ISA validator to
reject invalid IR with `ValueError` before encoding or generating C. A jump to
instruction `len(ir)` is a valid successful exit.

This is **deterrence, not confidentiality**: anyone holding the seed can fully
reconstruct the IR with `disasm`. Treat the seed as a secret if that matters.

## Back-end

The back-end emits a single `main()` where registers are `int64_t` locals and
each IR instruction gets a C label (`I0:`, `I1:`, …) with `goto` for control
flow. `clang -O3` then does register allocation, dead-label elimination, and
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

## Community & CI governance

The repository is set up so contributions from a group of maintainers
("subscribers") are reviewed and tested automatically **before merge**.

### What lives in the repo

- `.github/workflows/ci.yml` — runs lint + tests (Python 3.8/3.10/3.12), a
  native smoke test, and native UBSan regressions on Python 3.12 on every pull
  request. No configuration needed to run.
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

### Letting a community of contributors in

- Create a **GitHub Team** (e.g. `@your-org/nox-maintainers`) and reference it
  in `CODEOWNERS` so review load is shared across subscribers.
- Add outside contributors via **fork + pull request** (they need no write
  access; CI still runs on their PRs).
- Use **GitHub Discussions** / issues for proposals, and label
  `good first issue` to onboard newcomers.
