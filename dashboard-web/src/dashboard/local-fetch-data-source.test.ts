import { afterEach, describe, expect, it, vi } from "vitest";

import { LocalFetchDashboardDataSource } from "./local-fetch-data-source";
import { activityData, cursorCodexTokenUsage, summaryData } from "./test-fixtures";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const tokenFilters = {
  fromUtc: "2026-10-01T00:00:00Z",
  toUtc: "2026-10-02T00:00:00Z",
  timeZone: "America/La_Paz",
};

function busyResponse(): Response {
  return new Response(JSON.stringify({
    apiVersion: { major: 1, minor: 13 }, ok: false, observedAt: null, data: null,
    error: { code: "INTERNAL_ERROR", message: "An internal error occurred." },
  }), { status: 503 });
}

function tokenResponse(): Response {
  return new Response(JSON.stringify({
    apiVersion: { major: 1, minor: 13 }, ok: true,
    observedAt: "2026-10-19T12:05:00Z", data: cursorCodexTokenUsage(), error: null,
  }), { status: 200 });
}

describe("local fetch transport", () => {
  it("owns the fixed same-origin route and source/received metadata", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          apiVersion: { major: 1, minor: 11 },
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
          apiVersion: { major: 1, minor: 11 },
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
            apiVersion: { major: 1, minor: 11 },
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

  it("requests cohort token usage without a run id and direct usage with the exact id", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        apiVersion: { major: 1, minor: 13 },
        ok: true,
        observedAt: "2026-10-19T12:05:00Z",
        data: cursorCodexTokenUsage(),
        error: null,
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        apiVersion: { major: 1, minor: 13 },
        ok: false,
        observedAt: null,
        data: null,
        error: { code: "INVALID_ARGUMENT", message: "Unsupported dashboard read operation." },
      }), { status: 400 }));
    vi.stubGlobal("fetch", fetchMock);
    const filters = {
      fromUtc: "2026-10-01T00:00:00Z",
      toUtc: "2026-10-02T00:00:00Z",
      timeZone: "America/La_Paz",
    };
    const source = new LocalFetchDashboardDataSource();
    const cohort = await source.tokenUsage(filters, null, new AbortController().signal);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      operation: "dashboard.token-usage",
      parameters: filters,
    });
    expect(cohort.data.source.knownTotalTokens).toBe(255);
    await expect(source.tokenUsage(filters, "run-1", new AbortController().signal))
      .rejects.toMatchObject({
        code: "INVALID_ARGUMENT",
        message: "Unsupported dashboard read operation.",
      });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).parameters.runId).toBe("run-1");
  });

  it.each([null, "exact-selected-run"])("recovers token scope %s after temporary HTTP saturation", async (runId) => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(busyResponse())
      .mockResolvedValueOnce(busyResponse())
      .mockResolvedValueOnce(tokenResponse());
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    const result = new LocalFetchDashboardDataSource().tokenUsage(tokenFilters, runId, signal);
    await vi.advanceTimersByTimeAsync(999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2000);
    expect((await result).data.source.knownTotalTokens).toBe(255);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const requests = fetchMock.mock.calls.map((call) => JSON.parse(call[1].body));
    expect(requests.every((request) => request.operation === "dashboard.token-usage")).toBe(true);
    expect(requests.every((request) => JSON.stringify(request) === JSON.stringify(requests[0]))).toBe(true);
    expect(requests[0].parameters.runId).toBe(runId ?? undefined);
    expect(fetchMock.mock.calls.every((call) => call[1].signal === signal)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("bounds saturation retries and reports temporary unavailability", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation(async () => busyResponse());
    vi.stubGlobal("fetch", fetchMock);
    const result = new LocalFetchDashboardDataSource().tokenUsage(tokenFilters, null, new AbortController().signal);
    const assertion = expect(result).rejects.toMatchObject({
      code: "SERVICE_UNAVAILABLE", message: expect.stringContaining("temporarily unavailable"),
    });
    await vi.advanceTimersByTimeAsync(7000);
    await assertion;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cancels backoff immediately when its coordinator signal retires", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation(async () => busyResponse());
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const result = new LocalFetchDashboardDataSource().tokenUsage(tokenFilters, "retired-run", controller.signal);
    const assertion = expect(result).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    await assertion;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not retry a genuine internal error", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      apiVersion: { major: 1, minor: 13 }, ok: false, observedAt: null, data: null,
      error: { code: "INTERNAL_ERROR", message: "An internal error occurred." },
    }), { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(new LocalFetchDashboardDataSource().tokenUsage(tokenFilters, null, new AbortController().signal))
      .rejects.toMatchObject({ code: "INTERNAL_ERROR" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
