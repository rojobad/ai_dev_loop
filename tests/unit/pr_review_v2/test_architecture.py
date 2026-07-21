"""Static architecture isolation tests for ai_dev_loop.pr_review_v2.domain."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

DOMAIN_ROOT = (
    Path(__file__).resolve().parents[3] / "src" / "ai_dev_loop" / "pr_review_v2" / "domain"
)
V2_ROOT = Path(__file__).resolve().parents[3] / "src" / "ai_dev_loop" / "pr_review_v2"
_MUTATION_OP_RE = re.compile(r"(?i)\bmutation\s*[({]")


ALLOWED_EXTERNAL = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
        "datetime",
        "enum",
        "typing",
        "pydantic",
        "pydantic.fields",
        "pydantic_core",
    }
)

FORBIDDEN_NAME_FRAGMENTS = (
    "sqlite",
    "subprocess",
    "socket",
    "http",
    "urllib",
    "requests",
    "pathlib",
    "uuid",
    "random",
    "tempfile",
    "os",
    "sys",
    "ai_dev_loop.state",
    "ai_dev_loop.config",
    "ai_dev_loop.workflow_engine",
    "ai_dev_loop.local_review_loop",
    "ai_dev_loop.legacy_pr_review",
    "ai_dev_loop.commands",
    "ai_dev_loop.runners",
    "ai_dev_loop.external_adjudication",
    "ai_dev_loop.github_pr_review_result",
    "ai_dev_loop.pr_review_worker",
    "ai_dev_loop.iterations",
    "ai_dev_loop.resume_planner",
)

FORBIDDEN_ATTR_CALLS = frozenset({"now", "utcnow", "uuid4", "uuid1", "random", "choice"})


def _domain_files() -> list[Path]:
    return sorted(DOMAIN_ROOT.glob("*.py"))


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_imports_are_isolated(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _assert_allowed_import(alias.name, path.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            _assert_allowed_import(module, path.name)
            if module.startswith("ai_dev_loop.") and not module.startswith(
                "ai_dev_loop.pr_review_v2.domain"
            ):
                raise AssertionError(f"{path.name} imports forbidden module {module}")


def _assert_allowed_import(module: str, filename: str) -> None:
    if not module:
        return
    if module.startswith("ai_dev_loop.pr_review_v2"):
        if not module.startswith("ai_dev_loop.pr_review_v2.domain"):
            raise AssertionError(f"{filename} imports non-domain v2 module {module}")
        return
    if module.startswith("ai_dev_loop"):
        raise AssertionError(f"{filename} imports legacy/shared module {module}")
    if module in ALLOWED_EXTERNAL:
        return
    root = module.split(".", 1)[0]
    if root in {"pydantic", "collections", "typing", "enum", "__future__", "datetime"}:
        return
    for fragment in FORBIDDEN_NAME_FRAGMENTS:
        if fragment in module:
            raise AssertionError(f"{filename} imports forbidden module {module}")
    raise AssertionError(f"{filename} imports unexpected module {module}")


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_has_no_forbidden_nondeterminism_or_io(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in FORBIDDEN_ATTR_CALLS
            and isinstance(node.value, ast.Name)
            and node.value.id in {"datetime", "uuid", "random", "time"}
        ):
            raise AssertionError(
                f"{path.name} uses forbidden attribute {node.value.id}.{node.attr}"
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"open", "Popen", "urlopen"}
        ):
            raise AssertionError(f"{path.name} calls forbidden function {node.func.id}")


def test_no_existing_production_modules_changed() -> None:
    repo = Path(__file__).resolve().parents[3]
    # Presence check: legacy modules remain and v2 does not live inside them.
    assert (repo / "src/ai_dev_loop/state.py").is_file()
    assert (repo / "src/ai_dev_loop/schemas/run-state-v1.json").is_file()
    assert (repo / "src/ai_dev_loop/local_review_loop.py").is_file()
    text = (repo / "src/ai_dev_loop/state.py").read_text(encoding="utf-8")
    assert "pr_review_v2" not in text


FORBIDDEN_LEGACY_IMPORTS = (
    "ai_dev_loop.runners.github",
    "ai_dev_loop.state",
    "ai_dev_loop.config",
    "ai_dev_loop.commands",
    "ai_dev_loop.workflow_engine",
    "ai_dev_loop.local_review_loop",
    "ai_dev_loop.external_adjudication",
    "ai_dev_loop.github_pr_review_result",
    "ai_dev_loop.pr_review_worker",
    "ai_dev_loop.iterations",
    "ai_dev_loop.resume_planner",
)


def _v2_non_domain_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(V2_ROOT.rglob("*.py")):
        if "/domain/" in path.as_posix() or path.name == "__pycache__":
            continue
        if "__pycache__" in path.parts:
            continue
        files.append(path)
    return files


@pytest.mark.parametrize("path", _v2_non_domain_files(), ids=lambda p: str(p.relative_to(V2_ROOT)))
def test_v2_modules_do_not_import_legacy_pr_or_lifecycle(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        for module in modules:
            for forbidden in FORBIDDEN_LEGACY_IMPORTS:
                if module == forbidden or module.startswith(forbidden + "."):
                    raise AssertionError(f"{path.name} imports forbidden module {module}")


def test_v2_read_gateway_has_no_mutation_operations() -> None:
    gateway = (V2_ROOT / "infrastructure" / "github_read_gateway.py").read_text(encoding="utf-8")
    transport = (V2_ROOT / "infrastructure" / "gh_transport.py").read_text(encoding="utf-8")
    # Explicitly forbid GraphQL mutation documents and REST write methods in production code.
    assert "mutation(" not in gateway.lower()
    assert "mutation($" not in gateway.lower()
    assert '--method", "POST"' not in gateway + transport
    assert '--method", "PATCH"' not in gateway + transport
    assert '--method", "PUT"' not in gateway + transport
    assert '--method", "DELETE"' not in gateway + transport
    assert "addPullRequestReviewThreadReply" not in gateway + transport
    assert "resolveReviewThread" not in gateway + transport
    assert _MUTATION_OP_RE.search(gateway) is None


def test_transport_exposes_only_typed_read_operations() -> None:
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhApiTransport

    public = {
        name
        for name in dir(GhApiTransport)
        if not name.startswith("_") and callable(getattr(GhApiTransport, name))
    }
    assert "graphql" not in public
    assert "rest_get" not in public
    assert {
        "fetch_pull_request_identity",
        "fetch_issue_comments_page",
        "fetch_review_threads_page",
        "fetch_issue_comment_reactions_page",
    }.issubset(public)
