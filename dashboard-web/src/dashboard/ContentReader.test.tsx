import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  CONTENT_CHUNK_BYTES,
  CONTENT_RAW_WINDOW_BYTES,
  ContentReader,
  incompleteUtf8Tail,
} from "./ContentReader";
import type { ContentTarget, ReadableContentChunk } from "./ContentReader";
import { observation } from "./test-fixtures";
import { DashboardDataSourceError } from "./types";
import type { FrozenPromptChunk, ProcessOutputChunk, SourceObservation } from "./types";

function encoded(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64");
}

function frozenChunk(
  bytes: Uint8Array,
  byteOffset: number,
  availableBytes: number,
  nextOffset: number | null,
): SourceObservation<FrozenPromptChunk> {
  return observation({
    available: true,
    reason: null,
    encoding: "base64",
    mediaType: "text/plain",
    byteOffset,
    returnedBytes: bytes.length,
    nextOffset,
    availableBytes,
    hasMore: nextOffset !== null,
    contentBase64: encoded(bytes),
    sha256: "a".repeat(64),
    runId: "run-reader",
    artifactKind: "initial_prompt",
  });
}

function target(
  key: string,
  read: ContentTarget["read"],
  pollable = false,
): ContentTarget {
  return { key, label: key, ownership: `Owned by ${key}`, pollable, read };
}

describe("C265-2 bounded UTF-8 content windows", () => {
  it("keeps an incomplete UTF-8 sequence across 65,536-byte chunks", async () => {
    const user = userEvent.setup();
    const requests: Array<{ offset: number; limit: number }> = [];
    const first = Uint8Array.from([0x41, 0xe2, 0x82]);
    const second = Uint8Array.from([0xac, 0x42]);
    const contentTarget = target("UTF-8 prompt", async (offset, limit) => {
      requests.push({ offset, limit });
      return offset === 0
        ? frozenChunk(first, 0, 5, 3)
        : frozenChunk(second, 3, 5, null);
    });
    render(<ContentReader target={contentTarget} />);

    expect(await screen.findByText("A")).toBeInTheDocument();
    expect(screen.queryByText(/�/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("A€B")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "UTF-8 prompt reader" })).toHaveFocus();
    expect(requests).toEqual([
      { offset: 0, limit: CONTENT_CHUNK_BYTES },
      { offset: 3, limit: CONTENT_CHUNK_BYTES },
    ]);
    expect(incompleteUtf8Tail(first)).toEqual(Uint8Array.from([0xe2, 0x82]));
  });

  it("caps raw memory at 1 MiB and advances through an explicit next window", async () => {
    const user = userEvent.setup();
    const total = CONTENT_RAW_WINDOW_BYTES + 4;
    const calls: Array<{ offset: number; limit: number }> = [];
    const contentTarget = target("Large output", async (offset, limit) => {
      calls.push({ offset, limit });
      const size = Math.min(limit, total - offset);
      const bytes = new Uint8Array(size).fill(offset < CONTENT_RAW_WINDOW_BYTES ? 0x78 : 0x79);
      const end = offset + size;
      return frozenChunk(bytes, offset, total, end < total ? end : null);
    });
    render(<ContentReader target={contentTarget} />);

    for (let expectedCalls = 2; expectedCalls <= 16; expectedCalls += 1) {
      await user.click(await screen.findByRole("button", { name: "Load more" }));
      await waitFor(() => expect(calls).toHaveLength(expectedCalls));
    }
    expect(screen.getByText("1,048,576 raw bytes in this window")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next bounded window" }));
    await waitFor(() => expect(calls).toHaveLength(17));
    expect(screen.getByRole("region", { name: "Large output reader" })).toHaveFocus();
    expect(calls.every((call) => call.limit <= CONTENT_CHUNK_BYTES)).toBe(true);
    expect(calls[16]).toEqual({ offset: CONTENT_RAW_WINDOW_BYTES, limit: CONTENT_CHUNK_BYTES });
    expect(await screen.findByText("yyyy")).toBeInTheDocument();
    expect(screen.getByText("4 raw bytes in this window")).toBeInTheDocument();
  });

  it("keeps active EOF, completion, paging and source truncation independent", async () => {
    const user = userEvent.setup();
    let reads = 0;
    const contentTarget = target("Active stdout", async (offset) => {
      reads += 1;
      const bytes = new TextEncoder().encode(offset === 0 ? "abc" : "d");
      const complete = offset !== 0;
      const data: ProcessOutputChunk = {
        available: true,
        reason: null,
        encoding: "base64",
        mediaType: "text/plain",
        byteOffset: offset,
        returnedBytes: bytes.length,
        nextOffset: complete ? null : 3,
        availableBytes: complete ? 4 : 3,
        hasMore: false,
        contentBase64: encoded(bytes),
        sha256: null,
        runId: "run-reader",
        attemptId: "attempt-active",
        component: "codex_implementer",
        stream: "stdout",
        format: "jsonl",
        processState: complete ? "completed" : "active",
        complete,
        truncatedAtSource: complete ? true : null,
        storedBytes: complete ? 4 : null,
      };
      return observation(data);
    }, true);
    render(<ContentReader target={contentTarget} />);

    expect(await screen.findByText("abc")).toBeInTheDocument();
    expect(screen.getByText("no / active")).toBeInTheDocument();
    expect(screen.getByText("not recorded")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Check for new bytes" }));
    expect(await screen.findByText("abcd")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Active stdout reader" })).toHaveFocus();
    expect(screen.getAllByText("yes")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Check for new bytes" })).not.toBeInTheDocument();
    expect(reads).toBe(2);
  });

  it("retires previously decoded bytes after an integrity/replacement failure", async () => {
    const user = userEvent.setup();
    let reads = 0;
    const contentTarget = target("Changed stderr", async (offset) => {
      reads += 1;
      if (reads === 2) {
        throw new DashboardDataSourceError(
          "DATA_INTEGRITY",
          "Process output stream changed during read.",
        );
      }
      return frozenChunk(new TextEncoder().encode("private-old-content"), offset, 40, 19);
    });
    render(<ContentReader target={contentTarget} />);
    expect(await screen.findByText("private-old-content")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("changed during read");
    expect(screen.queryByText("private-old-content")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Literal search/ })).not.toBeInTheDocument();
  });
});

describe("C265-3 content retirement", () => {
  it("aborts an old owner, ignores its late success, and clears literal search state", async () => {
    const user = userEvent.setup();
    let oldSignal: AbortSignal | undefined;
    let resolveOld!: (value: SourceObservation<ReadableContentChunk>) => void;
    const held = new Promise<SourceObservation<ReadableContentChunk>>((resolve) => {
      resolveOld = resolve;
    });
    const oldTarget = target("Old owner", (_offset, _limit, signal) => {
      oldSignal = signal;
      return held;
    });
    const literal = "<img src=x onerror=bad> literal";
    const newTarget = target("New owner", async () =>
      frozenChunk(new TextEncoder().encode(literal), 0, literal.length, null));

    const view = render(<ContentReader target={oldTarget} />);
    view.rerender(<ContentReader target={newTarget} />);
    expect(await screen.findByText(literal)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    const search = screen.getByRole("textbox", { name: /Literal search/ });
    await user.type(search, "literal");
    expect(screen.getByText("1 matches")).toBeInTheDocument();
    expect(oldSignal?.aborted).toBe(true);

    await act(async () => {
      resolveOld(frozenChunk(new TextEncoder().encode("late private reply"), 0, 18, null));
      await held;
    });
    expect(screen.queryByText("late private reply")).not.toBeInTheDocument();
    expect(screen.getByText(literal)).toBeInTheDocument();

    view.rerender(<ContentReader target={target("Third owner", async () =>
      frozenChunk(new TextEncoder().encode("third"), 0, 5, null))} />);
    expect(await screen.findByText("third")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /Literal search/ })).toHaveValue("");
    view.unmount();
  });
});
