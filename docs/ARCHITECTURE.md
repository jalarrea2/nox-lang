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

## Artifact format (`.nxa`, version 2)

The byte layout **before** encryption and carrier encoding:

```
┌───────────────────────── bytecode ─────────────────────────┐
│ "NOX1" │ ver │ nregs │ memsize │ count │ instruction bytes… │  + HMAC tag
│  4 B   │ 1 B │  1 B  │   4 B    │  4 B  │        …           │   16 B
└─────────────────────────────────────────────────────────────┘
```

1. **Bytecode** — the header (`MAGIC`, version, register count, memory size,
   instruction count) followed by serialized instructions.
2. **Integrity tag** — `HMAC-SHA256(key⊕"mac", bytecode)` truncated to 16 bytes,
   appended to the bytecode.
3. **Cipher** — the whole payload is XORed with a keystream
   `SHA-256(key⊕"cs" ‖ counter)` in CTR fashion.
4. **Carrier** — each ciphertext byte is mapped through a seed-derived
   permutation and emitted as one Braille glyph (`U+2800 + perm[byte]`). The
   Braille block is exactly 256 code points, so the mapping is a clean bijection
   onto a visually uniform alphabet.

### Seed derivation

All secrets derive from `SHA-256(seed)`, domain-separated by a suffix so the
keystream, the permutation, and the HMAC key never collide:

- keystream: `SHA-256(key + "cs" + counter)`
- permutation: seeded `random.Random(SHA-256(key + "perm"))`
- HMAC key: `key + "mac"`

### Decoding and failure modes

`decode()` inverts carrier → cipher → tag check:

- **Wrong seed** → a different permutation/keystream → the `MAGIC` header does
  not validate → `NoxIntegrityError` ("invalid magic").
- **Tampered artifact** (right seed, edited glyphs) → the recomputed HMAC does
  not match → `NoxIntegrityError` ("integrity check failed").

This is **deterrence, not confidentiality**: anyone holding the seed can fully
reconstruct the IR with `disasm`. Treat the seed as a secret if that matters.

## Back-end

The back-end emits a single `main()` where registers are `int64_t` locals and
each IR instruction gets a C label (`I0:`, `I1:`, …) with `goto` for control
flow. `clang -O3` then does register allocation, dead-label elimination, and
CFG construction, producing code close to hand-written C. Division and modulo
guard against division by zero (result `0`), matching the ISA contract.

## Community & CI governance

The repository is set up so contributions from a group of maintainers
("subscribers") are reviewed and tested automatically **before merge**.

### What lives in the repo

- `.github/workflows/ci.yml` — runs lint + tests (Python 3.8/3.10/3.12) and a
  native smoke test on every pull request. No configuration needed to run.
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
