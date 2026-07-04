"""Read-only logs command."""

from __future__ import annotations

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.run_discovery import load_run


def render_logs(run_id: str, *, component: str | None = None) -> str:
    run_path, _ = load_run(run_id)
    if component is not None and component not in {"cursor", "codex", "ai_dev_loop"}:
        raise ValidationError("component must be one of: cursor, codex, ai_dev_loop")

    if component == "cursor":
        log_path = run_path / "cursor"
        if not log_path.exists():
            return "No Cursor logs found.\n"
        return _read_tree_logs(log_path)
    if component == "codex":
        log_path = run_path / "codex"
        if not log_path.exists():
            return "No Codex logs found.\n"
        return _read_tree_logs(log_path)

    main_log = run_path / "logs" / "ai_dev_loop.log"
    if not main_log.is_file():
        return "No ai_dev_loop log found.\n"
    return main_log.read_text(encoding="utf-8")


def _read_tree_logs(root) -> str:  # type: ignore[no-untyped-def]
    chunks: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(f"=== {path.name} ===\n")
            chunks.append(path.read_text(encoding="utf-8"))
            if not chunks[-1].endswith("\n"):
                chunks.append("\n")
    return "".join(chunks) if chunks else "No log files found.\n"
