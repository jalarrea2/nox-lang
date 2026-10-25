# Security model

NOX is an experimental compiler with seed-based artifact obfuscation. The v2,
v3, v4, and v5 formats are not a confidentiality boundary for sensitive source code.

## What the artifact protects

A `.nxa` file obscures serialized instructions using a seed-derived keystream
and byte-to-Braille permutation. An HMAC-SHA256 tag, truncated to 16 bytes,
authenticates the decoded bytecode. Wrong seeds and modified payloads fail the
same authentication check before bytecode parsing. Structural validation then
checks dimensions, operands, jump targets, and complete bytecode consumption.

The text carrier accepts Braille U+2800..U+28FF and four layout characters:
ASCII space, tab, carriage return, and line feed. Layout characters are ignored
and are not authenticated. Every other character is rejected, with a one-based
line and character column and its Unicode codepoint. Authentication therefore
protects the decoded payload, not a byte-for-byte representation of the file.

## Known limits

- A seed always generates the same permutation and keystream. There is no
  per-artifact nonce or salt. The same program and seed produce the same
  artifact; reusing a seed exposes equality patterns between programs.
- The key is derived with one SHA-256 invocation. Short or guessable seeds can
  be tested offline against the authentication tag; there is no password
  stretching or rate limit.
- Anyone with the seed can recover all instructions using `disasm` and can
  create new artifacts with valid tags. The HMAC is not an author signature.
- Decoding is resolved at build time. The native executable does not need the
  seed and remains available to ordinary binary analysis. Obfuscation of the
  `.nxa` does not protect the compiled executable from reverse engineering.
- `run` executes a native program as the current user. Memory bounds checks and
  extended-program string/call limits define runtime behavior; they do not
  provide an OS sandbox, CPU limit, or total process-memory limit. The shared
  linear memory and native compiler can consume additional resources.
- The CLI currently receives the seed through `--seed`; command-line arguments
  may be recorded in shell history or visible through process inspection.

The native MCP stdio server runs tools in bounded child processes and reserves
stdout for protocol messages. This isolates process-local failures and state;
it does not provide a filesystem/network sandbox or authorization policy.
Connect it through a trusted local host. See [`MCP.md`](MCP.md) for transport,
argument, timeout, output, and lifecycle limits.

## Compatibility and future changes

NOX 0.5.0 writes v5 for JSON/MCP, v4 for functions/strings, and retains v3 encoding
for pure register programs. It reads v2/v3/v4/v5 artifacts. Stricter text
validation changes the accepted carrier syntax, but does not change emitted
bytes, the cipher, permutation, or integrity tag. Files that previously hid
arbitrary non-Braille text must remove that text before decoding.

If confidentiality becomes a goal, a future outer envelope must expose a
format version before decryption and use standard authenticated encryption,
per-artifact randomness, and an appropriate password KDF. That migration needs
explicit legacy decoding and independent security review; changing the current
keystream alone would not provide that design.
