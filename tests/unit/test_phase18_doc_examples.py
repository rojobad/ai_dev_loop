"""Documentation example guards for Phase 18 scheduler command shapes."""

from __future__ import annotations

from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"


@pytest.mark.parametrize(
    "relative_path",
    [
        "referencia/cli.md",
        "guia/guia-rapida.md",
        "operacion/prepare-start-resume-abort.md",
        "guia/flujo-handoff.md",
        "referencia/configuracion.md",
        "operacion/seguridad-privacidad.md",
        "operacion/estado-artefactos.md",
    ],
)
def test_scheduler_start_examples_do_not_require_controller_flag(relative_path: str) -> None:
    text = (DOCS_ROOT / relative_path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if "scheduler start" not in line:
            continue
        assert "--controller-session-id" not in line, (
            f"{relative_path} still documents controller flag on scheduler start: {line.strip()}"
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "operacion/prepare-start-resume-abort.md",
        "guia/flujo-handoff.md",
        "referencia/configuracion.md",
    ],
)
def test_submit_docs_do_not_claim_controller_is_mandatory(relative_path: str) -> None:
    text = (DOCS_ROOT / relative_path).read_text(encoding="utf-8")
    lowered = text.lower()
    assert "--controller-session-id` son obligatorios" not in lowered
    assert "controller-session-id`, `--codex-review-model` y" not in lowered
    assert "opcional" in lowered or "optional" in lowered
