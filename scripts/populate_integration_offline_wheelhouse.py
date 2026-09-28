#!/usr/bin/env python3
"""Populate the offline wheelhouse used by Phase 21.1 integration tests (network required)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WHEELHOUSE = REPO_ROOT / "tests" / "fixtures" / "integration_api" / "offline_wheelhouse"
MARKER = WHEELHOUSE / ".complete"


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    if WHEELHOUSE.exists():
        shutil.rmtree(WHEELHOUSE)
    WHEELHOUSE.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ai-dev-loop-offline-build-") as tmp:
        dist = Path(tmp) / "dist"
        dist.mkdir()
        _run(["uv", "run", "python", "-m", "build", "--outdir", str(dist)], cwd=REPO_ROOT)
        wheels = sorted(dist.glob("ai_dev_loop-*.whl"))
        if not wheels:
            raise SystemExit("build did not produce expected wheel artifact")

        seed_venv = Path(tmp) / "seed-venv"
        _run(["uv", "venv", str(seed_venv), "--python", sys.executable, "--seed"], cwd=REPO_ROOT)
        pip = seed_venv / "bin" / "python"
        download_targets = [str(wheels[-1]), "hatchling", "build"]
        _run(
            [str(pip), "-m", "pip", "download", "-d", str(WHEELHOUSE), *download_targets],
            cwd=REPO_ROOT,
        )

    MARKER.write_text("ok\n", encoding="utf-8")
    print(f"Offline wheelhouse ready at {WHEELHOUSE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
