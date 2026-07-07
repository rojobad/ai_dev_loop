# Codex Desktop WSL sessions (Phase 8.5 placeholder)

> **Status:** Placeholder for Phase 8.5 documentation. This page preserves safety
> facts from legacy workstation notes. It is not final product documentation.

## Problem

Codex Desktop on Windows and the Codex CLI inside WSL use **separate** `.codex`
homes on different filesystems:

| Tool | Home directory | Filesystem |
| --- | --- | --- |
| Codex CLI in WSL | `/home/<user>/.codex` | native ext4 |
| Codex Desktop (Windows) | `C:\Users\<user>\.codex` (i.e. `/mnt/c/Users/<user>/.codex`) | DrvFS |

Even with **Agent environment = WSL** in desktop settings, the desktop app-server
still reads and writes the Windows `.codex`. A WSL terminal uses `$HOME` under
`/home/<user>`, so `codex resume` in bash and the desktop app show different
chats.

## Why whole-home sharing is unsafe

Do **not** set WSL `CODEX_HOME` to a `/mnt/c/.../.codex` path.

Two independent reasons:

1. **Incompatible state DB schemas.** Desktop and standalone CLI builds maintain
   `state_5.sqlite` differently; the CLI may refuse to migrate a DB written by
   the desktop.
2. **SQLite over the WSL↔Windows boundary.** SQLite (WAL mode) cannot be shared
   reliably across DrvFS (`/mnt/c`), risking locking errors and corruption.

## Safe bridge: nested symlink for rollout files only

The CLI interactive `codex resume` picker is driven by the native state-DB index,
so it cannot list sessions that live only in the Windows home. However,
`codex resume <SESSION_ID>` and `codex exec resume <SESSION_ID>` resolve a
session by reading its rollout `.jsonl` file directly.

The supported product surface is the Python CLI:

```bash
ai_dev_loop integrations sessions install
ai_dev_loop integrations sessions status
ai_dev_loop integrations sessions list
ai_dev_loop integrations sessions remove
```

The bridge creates only a nested symlink:

```text
~/.codex/sessions/from-desktop -> /mnt/c/Users/<user>/.codex/sessions
```

Each tool keeps its own `state_5.sqlite`. Only rollout JSONL files are shared.

Resume a desktop chat from WSL by exact session ID:

```bash
codex resume <SESSION_ID>
codex exec resume <SESSION_ID> "prompt"
```

## Limitations and cautions

- The `codex resume` picker (no id) does **not** list desktop chats. Use
  `ai_dev_loop integrations sessions list` or resume by exact id.
- Do not edit the same chat in the desktop app and in WSL concurrently.
- Reading/writing under `/mnt/c` is slower than native ext4.
- Resuming a desktop chat and sending a prompt appends turns to that chat's
  rollout file, as continuing in the desktop would.

## Phase 8.5 TODO

- [ ] Full install and bridge workflow with `codex-desktop-wsl` target
- [ ] Hook trust in Codex Desktop (`/hooks`)
- [ ] SessionStart context and exact session ID handoff
- [ ] Troubleshooting mispointed `from-desktop` symlinks
- [ ] Revert/remove bridge without affecting native WSL sessions
