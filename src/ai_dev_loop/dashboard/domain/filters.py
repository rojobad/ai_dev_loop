"""Dashboard filter parsing and public collection bounds."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIME_ZONE = "America/La_Paz"
MAX_RANGE = timedelta(days=366)
DASHBOARD_DEFAULT_LIMIT = 25
DASHBOARD_MAX_LIMIT = 100
DASHBOARD_RUNS_MAX_LIMIT = 1000
PROJECT_NAME_UNSET = object()
_RFC3339_INSTANT = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


class DashboardFilterError(ValueError):
    """A public dashboard filter failed validation."""


def _parse_rfc3339_instant(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DashboardFilterError(f"{field_name} must be an RFC3339 timestamp.")
    text = value.strip()
    if _RFC3339_INSTANT.fullmatch(text) is None:
        raise DashboardFilterError(f"{field_name} must be an RFC3339 timestamp.")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DashboardFilterError(f"{field_name} must be an RFC3339 timestamp.") from exc
    if parsed.tzinfo is None:
        raise DashboardFilterError(f"{field_name} must include an explicit UTC offset.")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    utc = value.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}".rstrip("0")
    return f"{text}Z"


@dataclass(frozen=True)
class DashboardFilters:
    from_utc: datetime
    to_utc: datetime
    time_zone: str
    zone: ZoneInfo
    project_name: str | None

    @classmethod
    def parse(
        cls,
        *,
        from_value: object,
        to_value: object,
        time_zone: object = DEFAULT_TIME_ZONE,
        project_name: object = PROJECT_NAME_UNSET,
    ) -> DashboardFilters:
        from_utc = _parse_rfc3339_instant(from_value, "from")
        to_utc = _parse_rfc3339_instant(to_value, "to")
        if from_utc >= to_utc:
            raise DashboardFilterError("from must be earlier than to.")
        if to_utc - from_utc > MAX_RANGE:
            raise DashboardFilterError("The dashboard range must not exceed 366 days.")
        if not isinstance(time_zone, str) or not time_zone.strip():
            raise DashboardFilterError("timeZone must be a valid IANA time zone name.")
        normalized_zone = time_zone.strip()
        try:
            zone = ZoneInfo(normalized_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DashboardFilterError("timeZone must be a valid IANA time zone name.") from exc
        project: str | None
        if project_name is PROJECT_NAME_UNSET:
            project = None
        else:
            if not isinstance(project_name, str) or not project_name.strip():
                raise DashboardFilterError("projectName must be a nonblank string when supplied.")
            if len(project_name.encode("utf-8")) > 256:
                raise DashboardFilterError("projectName must not exceed 256 UTF-8 bytes.")
            project = project_name
        return cls(
            from_utc=from_utc,
            to_utc=to_utc,
            time_zone=normalized_zone,
            zone=zone,
            project_name=project,
        )

    def contains(self, value: datetime) -> bool:
        instant = value.astimezone(UTC)
        return self.from_utc <= instant < self.to_utc

    @property
    def from_text(self) -> str:
        return _format_utc(self.from_utc)

    @property
    def to_text(self) -> str:
        return _format_utc(self.to_utc)


@dataclass(frozen=True)
class DashboardPage:
    offset: int
    limit: int

    @classmethod
    def parse(
        cls,
        *,
        offset: object = 0,
        limit: object = DASHBOARD_DEFAULT_LIMIT,
    ) -> DashboardPage:
        return cls._parse(offset=offset, limit=limit, maximum=DASHBOARD_MAX_LIMIT)

    @classmethod
    def parse_runs(
        cls,
        *,
        offset: object = 0,
        limit: object = DASHBOARD_DEFAULT_LIMIT,
    ) -> DashboardPage:
        """Parse the wider page bound owned only by ``dashboard.runs``."""

        return cls._parse(offset=offset, limit=limit, maximum=DASHBOARD_RUNS_MAX_LIMIT)

    @classmethod
    def _parse(
        cls,
        *,
        offset: object,
        limit: object,
        maximum: int,
    ) -> DashboardPage:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise DashboardFilterError("offset must be a nonnegative integer.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > maximum
        ):
            raise DashboardFilterError(
                f"limit must be an integer between 1 and {maximum}."
            )
        return cls(offset=offset, limit=limit)
