import { describe, expect, it, vi } from "vitest";

import {
  CAPACITY_REFRESH_MS,
  SUMMARY_REFRESH_MS,
  RefreshCoordinator,
} from "./refreshCoordinator";

interface TimerEntry {
  due: number;
  callback: () => void;
}

class FakeRuntime {
  time = 0;
  nextId = 1;
  timers = new Map<number, TimerEntry>();

  now(): number {
    return this.time;
  }

  setTimeout(callback: () => void, delayMs: number): number {
    const id = this.nextId++;
    this.timers.set(id, { due: this.time + delayMs, callback });
    return id;
  }

  clearTimeout(handle: unknown): void {
    this.timers.delete(handle as number);
  }

  async advance(milliseconds: number): Promise<void> {
    const target = this.time + milliseconds;
    for (;;) {
      const next = [...this.timers.entries()]
        .filter(([, timer]) => timer.due <= target)
        .sort((left, right) => left[1].due - right[1].due || left[0] - right[0])[0];
      if (!next) break;
      this.time = next[1].due;
      this.timers.delete(next[0]);
      next[1].callback();
      for (let index = 0; index < 8; index += 1) await Promise.resolve();
    }
    this.time = target;
    for (let index = 0; index < 8; index += 1) await Promise.resolve();
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("C266-2 refresh coordinator", () => {
  it("limits active reads to two and retires owned work on hidden, Pause, and unmount", async () => {
    const runtime = new FakeRuntime();
    const coordinator = new RefreshCoordinator({
      runtime,
      timeoutMs: 1_000_000,
      maxActive: 2,
    });
    const starts: string[] = [];
    const signals = new Map<string, AbortSignal>();
    const releases = new Map<string, ReturnType<typeof deferred<string>>>();
    const errors = vi.fn();
    const retire: Array<() => void> = [];
    for (const key of ["summary", "map", "activity", "capacity"]) {
      const held = deferred<string>();
      releases.set(key, held);
      retire.push(
        coordinator.observe({
          key,
          cadenceMs: key === "capacity" ? CAPACITY_REFRESH_MS : SUMMARY_REFRESH_MS,
          read: (signal) => {
            starts.push(key);
            signals.set(key, signal);
            return held.promise;
          },
          onSuccess: vi.fn(),
          onError: errors,
        }),
      );
    }

    expect(starts).toEqual(["summary", "map"]);
    expect(coordinator.activeCount()).toBe(2);
    await runtime.advance(SUMMARY_REFRESH_MS * 2);
    expect(starts).toEqual(["summary", "map"]);

    coordinator.setVisible(false);
    expect(signals.get("summary")?.aborted).toBe(true);
    expect(signals.get("map")?.aborted).toBe(true);
    expect(coordinator.activeCount()).toBe(0);
    await runtime.advance(CAPACITY_REFRESH_MS * 2);
    expect(starts).toEqual(["summary", "map"]);

    coordinator.setVisible(true);
    expect(starts).toEqual(["summary", "map", "summary", "map"]);
    coordinator.setPaused(true);
    expect(coordinator.activeCount()).toBe(0);
    const beforeManual = starts.length;
    coordinator.refreshAll();
    expect(starts.length - beforeManual).toBe(2);
    expect(coordinator.activeCount()).toBe(2);
    coordinator.setPaused(true);

    for (const stop of retire) stop();
    expect(coordinator.recurringCount()).toBe(0);
    expect(coordinator.activeCount()).toBe(0);
    await runtime.advance(CAPACITY_REFRESH_MS * 3);
    expect(errors).not.toHaveBeenCalled();
  });

  it("uses start-to-start metric cadence and a distinct capacity cadence", async () => {
    const runtime = new FakeRuntime();
    const coordinator = new RefreshCoordinator({ runtime, timeoutMs: 1_000_000 });
    const summary = vi.fn(async () => "summary");
    const capacity = vi.fn(async () => "capacity");
    const stopSummary = coordinator.observe({
      key: "summary",
      cadenceMs: SUMMARY_REFRESH_MS,
      read: summary,
      onSuccess: vi.fn(),
      onError: vi.fn(),
    });
    const stopCapacity = coordinator.observe({
      key: "capacity",
      cadenceMs: CAPACITY_REFRESH_MS,
      read: capacity,
      onSuccess: vi.fn(),
      onError: vi.fn(),
    });
    await runtime.advance(0);
    expect(summary).toHaveBeenCalledTimes(1);
    expect(capacity).toHaveBeenCalledTimes(1);
    await runtime.advance(SUMMARY_REFRESH_MS);
    expect(summary).toHaveBeenCalledTimes(2);
    expect(capacity).toHaveBeenCalledTimes(1);
    await runtime.advance(SUMMARY_REFRESH_MS);
    expect(summary).toHaveBeenCalledTimes(3);
    expect(capacity).toHaveBeenCalledTimes(2);
    stopSummary();
    stopCapacity();
  });

  it("does not overlap or catch up a slow key and retries failures only at normal cadence", async () => {
    const runtime = new FakeRuntime();
    const coordinator = new RefreshCoordinator({ runtime, timeoutMs: 1_000_000 });
    const first = deferred<string>();
    const starts: number[] = [];
    let call = 0;
    coordinator.observe({
      key: "slow",
      cadenceMs: SUMMARY_REFRESH_MS,
      read: async () => {
        starts.push(runtime.now());
        call += 1;
        if (call === 1) return first.promise;
        if (call === 2) throw new Error("drop");
        return "recovered";
      },
      onSuccess: vi.fn(),
      onError: vi.fn(),
    });
    await runtime.advance(SUMMARY_REFRESH_MS * 2);
    expect(starts).toEqual([0]);
    first.resolve("late success");
    await runtime.advance(0);
    await runtime.advance(SUMMARY_REFRESH_MS - 1);
    expect(starts).toEqual([0]);
    await runtime.advance(1);
    expect(starts).toEqual([0, 90_000]);
    await runtime.advance(SUMMARY_REFRESH_MS - 1);
    expect(starts).toHaveLength(2);
    await runtime.advance(1);
    expect(starts).toEqual([0, 90_000, 120_000]);
  });

  it("bounds deliberate reads and never invents a sensitive polling key", async () => {
    const runtime = new FakeRuntime();
    const coordinator = new RefreshCoordinator({ runtime, timeoutMs: 50 });
    const signalSeen = deferred<AbortSignal>();
    const read = coordinator.request("run.output:deliberate", async (signal) => {
      signalSeen.resolve(signal);
      return await new Promise<string>(() => undefined);
    });
    expect(coordinator.recurringCount()).toBe(0);
    await runtime.advance(50);
    await expect(read).rejects.toMatchObject({ name: "TimeoutError" });
    expect((await signalSeen.promise).aborted).toBe(true);
    expect(coordinator.activeCount()).toBe(0);
  });

  it("owns automatic page continuations under Pause and visibility cancellation", async () => {
    const runtime = new FakeRuntime();
    const coordinator = new RefreshCoordinator({ runtime, timeoutMs: 1_000_000 });
    const ownerRead = vi.fn(async () => "first page");
    const retire = coordinator.observe({
      key: "map-owner",
      cadenceMs: SUMMARY_REFRESH_MS,
      read: ownerRead,
      onSuccess: vi.fn(),
      onError: vi.fn(),
    });
    await runtime.advance(0);

    const firstSignals: AbortSignal[] = [];
    const first = coordinator.requestRefreshContinuation(
      "map-owner",
      "map-page-1",
      async (signal) => {
        firstSignals.push(signal);
        return await new Promise<string>(() => undefined);
      },
    );
    expect(coordinator.activeCount()).toBe(1);
    coordinator.setPaused(true);
    await expect(first).rejects.toMatchObject({ name: "AbortError" });
    expect(firstSignals[0].aborted).toBe(true);
    expect(coordinator.activeCount()).toBe(0);

    coordinator.refresh("map-owner");
    await runtime.advance(0);
    expect(ownerRead).toHaveBeenCalledTimes(2);
    const held = deferred<string>();
    const deliberateWhilePaused = coordinator.requestRefreshContinuation(
      "map-owner",
      "map-page-2",
      () => held.promise,
    );
    held.resolve("second page");
    await expect(deliberateWhilePaused).resolves.toBe("second page");
    await runtime.advance(0);

    coordinator.setPaused(false);
    await runtime.advance(0);
    const hidden = coordinator.requestRefreshContinuation(
      "map-owner",
      "map-page-3",
      async () => await new Promise<string>(() => undefined),
    );
    coordinator.setVisible(false);
    await expect(hidden).rejects.toMatchObject({ name: "AbortError" });
    expect(coordinator.activeCount()).toBe(0);
    retire();
  });

  it("lets an owned continuation finish before scheduling another first-page poll", async () => {
    const runtime = new FakeRuntime();
    const coordinator = new RefreshCoordinator({ runtime, timeoutMs: 1_000_000 });
    const ownerRead = vi.fn(async () => "first page");
    const retire = coordinator.observe({
      key: "map-owner",
      cadenceMs: SUMMARY_REFRESH_MS,
      read: ownerRead,
      onSuccess: vi.fn(),
      onError: vi.fn(),
    });
    await runtime.advance(0);

    const continuation = deferred<string>();
    const pageRead = coordinator.requestRefreshContinuation(
      "map-owner",
      "map-page-1",
      () => continuation.promise,
    );
    await runtime.advance(SUMMARY_REFRESH_MS);
    expect(ownerRead).toHaveBeenCalledTimes(1);

    continuation.resolve("last page");
    await expect(pageRead).resolves.toBe("last page");
    await runtime.advance(SUMMARY_REFRESH_MS - 1);
    expect(ownerRead).toHaveBeenCalledTimes(1);
    await runtime.advance(1);
    expect(ownerRead).toHaveBeenCalledTimes(2);
    retire();
  });
});
