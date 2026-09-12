"""Packaged systemd timer/service asset validation for scheduler ticks."""

from __future__ import annotations

import re
from importlib import resources

_SENSITIVE_PATTERNS = (
    re.compile(r"/home/"),
    re.compile(r"/mnt/"),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"\.env"),
    re.compile(r"&&|\|\||;|`|\$\("),
)

SERVICE_NAME = "ai-dev-loop-scheduler-tick.service"
TIMER_NAME = "ai-dev-loop-scheduler-tick.timer"
EXPECTED_SERVICE_PATH = "%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EXPECTED_SERVICE_EXEC_START = "/usr/bin/env ai_dev_loop scheduler tick"
_TIMER_DIRECTIVES = {
    "OnBootSec": "30",
    "OnUnitActiveSec": "30",
    "AccuracySec": "1s",
}


def _asset_text(name: str) -> str:
    package = resources.files("ai_dev_loop.scheduler.assets")
    return (package / name).read_text(encoding="utf-8")


def load_service_template() -> str:
    return _asset_text(SERVICE_NAME)


def load_timer_template() -> str:
    return _asset_text(TIMER_NAME)


def _systemd_section_lines(content: str, section_name: str) -> list[str]:
    lines: list[str] = []
    in_section = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{section_name}]"
            continue
        if not in_section or not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        lines.append(stripped)
    return lines


def _parse_directive_assignment(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    return key, value


def _directive_values(section_lines: list[str], directive: str) -> list[str]:
    values: list[str] = []
    for line in section_lines:
        parsed = _parse_directive_assignment(line)
        if parsed is None:
            continue
        key, value = parsed
        if key == directive:
            values.append(value)
    return values


def _validate_timer_section(timer: str, errors: list[str]) -> None:
    timer_lines = _systemd_section_lines(timer, "Timer")
    if not timer_lines:
        errors.append("timer must define a [Timer] section")
        return
    for directive, expected in _TIMER_DIRECTIVES.items():
        values = _directive_values(timer_lines, directive)
        if not values:
            errors.append(f"timer must set {directive}={expected}")
            continue
        if len(values) != 1:
            errors.append(f"timer must contain exactly one {directive} directive")
            continue
        if values[0] != expected:
            errors.append(f"timer {directive} must be {expected}")
    if "ai-dev-loop-scheduler-tick.service" not in timer:
        errors.append("timer must target the packaged one-shot service unit")


def validate_packaged_assets() -> list[str]:
    errors: list[str] = []
    service = load_service_template()
    timer = load_timer_template()
    for label, content in (("service", service), ("timer", timer)):
        for pattern in _SENSITIVE_PATTERNS:
            if pattern.search(content):
                errors.append(f"{label} template contains disallowed pattern: {pattern.pattern}")
    if "Type=oneshot" not in service:
        errors.append("service must be a one-shot tick invocation")
    if f"Environment=PATH={EXPECTED_SERVICE_PATH}" not in service:
        errors.append("service must set a constrained PATH for uv-tool CLI lookup")
    if f"ExecStart={EXPECTED_SERVICE_EXEC_START}" not in service:
        errors.append("service must invoke ai_dev_loop scheduler tick through /usr/bin/env")
    if re.search(r"(?m)^ExecStart=ai_dev_loop scheduler tick\s*$", service):
        errors.append("service must not invoke ai_dev_loop without /usr/bin/env")
    _validate_timer_section(timer, errors)
    return errors
