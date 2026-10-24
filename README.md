# NOX — opaque metalanguage compiled to native code

NOX is a language that is **unreadable to a human yet executable**: the final
source (`.nxa`) is a string of Braille glyphs with no visible structure, and
only the correct **seed** can decode and compile it. The resulting binary is
**native and efficient** (`clang -O3`), indistinguishable from hand-written C —
all obfuscation is resolved at compile time, with zero runtime overhead.

## Why it is "meta"

There are no fixed keywords. The `byte → glyph` dictionary and the cipher
keystream are **derived from the seed, per program**: two programs with
different seeds produce different alphabets and ciphers. You cannot learn the
language by reading it.

## Pipeline

```
 readable DSL (.nox)
        │  python -m nox compile … --seed K
        ▼
 opaque artifact (.nxa)   ← what you distribute; noise without the seed
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
- **Integrity:** an HMAC-SHA256 tag detects a tampered artifact, distinct from a
  simple wrong-seed rejection.

## Usage

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

`compile` also accepts `--regs N` and `--mem N` to size the register file and
linear memory. A wrong seed fails the magic-header check; a tampered artifact
fails the HMAC check.

## DSL

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

## ISA

`IMM MOV ADD SUB MUL DIV MOD LT EQ GT AND OR XOR SHL SHR LD ST JMP JZ JNZ IN OUT OUTN HALT`
(up to 256 i64 registers, linear memory of i64 cells; both configurable,
defaults 32 registers / 65536 cells).

## Project layout

```
nox/
  isa.py        opcodes + signatures
  dsl.py        DSL parser -> IR
  codec.py      IR <-> bytecode <-> artifact (seed-based cipher/carrier + HMAC)
  backend_c.py  IR -> C -> clang -O3
  __main__.py   CLI
examples/       fib.nox, cat.nox (+ compiled .nxa, seed "demo")
tests/          test_nox.py
```

## Tests

```bash
make test            # or: python -m unittest discover -s tests
```

The native-output tests are skipped automatically when no C compiler is found.

## Documentation & contributing

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — pipeline, artifact format, and CI governance.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup and the pre-merge checks.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.

## Roadmap

- **LLVM IR / WASM back-end** (`llc -O3`) — more control and a web target.
- `f64` types, calls/stack frames, named arrays in the DSL.
- Port the toolchain to Rust (a single distributable binary).

> Security note: seed-based obfuscation is *deterrence*, not military-grade
> cryptography — the keystream is SHA-256-CTR and protects the dictionary, and
> the HMAC tag detects tampering, but anyone who holds the seed can reconstruct
> the full IR (`disasm`). For real confidentiality, treat the seed as a secret.
