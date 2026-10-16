import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ActivityCalendar } from "./ActivityCalendar";
import { AttemptTimeline } from "./AttemptTimeline";
import { DashboardChart } from "./DashboardChart";
import { RunMap } from "./RunMap";
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

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toISOString().replace(".000Z", "Z") : "Unavailable";
}

function formatAge(value: string): string {
  const milliseconds = Math.max(0, Date.now() - Date.parse(value));
  if (milliseconds < 60_000) return `${Math.floor(milliseconds / 1000)}s old`;
  if (milliseconds < 3_600_000) return `${Math.floor(milliseconds / 60_000)}m old`;
  return `${Math.floor(milliseconds / 3_600_000)}h old`;
}

function ObservationLine({ observation }: { observation: SourceObservation<unknown> }) {
  return (
    <p className="observation-line">
      Local read · source observed {formatTime(observation.observedAt)} · received {formatTime(
        observation.receivedAt,
      )} · {formatAge(observation.receivedAt)}
    </p>
  );
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
}: {
  state: AsyncObservation<CodexCapacityData>;
  theme: "light" | "dark";
}) {
  return (
    <section className="panel" aria-labelledby="capacity-heading">
      <div className="panel-heading">
        <div>
          <h2 id="capacity-heading">Current Codex capacity</h2>
          <p>Current local observation · independent of project and historical range</p>
        </div>
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
          <ObservationLine observation={state.observation} />
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
                    {limit.resetsAt ? `Resets ${formatTime(limit.resetsAt)}` : "Reset unavailable"}
                  </p>
                  {limit.windowDurationMinutes ? (
                    <p>{limit.windowDurationMinutes} minute window</p>
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
}: {
  observation: SourceObservation<DashboardSummaryData>;
  selectedProject?: string;
  selectProject: (project: string) => void;
  theme: "light" | "dark";
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
      <ObservationLine observation={observation} />
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
            <div>
              <h2 id="states-heading">Started runs by project and state</h2>
              <p>Complete summary counts; unknown additive states remain grouped as Other.</p>
            </div>
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
            <div>
              <h2 id="workload-heading">Project workload</h2>
              <p>Area is started-run count; grouping comes from authoritative sequence bindings.</p>
            </div>
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
  const draftRef = useRef(draft);
  const [appliedFilters, setAppliedFilters] = useState(initial.filters);
  const appliedRef = useRef(appliedFilters);
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
    draftRef.current = draft;
  }, [draft]);

  useEffect(() => {
    appliedRef.current = appliedFilters;
  }, [appliedFilters]);

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

  const applyDraft = useCallback(() => {
    try {
      const filters = commitFilterDraft(draftRef.current);
      setValidationError(null);
      setAppliedFilters(filters);
      setRevision((value) => value + 1);
    } catch (error) {
      setValidationError(safeError(error));
    }
  }, []);

  const selectProject = useCallback((projectName: string) => {
    setDraft((current) => ({ ...current, projectName }));
    setValidationError(null);
    setAppliedFilters((current) => ({ ...current, projectName }));
    setRevision((value) => value + 1);
  }, []);

  const choosePreset = (days: 7 | 30 | 90) => {
    try {
      const values = presetDraft(days, new Date(), draft.timeZone.trim());
      setDraft((current) => ({ ...current, ...values }));
      setValidationError(null);
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
            <div>
              <h2 id="filters-heading">Applied inspection scope</h2>
              <p>From is inclusive; Until is exclusive. Draft edits do not change data until Apply.</p>
            </div>
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
                onChange={(event) =>
                  setDraft((current) => ({ ...current, projectName: event.target.value }))
                }
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
                aria-describedby="date-format-help"
                value={draft.fromLocal}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, fromLocal: event.target.value }))
                }
              />
            </label>
            <label>
              Until (exclusive)
              <input
                aria-describedby="date-format-help"
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
          <p id="date-format-help" className="field-help">
            Local format: YYYY-MM-DDTHH:mm. For ambiguous clock times, include an RFC3339 offset.
          </p>
          <div className="filter-actions">
            <button className="primary" type="button" onClick={applyDraft}>
              Apply
            </button>
            <button type="button" onClick={() => setRevision((value) => value + 1)}>
              Refresh applied scope
            </button>
            <button type="button" aria-pressed={paused} onClick={togglePause}>
              {paused ? "Resume automatic refresh" : "Pause automatic refresh"}
            </button>
            <span>
              Applied: {appliedFilters.projectName ?? "All projects"} · {appliedFilters.timeZone}
            </span>
            <span className="refresh-status" aria-live="polite">
              Automatic refresh: {paused ? "paused" : "running"}
            </span>
          </div>
          {validationError ? (
            <p className="error-banner" role="alert">
              {validationError} Previous applied results remain unchanged.
            </p>
          ) : null}
          {catalogError ? <p className="error-banner">Project catalog: {catalogError}</p> : null}
        </section>

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
          />
        ) : !summary.loading ? (
          <div className="empty-state">No overview observation is available.</div>
        ) : null}

        <CapacityPanel state={capacity} theme={theme} />

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
              <h2 id="run-map-heading">Linked run map</h2>
              <p>Settling bounded supporting reads before reading matching runs…</p>
            </section>
          )}
          <AttemptTimeline
            coordinator={coordinator}
            dataSource={dataSource}
            selectedRun={selectedRun}
            theme={theme}
          />
        </div>

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
