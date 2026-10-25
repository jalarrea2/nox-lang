# Contributing to NOX

Thanks for your interest in contributing! This guide covers the local workflow
and the checks that run before a pull request can be merged.

## Development setup

```bash
git clone https://github.com/jalarrea2/nox-lang
cd nox-lang
python3 -m venv .venv
. .venv/bin/activate
make dev          # pip install -e ".[dev]" (tests, lint, and packaging tools)
```

A C compiler (`clang`, `gcc`, or `cc`) is required for the native pipeline.
Tests that produce native output are skipped automatically when none is found.

## Everyday commands

```bash
make test         # run the test suite
make lint         # ruff check
make fmt          # ruff check --fix
make demo         # run the bundled example artifacts
make dist         # build and check wheel + source distribution
make check-dist   # verify clean installed packages and native output
```

## Pull request workflow

1. Create a branch from `main` (e.g. `feat/...`, `fix/...`, `docs/...`).
2. Keep changes focused; add or update tests for any behavior change.
3. Make sure `make lint` and `make test` pass locally.
4. Open a pull request. CI runs automatically and **must be green before the
   PR can be merged** (see below).
5. A code owner reviews and approves.

### Required checks before merge

Maintainers should configure branch protection on `main` to require:

- the **CI** jobs (Linux Clang/GCC, macOS Clang, native UBSan, and installed
  package checks) passing, and
- at least one approved review from a code owner.

These rules must be configured in the repository settings (see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#community--ci-governance)); the CI
workflow itself lives in [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
and needs no setup to run on your PR. The presence of a workflow or CODEOWNERS
file does not enable these merge gates by itself. See
[`docs/RELEASING.md`](docs/RELEASING.md) for release verification and the
artifact-only packaging workflow.

## Coding conventions

- **English only** for all code, comments, docstrings, messages, and docs.
- Follow `ruff` (config in `pyproject.toml`, line length 100).
- Match the style of the surrounding code.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):
`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.

## Changing the artifact format

The `.nxa` format is versioned (`VERSION` in `nox/codec.py`). Any change to
serialization, the cipher, the carrier, or the integrity tag **must** bump that
version and be noted in `CHANGELOG.md`. Preserve explicit legacy decoding when
possible; document any artifacts that will no longer be supported.
Tightening accepted layout text without changing serialized bytes, the cipher,
or glyph mapping does not change the bytecode version, but must document any
previously accepted text that is now rejected.
