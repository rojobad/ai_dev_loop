import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("./DashboardChart", () => ({
  DashboardChart: ({ ariaLabel }: { ariaLabel: string }) => (
    <button type="button" aria-label={ariaLabel}>chart</button>
  ),
}));

import { AttemptModal } from "./AttemptModal";
import { AttemptTimeline } from "./AttemptTimeline";
import { RefreshCoordinator } from "./refreshCoordinator";
import { ImmediateDataSource, observation, standaloneRun } from "./test-fixtures";
import type {
  AttemptTimelineItem,
  FrozenPromptChunk,
  ProcessOutputChunk,
  ProcessStream,
  ReviewContentChunk,
  ReviewContentKind,
  ReviewDetailData,
  SourceObservation,
} from "./types";

function base64(text: string): string {
  return btoa(text);
}

function attempt(
  attemptId: string,
  component: string,
  effectKind: string,
  iteration = 1,
): AttemptTimelineItem {
  return {
    attemptId,
    component,
    effectKind,
    iteration,
    phaseAttempt: 1,
    status: "completed",
    createdAt: "2026-01-01T01:00:00Z",
    launchRequestedAt: "2026-01-01T01:00:01Z",
    completedAt: "2026-01-01T01:01:00Z",
    observedDurationSeconds: 59,
  };
}

const reviewerAttempt = attempt(
  "attempt-reviewer",
  "codex",
  "codex.bootstrap_review",
);
const implementerAttempt = attempt(
  "attempt-codex-implementer",
  "codex_implementer",
  "codex.implementer_bootstrap",
);
const correctionAttempt = attempt(
  "attempt-correction",
  "codex_implementer",
  "codex.implementer_resume",
  2,
);

class ModalDataSource extends ImmediateDataSource {
  readonly contentCalls: Array<Record<string, unknown>> = [];

  override async initialPrompt(
    runId: string,
    read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<FrozenPromptChunk>> {
    this.contentCalls.push({ operation: "run.initial-prompt", runId, ...read });
    const content = "initial <context>";
    return observation({
      available: true,
      reason: null,
      encoding: "base64",
      mediaType: "text/plain",
      byteOffset: read.byteOffset,
      returnedBytes: content.length,
      nextOffset: null,
      availableBytes: content.length,
      hasMore: false,
      contentBase64: base64(content),
      sha256: "a".repeat(64),
      runId,
      artifactKind: "initial_prompt",
    });
  }

  override async review(
    runId: string,
    attemptId: string,
    _signal: AbortSignal,
  ): Promise<SourceObservation<ReviewDetailData>> {
    this.contentCalls.push({ operation: "run.review", runId, attemptId });
    return observation({
      runId,
      attemptId,
      iteration: 1,
      phaseAttempt: 1,
      status: "completed",
      reviewMode: "bootstrap",
      createdAt: "2026-01-01T01:00:00Z",
      launchRequestedAt: "2026-01-01T01:00:01Z",
      completedAt: "2026-01-01T01:01:00Z",
      resultState: "valid",
      content: {
        prompt: { available: true, reason: null },
        response: { available: true, reason: null },
        reviewMarkdown: { available: true, reason: null },
        cursorFixPrompt: { available: true, reason: null },
      },
      processOutput: {
        stdout: { stream: "stdout", available: true, reason: null },
        stderr: { stream: "stderr", available: true, reason: null },
      },
    });
  }

  override async reviewContent(
    runId: string,
    attemptId: string,
    kind: ReviewContentKind,
    read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<ReviewContentChunk>> {
    this.contentCalls.push({ operation: "run.review-content", runId, attemptId, kind, ...read });
    const content = kind === "prompt" ? "exact review prompt" : "review-owned fix";
    return observation({
      available: true,
      reason: null,
      encoding: "base64",
      mediaType: "text/plain",
      byteOffset: read.byteOffset,
      returnedBytes: content.length,
      nextOffset: null,
      availableBytes: content.length,
      hasMore: false,
      contentBase64: base64(content),
      sha256: "b".repeat(64),
      runId,
      attemptId,
      contentKind: kind,
    });
  }

  override async runOutput(
    runId: string,
    attemptId: string,
    stream: ProcessStream,
    read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<ProcessOutputChunk>> {
    this.contentCalls.push({ operation: "run.output", runId, attemptId, stream, ...read });
    const content = `${stream} sentinel`;
    const component = attemptId === reviewerAttempt.attemptId ? "codex" : "codex_implementer";
    return observation({
      available: true,
      reason: null,
      encoding: "base64",
      mediaType: "text/plain",
      byteOffset: read.byteOffset,
      returnedBytes: content.length,
      nextOffset: null,
      availableBytes: content.length,
      hasMore: false,
      contentBase64: base64(content),
      sha256: null,
      runId,
      attemptId,
      component,
      stream,
      format: "text",
      processState: "completed",
      complete: true,
      truncatedAtSource: false,
      storedBytes: content.length,
    });
  }
}

describe("C265-1 attempt metadata and deliberate ownership", () => {
  it("opens on metadata without content and targets the exact reviewer attempt", async () => {
    const user = userEvent.setup();
    const dataSource = new ModalDataSource();
    render(
      <AttemptModal
        attempt={reviewerAttempt}
        dataSource={dataSource}
        runId="run-review"
        onClose={() => undefined}
      />,
    );

    expect(screen.getByRole("dialog")).toHaveTextContent("attempt-reviewer");
    expect(screen.getByRole("dialog")).toHaveTextContent("codex.bootstrap_review");
    expect(dataSource.contentCalls).toEqual([]);

    await user.click(screen.getByRole("button", { name: "Load exact review prompt" }));
    expect(await screen.findByText("exact review prompt")).toBeInTheDocument();
    expect(dataSource.contentCalls.map((call) => call.operation)).toEqual([
      "run.review",
      "run.review-content",
    ]);
    expect(dataSource.contentCalls[1]).toMatchObject({
      runId: "run-review",
      attemptId: "attempt-reviewer",
      kind: "prompt",
      byteOffset: 0,
      limit: 65_536,
    });
  });

  it("labels implementer prompt context and never sends a Codex implementer to reviewer content", async () => {
    const user = userEvent.setup();
    const dataSource = new ModalDataSource();
    const view = render(
      <AttemptModal
        attempt={implementerAttempt}
        dataSource={dataSource}
        runId="run-implementer"
        onClose={() => undefined}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Load initial run prompt (context)" }));
    expect(await screen.findByText("initial <context>")).toBeInTheDocument();
    expect(screen.getByText(/not its exact execution envelope/)).toBeInTheDocument();
    expect(dataSource.contentCalls.map((call) => call.operation)).toEqual(["run.initial-prompt"]);

    view.rerender(
      <AttemptModal
        attempt={correctionAttempt}
        dataSource={dataSource}
        runId="run-implementer"
        onClose={() => undefined}
      />,
    );
    expect(screen.getByText(/Exact correction prompt unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load initial run prompt (context)" }))
      .toBeInTheDocument();
  });

  it("keeps a historical not_recorded review prompt unavailable without fabricating bytes", async () => {
    const user = userEvent.setup();
    class HistoricalDataSource extends ModalDataSource {
      override async review(runId: string, attemptId: string, signal: AbortSignal) {
        const result = await super.review(runId, attemptId, signal);
        return {
          ...result,
          data: {
            ...result.data,
            content: {
              ...result.data.content,
              prompt: { available: false, reason: "not_recorded" },
            },
          },
        };
      }
    }
    const dataSource = new HistoricalDataSource();
    render(
      <AttemptModal
        attempt={reviewerAttempt}
        dataSource={dataSource}
        runId="historical-run"
        onClose={() => undefined}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Load exact review prompt" }));
    expect(await screen.findByText("Unavailable: not_recorded")).toBeInTheDocument();
    expect(dataSource.contentCalls.map((call) => call.operation)).toEqual(["run.review"]);
  });
});

describe("C265-3 modal accessibility and focus ownership", () => {
  it("traps focus, closes with Escape, and restores the exact companion row", async () => {
    const user = userEvent.setup();
    const dataSource = new ModalDataSource();
    render(<AttemptTimeline coordinator={new RefreshCoordinator()} dataSource={dataSource} selectedRun={standaloneRun} theme="light" />);
    const row = await screen.findByRole("button", { name: /^attempt-cursor Cursor implementer/ });
    await user.click(row);
    const close = screen.getByRole("button", { name: "Close attempt details" });
    await waitFor(() => expect(close).toHaveFocus());
    row.focus();
    await user.tab();
    expect(close).toHaveFocus();
    row.focus();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Load stderr" })).toHaveFocus();
    close.focus();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Load stderr" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(row).toHaveFocus());
    expect(dataSource.contentCalls).toEqual([]);
  });
});
