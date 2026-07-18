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

### Optional: cache the SSH key in WSL

The autonomous GitHub PR cycle uses `gh` for the GitHub API and SSH for `git
push`. If an SSH key has a passphrase, [keychain](https://www.funtoo.org/Keychain)
can load it once per WSL session instead of asking again in every terminal:

```bash
sudo apt update
sudo apt install -y keychain
```

Add this line to `~/.bashrc` manually:

```bash
eval "$(keychain --eval --quiet id_ed25519)"
```

The first WSL terminal after a WSL/Windows restart asks for the key passphrase;
later terminals and detached workers launched from that environment reuse the
same agent.

### Workaround: a detached A worker cannot receive the passphrase

`keychain` is not enough when Codex Desktop launches a detached A worker that
does not inherit the terminal's `SSH_AUTH_SOCK`. Keep the passphrase on the key,
but expose a persistent, user-only WSL agent at a fixed socket instead.

Create `~/.config/systemd/user/ai-dev-loop-ssh-agent.service` with:

```ini
[Unit]
Description=Persistent SSH agent for ai_dev_loop GitHub publication

[Service]
Type=simple
ExecStartPre=/usr/bin/rm -f %h/.ssh/ai-dev-loop-ssh-agent.sock
ExecStart=/usr/bin/ssh-agent -D -a %h/.ssh/ai-dev-loop-ssh-agent.sock
Restart=on-failure

[Install]
WantedBy=default.target
```

Add this stanza to `~/.ssh/config` (preserve any existing host configuration):

```text
Host github.com
  IdentityAgent ~/.ssh/ai-dev-loop-ssh-agent.sock
```

Enable the agent once:

```bash
mkdir -p ~/.config/systemd/user ~/.ssh
chmod 700 ~/.ssh
chmod 600 ~/.ssh/config ~/.config/systemd/user/ai-dev-loop-ssh-agent.service
systemctl --user daemon-reload
systemctl --user enable --now ai-dev-loop-ssh-agent.service
```

From any WSL terminal where you can enter the passphrase, load the key into
that socket once:

```bash
SSH_AUTH_SOCK="$HOME/.ssh/ai-dev-loop-ssh-agent.sock" ssh-add ~/.ssh/id_ed25519
ssh -T git@github.com
```

The detached worker then uses the socket through SSH configuration; it does not
need the passphrase or inherited environment variables. Verify before a GitHub
cycle:

```bash
ai_dev_loop github doctor --repo-path /path/to/repository
```

After `wsl --shutdown` or a reboot, the service restarts but its loaded keys are
intentionally gone, so repeat only the `ssh-add` command from an accessible
terminal. Do not remove the passphrase or replace this with an unencrypted
deployment key merely to automate the prompt.

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
9. If a GitHub PR-review adjudication fails because Codex rejected the output schema before any thread side effects, use `ai_dev_loop pr-review recover --dry-run <failed-run-id>` then `pr-review recover` / `pr-review resume` on the successor. That path reuses the same PR, SHA, trigger, threads, Codex B session, and controller A, and does not post another `@codex review`.
10. If a PR-review run is already `awaiting_bot_review` but its worker launcher is absent/stale, reattach polling from controller A with `ai_dev_loop pr-review resume <run-id> --controller-session-id <exact-A>` — same run; may read PR/head for validation, but no GitHub writes, no second `@codex review`, and no Cursor/Codex invocation during the command.

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
