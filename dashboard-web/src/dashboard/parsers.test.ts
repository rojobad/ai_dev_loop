import { describe, expect, it } from "vitest";

import { parseActivityData, parseCapacityData, parseSummaryData } from "./parsers";
import { activityData, capacityData, summaryData } from "./test-fixtures";

describe("explicit public DTO parsers", () => {
  it("accepts additive fields while preserving open labels and exact aggregate counts", () => {
    const input = { ...summaryData(), futureField: "ignored" };
    const result = parseSummaryData(input);
    expect(result.projects[0].projectName).toContain("<script>");
    expect(result.projects[0].stateCounts.other).toBe(1);
    expect(result.projects[0].otherStates).toEqual([
      { state: '<future-state onmouseover="bad">', count: 1 },
    ]);
    expect(result.totals.runsStarted).toBe(7);
  });

  it("rejects raw Other aggregates that do not equal stateCounts.other", () => {
    const input = summaryData();
    input.projects[0].otherStates[0].count = 2;
    expect(() => parseSummaryData(input)).toThrow("otherStates");
  });

  it("rejects aggregate payloads whose state groups contradict the KPI authority", () => {
    const input = summaryData();
    input.projects[0].stateCounts.completed = 99;
    expect(() => parseSummaryData(input)).toThrow("state counts");
  });

  it("does not coerce missing capacity percentages to zero", () => {
    expect(parseCapacityData(capacityData).limits).toHaveLength(2);
    expect(() =>
      parseCapacityData({
        status: "available",
        reason: null,
        limits: [{ ...capacityData.limits[0], remainingPercent: undefined }],
      }),
    ).toThrow("remainingPercent");
  });

  it("keeps activity seconds typed and rejects metric/unit drift", () => {
    const input = activityData("process-hours");
    expect(parseActivityData(input).days[0].value).toBe(5400);
    expect(() => parseActivityData({ ...input, unit: "count" })).toThrow(
      "unit does not match",
    );
    expect(() => parseActivityData({ ...input, total: input.total + 1 })).toThrow(
      "must equal returned daily values",
    );
  });
});
