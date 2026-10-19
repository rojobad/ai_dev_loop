import { expect, test, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

let serverProcess: ChildProcess;
let stateRoot: string;
let baseUrl: string;

async function reservePort(): Promise<number> {
  return await new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("Unable to reserve a loopback port."));
        return;
      }
      server.close((error) => (error ? reject(error) : resolvePort(address.port)));
    });
  });
}

async function waitForServer(url: string): Promise<void> {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (serverProcess.exitCode !== null) {
      throw new Error(`Fixture server exited early with ${serverProcess.exitCode}.`);
    }
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // The owned server is still starting.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 50));
  }
  throw new Error("Fixture dashboard server did not become ready.");
}

async function applyMapScope(page: Page, fakeClock = false): Promise<void> {
  await page.goto(`${baseUrl}/`);
  await page.locator(".filters select").selectOption("map-project");
  await page.getByLabel("IANA time zone").fill("UTC");
  await page.getByLabel("From").fill("01/01/2026 00:00");
  await page.getByLabel("Until (exclusive)").fill("05/01/2026 00:00");
  if (fakeClock) await page.clock.runFor(400);
  else await page.waitForTimeout(450);
  await expect(page.getByText("28/28 loaded")).toBeVisible();
}

async function clickReplacementChartCell(page: Page, position: 1 | 2): Promise<void> {
  const chart = page.getByRole("img", {
    name: "Selectable chart cells for authoritative run IDs",
  });
  await chart.scrollIntoViewIfNeeded();
  const box = await chart.boundingBox();
  if (!box) throw new Error("Run map chart has no browser layout box.");
  const plotWidth = box.width - 220 - 24;
  const plotHeight = box.height - 18 - 54;
  const x = box.x + 220 + ((position - 0.5) / 26) * plotWidth;
  const sequenceRowY = box.y + 18 + plotHeight * 0.75;
  await page.mouse.click(x, sequenceRowY);
}

async function clickTimelineInterval(page: Page, runId: string): Promise<void> {
  const chart = page.getByRole("img", { name: `Gantt timeline for ${runId}` });
  await chart.scrollIntoViewIfNeeded();
  const point = await chart.evaluate((element) => {
    for (const canvas of element.querySelectorAll("canvas")) {
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) continue;
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      let minX = canvas.width;
      let maxX = -1;
      let minY = canvas.height;
      let maxY = -1;
      for (let y = 0; y < canvas.height; y += 1) {
        for (let x = 0; x < canvas.width; x += 1) {
          const offset = (y * canvas.width + x) * 4;
          if (
            Math.abs(pixels[offset] - 128) <= 4 &&
            Math.abs(pixels[offset + 1] - 84) <= 4 &&
            Math.abs(pixels[offset + 2] - 174) <= 4 &&
            pixels[offset + 3] >= 240
          ) {
            minX = Math.min(minX, x);
            maxX = Math.max(maxX, x);
            minY = Math.min(minY, y);
            maxY = Math.max(maxY, y);
          }
        }
      }
      if (maxX >= minX && maxY >= minY) {
        const box = canvas.getBoundingClientRect();
        return {
          x: box.left + ((minX + maxX) / 2 / canvas.width) * box.width,
          y: box.top + ((minY + maxY) / 2 / canvas.height) * box.height,
        };
      }
    }
    throw new Error("Rendered Codex attempt interval was not found on the ECharts canvas.");
  });
  await page.mouse.click(point.x, point.y);
}

async function finishAnimationFrames(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolveFrames) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolveFrames()));
  }));
}

test.beforeAll(async () => {
  const port = await reservePort();
  baseUrl = `http://127.0.0.1:${port}`;
  stateRoot = mkdtempSync(join(tmpdir(), "ai-dev-loop-phase264-e2e-"));
  const repositoryRoot = resolve(process.cwd(), "..");
  serverProcess = spawn(
    resolve(repositoryRoot, ".venv/bin/python"),
    [
      resolve(process.cwd(), "e2e/fixture_server.py"),
      "--port",
      String(port),
      "--state-root",
      stateRoot,
    ],
    {
      cwd: repositoryRoot,
      env: {
        ...process.env,
        HOME: join(stateRoot, "home"),
        XDG_CONFIG_HOME: join(stateRoot, "config"),
        XDG_STATE_HOME: join(stateRoot, "state"),
        XDG_CACHE_HOME: join(stateRoot, "cache"),
        PYTHONPATH: `${resolve(repositoryRoot, "src")}:${repositoryRoot}`,
      },
      stdio: "pipe",
    },
  );
  await waitForServer(`${baseUrl}/`);
});

test.afterAll(async () => {
  if (serverProcess?.exitCode === null) {
    serverProcess.kill("SIGTERM");
    await new Promise<void>((resolveExit) => {
      const timer = setTimeout(resolveExit, 3_000);
      serverProcess.once("exit", () => {
        clearTimeout(timer);
        resolveExit();
      });
    });
  }
  if (stateRoot) rmSync(stateRoot, { recursive: true, force: true });
});

test("map scroll stays inside its viewport and metric loading preserves page position", async ({ page }) => {
  test.setTimeout(45_000);
  await page.setViewportSize({ width: 1095, height: 884 });
  let pendingMetric: string | null = null;
  let metricStarted = false;
  let releaseMetric: (() => void) | undefined;
  let metricGate = Promise.resolve();
  await page.route("**/api/dashboard/read", async (route) => {
    const request = JSON.parse(route.request().postData() ?? "{}");
    if (request.operation === "dashboard.activity" && request.parameters.metric === pendingMetric) {
      metricStarted = true;
      await metricGate;
    }
    if (request.operation === "dashboard.runs") {
      const response = await route.fetch();
      const payload = await response.json();
      if (payload.ok && payload.data.items.length) {
        const binding = payload.data.items.find((item: { sequenceBinding: unknown }) => item.sequenceBinding)?.sequenceBinding;
        if (binding) {
          payload.data.items.forEach((item: { sequenceBinding: unknown }, index: number) => {
            item.sequenceBinding = {
              ...binding,
              sequenceId: `scroll-sequence-${index}`,
              sequenceName: `Scroll sequence ${index}`,
              sequenceShortName: `Scroll sequence ${index}`,
            };
          });
        }
      }
      await route.fulfill({ response, json: payload });
      return;
    }
    await route.continue();
  });
  await applyMapScope(page);
  const map = page.getByRole("region", { name: "Scrollable sequence and run map" });
  await map.scrollIntoViewIfNeeded();
  const dimensions = await map.evaluate((element) => ({
    height: element.getBoundingClientRect().height,
    contentHeight: element.scrollHeight,
  }));
  expect(dimensions.height).toBeLessThanOrEqual(500);
  expect(dimensions.contentHeight).toBeGreaterThan(500);
  const beforeMapScroll = await page.evaluate(() => window.scrollY);
  await map.hover();
  await page.mouse.wheel(0, 250);
  await expect.poll(() => map.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  expect(await page.evaluate(() => window.scrollY)).toBe(beforeMapScroll);

  const activity = page.locator(".activity-panel");
  for (const [metric, label] of [
    ["review-rounds", "Review rounds"],
    ["process-hours", "Process hours"],
    ["submissions", "Submissions"],
  ]) {
    await activity.evaluate((element) => element.scrollIntoView({ block: "end" }));
    const before = await page.evaluate(() => ({ scroll: window.scrollY, height: document.documentElement.scrollHeight }));
    pendingMetric = metric;
    metricStarted = false;
    metricGate = new Promise<void>((resolve) => { releaseMetric = resolve; });
    try {
      await page.getByLabel("Activity metric").selectOption(metric);
      await expect.poll(() => metricStarted).toBe(true);
      await expect(activity.locator(".activity-results")).toHaveAttribute("aria-busy", "true");
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      expect(Math.abs(await page.evaluate(() => window.scrollY) - before.scroll)).toBeLessThanOrEqual(1);
      expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeGreaterThanOrEqual(before.height - 1);
    } finally {
      pendingMetric = null;
      releaseMetric?.();
    }
    const chart = page.getByRole("img", { name: `${label} activity calendar in UTC` });
    await expect(chart).toBeVisible();
    const after = await chart.boundingBox();
    expect(after).not.toBeNull();
    expect(after!.y).toBeGreaterThanOrEqual(0);
    expect(after!.y + after!.height).toBeLessThanOrEqual(884);
    expect(Math.abs(await page.evaluate(() => window.scrollY) - before.scroll)).toBeLessThanOrEqual(1);
  }
});

test("compact panels keep full-width charts, spaced filters and collapsed attempts", async ({ page }) => {
  await applyMapScope(page);
  await expect(page.locator(".overview-grid")).toBeVisible();
  for (const width of [1095, 390]) {
    await page.setViewportSize({ width, height: 884 });
    const geometry = await page.evaluate(() => {
      const overview = document.querySelector<HTMLElement>(".overview-grid")!;
      const panels = [...overview.children].map((element) => element.getBoundingClientRect());
      const fields = [...document.querySelectorAll(".filter-grid input, .filter-grid select")]
        .map((element) => element.getBoundingClientRect().bottom);
      const actions = document.querySelector(".filter-actions")!.getBoundingClientRect();
      return {
        widths: panels.map((box) => box.width),
        overviewWidth: overview.getBoundingClientRect().width,
        separated: panels[1].top >= panels[0].bottom,
        filterGap: actions.top - Math.max(...fields),
        icons: [...document.querySelectorAll(".information-trigger")].every((element) =>
          element.textContent?.trim() === "i" && getComputedStyle(element).borderRadius === "50%",
        ),
      };
    });
    expect(geometry.widths.every((width) => Math.abs(width - geometry.overviewWidth) < 2)).toBe(true);
    expect(geometry.separated).toBe(true);
    expect(geometry.filterGap).toBeGreaterThanOrEqual(12);
    expect(geometry.icons).toBe(true);
  }
  await page.setViewportSize({ width: 1095, height: 884 });
  await page.getByLabel("Select loaded run").selectOption("current-map-run");
  await expect(page.getByLabel("Selected run", { exact: true })).toHaveAttribute("data-run-id", "current-map-run");
  await expect(page.locator(".run-metadata")).toHaveCount(0);
  await expect(page.locator(".lineage-line")).toHaveCount(0);
  await expect(page.locator(".attempt-details-disclosure")).not.toHaveAttribute("open");
  await expect(page.locator(".attempt-rows")).not.toBeVisible();
  await clickTimelineInterval(page, "current-map-run");
  await page.getByRole("button", { name: "Close attempt details" }).click();
  const summary = page.locator(".attempt-details-disclosure > summary");
  await expect(summary).toBeFocused();
  await expect(page.locator(".attempt-details-disclosure")).not.toHaveAttribute("open");
  await summary.click();
  await expect(page.getByRole("button", { name: /Attempt ID: attempt-current-map-run-execution/ })).toBeVisible();
});

test("C282-1..2 real chart keeps one selected run and stable paged attempt captions", async ({
  page,
}) => {
  const operations: Array<{ operation: string; parameters: Record<string, unknown> }> = [];
  await page.route("**/api/dashboard/read", async (route) => {
    const request = JSON.parse(route.request().postData() ?? "{}") as {
      operation?: string;
      parameters?: Record<string, unknown>;
    };
    if (request.operation) {
      operations.push({ operation: request.operation, parameters: request.parameters ?? {} });
    }
    await route.continue();
  });

  await applyMapScope(page);
  const projectSelect = page.locator(".filters select");
  await expect(page.getByText("28/28 loaded")).toBeVisible();
  await expect(page.locator(".run-metadata")).toHaveCount(0);

  await clickReplacementChartCell(page, 1);
  const firstSelectionScroll = await page.evaluate(() => window.scrollY);
  await expect(page.getByLabel("Selected run", { exact: true })).toHaveAttribute("data-run-id", "older-map-run");
  await expect(page.locator(".run-metadata")).toHaveCount(0);
  await page.getByRole("button", { name: "Information about Attempt timeline" }).click();
  await expect(page.locator(".lineage-line")).toContainText("generation 1 · planned_run");
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await clickReplacementChartCell(page, 2);
  expect(await page.evaluate(() => window.scrollY)).toBe(firstSelectionScroll);
  await expect(page.getByLabel("Selected run", { exact: true })).toHaveAttribute("data-run-id", "current-map-run");
  await expect(page.locator(".run-metadata")).toHaveCount(0);
  await page.getByRole("button", { name: "Information about Attempt timeline" }).click();
  await expect(page.locator(".lineage-line")).toContainText(
    "generation 2 · implementer_retry",
  );
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.locator(".attempt-rows")).not.toBeVisible();
  await page.locator(".attempt-details-disclosure > summary").click();
  const currentAttemptRow = page.getByRole("button", {
    name: /Attempt ID: attempt-current-map-run-execution/,
  });
  await expect(currentAttemptRow).toBeVisible();

  await clickTimelineInterval(page, "current-map-run");
  const timelineScroll = await page.evaluate(() => window.scrollY);
  const closeAttempt = page.getByRole("button", { name: "Close attempt details" });
  await expect(page.getByRole("heading", { name: "1 - Iteration 1 - Implementer" }))
    .toBeVisible();
  await finishAnimationFrames(page);
  await expect(closeAttempt).toBeFocused();
  await closeAttempt.click();
  await finishAnimationFrames(page);
  await expect(currentAttemptRow).toBeFocused();
  expect(await page.evaluate(() => window.scrollY)).toBe(timelineScroll);

  await page.getByLabel("Select loaded run").selectOption("older-map-run");
  await expect(page.getByLabel("Selected run", { exact: true })).toHaveAttribute("data-run-id", "older-map-run");
  await expect(page.getByText("25 loaded / 27 recorded attempts")).toBeVisible();
  await page.locator(".attempt-details-disclosure > summary").click();
  await page.getByRole("button", { name: "Load more attempts" }).click();
  const reviewerRow = page.getByRole("button", {
    name: /Attempt ID: attempt-older-map-run-review-retry/,
  });
  await expect(reviewerRow).toContainText("26 - Iteration 1 - Reviewer");
  await expect(page.getByText("Codex reviewer · completed")).toBeVisible();
  await expect(page.getByText("Codex implementer · active")).toBeVisible();
  await expect(page.getByText(/no observed end/)).toBeVisible();
  await reviewerRow.click();
  await expect(page.getByRole("heading", { name: "26 - Iteration 1 - Reviewer" }))
    .toBeVisible();
  await page.getByRole("button", { name: "Close attempt details" }).click();

  await projectSelect.selectOption("project-a");
  await expect(page.getByText("Select an exact run cell or the loaded-run control to inspect it.")).toBeVisible();

  expect(
    operations.some((call) =>
      ["run.initial-prompt", "run.review", "run.review-content", "run.output"].includes(
        call.operation,
      ),
    ),
  ).toBe(false);
});

test("F-03 refreshes the exact current selection at the normal cadence after saturation", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-01-05T12:00:00Z") });
  const control = (name: string) => join(stateRoot, name);
  for (const name of [
    "hold-service-jobs",
    "release-service-jobs",
    "held-inspect-started",
    "held-timeline-started",
  ]) {
    rmSync(control(name), { force: true });
  }
  const operations: Array<{ operation: string; parameters: Record<string, unknown> }> = [];
  await page.route("**/api/dashboard/read", async (route) => {
    const request = JSON.parse(route.request().postData() ?? "{}") as {
      operation?: string;
      parameters?: Record<string, unknown>;
    };
    if (request.operation) {
      operations.push({ operation: request.operation, parameters: request.parameters ?? {} });
    }
    await route.continue();
  });

  await applyMapScope(page, true);
  writeFileSync(control("hold-service-jobs"), "hold\n", "utf-8");
  try {
    await page.getByLabel("Select loaded run").selectOption("older-map-run");
    await expect
      .poll(
        () =>
          existsSync(control("held-inspect-started")) &&
          existsSync(control("held-timeline-started")),
      )
      .toBe(true);

    await page.getByLabel("Select loaded run").selectOption("current-map-run");
    await expect
      .poll(
        () =>
          operations.filter(
            (call) =>
              call.parameters.runId === "current-map-run" &&
              (call.operation === "run.inspect" || call.operation === "run.timeline"),
          ).length,
      )
      .toBeGreaterThanOrEqual(2);
    await expect(page.getByText(/Run metadata:/)).toBeVisible();
    await expect(page.getByText(/Timeline:/)).toBeVisible();
    writeFileSync(control("release-service-jobs"), "release\n", "utf-8");
    // Let the two fixture worker threads observe their release before advancing
    // the virtual cadence; otherwise the retry can race the synthetic saturation.
    await page.waitForTimeout(100);
    await page.clock.runFor(10_000);

    await expect(page.getByLabel("Selected run", { exact: true })).toHaveAttribute("data-run-id", "current-map-run");
    await expect(page.getByRole("img", { name: "Gantt timeline for current-map-run" })).toBeVisible();
    await expect(page.locator(".attempt-rows")).not.toBeVisible();
    await page.locator(".attempt-details-disclosure > summary").click();
    await expect(page.getByText("Attempt ID: attempt-current-map-run-execution")).toBeVisible();
    await expect(page.getByText("Attempt ID: attempt-older-map-run-execution")).toHaveCount(0);
    await expect
      .poll(
        () =>
          operations.filter(
            (call) => call.parameters.runId === "current-map-run" && call.operation === "run.inspect",
          ).length,
      )
      .toBeGreaterThanOrEqual(2);
    await expect
      .poll(
        () =>
          operations.filter(
            (call) => call.parameters.runId === "current-map-run" && call.operation === "run.timeline",
          ).length,
      )
      .toBeGreaterThanOrEqual(2);
    expect(
      operations.filter(
        (call) =>
          call.parameters.runId === "older-map-run" && call.operation === "run.inspect",
      ),
    ).toHaveLength(1);
    expect(
      operations.filter(
        (call) =>
          call.parameters.runId === "older-map-run" && call.operation === "run.timeline",
      ),
    ).toHaveLength(1);
  } finally {
    writeFileSync(control("release-service-jobs"), "release\n", "utf-8");
    rmSync(control("hold-service-jobs"), { force: true });
  }
});
