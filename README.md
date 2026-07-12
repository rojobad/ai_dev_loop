# ai_dev_loop

Deterministic local orchestrator for Codex/Cursor development loops.

The main product documentation lives under `docs/` and is currently written in
Spanish. It is built with MkDocs:

```bash
uv sync --all-extras
uv run mkdocs build --strict
uv run mkdocs serve -o
```

The dev server speaks plain HTTP only. Open `http://127.0.0.1:8000/` (not `https://localhost:8000`).
Browsers such as Opera GX may upgrade bare `localhost:8000` to HTTPS and show `ERR_SSL_PROTOCOL_ERROR`.
Use `-o` to open the correct URL automatically, or type the `http://127.0.0.1:8000/` address explicitly.

Entry points:

- `docs/index.md`
- `mkdocs.yml`

## Quick Install

Development:

```bash
uv sync --all-extras
uv run ai_dev_loop --help
```

User-level tool install:

```bash
uv tool install .
ai_dev_loop --help
```

Reinstall the current local build in WSL after validation:

```bash
uv tool install --force .
which ai_dev_loop
ai_dev_loop --version
```

## Basic Workflow

1. Install the matching Codex integration:
   - `wsl-cli` when Codex runs inside WSL.
   - `codex-desktop-wsl` when Codex Desktop runs on Windows and agents run in WSL.
2. Configure `ai_dev_loop.yaml` in the target repository. Omit `codex.review_model` and `codex.review_reasoning_effort` to capture both from the exact Codex session during `prepare`, or set either override independently.
3. From the original Codex session, run `ai_dev_loop prepare` with the exact Cursor prompt on stdin. New runs freeze the captured/effective review runtime and pass it explicitly on every resume.
4. Exit or stop using the active Codex UI for that session.
5. Run `ai_dev_loop start <run-id>` from WSL.
6. Inspect the staged changes and review artifacts.
7. If a run fails after Cursor + staging with an intact staged patch, use `ai_dev_loop recover --dry-run <run-id>` then `recover` / `resume` on the successor. Do not expect ordinary `resume` to reopen a terminal `failed` run.
8. If Cursor fails with a usage limit on its configured model, recover with the same chat via `ai_dev_loop recover <run-id> --cursor-model auto` then `resume` on the successor. In a TTY, `start`/`resume` may offer this recovery after the failure; non-TTY runs require the explicit recover command.

`start` and `resume` probe WSL Cursor/Codex CLI model compatibility. In a TTY they may offer each incompatible tool's official updater; non-interactive runs require explicit `--update-tools` or `--allow-incompatible-tools`. These updates do not update Windows desktop applications.

## Validation Commands

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
uv run mkdocs build --strict
```

## License

MIT
