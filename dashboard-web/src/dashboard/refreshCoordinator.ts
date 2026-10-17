export const SUMMARY_REFRESH_MS = 30_000;
export const SELECTED_RUN_REFRESH_MS = 10_000;
export const CAPACITY_REFRESH_MS = 60_000;
export const DEFAULT_READ_TIMEOUT_MS = 20_000;

interface TimerRuntime {
  now(): number;
  setTimeout(callback: () => void, delayMs: number): unknown;
  clearTimeout(handle: unknown): void;
}

const browserRuntime: TimerRuntime = {
  now: () => Date.now(),
  setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clearTimeout: (handle) => window.clearTimeout(handle as number),
};

export interface RefreshObserver<T> {
  key: string;
  cadenceMs: number;
  read(signal: AbortSignal): Promise<T>;
  onSuccess(value: T): void;
  onError(error: unknown): void;
}

interface RegisteredRefresh<T = unknown> extends RefreshObserver<T> {
  token: symbol;
  timer: unknown | null;
  lastStartedAt: number | null;
}

interface ReadJob<T = unknown> {
  key: string;
  ownerKey: string | null;
  kind: "refresh" | "one-shot";
  controller: AbortController;
  read(signal: AbortSignal): Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
  timeout: unknown | null;
  externalSignal: AbortSignal | null;
  removeExternalAbort: (() => void) | null;
  onStart?: (startedAt: number) => void;
  finished: boolean;
}

function abortError(message: string): DOMException {
  return new DOMException(message, "AbortError");
}

function timeoutError(): DOMException {
  return new DOMException("The local dashboard read exceeded its bounded timeout.", "TimeoutError");
}

function isExpectedCancellation(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * One memory-only owner for every automatic and deliberate dashboard read.
 * Recurring keys never overlap, and all work shares the same two-slot queue.
 */
export class RefreshCoordinator {
  private readonly runtime: TimerRuntime;
  private readonly maxActive: number;
  private readonly timeoutMs: number;
  private readonly refreshes = new Map<string, RegisteredRefresh>();
  private readonly queue: ReadJob[] = [];
  private readonly active = new Map<string, ReadJob>();
  private paused = false;
  private visible = true;
  private disposed = false;

  constructor({
    runtime = browserRuntime,
    maxActive = 2,
    timeoutMs = DEFAULT_READ_TIMEOUT_MS,
    visible = true,
  }: {
    runtime?: TimerRuntime;
    maxActive?: number;
    timeoutMs?: number;
    visible?: boolean;
  } = {}) {
    if (!Number.isInteger(maxActive) || maxActive < 1) {
      throw new TypeError("maxActive must be a positive integer.");
    }
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new TypeError("timeoutMs must be positive.");
    }
    this.runtime = runtime;
    this.maxActive = maxActive;
    this.timeoutMs = timeoutMs;
    this.visible = visible;
  }

  observe<T>(observer: RefreshObserver<T>): () => void {
    if (this.disposed) throw new Error("The refresh coordinator is disposed.");
    if (!observer.key || !Number.isFinite(observer.cadenceMs) || observer.cadenceMs <= 0) {
      throw new TypeError("Refresh observers require a key and positive cadence.");
    }
    if (this.refreshes.has(observer.key)) {
      throw new Error(`A refresh observer already owns ${observer.key}.`);
    }
    const entry: RegisteredRefresh<T> = {
      ...observer,
      token: Symbol(observer.key),
      timer: null,
      lastStartedAt: null,
    };
    this.refreshes.set(observer.key, entry as RegisteredRefresh);
    // Registering/replacing a visible query is a deliberate read. Pause stops
    // recurrence, but does not prevent a newly applied scope or metric reading once.
    if (this.visible) this.trigger(entry, this.paused);
    return () => {
      const current = this.refreshes.get(observer.key);
      if (current?.token !== entry.token) return;
      this.clearRefreshTimer(current);
      this.refreshes.delete(observer.key);
      this.cancelOwnedRefresh(observer.key, "Refresh ownership retired.");
    };
  }

  setPaused(paused: boolean): void {
    if (this.disposed || this.paused === paused) return;
    this.paused = paused;
    if (paused) {
      this.retireRecurringWork("Automatic refresh paused.");
      return;
    }
    if (this.visible) this.triggerAllFresh();
  }

  isPaused(): boolean {
    return this.paused;
  }

  setVisible(visible: boolean): void {
    if (this.disposed || this.visible === visible) return;
    this.visible = visible;
    if (!visible) {
      this.retireRecurringWork("Document is hidden.");
      return;
    }
    if (!this.paused) this.triggerAllFresh();
  }

  refresh(key: string): void {
    const entry = this.refreshes.get(key);
    if (entry) this.trigger(entry, true);
  }

  refreshAll(): void {
    for (const entry of this.refreshes.values()) this.trigger(entry, true);
  }

  request<T>(
    key: string,
    read: (signal: AbortSignal) => Promise<T>,
    externalSignal?: AbortSignal,
  ): Promise<T> {
    if (this.disposed) return Promise.reject(abortError("The dashboard route left."));
    if (!key) return Promise.reject(new TypeError("Read jobs require a key."));
    const existing = this.active.get(key) ?? this.queue.find((job) => job.key === key);
    if (existing) {
      return Promise.reject(new Error(`A dashboard read already owns ${key}.`));
    }
    return this.enqueue({
      key,
      ownerKey: null,
      kind: "one-shot",
      read,
      externalSignal: externalSignal ?? null,
    });
  }

  requestRefreshContinuation<T>(
    ownerKey: string,
    key: string,
    read: (signal: AbortSignal) => Promise<T>,
    externalSignal?: AbortSignal,
  ): Promise<T> {
    if (this.disposed) return Promise.reject(abortError("The dashboard route left."));
    if (!this.refreshes.has(ownerKey)) {
      return Promise.reject(abortError("Refresh ownership retired."));
    }
    if (!key) return Promise.reject(new TypeError("Read jobs require a key."));
    const existing = this.active.get(key) ?? this.queue.find((job) => job.key === key);
    if (existing) {
      return Promise.reject(new Error(`A dashboard read already owns ${key}.`));
    }
    return this.enqueue({
      key,
      ownerKey,
      kind: "refresh",
      read,
      externalSignal: externalSignal ?? null,
    });
  }

  recurringCount(): number {
    return this.refreshes.size;
  }

  activeCount(): number {
    return this.active.size;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    for (const entry of this.refreshes.values()) this.clearRefreshTimer(entry);
    this.refreshes.clear();
    for (const job of [...this.queue]) this.cancelJob(job, "The dashboard route left.");
    for (const job of [...this.active.values()]) this.cancelJob(job, "The dashboard route left.");
  }

  private triggerAllFresh(): void {
    for (const entry of this.refreshes.values()) this.trigger(entry, false);
  }

  private trigger(entry: RegisteredRefresh, explicit: boolean): void {
    if (this.disposed || this.refreshes.get(entry.key)?.token !== entry.token) return;
    if ((!this.visible || this.paused) && !explicit) return;
    this.clearRefreshTimer(entry);
    if (this.active.has(entry.key) || this.queue.some((job) => job.key === entry.key)) return;
    const continuationPending = [...this.active.values(), ...this.queue].some(
      (job) => job.ownerKey === entry.key,
    );
    if (continuationPending) {
      if (!this.paused && this.visible) this.scheduleNext(entry);
      return;
    }
    void this.enqueue({
      key: entry.key,
      ownerKey: entry.key,
      kind: "refresh",
      read: entry.read,
      externalSignal: null,
      onStart: (startedAt) => {
        const current = this.refreshes.get(entry.key);
        if (current?.token === entry.token) current.lastStartedAt = startedAt;
      },
    })
      .then((value) => {
        const current = this.refreshes.get(entry.key);
        if (current?.token === entry.token) current.onSuccess(value);
      })
      .catch((error) => {
        const current = this.refreshes.get(entry.key);
        if (current?.token === entry.token && !isExpectedCancellation(error)) {
          current.onError(error);
        }
      })
      .finally(() => {
        const current = this.refreshes.get(entry.key);
        if (
          current?.token === entry.token &&
          !this.disposed &&
          !this.paused &&
          this.visible
        ) {
          this.scheduleNext(current);
        }
      });
  }

  private scheduleNext(entry: RegisteredRefresh): void {
    this.clearRefreshTimer(entry);
    const now = this.runtime.now();
    const target = (entry.lastStartedAt ?? now) + entry.cadenceMs;
    const delay = target > now ? target - now : entry.cadenceMs;
    entry.timer = this.runtime.setTimeout(() => {
      entry.timer = null;
      this.trigger(entry, false);
    }, delay);
  }

  private enqueue<T>({
    key,
    ownerKey,
    kind,
    read,
    externalSignal,
    onStart,
  }: {
    key: string;
    ownerKey: string | null;
    kind: "refresh" | "one-shot";
    read(signal: AbortSignal): Promise<T>;
    externalSignal: AbortSignal | null;
    onStart?: (startedAt: number) => void;
  }): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const controller = new AbortController();
      const job: ReadJob<T> = {
        key,
        ownerKey,
        kind,
        controller,
        read,
        resolve,
        reject,
        timeout: null,
        externalSignal,
        removeExternalAbort: null,
        onStart,
        finished: false,
      };
      if (externalSignal?.aborted) {
        reject(externalSignal.reason ?? abortError("Read ownership retired."));
        return;
      }
      if (externalSignal) {
        const abort = () => this.cancelJob(job, "Read ownership retired.");
        externalSignal.addEventListener("abort", abort, { once: true });
        job.removeExternalAbort = () => externalSignal.removeEventListener("abort", abort);
      }
      this.queue.push(job as ReadJob);
      this.pump();
    });
  }

  private pump(): void {
    if (this.disposed) return;
    while (this.active.size < this.maxActive && this.queue.length > 0) {
      const job = this.queue.shift()!;
      if (job.controller.signal.aborted) continue;
      this.active.set(job.key, job);
      const startedAt = this.runtime.now();
      job.onStart?.(startedAt);
      job.timeout = this.runtime.setTimeout(() => {
        if (!job.controller.signal.aborted) this.terminateActiveJob(job, timeoutError());
      }, this.timeoutMs);
      void job
        .read(job.controller.signal)
        .then((value) => {
          if (job.controller.signal.aborted) {
            job.reject(job.controller.signal.reason ?? abortError("Read ownership retired."));
          } else {
            job.resolve(value);
          }
        })
        .catch((error) => {
          job.reject(job.controller.signal.aborted ? job.controller.signal.reason ?? error : error);
        })
        .finally(() => this.finish(job));
    }
  }

  private finish(job: ReadJob): void {
    if (job.finished) return;
    job.finished = true;
    if (job.timeout !== null) this.runtime.clearTimeout(job.timeout);
    job.removeExternalAbort?.();
    if (this.active.get(job.key) === job) this.active.delete(job.key);
    this.pump();
  }

  private retireRecurringWork(reason: string): void {
    for (const entry of this.refreshes.values()) this.clearRefreshTimer(entry);
    for (const job of [...this.queue]) {
      if (job.kind === "refresh") this.cancelJob(job, reason);
    }
    for (const job of [...this.active.values()]) {
      if (job.kind === "refresh") this.cancelJob(job, reason);
    }
  }

  private cancelOwnedRefresh(ownerKey: string, reason: string): void {
    for (const job of [...this.queue]) {
      if (job.ownerKey === ownerKey) this.cancelJob(job, reason);
    }
    for (const job of [...this.active.values()]) {
      if (job.ownerKey === ownerKey) this.cancelJob(job, reason);
    }
  }

  private cancelJob(job: ReadJob, reason: string): void {
    const queuedIndex = this.queue.indexOf(job);
    if (queuedIndex >= 0) {
      this.queue.splice(queuedIndex, 1);
      job.removeExternalAbort?.();
      job.controller.abort(abortError(reason));
      job.reject(job.controller.signal.reason);
      return;
    }
    if (this.active.get(job.key) === job && !job.controller.signal.aborted) {
      this.terminateActiveJob(job, abortError(reason));
    }
  }

  private terminateActiveJob(job: ReadJob, error: DOMException): void {
    job.controller.abort(error);
    job.reject(error);
    this.finish(job);
  }

  private clearRefreshTimer(entry: RegisteredRefresh): void {
    if (entry.timer !== null) {
      this.runtime.clearTimeout(entry.timer);
      entry.timer = null;
    }
  }
}

export function canonicalReadKey(
  operation: string,
  parameters: Record<string, string | number | null | undefined>,
): string {
  const values = Object.entries(parameters)
    .filter((entry): entry is [string, string | number | null] => entry[1] !== undefined)
    .sort(([left], [right]) => left.localeCompare(right));
  return `${operation}:${JSON.stringify(Object.fromEntries(values))}`;
}
