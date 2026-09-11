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
2. Configure `ai_dev_loop.yaml` in the target repository.
3. From controller session A, submit a frozen run:

```bash
ai_dev_loop scheduler submit \
  --repo-path /path/to/repo \
  --plan-path docs/plans/my-plan.md \
  --prompt-source-path docs/plans/prompt_my-plan.txt \
  --controller-session-id "<exact-controller-session-id>" \
  --codex-review-model "<model>" \
  --codex-review-reasoning-effort "<effort>" \
  --output json < /path/to/repo/docs/plans/prompt_my-plan.txt
```

4. Authorize the run from the same controller session:

```bash
ai_dev_loop scheduler start <run-id> --controller-session-id "<exact-controller-session-id>"
```

5. Install and enable the packaged scheduler timer while WSL is active, or run
   `ai_dev_loop scheduler tick` manually during development.
6. Observe progress with `ai_dev_loop controller status`, `scheduler status`,
   `scheduler list`, and `scheduler history`.
7. Stop safely with `ai_dev_loop scheduler abort <run-id>` when needed.

Reviewer B is created once at the first review boundary and resumed exactly on
later corrections. Never use `--last` or pass a pre-existing reviewer session ID
at submit time.

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
