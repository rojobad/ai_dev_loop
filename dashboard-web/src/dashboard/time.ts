import { DEFAULT_TIME_ZONE, type DashboardFilters } from "./types";

export interface FilterDraft {
  projectName: string;
  fromLocal: string;
  untilLocal: string;
  timeZone: string;
}

const LOCAL_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;
const OFFSET_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:\d{2})$/;

interface Parts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

function parseParts(match: RegExpMatchArray): Parts {
  return {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
    second: Number(match[6] ?? "0"),
  };
}

function formatter(timeZone: string): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
  } catch {
    throw new Error("Time zone must be a valid IANA name.");
  }
}

function partsAt(instant: Date, timeZone: string): Parts {
  const values = new Map(
    formatter(timeZone)
      .formatToParts(instant)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, Number(part.value)]),
  );
  return {
    year: values.get("year") ?? -1,
    month: values.get("month") ?? -1,
    day: values.get("day") ?? -1,
    hour: values.get("hour") ?? -1,
    minute: values.get("minute") ?? -1,
    second: values.get("second") ?? -1,
  };
}

function equalParts(left: Parts, right: Parts): boolean {
  return Object.keys(left).every(
    (key) => left[key as keyof Parts] === right[key as keyof Parts],
  );
}

function validCalendarParts(parts: Parts): boolean {
  const check = new Date(
    Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second),
  );
  return (
    check.getUTCFullYear() === parts.year &&
    check.getUTCMonth() + 1 === parts.month &&
    check.getUTCDate() === parts.day &&
    check.getUTCHours() === parts.hour &&
    check.getUTCMinutes() === parts.minute &&
    check.getUTCSeconds() === parts.second
  );
}

export function localDraftToUtc(value: string, timeZone: string): string {
  formatter(timeZone);
  const offsetMatch = value.match(OFFSET_PATTERN);
  if (offsetMatch) {
    const expected = parseParts(offsetMatch);
    if (!validCalendarParts(expected)) {
      throw new Error("Date and time are invalid.");
    }
    const instant = new Date(value);
    if (!Number.isFinite(instant.getTime()) || !equalParts(partsAt(instant, timeZone), expected)) {
      throw new Error("Explicit offset does not match the selected time zone.");
    }
    return instant.toISOString();
  }

  const localMatch = value.match(LOCAL_PATTERN);
  if (!localMatch) {
    throw new Error("Use YYYY-MM-DDTHH:mm or an RFC3339 value with an explicit offset.");
  }
  const expected = parseParts(localMatch);
  if (!validCalendarParts(expected)) {
    throw new Error("Date and time are invalid.");
  }
  const nominal = Date.UTC(
    expected.year,
    expected.month - 1,
    expected.day,
    expected.hour,
    expected.minute,
    expected.second,
  );
  const candidates: number[] = [];
  for (let offsetMinutes = -14 * 60; offsetMinutes <= 14 * 60; offsetMinutes += 15) {
    const candidate = nominal - offsetMinutes * 60_000;
    if (equalParts(partsAt(new Date(candidate), timeZone), expected)) {
      candidates.push(candidate);
    }
  }
  const unique = [...new Set(candidates)];
  if (unique.length === 0) {
    throw new Error("This local time does not exist in the selected time zone.");
  }
  if (unique.length > 1) {
    throw new Error("This local time is ambiguous; enter an RFC3339 value with an offset.");
  }
  return new Date(unique[0]).toISOString();
}

export function formatLocalDraft(instant: Date, timeZone: string): string {
  const parts = partsAt(instant, timeZone);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${parts.year}-${pad(parts.month)}-${pad(parts.day)}T${pad(parts.hour)}:${pad(
    parts.minute,
  )}:${pad(parts.second)}`;
}

export function presetDraft(
  days: 7 | 30 | 90,
  now: Date,
  timeZone = DEFAULT_TIME_ZONE,
): Pick<FilterDraft, "fromLocal" | "untilLocal" | "timeZone"> {
  const today = partsAt(now, timeZone);
  const startDay = new Date(Date.UTC(today.year, today.month - 1, today.day - (days - 1)));
  const pad = (value: number) => String(value).padStart(2, "0");
  return {
    fromLocal: `${startDay.getUTCFullYear()}-${pad(startDay.getUTCMonth() + 1)}-${pad(
      startDay.getUTCDate(),
    )}T00:00:00`,
    untilLocal: formatLocalDraft(now, timeZone),
    timeZone,
  };
}

export function initialFilterDraft(now = new Date()): FilterDraft {
  return { projectName: "", ...presetDraft(7, now, DEFAULT_TIME_ZONE) };
}

export function commitFilterDraft(draft: FilterDraft): DashboardFilters {
  const timeZone = draft.timeZone.trim();
  const fromUtc = localDraftToUtc(draft.fromLocal.trim(), timeZone);
  const toUtc = localDraftToUtc(draft.untilLocal.trim(), timeZone);
  const from = Date.parse(fromUtc);
  const to = Date.parse(toUtc);
  if (from >= to) {
    throw new Error("From must be earlier than the exclusive Until boundary.");
  }
  if (to - from > 366 * 24 * 60 * 60 * 1000) {
    throw new Error("The dashboard range must not exceed 366 days.");
  }
  const projectName = draft.projectName.trim();
  if (projectName && new TextEncoder().encode(projectName).length > 256) {
    throw new Error("Project name must not exceed 256 UTF-8 bytes.");
  }
  return {
    fromUtc,
    toUtc,
    timeZone,
    ...(projectName ? { projectName } : {}),
  };
}
