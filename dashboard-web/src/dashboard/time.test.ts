import { describe, expect, it } from "vitest";

import { commitFilterDraft, localDraftToUtc, presetDraft } from "./time";

describe("named-zone filter conversion", () => {
  it("uses local calendar presets and canonical UTC boundaries", () => {
    const draft = presetDraft(7, new Date("2026-10-15T16:20:30Z"), "America/La_Paz");
    expect(draft.fromLocal).toBe("2026-10-09T00:00:00");
    const committed = commitFilterDraft({ projectName: "", ...draft });
    expect(committed.fromUtc).toBe("2026-10-09T04:00:00.000Z");
    expect(committed.toUtc).toBe("2026-10-15T16:20:30.000Z");
    expect(committed).not.toHaveProperty("projectName");
  });

  it("rejects nonexistent and ambiguous drafts until an explicit matching offset is given", () => {
    expect(() => localDraftToUtc("2026-03-08T02:30", "America/New_York")).toThrow(
      "does not exist",
    );
    expect(() => localDraftToUtc("2026-11-01T01:30", "America/New_York")).toThrow(
      "ambiguous",
    );
    expect(localDraftToUtc("2026-11-01T01:30-04:00", "America/New_York")).toBe(
      "2026-11-01T05:30:00.000Z",
    );
    expect(() => localDraftToUtc("2026-11-01T01:30-03:00", "America/New_York")).toThrow(
      "does not match",
    );
  });

  it("keeps an invalid reversed draft out of the applied contract", () => {
    expect(() =>
      commitFilterDraft({
        projectName: "project-a",
        fromLocal: "2026-01-03T00:00",
        untilLocal: "2026-01-01T00:00",
        timeZone: "UTC",
      }),
    ).toThrow("earlier");
  });
});
