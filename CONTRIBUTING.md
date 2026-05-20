# Contributing

Thanks for your interest in `tradestation-data-provider`! This document covers what you
need to know to file useful issues and ship clean PRs.

## Development setup

```powershell
# Clone (uv handles the rest)
git clone https://github.com/millerlai/tradestation-data-provider.git
cd tradestation-data-provider

# Install base + dev deps. Pin to a CI-tested Python.
uv sync --extra dev --python 3.12
```

Useful one-liners:

```powershell
uv run pytest -q                          # full suite (~2s, 272 tests)
uv run pytest tests/test_sinks_pipeline.py::test_empty_pipeline_is_safe_no_op
uv run ruff check . ; uv run ruff format .
uv run mypy                               # strict on src/
uv build                                  # sdist + wheel into dist/
```

The bundled `scripts/` wrappers all shell out to `uv run …` via `scripts/_common.py`, so
`python scripts/<name>.py` works without manually activating the venv.

## Before opening a PR

CI runs `ruff check`, `ruff format --check`, `mypy`, and `pytest` on Python
3.11 / 3.12 / 3.13 × Ubuntu + Windows. Run them locally first — the same commands above
are exactly what CI invokes.

Specifically, you must keep these green:

1. **`uv run pytest -q`** — all 272 tests pass.
2. **`uv run ruff format --check .`** — no formatting drift.
3. **`uv run ruff check .`** — no lint warnings.
4. **`uv run mypy`** — strict mode, zero errors on `src/`.

Pytest is configured with `filterwarnings = ["error", "ignore::DeprecationWarning"]`
in `pyproject.toml`. A *new* warning will fail the build — fix the root cause, don't
broaden the filter.

## Coding conventions

These match the existing codebase; please follow them in new code.

- **Lint / format**: ruff (line length 100, target py311; rules `E,F,W,I,N,UP,B,SIM,RUF`).
- **Types**: mypy strict on `src/`. `tests/` is excluded but new helpers should still
  carry annotations.
- **Dataclasses**: use `slots=True`; add `frozen=True` for value types. The codebase is
  consistent on this — please don't introduce `attrs` or pydantic models for plain data.
- **Logging**: stdlib `logging` everywhere, structured kwargs via `extra={...}`. Match
  the existing event-name convention (`sink_on_tick_failed`, `sinks_loaded`, …).
- **No comments that describe what the code does.** Comments are reserved for why
  something non-obvious is there — a hidden constraint, a workaround, a subtle invariant.
- **No backwards-compat shims.** If you change behaviour, update callers and tests.
  The package is `0.x`; there is no API stability promise yet.

## Writing a new sink

Sinks are the supported extension point. To add one:

1. Subclass `tradestation_data.sinks.base.BaseSink` (or implement the `Sink` Protocol
   directly).
2. Accept `name=` as a keyword argument in `__init__` and assign it to `self.name` —
   the registry passes the YAML name through that keyword.
3. Implement only the hooks you care about (`on_tick`, `on_bar`, `should_flush`,
   `flush`, `close`). `BaseSink` provides no-op defaults.
4. Add a test in `tests/test_sinks_<yourname>.py` — see the existing sink tests for
   shape. If you need to point the registry at a fixture class, register it in
   `tests/_sink_fixtures.py` (already wired into `tests/conftest.py`).

## Filing issues

- **Bugs**: include the version (`python -c "import tradestation_data; print(tradestation_data.__version__)"`),
  OS, Python version, and a minimal reproducer.
- **Feature requests**: explain the use case before proposing an API. We're more
  interested in the problem than the solution.

## Commit messages

Short, imperative subject (under 72 chars), then a body that explains *why*. For PR
titles, same shape. We squash-merge by default, so the PR title becomes the commit
on `main`.

## Releases

Maintainers only: bump `version` in `pyproject.toml`, update `CHANGELOG.md` (move
`Unreleased` content under the new version heading + add a fresh `Unreleased` section),
commit, then `git tag vX.Y.Z && git push --tags`. The release workflow handles PyPI.
