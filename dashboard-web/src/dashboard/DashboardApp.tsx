import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ActivityCalendar } from "./ActivityCalendar";
import { AttemptTimeline } from "./AttemptTimeline";
import { DashboardChart } from "./DashboardChart";
import { InformationControl } from "./InformationControl";
import { RunMap } from "./RunMap";
import { RunTokenConsumption } from "./RunTokenConsumption";
import { TokenAnalytics } from "./TokenAnalytics";
import {
  CAPACITY_REFRESH_MS,
  SUMMARY_REFRESH_MS,
  RefreshCoordinator,
  canonicalReadKey,
} from "./refreshCoordinator";
import {
  capacityOption,
  projectFromChartEvent,
  projectStateOption,
  workloadOption,
} from "./charts";
import {
  commitFilterDraft,
  formatAge,
  formatDuration,
  formatInstant,
  initialFilterDraft,
  presetDraft,
  type FilterDraft,
} from "./time";
import type {
  CodexCapacityData,
  DashboardDataSource,
  DashboardFilters,
  DashboardProjectSummary,
  DashboardRunItem,
  DashboardSummaryData,
  SourceObservation,
} from "./types";

interface AsyncObservation<T> {
  loading: boolean;
  error: string | null;
  observation?: SourceObservation<T>;
}

interface InitialState {
  draft: FilterDraft;
  filters: DashboardFilters;
}

function makeInitialState(): InitialState {
  const draft = initialFilterDraft();
  return { draft, filters: commitFilterDraft(draft) };
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The local dashboard read failed.";
}

function ObservationLine({
  observation,
  timeZone,
}: {
  observation: SourceObservation<unknown>;
  timeZone: string;
}) {
  return (
    <p className="observation-line">
      Last successful local read · {formatAge(observation.receivedAt)}
    </p>
  );
}

function ObservationProvenance({
  observation,
  timeZone,
}: {
  observation: SourceObservation<unknown>;
  timeZone: string;
}) {
  return (
    <p>
      The source observed this data at {formatInstant(observation.observedAt, timeZone)};
      the browser received it at {formatInstant(observation.receivedAt, timeZone)}. These are
      separate freshness markers and do not imply one atomic snapshot across panels.
    </p>
  );
}

function sameFilters(left: DashboardFilters, right: DashboardFilters): boolean {
  return left.fromUtc === right.fromUtc &&
    left.toUtc === right.toUtc &&
    left.timeZone === right.timeZone &&
    left.projectName === right.projectName;
}

function Kpi({ label, value }: { label: string; value: number }) {
  return (
    <article className="kpi" aria-label={`${label}: ${value}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function ProjectCompanions({
  projects,
  selected,
  selectProject,
}: {
  projects: DashboardProjectSummary[];
  selected?: string;
  selectProject: (project: string) => void;
}) {
  return (
    <div className="companion-list" aria-label="Project state chart controls">
      {projects.map((project) => (
        <button
          type="button"
          key={project.projectName}
          aria-pressed={selected === project.projectName}
          onClick={() => selectProject(project.projectName)}
        >
          <span>{project.projectName}</span>
          <span>{project.total} started</span>
          {project.stateCounts.other > 0 ? (
            <span className="other-state">
              Other: {project.stateCounts.other} · {project.otherStates.map((item) => `${item.state} (${item.count})`).join(", ")}
            </span>
          ) : null}
        </button>
      ))}
    </div>
  );
}

function CapacityPanel({
  state,
  theme,
  timeZone,
}: {
  state: AsyncObservation<CodexCapacityData>;
  theme: "light" | "dark";
  timeZone: string;
}) {
  return (
    <section className="panel" aria-labelledby="capacity-heading">
      <div className="panel-heading">
        <h2 id="capacity-heading">Current Codex capacity</h2>
        <InformationControl label="Current Codex capacity">
          <p>This is a current local observation, independent of project and historical range.</p>
          <p>Percentages and reset windows come from the explicit bounded capacity probe.</p>
          {state.observation ? (
            <ObservationProvenance observation={state.observation} timeZone={timeZone} />
          ) : null}
        </InformationControl>
      </div>
      {state.error ? (
        <p className="error-banner">
          {state.error}
          {state.observation
            ? ` · showing the last successful observation (${formatAge(state.observation.receivedAt)})`
            : ""}
        </p>
      ) : null}
      {state.loading && !state.observation ? <p>Observing current capacity…</p> : null}
      {state.observation ? (
        <>
          <ObservationLine observation={state.observation} timeZone={timeZone} />
          {state.observation.data.status === "unavailable" ? (
            <div className="empty-state">
              Capacity unavailable
              {state.observation.data.reason ? ` · ${state.observation.data.reason}` : ""}
            </div>
          ) : state.observation.data.limits.length === 0 ? (
            <div className="empty-state">No capacity windows were returned.</div>
          ) : (
            <div className="capacity-grid">
              {state.observation.data.limits.map((limit) => (
                <article className="capacity-window" key={`${limit.id}:${limit.window}`}>
                  <h3>{limit.window}</h3>
                  <DashboardChart
                    ariaLabel={`${limit.window}: ${limit.remainingPercent}% remaining`}
                    height={155}
                    option={capacityOption(limit)}
                    theme={theme}
                  />
                  <p>
                    {limit.remainingPercent}% remaining · {limit.usedPercent}% used
                  </p>
                  <p>
                    {limit.resetsAt
                      ? `Resets ${formatInstant(limit.resetsAt, timeZone)}`
                      : "Reset unavailable"}
                  </p>
                  {limit.windowDurationMinutes ? (
                    <p>{formatDuration(limit.windowDurationMinutes * 60)} window</p>
                  ) : null}
                </article>
              ))}
            </div>
          )}
        </>
      ) : null}
    </section>
  );
}

function SummaryPanels({
  observation,
  selectedProject,
  selectProject,
  theme,
  timeZone,
}: {
  observation: SourceObservation<DashboardSummaryData>;
  selectedProject?: string;
  selectProject: (project: string) => void;
  theme: "light" | "dark";
  timeZone: string;
}) {
  const summary = observation.data;
  const stateOption = useMemo(() => projectStateOption(summary.projects), [summary.projects]);
  const treeOption = useMemo(() => workloadOption(summary.workload), [summary.workload]);
  const chartEvents = useMemo(
    () => ({
      click: (event: unknown) => {
        const project = projectFromChartEvent(event);
        if (project) selectProject(project);
      },
    }),
    [selectProject],
  );

  return (
    <>
      <ObservationLine observation={observation} timeZone={timeZone} />
      <p className={`coverage coverage-${summary.coverage.dataState}`}>
        Coverage: {summary.coverage.dataState}
        {summary.coverage.warnings.length
          ? ` · ${summary.coverage.warnings.join(", ")}`
          : " · no historical evidence warnings"}
      </p>
      <div className="kpi-grid">
        <Kpi label="Runs started" value={summary.totals.runsStarted} />
        <Kpi label="Completed" value={summary.totals.completed} />
        <Kpi label="Completed with residual risk" value={summary.totals.residualRisk} />
        <Kpi label="Waiting / blocked" value={summary.totals.waitingBlocked} />
      </div>
      <div className="overview-grid">
        <section className="panel" aria-labelledby="states-heading">
          <div className="panel-heading">
            <h2 id="states-heading">Started runs by project and state</h2>
            <InformationControl label="Started runs by project and state">
              <p>The overview counts the complete first-execution cohort in the current valid scope.</p>
              <p>Unknown additive states remain grouped as Other without changing their raw labels.</p>
              <ObservationProvenance observation={observation} timeZone={timeZone} />
            </InformationControl>
          </div>
          {summary.projects.length ? (
            <>
              <DashboardChart
                ariaLabel="Stacked horizontal chart of started runs by project and state"
                height={300}
                onEvents={chartEvents}
                option={stateOption}
                theme={theme}
              />
              <ProjectCompanions
                projects={summary.projects}
                selected={selectedProject}
                selectProject={selectProject}
              />
            </>
          ) : (
            <div className="empty-state">No started runs match the applied scope.</div>
          )}
        </section>
        <section className="panel" aria-labelledby="workload-heading">
          <div className="panel-heading">
            <h2 id="workload-heading">Project workload</h2>
            <InformationControl label="Project workload">
              <p>Area is started-run count; grouping comes from authoritative sequence bindings.</p>
              <p>Presentation names label groups, while IDs remain the identity and lineage keys.</p>
              <ObservationProvenance observation={observation} timeZone={timeZone} />
            </InformationControl>
          </div>
          {summary.workload.length ? (
            <>
              <DashboardChart
                ariaLabel="Treemap of project, sequence, and standalone workload"
                height={300}
                onEvents={chartEvents}
                option={treeOption}
                theme={theme}
              />
              <div className="companion-list" aria-label="Workload treemap controls">
                {[...new Set(summary.workload.map((item) => item.projectName))].map((project) => (
                  <button
                    type="button"
                    key={project}
                    aria-pressed={selectedProject === project}
                    onClick={() => selectProject(project)}
                  >
                    Inspect {project}
                  </button>
                ))}
              </div>
            </>
          ) : (
            <div className="empty-state">No workload matches the applied scope.</div>
          )}
        </section>
      </div>
    </>
  );
}

function storedTheme(): "light" | "dark" {
  try {
    return localStorage.getItem("ai-dev-loop-dashboard-theme") === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function DashboardApp({ dataSource }: { dataSource: DashboardDataSource }) {
  const [initial] = useState(makeInitialState);
  const [draft, setDraft] = useState(initial.draft);
  const [appliedFilters, setAppliedFilters] = useState(initial.filters);
  const [revision, setRevision] = useState(0);
  const [paused, setPaused] = useState(false);
  const [coordinator] = useState(
    () => new RefreshCoordinator({ visible: document.visibilityState !== "hidden" }),
  );
  const [validationError, setValidationError] = useState<string | null>(null);
  const [projects, setProjects] = useState<string[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogSettled, setCatalogSettled] = useState(false);
  const [summary, setSummary] = useState<AsyncObservation<DashboardSummaryData>>({
    loading: true,
    error: null,
  });
  const [capacity, setCapacity] = useState<AsyncObservation<CodexCapacityData>>({
    loading: true,
    error: null,
  });
  const [theme, setTheme] = useState<"light" | "dark">(storedTheme);
  const [selectedRun, setSelectedRun] = useState<DashboardRunItem | null>(null);
  const summaryGeneration = useRef(0);
  const previousFilterKey = useRef("");
  const typedDraftInitialized = useRef(false);
  const datasetKey = useMemo(
    () =>
      canonicalReadKey("dashboard.dataset", {
        fromUtc: appliedFilters.fromUtc,
        toUtc: appliedFilters.toUtc,
        timeZone: appliedFilters.timeZone,
        projectName: appliedFilters.projectName ?? null,
        revision,
      }),
    [appliedFilters, revision],
  );

  useEffect(() => {
    if (!typedDraftInitialized.current) {
      typedDraftInitialized.current = true;
      return;
    }
    const handle = window.setTimeout(() => {
      try {
        const typedFilters = commitFilterDraft({ ...draft, projectName: "" });
        setValidationError(null);
        setAppliedFilters((current) => {
          const filters: DashboardFilters = current.projectName
            ? { ...typedFilters, projectName: current.projectName }
            : typedFilters;
          return sameFilters(current, filters) ? current : filters;
        });
      } catch (error) {
        setValidationError(safeError(error));
      }
    }, 400);
    return () => window.clearTimeout(handle);
  }, [draft.fromLocal, draft.timeZone, draft.untilLocal]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("ai-dev-loop-dashboard-theme", theme);
    } catch {
      // Theme persistence is optional; no other dashboard state is stored.
    }
  }, [theme]);

  useEffect(() => {
    const updateVisibility = () => coordinator.setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", updateVisibility);
    updateVisibility();
    return () => document.removeEventListener("visibilitychange", updateVisibility);
  }, [coordinator]);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    async function loadCatalog() {
      const names: string[] = [];
      const seen = new Set<string>();
      let offset = 0;
      do {
        const observation = await coordinator.request(
          canonicalReadKey("dashboard.projects", { offset, limit: 100 }),
          (signal) => dataSource.projects({ offset, limit: 100 }, signal),
          controller.signal,
        );
        for (const item of observation.data.items) {
          if (!seen.has(item.projectName)) {
            seen.add(item.projectName);
            names.push(item.projectName);
          }
        }
        if (!observation.data.page.hasMore) break;
        if (observation.data.page.nextOffset === null || observation.data.page.nextOffset <= offset) {
          throw new Error("Project catalog paging did not advance.");
        }
        offset = observation.data.page.nextOffset;
      } while (!controller.signal.aborted);
      if (current) {
        setProjects(names);
        setCatalogError(null);
      }
    }
    void loadCatalog()
      .catch((error) => {
        if (current && !controller.signal.aborted) setCatalogError(safeError(error));
      })
      .finally(() => {
        if (current && !controller.signal.aborted) setCatalogSettled(true);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [coordinator, dataSource]);

  useEffect(() => {
    const generation = ++summaryGeneration.current;
    const key = JSON.stringify(appliedFilters);
    const changedScope = previousFilterKey.current !== key;
    previousFilterKey.current = key;
    setSummary((current) => ({
      loading: true,
      error: null,
      observation: changedScope ? undefined : current.observation,
    }));
    return coordinator.observe({
      key: canonicalReadKey("dashboard.summary", {
        fromUtc: appliedFilters.fromUtc,
        toUtc: appliedFilters.toUtc,
        timeZone: appliedFilters.timeZone,
        projectName: appliedFilters.projectName ?? null,
        datasetKey,
      }),
      cadenceMs: SUMMARY_REFRESH_MS,
      read: (signal) => dataSource.summary(appliedFilters, signal),
      onSuccess: (observation) => {
        if (generation === summaryGeneration.current) {
          setSummary({ loading: false, error: null, observation });
        }
      },
      onError: (error) => {
        if (generation === summaryGeneration.current) {
          setSummary((current) => ({
            loading: false,
            error: safeError(error),
            observation: current.observation,
          }));
        }
      },
    });
  }, [appliedFilters, coordinator, dataSource, datasetKey]);

  useEffect(() => {
    setSelectedRun(null);
  }, [appliedFilters, revision]);

  useEffect(() => {
    if (!catalogSettled) return;
    return coordinator.observe({
      key: "machine.codex-capacity",
      cadenceMs: CAPACITY_REFRESH_MS,
      read: (signal) => dataSource.capacity(signal),
      onSuccess: (observation) => setCapacity({ loading: false, error: null, observation }),
      onError: (error) =>
        setCapacity((current) => ({
          loading: false,
          error: safeError(error),
          observation: current.observation,
        })),
    });
  }, [catalogSettled, coordinator, dataSource]);

  const selectProject = useCallback((projectName: string) => {
    setDraft((current) => ({ ...current, projectName }));
    setAppliedFilters((current) => {
      const next: DashboardFilters = projectName
        ? { ...current, projectName }
        : {
            fromUtc: current.fromUtc,
            toUtc: current.toUtc,
            timeZone: current.timeZone,
          };
      return sameFilters(current, next) ? current : next;
    });
  }, []);

  const choosePreset = (days: 7 | 30 | 90) => {
    try {
      const values = presetDraft(days, new Date(), draft.timeZone.trim());
      const nextDraft = { ...draft, ...values };
      const filters = commitFilterDraft(nextDraft);
      setDraft(nextDraft);
      setValidationError(null);
      setAppliedFilters((current) => sameFilters(current, filters) ? current : filters);
    } catch (error) {
      setValidationError(safeError(error));
    }
  };

  const togglePause = () => {
    const next = !paused;
    coordinator.setPaused(next);
    setPaused(next);
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">ai_dev_loop · local read-only dashboard</p>
          <h1>Project overview</h1>
          <p>Started runs, project workload, and current machine capacity</p>
        </div>
        <button
          className="theme-toggle"
          type="button"
          aria-label={`Use ${theme === "light" ? "dark" : "light"} theme`}
          onClick={() => setTheme((current) => (current === "light" ? "dark" : "light"))}
        >
          {theme === "light" ? "Dark" : "Light"} theme
        </button>
      </header>

      <main>
        <section className="filters" aria-labelledby="filters-heading">
          <div className="filter-title">
            <h2 id="filters-heading">Inspection scope</h2>
            <InformationControl label="Inspection scope">
              <p>Project and range presets apply immediately. Typed date and time-zone edits commit 400 ms after the complete draft becomes valid.</p>
              <p>From is inclusive and Until is exclusive. Invalid or incomplete drafts leave the last valid view unchanged.</p>
              <p>Local date format is DD/MM/yyyy HH:mm in the selected IANA zone. Ambiguous clock times require Z or an explicit ±HH:mm offset; nonexistent local times are rejected.</p>
            </InformationControl>
            <div className="preset-buttons" aria-label="Range presets">
              {([7, 30, 90] as const).map((days) => (
                <button type="button" key={days} onClick={() => choosePreset(days)}>
                  {days} days
                </button>
              ))}
            </div>
          </div>
          <div className="filter-grid">
            <label>
              Project
              <select
                value={draft.projectName}
                onChange={(event) => selectProject(event.target.value)}
              >
                <option value="">All projects</option>
                {projects.map((project) => (
                  <option key={project} value={project}>
                    {project}
                  </option>
                ))}
              </select>
            </label>
            <label>
              From
              <input
                value={draft.fromLocal}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, fromLocal: event.target.value }))
                }
              />
            </label>
            <label>
              Until (exclusive)
              <input
                value={draft.untilLocal}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, untilLocal: event.target.value }))
                }
              />
            </label>
            <label>
              IANA time zone
              <input
                value={draft.timeZone}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, timeZone: event.target.value }))
                }
              />
            </label>
          </div>
          <div className="filter-actions">
            <button type="button" onClick={() => setRevision((value) => value + 1)}>
              Refresh current scope
            </button>
            <button type="button" aria-pressed={paused} onClick={togglePause}>
              {paused ? "Resume automatic refresh" : "Pause automatic refresh"}
            </button>
            <span>
              Current: {appliedFilters.projectName ?? "All projects"} · {appliedFilters.timeZone}
            </span>
            <span className="refresh-status" aria-live="polite">
              Automatic refresh: {paused ? "paused" : "running"}
            </span>
          </div>
          {validationError ? (
            <p className="error-banner" role="alert">
              {validationError} The previous valid results remain unchanged.
            </p>
          ) : null}
          {catalogError ? <p className="error-banner">Project catalog: {catalogError}</p> : null}
        </section>

        <div className="summary-observation">
          {summary.error ? (
            <p className="error-banner" role="alert">
              Summary read: {summary.error}
              {summary.observation ? " · showing the last successful observation" : ""}
            </p>
          ) : null}
          {summary.loading ? <p className="loading-line">Reading applied overview…</p> : null}
          {summary.observation ? (
            <SummaryPanels
              observation={summary.observation}
              selectedProject={appliedFilters.projectName}
              selectProject={selectProject}
              theme={theme}
              timeZone={appliedFilters.timeZone}
            />
          ) : !summary.loading ? (
            <div className="empty-state">No overview observation is available.</div>
          ) : null}
        </div>

        <CapacityPanel
          state={capacity}
          theme={theme}
          timeZone={appliedFilters.timeZone}
        />

        <TokenAnalytics
          coordinator={coordinator}
          dataSource={dataSource}
          datasetKey={datasetKey}
          filters={appliedFilters}
          theme={theme}
        />

        <div className="linked-inspection-grid">
          {catalogSettled ? (
            <RunMap
              dataSource={dataSource}
              coordinator={coordinator}
              filters={appliedFilters}
              datasetKey={datasetKey}
              selectedRunId={selectedRun?.runId ?? null}
              onSelectRun={setSelectedRun}
              theme={theme}
            />
          ) : (
            <section className="panel" aria-labelledby="run-map-heading">
              <div className="panel-heading">
                <h2 id="run-map-heading">Linked run map</h2>
                <InformationControl label="Linked run map">
                  <p>The project catalog settles before the first bounded run cohort read.</p>
                </InformationControl>
              </div>
              <p>Settling bounded supporting reads before reading matching runs…</p>
            </section>
          )}
          <AttemptTimeline
            coordinator={coordinator}
            dataSource={dataSource}
            selectedRun={selectedRun}
            theme={theme}
            timeZone={appliedFilters.timeZone}
          />
        </div>

        <RunTokenConsumption
          coordinator={coordinator}
          dataSource={dataSource}
          datasetKey={datasetKey}
          filters={appliedFilters}
          selectedRun={selectedRun}
          theme={theme}
        />

        <ActivityCalendar
          coordinator={coordinator}
          dataSource={dataSource}
          datasetKey={datasetKey}
          filters={appliedFilters}
          theme={theme}
        />
      </main>
    </div>
  );
}
