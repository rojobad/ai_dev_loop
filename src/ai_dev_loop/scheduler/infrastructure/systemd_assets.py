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


def _asset_text(name: str) -> str:
    package = resources.files("ai_dev_loop.scheduler.assets")
    return (package / name).read_text(encoding="utf-8")


def load_service_template() -> str:
    return _asset_text(SERVICE_NAME)


def load_timer_template() -> str:
    return _asset_text(TIMER_NAME)


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
    if "OnUnitActiveSec=30" not in timer:
        errors.append("timer interval must be 30 seconds")
    if "ai-dev-loop-scheduler-tick.service" not in timer:
        errors.append("timer must target the packaged one-shot service unit")
    return errors
