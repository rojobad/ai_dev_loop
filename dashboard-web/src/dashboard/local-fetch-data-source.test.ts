import { describe, expect, it, vi } from "vitest";

import { LocalFetchDashboardDataSource } from "./local-fetch-data-source";
import { activityData, summaryData } from "./test-fixtures";

describe("local fetch transport", () => {
  it("owns the fixed same-origin route and source/received metadata", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          apiVersion: { major: 1, minor: 9 },
          ok: true,
          observedAt: "2026-01-03T00:00:00Z",
          data: summaryData(),
          error: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await new LocalFetchDashboardDataSource().summary(
      {
        fromUtc: "2026-01-01T00:00:00Z",
        toUtc: "2026-01-03T00:00:00Z",
        timeZone: "UTC",
      },
      new AbortController().signal,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/dashboard/read",
      expect.objectContaining({ method: "POST", cache: "no-store", credentials: "same-origin" }),
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({
      operation: "dashboard.summary",
    });
    expect(result.source).toBe("local");
    expect(result.observedAt).toBe("2026-01-03T00:00:00Z");
    expect(Date.parse(result.receivedAt)).not.toBeNaN();
  });

  it("rejects a foreign major before parsing data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            apiVersion: { major: 2, minor: 0 },
            ok: true,
            observedAt: "2026-01-03T00:00:00Z",
            data: {},
            error: null,
          }),
          { status: 200 },
        ),
      ),
    );
    await expect(
      new LocalFetchDashboardDataSource().capacity(new AbortController().signal),
    ).rejects.toMatchObject({ code: "UPDATE_REQUIRED" });
  });

  it("dispatches the selected activity metric through the typed operation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          apiVersion: { major: 1, minor: 9 },
          ok: true,
          observedAt: "2026-01-03T00:00:00Z",
          data: activityData("review-rounds"),
          error: null,
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const filters = {
      fromUtc: "2026-01-01T00:00:00Z",
      toUtc: "2026-01-03T00:00:00Z",
      timeZone: "UTC",
    };
    const result = await new LocalFetchDashboardDataSource().activity(
      filters,
      "review-rounds",
      new AbortController().signal,
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      operation: "dashboard.activity",
      parameters: { ...filters, metric: "review-rounds" },
    });
    expect(result.data.metric).toBe("review-rounds");
  });

  it("classifies HTTP 503 as transient local service saturation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            apiVersion: { major: 1, minor: 9 },
            ok: false,
            observedAt: null,
            data: null,
            error: { code: "INTERNAL_ERROR", message: "Internal error." },
          }),
          { status: 503 },
        ),
      ),
    );
    await expect(
      new LocalFetchDashboardDataSource().runInspect(
        "selected-run",
        new AbortController().signal,
      ),
    ).rejects.toMatchObject({ code: "SERVICE_UNAVAILABLE" });
  });
});
