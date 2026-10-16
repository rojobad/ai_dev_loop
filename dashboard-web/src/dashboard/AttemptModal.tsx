import React, { useEffect, useMemo, useRef, useState } from "react";

import { ContentReader } from "./ContentReader";
import type { ContentTarget, ReadableContentChunk } from "./ContentReader";
import { DashboardDataSourceError } from "./types";
import type {
  AttemptTimelineItem,
  ContentAvailability,
  DashboardDataSource,
  ProcessStream,
  ReviewContentChunk,
  ReviewContentKind,
  SourceObservation,
} from "./types";

export interface AttemptModalProps {
  attempt: AttemptTimelineItem;
  dataSource: DashboardDataSource;
  runId: string;
  onClose(): void;
}

const REVIEW_EFFECTS = new Set(["codex.bootstrap_review", "codex.resume_review"]);
const IMPLEMENTER_EFFECTS = new Set([
  "cursor.run_turn",
  "codex.implementer_bootstrap",
  "codex.implementer_resume",
]);

function isReviewer(attempt: AttemptTimelineItem): boolean {
  return attempt.component === "codex" &&
    attempt.effectKind !== null &&
    REVIEW_EFFECTS.has(attempt.effectKind);
}

function isImplementer(attempt: AttemptTimelineItem): boolean {
  return attempt.effectKind !== null &&
    IMPLEMENTER_EFFECTS.has(attempt.effectKind) &&
    (attempt.component === "cursor" || attempt.component === "codex_implementer");
}

function availabilityFor(
  kind: ReviewContentKind,
  detail: Awaited<ReturnType<DashboardDataSource["review"]>>["data"],
): ContentAvailability {
  if (kind === "prompt") return detail.content.prompt;
  if (kind === "response") return detail.content.response;
  if (kind === "review-markdown") return detail.content.reviewMarkdown;
  return detail.content.cursorFixPrompt;
}

function unavailableReviewObservation(
  observation: Awaited<ReturnType<DashboardDataSource["review"]>>,
  kind: ReviewContentKind,
  reason: string,
): SourceObservation<ReviewContentChunk> {
  return {
    ...observation,
    data: {
      available: false,
      reason,
      encoding: "base64",
      mediaType: null,
      byteOffset: 0,
      returnedBytes: 0,
      nextOffset: null,
      availableBytes: null,
      hasMore: false,
      contentBase64: "",
      sha256: null,
      runId: observation.data.runId,
      attemptId: observation.data.attemptId,
      contentKind: kind,
    },
  };
}

function ownershipError(): DashboardDataSourceError {
  return new DashboardDataSourceError(
    "INVALID_RESPONSE",
    "The content response did not match the selected run and attempt.",
  );
}

function optionalTime(value: string | null): string {
  return value ?? "not recorded";
}

export function AttemptModal({
  attempt,
  dataSource,
  runId,
  onClose,
}: AttemptModalProps): React.JSX.Element {
  const [target, setTarget] = useState<ContentTarget | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const reviewer = isReviewer(attempt);
  const implementer = isImplementer(attempt);

  useEffect(() => {
    setTarget(null);
  }, [attempt.attemptId, runId]);

  useEffect(() => {
    closeButton.current?.focus();
  }, []);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || dialog.current === null) return;
      const focusable = [...dialog.current.querySelectorAll<HTMLElement>(
        "button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])",
      )].filter((element) => !element.hasAttribute("hidden"));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      const activeIndex = active instanceof HTMLElement ? focusable.indexOf(active) : -1;
      if (!dialog.current.contains(active) || activeIndex < 0) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  const initialPromptTarget = useMemo<ContentTarget>(() => ({
    key: `${runId}:initial-prompt`,
    label: "Initial run prompt (context)",
    ownership: `Run ${runId}; context for implementer attempt ${attempt.attemptId}, not its exact execution envelope.`,
    pollable: false,
    read: async (byteOffset, limit, signal) => {
      const observation = await dataSource.initialPrompt(runId, { byteOffset, limit }, signal);
      if (observation.data.runId !== runId || observation.data.artifactKind !== "initial_prompt") {
        throw ownershipError();
      }
      return observation as SourceObservation<ReadableContentChunk>;
    },
  }), [attempt.attemptId, dataSource, runId]);

  const reviewTarget = (kind: "prompt" | "cursor-fix-prompt", label: string): ContentTarget => ({
    key: `${runId}:${attempt.attemptId}:review:${kind}`,
    label,
    ownership: `Reviewer attempt ${attempt.attemptId} in run ${runId}.`,
    pollable: false,
    read: async (byteOffset, limit, signal) => {
      if (!reviewer) throw ownershipError();
      if (byteOffset === 0) {
        const detail = await dataSource.review(runId, attempt.attemptId, signal);
        if (detail.data.runId !== runId || detail.data.attemptId !== attempt.attemptId) {
          throw ownershipError();
        }
        const availability = availabilityFor(kind, detail.data);
        if (!availability.available) {
          return unavailableReviewObservation(
            detail,
            kind,
            availability.reason ?? "unavailable",
          );
        }
      }
      const observation = await dataSource.reviewContent(
        runId,
        attempt.attemptId,
        kind,
        { byteOffset, limit },
        signal,
      );
      if (
        observation.data.runId !== runId ||
        observation.data.attemptId !== attempt.attemptId ||
        observation.data.contentKind !== kind
      ) throw ownershipError();
      return observation as SourceObservation<ReadableContentChunk>;
    },
  });

  const outputTarget = (stream: ProcessStream): ContentTarget => ({
    key: `${runId}:${attempt.attemptId}:output:${stream}`,
    label: stream,
    ownership: `Captured child-process ${stream} for exact attempt ${attempt.attemptId}.`,
    pollable: true,
    read: async (byteOffset, limit, signal) => {
      const observation = await dataSource.runOutput(
        runId,
        attempt.attemptId,
        stream,
        { byteOffset, limit },
        signal,
      );
      if (
        observation.data.runId !== runId ||
        observation.data.attemptId !== attempt.attemptId ||
        observation.data.component !== attempt.component ||
        observation.data.stream !== stream
      ) throw ownershipError();
      return observation as SourceObservation<ReadableContentChunk>;
    },
  });

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <div
        aria-labelledby="attempt-modal-title"
        aria-modal="true"
        className="attempt-modal"
        ref={dialog}
        role="dialog"
      >
        <div className="modal-heading">
          <div>
            <p className="eyebrow">Exact attempt metadata</p>
            <h2 id="attempt-modal-title">Attempt {attempt.attemptId}</h2>
          </div>
          <button ref={closeButton} type="button" onClick={onClose} aria-label="Close attempt details">Close</button>
        </div>
        <dl className="attempt-modal-metadata">
          <div><dt>Run ID</dt><dd>{runId}</dd></div>
          <div><dt>Attempt ID</dt><dd>{attempt.attemptId}</dd></div>
          <div><dt>Component</dt><dd>{attempt.component}</dd></div>
          <div><dt>Effect</dt><dd>{attempt.effectKind ?? "not recorded"}</dd></div>
          <div><dt>Iteration / phase attempt</dt><dd>{attempt.iteration} / {attempt.phaseAttempt}</dd></div>
          <div><dt>Status</dt><dd>{attempt.status}</dd></div>
          <div><dt>Created</dt><dd>{attempt.createdAt}</dd></div>
          <div><dt>Launch requested</dt><dd>{optionalTime(attempt.launchRequestedAt)}</dd></div>
          <div><dt>Completed</dt><dd>{optionalTime(attempt.completedAt)}</dd></div>
          <div><dt>Observed duration</dt><dd>{attempt.observedDurationSeconds ?? "not recorded"}</dd></div>
        </dl>
        <section className="content-controls" aria-labelledby="attempt-content-heading">
          <h3 id="attempt-content-heading">Deliberate content inspection</h3>
          <p>No content is loaded until one of these controls is pressed. One target is retained in memory at a time.</p>
          <div className="content-control-buttons">
            {implementer ? (
              <button type="button" onClick={() => setTarget(initialPromptTarget)}>
                Load initial run prompt (context)
              </button>
            ) : reviewer ? (
              <>
                <button type="button" onClick={() => setTarget(reviewTarget("prompt", "Exact review prompt"))}>
                  Load exact review prompt
                </button>
                <button type="button" onClick={() => setTarget(reviewTarget("cursor-fix-prompt", "Review-owned Cursor fix prompt"))}>
                  Load review-owned fix prompt
                </button>
              </>
            ) : (
              <span className="content-unavailable">No prompt reader is defined for this component/effect pair.</span>
            )}
            {implementer && attempt.iteration > 1 ? (
              <span className="content-unavailable">Exact correction prompt unavailable: no public per-attempt binding exists.</span>
            ) : null}
            <button type="button" onClick={() => setTarget(outputTarget("stdout"))}>Load stdout</button>
            <button type="button" onClick={() => setTarget(outputTarget("stderr"))}>Load stderr</button>
          </div>
        </section>
        {target ? <ContentReader key={target.key} target={target} /> : null}
      </div>
    </div>
  );
}
