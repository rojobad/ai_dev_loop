import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { formatInstant } from "./time";
import { DashboardDataSourceError } from "./types";
import type {
  ArtifactContentChunk,
  FrozenPromptChunk,
  ProcessOutputChunk,
  ReviewContentChunk,
  SourceObservation,
} from "./types";

export const CONTENT_CHUNK_BYTES = 65_536;
export const CONTENT_RAW_WINDOW_BYTES = 1_048_576;
export const CONTENT_DECODED_WINDOW_BYTES = 2_097_152;

type BinaryBytes = Uint8Array<ArrayBufferLike>;

export type ReadableContentChunk = FrozenPromptChunk | ReviewContentChunk | ProcessOutputChunk;

export interface ContentTarget {
  key: string;
  label: string;
  ownership: string;
  pollable: boolean;
  read(
    byteOffset: number,
    limit: number,
    signal: AbortSignal,
  ): Promise<SourceObservation<ReadableContentChunk>>;
}

export interface ContentReaderProps {
  target: ContentTarget;
  timeZone?: string;
}

interface ReaderState {
  status: "loading" | "ready" | "unavailable" | "error";
  text: string;
  rawBytes: number;
  windowStart: number;
  nextOffset: number | null;
  hasMore: boolean;
  complete: boolean | null;
  truncatedAtSource: boolean | null;
  availableBytes: number | null;
  observedAt: string | null;
  reason: string | null;
  error: string | null;
  search: string;
}

function initialState(): ReaderState {
  return {
    status: "loading",
    text: "",
    rawBytes: 0,
    windowStart: 0,
    nextOffset: null,
    hasMore: false,
    complete: null,
    truncatedAtSource: null,
    availableBytes: null,
    observedAt: null,
    reason: null,
    error: null,
    search: "",
  };
}

function decodeBase64(value: string, expectedBytes: number): BinaryBytes {
  let decoded: string;
  try {
    decoded = atob(value);
  } catch {
    throw new DashboardDataSourceError(
      "INVALID_RESPONSE",
      "The content response contained invalid base64.",
    );
  }
  if (decoded.length !== expectedBytes) {
    throw new DashboardDataSourceError(
      "INVALID_RESPONSE",
      "The content byte count did not match the response metadata.",
    );
  }
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

function rollingBytes(previous: BinaryBytes, incoming: BinaryBytes): BinaryBytes {
  const joined = new Uint8Array(previous.length + incoming.length);
  joined.set(previous);
  joined.set(incoming, previous.length);
  return joined.slice(Math.max(0, joined.length - 4));
}

export function incompleteUtf8Tail(value: BinaryBytes): BinaryBytes {
  if (value.length === 0) return value;
  let continuationCount = 0;
  let cursor = value.length - 1;
  while (cursor >= 0 && continuationCount < 3 && (value[cursor] & 0xc0) === 0x80) {
    continuationCount += 1;
    cursor -= 1;
  }
  if (cursor < 0) return new Uint8Array();
  const lead = value[cursor];
  const expectedContinuations =
    (lead & 0xe0) === 0xc0 ? 1 : (lead & 0xf0) === 0xe0 ? 2 : (lead & 0xf8) === 0xf0 ? 3 : 0;
  if (expectedContinuations > continuationCount) return value.slice(cursor);
  return new Uint8Array();
}

function processChunk(value: ReadableContentChunk): value is ProcessOutputChunk {
  return "complete" in value;
}

function clearForIntegrity(state: ReaderState, message: string): ReaderState {
  return {
    ...initialState(),
    status: "error",
    windowStart: state.windowStart,
    error: message,
  };
}

function countLiteralMatches(text: string, query: string): number {
  if (query.length === 0) return 0;
  let matches = 0;
  let offset = 0;
  while (offset <= text.length - query.length) {
    const found = text.indexOf(query, offset);
    if (found < 0) break;
    matches += 1;
    offset = found + Math.max(1, query.length);
  }
  return matches;
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The content read failed.";
}

function assertChunkOwnership(chunk: ArtifactContentChunk, requestedOffset: number): void {
  if (chunk.byteOffset !== requestedOffset) {
    throw new DashboardDataSourceError(
      "INVALID_RESPONSE",
      "The content response did not match the requested byte offset.",
    );
  }
}

export function ContentReader({ target, timeZone = "UTC" }: ContentReaderProps): React.JSX.Element {
  const [state, setState] = useState<ReaderState>(initialState);
  const generation = useRef(0);
  const requestSerial = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const decoder = useRef(new TextDecoder("utf-8"));
  const text = useRef("");
  const rawBytes = useRef(0);
  const rolling = useRef<BinaryBytes>(new Uint8Array());
  const nextOffset = useRef<number | null>(null);
  const reader = useRef<HTMLElement>(null);

  const resetBuffers = useCallback((
    windowStart = 0,
    carry: BinaryBytes = new Uint8Array(),
  ) => {
    decoder.current = new TextDecoder("utf-8");
    text.current = decoder.current.decode(carry, { stream: true });
    rawBytes.current = carry.length;
    rolling.current = carry.slice(-4);
    nextOffset.current = null;
    setState({
      ...initialState(),
      text: text.current,
      rawBytes: rawBytes.current,
      windowStart,
    });
  }, []);

  const loadAt = useCallback(async (offset: number) => {
    controller.current?.abort();
    const requestController = new AbortController();
    controller.current = requestController;
    const ownedGeneration = generation.current;
    const ownedSerial = ++requestSerial.current;
    const remaining = CONTENT_RAW_WINDOW_BYTES - rawBytes.current;
    if (remaining <= 0) return;
    const limit = Math.min(CONTENT_CHUNK_BYTES, remaining);
    setState((current) => ({ ...current, status: "loading", error: null, reason: null }));
    try {
      const observation = await target.read(offset, limit, requestController.signal);
      if (
        requestController.signal.aborted ||
        ownedGeneration !== generation.current ||
        ownedSerial !== requestSerial.current
      ) return;
      const chunk = observation.data;
      assertChunkOwnership(chunk, offset);
      if (target.pollable !== processChunk(chunk)) {
        throw new DashboardDataSourceError(
          "INVALID_RESPONSE",
          "The content response did not match the selected reader type.",
        );
      }
      if (!chunk.available) {
        decoder.current = new TextDecoder("utf-8");
        text.current = "";
        rawBytes.current = 0;
        rolling.current = new Uint8Array();
        nextOffset.current = null;
        setState((current) => ({
          ...initialState(),
          status: "unavailable",
          windowStart: current.windowStart,
          reason: chunk.reason ?? "unavailable",
          observedAt: observation.observedAt,
        }));
        return;
      }
      if (chunk.returnedBytes > limit) {
        throw new DashboardDataSourceError(
          "INVALID_RESPONSE",
          "The content response exceeded the requested chunk size.",
        );
      }
      if (chunk.nextOffset !== null && chunk.nextOffset !== offset + chunk.returnedBytes) {
        throw new DashboardDataSourceError(
          "INVALID_RESPONSE",
          "The content response did not advance to the expected byte offset.",
        );
      }
      const bytes = decodeBase64(chunk.contentBase64, chunk.returnedBytes);
      if (rawBytes.current + bytes.length > CONTENT_RAW_WINDOW_BYTES) {
        throw new DashboardDataSourceError(
          "INVALID_RESPONSE",
          "The content response exceeded the raw window bound.",
        );
      }
      const complete = processChunk(chunk) ? chunk.complete : null;
      const terminal = !chunk.hasMore && (complete === null || complete);
      const decoded = decoder.current.decode(bytes, { stream: !terminal });
      const updatedText = text.current + decoded;
      if (updatedText.length * 2 > CONTENT_DECODED_WINDOW_BYTES) {
        throw new DashboardDataSourceError(
          "INVALID_RESPONSE",
          "The decoded content exceeded the text window bound.",
        );
      }
      text.current = updatedText;
      rawBytes.current += bytes.length;
      rolling.current = rollingBytes(rolling.current, bytes);
      nextOffset.current = chunk.nextOffset;
      setState((current) => ({
        ...current,
        status: "ready",
        text: text.current,
        rawBytes: rawBytes.current,
        nextOffset: chunk.nextOffset,
        hasMore: chunk.hasMore,
        complete,
        truncatedAtSource: processChunk(chunk) ? chunk.truncatedAtSource : null,
        availableBytes: chunk.availableBytes,
        observedAt: observation.observedAt,
        reason: null,
        error: null,
      }));
    } catch (error) {
      if (
        requestController.signal.aborted ||
        ownedGeneration !== generation.current ||
        ownedSerial !== requestSerial.current
      ) return;
      const retire =
        error instanceof DashboardDataSourceError &&
        (error.code === "DATA_INTEGRITY" || error.code === "INVALID_RESPONSE");
      setState((current) => retire
        ? clearForIntegrity(current, safeError(error))
        : { ...current, status: "error", error: safeError(error) });
      if (retire) {
        decoder.current = new TextDecoder("utf-8");
        text.current = "";
        rawBytes.current = 0;
        rolling.current = new Uint8Array();
        nextOffset.current = null;
      }
    }
  }, [target]);

  useEffect(() => {
    generation.current += 1;
    requestSerial.current += 1;
    controller.current?.abort();
    resetBuffers(0);
    void loadAt(0);
    return () => {
      generation.current += 1;
      requestSerial.current += 1;
      controller.current?.abort();
      decoder.current = new TextDecoder("utf-8");
      text.current = "";
      rawBytes.current = 0;
      rolling.current = new Uint8Array();
      nextOffset.current = null;
    };
  }, [loadAt, resetBuffers, target.key]);

  const nextWindow = () => {
    const offset = nextOffset.current;
    if (offset === null) return;
    reader.current?.focus({ preventScroll: true });
    const carry = incompleteUtf8Tail(rolling.current);
    generation.current += 1;
    requestSerial.current += 1;
    controller.current?.abort();
    resetBuffers(offset - carry.length, carry);
    void loadAt(offset);
  };

  const matches = useMemo(
    () => countLiteralMatches(state.text, state.search),
    [state.search, state.text],
  );
  const windowFull = state.rawBytes >= CONTENT_RAW_WINDOW_BYTES;
  const canContinue = state.nextOffset !== null && (state.hasMore || state.complete === false);
  const continueAt = (offset: number) => {
    reader.current?.focus({ preventScroll: true });
    void loadAt(offset);
  };

  return (
    <section
      aria-label={`${target.label} reader`}
      className="content-reader"
      ref={reader}
      tabIndex={-1}
    >
      <div className="content-reader-heading">
        <div>
          <h3>{target.label}</h3>
          <p>{target.ownership}</p>
        </div>
        <span>{state.rawBytes.toLocaleString()} raw bytes in this window</span>
      </div>
      {state.status === "loading" ? <p className="loading-line">Reading one bounded content chunk…</p> : null}
      {state.status === "unavailable" ? (
        <p className="content-status" role="status">Unavailable: {state.reason}</p>
      ) : null}
      {state.error ? <p className="error-banner" role="alert">{state.error}</p> : null}
      {state.status === "ready" ? (
        <>
          <dl className="content-metadata">
            <div><dt>Window start</dt><dd>{state.windowStart}</dd></div>
            <div><dt>Observed bytes</dt><dd>{state.availableBytes ?? "unknown"}</dd></div>
            <div><dt>More in snapshot</dt><dd>{state.hasMore ? "yes" : "no"}</dd></div>
            <div><dt>Process complete</dt><dd>{state.complete === null ? "not applicable" : state.complete ? "yes" : "no / active"}</dd></div>
            <div><dt>Next offset</dt><dd>{state.nextOffset ?? "none"}</dd></div>
            <div><dt>Source truncated</dt><dd>{state.truncatedAtSource === null ? "not recorded" : state.truncatedAtSource ? "yes" : "no"}</dd></div>
          </dl>
          <label className="content-search">
            Literal search in this window
            <input
              maxLength={256}
              value={state.search}
              onChange={(event) => setState((current) => ({ ...current, search: event.target.value }))}
            />
            <span>{state.search ? `${matches} matches` : "Enter text to search the bounded window."}</span>
          </label>
          <pre className="content-text" tabIndex={0}>{state.text}</pre>
        </>
      ) : null}
      <div className="content-actions">
        {state.status !== "loading" && !windowFull && state.hasMore && state.nextOffset !== null ? (
          <button type="button" onClick={() => continueAt(state.nextOffset!)}>Load more</button>
        ) : null}
        {state.status !== "loading" && !windowFull && !state.hasMore && state.complete === false && state.nextOffset !== null ? (
          <button type="button" onClick={() => continueAt(state.nextOffset!)}>Check for new bytes</button>
        ) : null}
        {state.status !== "loading" && windowFull && canContinue ? (
          <button type="button" onClick={nextWindow}>Next bounded window</button>
        ) : null}
      </div>
      {state.observedAt ? <p className="observation-line">Content observed at {formatInstant(state.observedAt, timeZone)}; it is held in memory only.</p> : null}
    </section>
  );
}
