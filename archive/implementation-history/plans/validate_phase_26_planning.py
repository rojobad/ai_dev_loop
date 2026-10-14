"""Validate Phase 26 planning files without preparing or starting scheduler work."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    plans = root / "archive/implementation-history/plans"
    sys.path.insert(0, str(root / "src"))
    from ai_dev_loop.scheduler.domain.sequence import SequenceManifest

    manifest_path = plans / "phase-26-sequence.yaml"
    manifest = SequenceManifest.model_validate(yaml.safe_load(manifest_path.read_text()))
    assert len(manifest.phases) == 7
    sections = (
        "Goals",
        "Non-Goals",
        "Scope",
        "Out of Scope",
        "Required Context",
        "Cursor Rules And Skills",
        "Architecture Guardrails",
        "Implementation Plan",
        "Testing Criteria",
        "Validation",
        "Risks Or Recovery Notes",
        "OpenQuestions",
    )
    contracts: list[str] = []
    reread: list[dict[str, int]] = []
    for ordinal, phase in enumerate(manifest.phases, 1):
        plan = root / phase.plan_path
        prompt = root / phase.prompt_source_path
        body, handoff = plan.read_text(), prompt.read_text()
        assert all(f"## {section}\n" in body for section in sections), plan.name
        assert "## OpenQuestions\n\nNone." in body, plan.name
        assert phase.plan_path in handoff
        assert prompt.name == f"prompt_{plan.stem}.txt"
        effort = "xhigh" if ordinal in (1, 2, 7) else "high"
        assert phase.codex.review_model == "gpt-6.1-sol"
        assert phase.codex.review_reasoning_effort == effort
        assert phase.cursor is not None and phase.cursor.model == "grok-4.7-high"
        assert phase.workflow is not None
        assert phase.workflow.max_review_iterations == 7
        assert phase.workflow.cursor_timeout_minutes == 90
        assert phase.workflow.codex_timeout_minutes == 90
        assert bool(phase.commit_message) == (ordinal < 7)
        own = sorted(set(re.findall(rf"C26{ordinal}-[1-9]", body)))
        assert len(own) >= 3 and all(item in handoff for item in own)
        contracts.extend(own)
        assert "ai-dev-loop-dashboard-contracts.mdc" in body
        assert "ai-dev-loop-dashboard-contracts.mdc" in handoff
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", str(prompt)],
            cwd=root,
            check=False,
        )
        assert ignored.returncode == 1, f"Ignored prompt: {prompt.name}"
        reread.append(
            {
                "ordinal": ordinal,
                "planWords": len(body.split()),
                "promptWords": len(handoff.split()),
            }
        )

    source = json.loads((plans / "phase-26-source-observations.json").read_text())
    # This historical hash check is for this planning baseline, before implementation.
    for path, digest in source["files"].items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == digest, path
    reference = plans / "phase-26-reference"
    capture = json.loads((reference / "manifest.json").read_text())
    for path, expected in capture["files"].items():
        content = (reference / path).read_bytes()
        assert len(content) == expected["bytes"], path
        assert hashlib.sha256(content).hexdigest() == expected["sha256"], path

    rules = re.findall(
        r"`(\.cursor/rules/[^`]+\.mdc)`",
        (root / manifest.phases[0].plan_path).read_text(),
    )
    assert len(rules) == 9 and all((root / path).is_file() for path in rules)
    documents = [*plans.glob("phase-26-*.md"), reference / "README.md"]
    internal_links = 0
    for document in documents:
        for _, target in re.findall(r"\[([^\]]+)\]\(([^\)]+)\)", document.read_text()):
            if target.startswith(("https:", "http:", "#")):
                continue
            resolved = (document.parent / target.split("#")[0]).resolve()
            assert resolved.is_file(), f"{document.name}: missing {target}"
            internal_links += 1

    text_files = [
        *documents,
        *plans.glob("prompt_phase-26-*.txt"),
        manifest_path,
        plans / "phase-26-source-observations.json",
        Path(__file__).resolve(),
        root / ".cursor/rules/ai-dev-loop-dashboard-contracts.mdc",
        plans.parent / "findings/phase-26-planning-validation.md",
    ]
    for path in text_files:
        result = subprocess.run(
            ["git", "diff", "--no-index", "--check", "/dev/null", str(path)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        # --no-index can return 1 for ordinary differences; whitespace errors emit text.
        assert result.returncode in (0, 1) and not (result.stdout or result.stderr), (
            path.name,
            result.returncode,
            result.stdout,
            result.stderr,
        )
    subprocess.run(["git", "diff", "--check"], cwd=root, check=True)
    print(
        json.dumps(
            {
                "status": "PASS",
                "manifestPhases": 7,
                "contracts": len(contracts),
                "rulePaths": len(rules),
                "internalLinks": internal_links,
                "sourceHashes": len(source["files"]),
                "referenceHashes": len(capture["files"]),
                "newTextWhitespaceFiles": len(text_files),
                "promptsNotIgnored": 7,
                "rereadPlanAndPromptWords": reread,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
