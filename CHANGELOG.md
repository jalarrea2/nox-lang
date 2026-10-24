# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Native arithmetic reference tests at `-O0` and `-O3`, memory-boundary tests,
  CLI error regressions, and an optional UBSan test mode exercised in CI.
- Finite state machine examples: `turnstile.nox` (2-state turnstile),
  `parity.nox` (parity DFA), and `div3.nox` (divisibility-by-3 DFA), with tests.
- `LICENSE` (MIT), `CHANGELOG.md`, `CONTRIBUTING.md`, `.editorconfig`.
- GitHub Actions CI (`.github/workflows/ci.yml`): lint + tests on Python
  3.8/3.10/3.12 plus a native-pipeline smoke test.
- `Makefile` with `install`, `dev`, `test`, `lint`, `fmt`, `demo`, `clean`.
- `docs/ARCHITECTURE.md`: end-to-end design and artifact format reference.
- `pyproject.toml` metadata (authors, classifiers, URLs), dev extras, and
  `ruff`/`pytest` configuration.

### Fixed
- Define signed i64 wraparound, masked shift counts, arithmetic right shifts,
  and division/modulo overflow without undefined C behavior.
- Check load/store addresses before indexing memory; report invalid addresses
  on stderr and exit with runtime error code `4`.
- Validate IR at serialization and C emission, including instruction shape,
  integer operands, register/immediate/jump bounds, and machine dimensions.
- Reject truncated operands, invalid bytecode dimensions/operands, and trailing
  bytes consistently with `NoxIntegrityError`.
- Preserve `#` and `;` inside character literals when stripping comments, and
  report out-of-range i64 immediates as source-line syntax errors.
- Correct integrity documentation: wrong seeds and edited payloads both fail
  HMAC authentication before bytecode parsing.
- Documentation: `nox run` / `build` take a compiled `.nxa` artifact, not a
  `.nox` source (README and Makefile examples corrected).

### Changed
- Artifact format v3 stores the register count in two bytes, fixing the
  advertised 256-register maximum. The encoder writes v3; the decoder retains
  v2 support so existing artifacts do not need to be rebuilt.

## [0.2.0]

### Added
- HMAC-SHA256 integrity tag on artifacts (format v2). Tampered artifacts are
  rejected with `NoxIntegrityError`, distinct from a wrong-seed/magic failure.
- Configurable register file and linear memory (`compile --regs/--mem`).
- Test suite (`tests/test_nox.py`).
- Initial `pyproject.toml` with a `nox` console script.

### Changed
- All code comments, docstrings, CLI messages, and the README translated to
  English.
- Stricter (de)serialization: version check, truncation guards, unknown-opcode
  detection, and operand-range validation.
- CLI exit codes: 2 = syntax error, 3 = integrity error, 1 = other.

## [0.1.0]

### Added
- Initial release: `.nox` DSL → opaque Braille `.nxa` artifact → C → native
  binary via `clang -O3`. Register ISA, seed-derived cipher and carrier.
