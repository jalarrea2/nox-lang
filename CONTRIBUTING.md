# Contributing to NOX

Thanks for your interest in contributing! This guide covers the local workflow
and the checks that run before a pull request can be merged.

## Development setup

```bash
git clone https://github.com/jalarrea2/nox-lang
cd nox-lang
make dev          # pip install -e ".[dev]"   (pytest + ruff)
```

A C compiler (`clang`, `gcc`, or `cc`) is required for the native pipeline.
Tests that produce native output are skipped automatically when none is found.

## Everyday commands

```bash
make test         # run the test suite
make lint         # ruff check
make fmt          # ruff check --fix
make demo         # run the bundled example artifacts
```

## Pull request workflow

1. Create a branch from `main` (e.g. `feat/...`, `fix/...`, `docs/...`).
2. Keep changes focused; add or update tests for any behavior change.
3. Make sure `make lint` and `make test` pass locally.
4. Open a pull request. CI runs automatically and **must be green before the
   PR can be merged** (see below).
5. A code owner reviews and approves.

### Required checks before merge

This repository uses branch protection on `main`. A PR can only be merged once:

- the **CI** workflow (lint + tests on Python 3.8/3.10/3.12 + native smoke
  test) passes, and
- at least one review from a code owner is approved.

These rules are configured in the repository settings (see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#community--ci-governance)); the CI
workflow itself lives in [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
and needs no setup to run on your PR.

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
version and be noted in `CHANGELOG.md`, since old artifacts will stop decoding.
