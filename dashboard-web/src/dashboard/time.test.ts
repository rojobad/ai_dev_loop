import { describe, expect, it } from "vitest";

import {
  commitFilterDraft,
  formatCalendarDay,
  formatDuration,
  formatInstant,
  localDraftToUtc,
  presetDraft,
} from "./time";

describe("named-zone filter conversion", () => {
  it("uses local calendar presets and canonical UTC boundaries", () => {
    const draft = presetDraft(7, new Date("2026-10-15T16:20:30Z"), "America/La_Paz");
    expect(draft.fromLocal).toBe("09/10/2026 00:00");
    const committed = commitFilterDraft({ projectName: "", ...draft });
    expect(committed.fromUtc).toBe("2026-10-09T04:00:00.000Z");
    expect(committed.toUtc).toBe("2026-10-15T16:20:00.000Z");
    expect(committed).not.toHaveProperty("projectName");
  });

  it("rejects nonexistent and ambiguous drafts until an explicit matching offset is given", () => {
    expect(() => localDraftToUtc("08/03/2026 02:30", "America/New_York")).toThrow(
      "does not exist",
    );
    expect(() => localDraftToUtc("01/11/2026 01:30", "America/New_York")).toThrow(
      "ambiguous",
    );
    expect(localDraftToUtc("01/11/2026 01:30-04:00", "America/New_York")).toBe(
      "2026-11-01T05:30:00.000Z",
    );
    expect(localDraftToUtc("01/11/2026 01:30-05:00", "America/New_York")).toBe(
      "2026-11-01T06:30:00.000Z",
    );
    expect(() => localDraftToUtc("01/11/2026 01:30-03:00", "America/New_York")).toThrow(
      "does not match",
    );
  });

  it("keeps an invalid reversed draft out of the applied contract", () => {
    expect(() =>
      commitFilterDraft({
        projectName: "project-a",
        fromLocal: "03/01/2026 00:00",
        untilLocal: "01/01/2026 00:00",
        timeZone: "UTC",
      }),
    ).toThrow("earlier");
  });

  it("formats instants, zoned calendar days, and minute-rounded durations independently", () => {
    expect(formatInstant("2026-07-01T12:34:56Z", "UTC")).toBe("01/07/2026 12:34");
    expect(formatInstant("2026-07-01T12:34:56Z", "America/La_Paz")).toBe(
      "01/07/2026 08:34",
    );
    expect(formatInstant("2026-11-01T05:30:00Z", "America/New_York")).toBe(
      "01/11/2026 01:30",
    );
    expect(formatCalendarDay("2026-01-02")).toBe("02/01/2026");
    expect([0, 29, 60, 7260, 360_060, null].map(formatDuration)).toEqual([
      "00 h 00 m",
      "<1 m",
      "00 h 01 m",
      "02 h 01 m",
      "100 h 01 m",
      "Unavailable",
    ]);
  });
});
