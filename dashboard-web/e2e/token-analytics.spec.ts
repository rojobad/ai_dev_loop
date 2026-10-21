import { expect, test, type Page, type Request } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
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

function watchReads(page: Page): { operations: () => string[] } {
  const seen: string[] = [];
  const record = (request: Request) => {
    if (request.method() !== "POST" || !request.url().includes("/api/dashboard/read")) return;
    try {
      const body = JSON.parse(request.postData() ?? "{}") as { operation?: string };
      if (body.operation) seen.push(body.operation);
    } catch {
      seen.push("unparsed");
    }
  };
  page.on("request", record);
  return { operations: () => [...seen] };
}

async function applyMapScope(page: Page): Promise<void> {
  await page.goto(`${baseUrl}/`);
  await page.locator(".filters select").selectOption("map-project");
  await page.getByLabel("IANA time zone").fill("America/La_Paz");
  await page.getByLabel("From").fill("01/01/2026 00:00");
  await page.getByLabel("Until (exclusive)").fill("05/01/2026 00:00");
  await page.waitForTimeout(450);
  await expect(page.getByText("28/28 loaded")).toBeVisible();
  await expect(page.getByText("363", { exact: true })).toBeVisible();
}

async function modelNodePoint(page: Page): Promise<{ x: number; y: number }> {
  const chart = page.getByRole("img", { name: "Token flow" });
  await chart.scrollIntoViewIfNeeded();
  const point = await chart.evaluate((element) => {
    for (const canvas of element.querySelectorAll("canvas")) {
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) continue;
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      const hits: Array<[number, number]> = [];
      for (let y = 0; y < canvas.height; y += 2) {
        for (let x = 0; x < canvas.width; x += 2) {
          const offset = (y * canvas.width + x) * 4;
          if (
            Math.abs(pixels[offset] - 142) <= 18 &&
            Math.abs(pixels[offset + 1] - 160) <= 18 &&
            Math.abs(pixels[offset + 2] - 184) <= 18 &&
            pixels[offset + 3] >= 200
          ) {
            hits.push([x, y]);
          }
        }
      }
      if (hits.length === 0) continue;
      hits.sort((left, right) => left[0] - right[0] || left[1] - right[1]);
      const [x, y] = hits[Math.floor(hits.length / 2)];
      const box = canvas.getBoundingClientRect();
      return {
        x: box.left + (x / canvas.width) * box.width,
        y: box.top + (y / canvas.height) * box.height,
      };
    }
    return null;
  });
  if (!point) throw new Error("Rendered token-flow model node was not found on the ECharts canvas.");
  return point;
}

function geometry(page: Page): Promise<{ scrollY: number; height: number; panelTop: number }> {
  return page.evaluate(() => {
    const panel = document.querySelector(".token-analytics");
    if (!panel) throw new Error("Token analytics panel is missing.");
    const panelTop = panel.getBoundingClientRect().top;
    return {
      scrollY: window.scrollY,
      height: document.documentElement.scrollHeight,
      panelTop: window.scrollY + panelTop,
    };
  });
}

function runGeometry(page: Page): Promise<{
  scrollY: number;
  height: number;
  panelHeight: number;
  panelTop: number;
  viewportTop: number;
  chartHeight: number;
  timelineHeight: number;
}> {
  return page.evaluate(() => {
    const panel = document.querySelector(".run-token-panel");
    const chart = document.querySelector(".run-token-chart-scroll");
    const timeline = document.querySelector(".attempt-timeline-panel");
    if (!(panel instanceof HTMLElement) || !(chart instanceof HTMLElement)) {
      throw new Error("Run token panel is missing.");
    }
    const panelBox = panel.getBoundingClientRect();
    const chartBox = chart.getBoundingClientRect();
    const timelineBox = timeline instanceof HTMLElement ? timeline.getBoundingClientRect() : null;
    return {
      scrollY: window.scrollY,
      height: document.documentElement.scrollHeight,
      panelHeight: panelBox.height,
      panelTop: window.scrollY + panelBox.top,
      viewportTop: panelBox.top,
      chartHeight: chartBox.height,
      timelineHeight: timelineBox?.height ?? 0,
    };
  });
}

function expectHeldRunPanel(
  before: {
    scrollY: number;
    height: number;
    panelHeight: number;
    panelTop: number;
    viewportTop: number;
    chartHeight: number;
    timelineHeight: number;
  },
  next: {
    scrollY: number;
    height: number;
    panelHeight: number;
    panelTop: number;
    viewportTop: number;
    chartHeight: number;
    timelineHeight: number;
  },
): void {
  expect(Math.abs(next.panelHeight - before.panelHeight)).toBeLessThanOrEqual(2);
  expect(Math.abs(next.chartHeight - before.chartHeight)).toBeLessThanOrEqual(2);
  expect(Math.abs(next.viewportTop - before.viewportTop)).toBeLessThanOrEqual(4);
  expect(Math.abs((next.scrollY - before.scrollY) - (next.panelTop - before.panelTop))).toBeLessThanOrEqual(4);
  expect(Math.abs((next.height - before.height) - (next.panelTop - before.panelTop))).toBeLessThanOrEqual(4);
}

async function clickWithoutScrolling(page: Page, name: string): Promise<void> {
  await page.getByRole("button", { name }).evaluate((element) => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

test.use({ timezoneId: "Pacific/Auckland", viewport: { width: 1280, height: 900 } });

test.beforeAll(async () => {
  const port = await reservePort();
  baseUrl = `http://127.0.0.1:${port}`;
  stateRoot = mkdtempSync(join(tmpdir(), "ai-dev-loop-phase292-e2e-"));
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

test.beforeEach(() => {
  for (const name of [
    "hold-token-usage",
    "release-token-usage",
    "held-token-usage-started",
    "fail-token-usage",
    "wide-direct-tokens",
  ]) {
    const path = join(stateRoot, name);
    if (existsSync(path)) unlinkSync(path);
  }
});

async function colorPoint(
  page: Page,
  chartName: string,
  red: number,
  green: number,
  blue: number,
  plotOnly = false,
  topBand = false,
): Promise<{ x: number; y: number }> {
  const chart = page.getByRole("img", { name: chartName });
  await chart.scrollIntoViewIfNeeded();
  const point = await chart.evaluate((element, color) => {
    for (const canvas of element.querySelectorAll("canvas")) {
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) continue;
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      const hits: Array<[number, number]> = [];
      let best = 36;
      for (let y = 0; y < canvas.height; y += 2) {
        if (color.plotOnly && (y < canvas.height * 0.08 || y > canvas.height * 0.84)) continue;
        for (let x = 0; x < canvas.width; x += 2) {
          const offset = (y * canvas.width + x) * 4;
          if (pixels[offset + 3] < 200) continue;
          const distance = Math.abs(pixels[offset] - color.red)
            + Math.abs(pixels[offset + 1] - color.green)
            + Math.abs(pixels[offset + 2] - color.blue);
          if (distance > 36 || distance > best) continue;
          if (distance < best) {
            best = distance;
            hits.length = 0;
          }
          hits.push([x, y]);
        }
      }
      if (hits.length === 0) continue;
      hits.sort((left, right) => left[1] - right[1] || left[0] - right[0]);
      const band = color.topBand
        ? hits.filter((hit) => hit[1] <= hits[0][1] + 10)
        : hits;
      const [x, y] = band[Math.floor(band.length / 2)];
      const scroll = element.closest(".token-heatmap-scroll, .token-sankey-scroll");
      if (scroll instanceof HTMLElement) {
        const before = canvas.getBoundingClientRect();
        const localY = (y / canvas.height) * before.height;
        const canvasOffset = before.top - scroll.getBoundingClientRect().top + scroll.scrollTop;
        scroll.scrollTop = Math.max(0, canvasOffset + localY - scroll.clientHeight / 2);
      }
      const box = canvas.getBoundingClientRect();
      return {
        x: box.left + (x / canvas.width) * box.width,
        y: box.top + (y / canvas.height) * box.height,
      };
    }
    let nearest = 10_000;
    let sample = "none";
    for (const canvas of element.querySelectorAll("canvas")) {
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) continue;
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      for (let y = 0; y < canvas.height; y += 6) {
        for (let x = 0; x < canvas.width; x += 6) {
          const offset = (y * canvas.width + x) * 4;
          if (pixels[offset + 3] < 200) continue;
          const distance = Math.abs(pixels[offset] - color.red)
            + Math.abs(pixels[offset + 1] - color.green)
            + Math.abs(pixels[offset + 2] - color.blue);
          if (distance < nearest) {
            nearest = distance;
            sample = `${pixels[offset]},${pixels[offset + 1]},${pixels[offset + 2]}`;
          }
        }
      }
    }
    return { x: -1, y: -1, sample };
  }, { red, green, blue, plotOnly, topBand });
  if (!point || point.x < 0) {
    throw new Error(`No ${chartName} pixel matched rgb(${red}, ${green}, ${blue}). Nearest ${point?.sample ?? "none"}.`);
  }
  return point;
}

async function selectWithoutScrolling(page: Page, label: string, value: string): Promise<void> {
  await page.getByLabel(label).evaluate((element, next) => {
    const select = element as HTMLSelectElement;
    select.value = next;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }, value);
}

function tokenReads(page: Page): { calls: () => Array<{ operation: string; runId: string | null }> } {
  const seen: Array<{ operation: string; runId: string | null }> = [];
  page.on("request", (request) => {
    if (request.method() !== "POST" || !request.url().includes("/api/dashboard/read")) return;
    try {
      const body = JSON.parse(request.postData() ?? "{}") as {
        operation?: string;
        parameters?: { runId?: unknown };
      };
      if (!body.operation) return;
      const runId = typeof body.parameters?.runId === "string" ? body.parameters.runId : null;
      seen.push({ operation: body.operation, runId });
    } catch {
      seen.push({ operation: "unparsed", runId: null });
    }
  });
  return { calls: () => [...seen] };
}

test("linked token charts keep disjoint totals, exact runs, and closed details", async ({ page }) => {
  test.setTimeout(90_000);
  const reads = watchReads(page);
  await applyMapScope(page);
  await expect(page.getByText("19/10/2026 08:05").first()).toBeVisible();
  await expect(page.getByText("20/10/2026 01:05")).toHaveCount(0);
  await expect(page.getByText("Partial lower bound").first()).toBeVisible();
  await expect(page.getByRole("option", { name: "Cursor · Unrecorded model" })).toHaveCount(1);
  await expect(page.locator("details[open]")).toHaveCount(0);
  await expect(page.locator(".token-details-disclosure")).toHaveCount(0);

  const composition = await page.getByRole("heading", { name: "Token composition" }).boundingBox();
  const categories = await page.getByRole("heading", { name: "Token categories" }).boundingBox();
  if (!composition || !categories) throw new Error("Token chart headings have no layout box.");
  expect(Math.abs(composition.y - categories.y)).toBeLessThan(40);
  const heatmap = page.locator(".token-heatmap-scroll");
  expect(await heatmap.evaluate((element) => element.scrollHeight > element.clientHeight + 8)).toBe(true);
  const wideSlot = await page.locator(".token-sankey-slot").boundingBox();
  const wideScroll = await page.locator(".token-sankey-scroll").evaluate((element) => element.clientHeight);

  await page.setViewportSize({ width: 800, height: 900 });
  const narrowComposition = await page.getByRole("heading", { name: "Token composition" }).boundingBox();
  const narrowCategories = await page.getByRole("heading", { name: "Token categories" }).boundingBox();
  if (!narrowComposition || !narrowCategories) throw new Error("Narrow token headings have no layout box.");
  expect(narrowCategories.y).toBeGreaterThan(narrowComposition.y + 80);
  const narrowSlot = await page.locator(".token-sankey-slot").boundingBox();
  const narrowScroll = await page.locator(".token-sankey-scroll").evaluate((element) => element.clientHeight);
  if (!wideSlot || !narrowSlot) throw new Error("Sankey slot has no layout box.");
  expect(Math.abs(narrowSlot.height - wideSlot.height)).toBeLessThanOrEqual(2);
  expect(narrowScroll).toBe(wideScroll);
  expect(wideScroll).toBeLessThanOrEqual(480);
  await page.setViewportSize({ width: 1280, height: 900 });

  const point = await modelNodePoint(page);
  const readsBeforeClick = reads.operations().filter((operation) => operation === "dashboard.token-usage").length;
  await page.mouse.click(point.x, point.y);
  await expect(page.getByLabel("Token model")).not.toHaveValue("");
  expect(reads.operations().filter((operation) => operation === "dashboard.token-usage")).toHaveLength(readsBeforeClick);
  await page.getByRole("button", { name: "Clear selection" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("Token model")).toHaveValue("");
  await expect(page.getByLabel("Token role")).toHaveValue("");
  await expect(page.getByText("363", { exact: true })).toBeVisible();

  const project = await page.locator(".filters select").inputValue();
  const from = await page.getByLabel("From").inputValue();
  const until = await page.getByLabel("Until (exclusive)").inputValue();
  const selectedRun = await page.getByLabel("Select loaded run").inputValue();
  const tokenCount = () => reads.operations().filter((operation) => operation === "dashboard.token-usage").length;
  const beforeFacets = tokenCount();
  const rolePoint = await colorPoint(page, "Token composition", 82, 101, 127);
  await page.mouse.click(rolePoint.x, rolePoint.y);
  await expect(page.getByLabel("Token model")).not.toHaveValue("");
  await expect(page.getByLabel("Token role")).not.toHaveValue("");
  const roleValue = await page.getByLabel("Token role").inputValue();
  expect(roleValue === "implementer" || roleValue === "reviewer").toBe(true);
  await page.getByRole("button", { name: "Clear selection" }).click();
  await expect(page.getByLabel("Token role")).toHaveValue("");
  const sunburstModel = await colorPoint(page, "Token composition", 142, 160, 184);
  await page.mouse.click(sunburstModel.x, sunburstModel.y);
  await expect(page.getByLabel("Token model")).not.toHaveValue("");
  await expect(page.getByLabel("Token role")).toHaveValue("");
  await page.getByRole("button", { name: "Clear selection" }).click();
  const unknownCell = await colorPoint(page, "Token categories", 217, 211, 197, true);
  await page.mouse.click(unknownCell.x, unknownCell.y);
  await expect(page.getByLabel("Token model")).toHaveValue("cursor:");
  await expect(page.getByLabel("Token role")).toHaveValue("");
  await page.getByRole("button", { name: "Clear selection" }).click();
  const zeroCell = await colorPoint(page, "Token categories", 231, 238, 246, true, true);
  await page.mouse.click(zeroCell.x, zeroCell.y);
  await expect(page.getByLabel("Token role")).toHaveValue("");
  const zeroModel = await page.getByLabel("Token model").inputValue();
  expect(zeroModel.startsWith("cursor:")).toBe(true);
  expect(zeroModel).not.toBe("cursor:");
  expect(tokenCount()).toBe(beforeFacets);
  expect(await page.locator(".filters select").inputValue()).toBe(project);
  expect(await page.getByLabel("From").inputValue()).toBe(from);
  expect(await page.getByLabel("Until (exclusive)").inputValue()).toBe(until);
  expect(await page.getByLabel("Select loaded run").inputValue()).toBe(selectedRun);
  await page.getByRole("button", { name: "Clear selection" }).click();

  await page.getByLabel("Token model").selectOption(`codex:${'review<img alt="x">'}`);
  await expect(page.locator("[data-run-token-total]")).toHaveCount(0);
  const xssPoint = await modelNodePoint(page);
  await page.mouse.move(xssPoint.x, xssPoint.y);
  const tooltip = page.locator("body > div").filter({ hasText: "review<img" }).filter({
    hasNot: page.locator(".app-shell"),
  }).last();
  await expect(tooltip).toBeVisible();
  const tooltipBox = await tooltip.boundingBox();
  if (!tooltipBox) throw new Error("Token tooltip has no layout box.");
  const viewport = page.viewportSize();
  if (!viewport) throw new Error("Viewport is unavailable.");
  expect(tooltipBox.x).toBeGreaterThanOrEqual(0);
  expect(tooltipBox.y).toBeGreaterThanOrEqual(0);
  expect(tooltipBox.x + tooltipBox.width).toBeLessThanOrEqual(viewport.width + 1);
  expect(tooltipBox.y + tooltipBox.height).toBeLessThanOrEqual(viewport.height + 1);
  await expect(page.locator('img[alt="x"]')).toHaveCount(0);
  await expect(tooltip).not.toContainText("older-map-run");
  await expect(tooltip).not.toContainText("attempt-");
  await page.getByRole("button", { name: "Clear selection" }).click();

  await page.getByLabel("Select loaded run").selectOption("older-map-run");
  await expect(page.locator(".token-details-disclosure")).not.toHaveAttribute("open");
  await expect(page.locator("[data-run-token-total]")).toHaveAttribute("data-run-token-total", "140");
  await expect(page.getByText("363", { exact: true })).toBeVisible();
  await page.getByLabel("Token model").selectOption("codex:shared");
  await expect(page.locator("[data-run-token-total]")).toHaveAttribute("data-run-token-total", "140");
  await page.getByRole("button", { name: "Clear selection" }).click();
  await page.getByLabel("Select loaded run").selectOption("current-map-run");
  await expect(page.locator("[data-run-token-total]")).toHaveAttribute("data-run-token-total", "115");
  await page.locator(".token-details-disclosure summary").click();
  await expect(page.locator(".token-detail-ids")).toContainText("current-map-run");
  await expect(page.locator(".token-detail-ids")).not.toContainText("older-map-run");

  const operations = reads.operations();
  expect(operations).toContain("dashboard.token-usage");
  expect(operations.some((operation) => (
    operation === "run.initial-prompt"
    || operation === "run.review"
    || operation === "run.review-content"
    || operation === "run.output"
  ))).toBe(false);

  const information = page.getByRole("button", { name: "Information about Token flow" });
  await information.scrollIntoViewIfNeeded();
  const scrollBefore = await page.evaluate(() => window.scrollY);
  await information.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(information).toBeFocused();
  expect(await page.evaluate(() => window.scrollY)).toBe(scrollBefore);

  await clickWithoutScrolling(page, "Pause automatic refresh");
  await expect(page.getByText("paused")).toBeVisible();
  await clickWithoutScrolling(page, "Refresh current scope");
  await expect(page.getByText("363", { exact: true })).toBeVisible();
});

test("held token refresh keeps the page geometry", async ({ page }) => {
  test.setTimeout(60_000);
  await applyMapScope(page);
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const before = await geometry(page);
  const observed = page.locator("[data-token-observed-at]");
  const beforeStamp = await observed.getAttribute("data-token-observed-at");
  if (!beforeStamp) throw new Error("Token observation stamp is missing.");
  writeFileSync(join(stateRoot, "hold-token-usage"), "hold\n");
  await clickWithoutScrolling(page, "Refresh current scope");
  await expect.poll(() => existsSync(join(stateRoot, "held-token-usage-started"))).toBe(true);
  await expect(page.getByText("Reading applied overview…")).toHaveCount(0);
  await expect(observed).toHaveAttribute("data-token-observed-at", beforeStamp);
  const during = await geometry(page);
  expect(Math.abs(during.scrollY - before.scrollY)).toBeLessThanOrEqual(2);
  expect(Math.abs(during.height - before.height)).toBeLessThanOrEqual(2);
  expect(Math.abs(during.panelTop - before.panelTop)).toBeLessThanOrEqual(2);
  writeFileSync(join(stateRoot, "release-token-usage"), "release\n");
  await expect.poll(async () => observed.getAttribute("data-token-observed-at")).not.toBe(beforeStamp);
  await expect(page.getByText("363", { exact: true })).toBeVisible();
  const after = await geometry(page);
  expect(Math.abs(after.scrollY - before.scrollY)).toBeLessThanOrEqual(2);
  expect(Math.abs(after.height - before.height)).toBeLessThanOrEqual(2);
  expect(Math.abs(after.panelTop - before.panelTop)).toBeLessThanOrEqual(2);

  const facetBefore = await geometry(page);
  const reads = watchReads(page);
  await page.getByLabel("Token role").selectOption("reviewer");
  await page.waitForTimeout(200);
  expect(reads.operations()).not.toContain("dashboard.token-usage");
  const facetAfter = await geometry(page);
  expect(Math.abs(facetAfter.height - facetBefore.height)).toBeLessThanOrEqual(2);
  expect(Math.abs(facetAfter.panelTop - facetBefore.panelTop)).toBeLessThanOrEqual(2);

  const runPanelBefore = await page.locator(".run-token-panel").boundingBox();
  await page.getByLabel("Select loaded run").selectOption("older-map-run");
  await expect(page.locator("[data-run-token-total]")).toHaveAttribute("data-run-token-total", "140");
  const runAfter = await geometry(page);
  const runPanelAfter = await page.locator(".run-token-panel").boundingBox();
  expect(Math.abs(runAfter.panelTop - facetBefore.panelTop)).toBeLessThanOrEqual(2);
  if (!runPanelBefore || !runPanelAfter) throw new Error("Run token panel has no layout box.");
  expect(Math.abs(runPanelAfter.height - runPanelBefore.height)).toBeLessThanOrEqual(2);
});

test("cohort and direct token reads follow 30s and 10s cadence, Pause, and hidden tabs", async ({ page }) => {
  test.setTimeout(60_000);
  await page.clock.install({ time: new Date("2026-10-21T12:00:00Z") });
  const reads = tokenReads(page);
  await page.goto(`${baseUrl}/`);
  await page.locator(".filters select").selectOption("map-project");
  await page.getByLabel("IANA time zone").fill("America/La_Paz");
  await page.getByLabel("From").fill("01/01/2026 00:00");
  await page.getByLabel("Until (exclusive)").fill("05/01/2026 00:00");
  await page.clock.runFor(500);
  await expect(page.getByText("363", { exact: true })).toBeVisible();
  await expect(page.getByText("28/28 loaded")).toBeVisible();
  const cohort = () => reads.calls().filter((call) => call.operation === "dashboard.token-usage" && call.runId === null).length;
  const direct = () => reads.calls().filter((call) => call.operation === "dashboard.token-usage" && call.runId !== null).length;
  const cohortAtScope = cohort();
  expect(cohortAtScope).toBeGreaterThan(0);
  expect(direct()).toBe(0);
  await page.clock.runFor(10_000);
  expect(cohort()).toBe(cohortAtScope);
  expect(direct()).toBe(0);

  await selectWithoutScrolling(page, "Select loaded run", "older-map-run");
  await expect(page.locator("[data-run-token-total]")).toHaveAttribute("data-run-token-total", "140");
  const directAtSelection = direct();
  const cohortAtSelection = cohort();
  expect(directAtSelection).toBeGreaterThan(0);
  await page.clock.runFor(10_000);
  await expect.poll(() => direct()).toBeGreaterThan(directAtSelection);
  expect(cohort()).toBe(cohortAtSelection);
  await page.clock.runFor(20_000);
  await expect.poll(() => cohort()).toBeGreaterThan(cohortAtSelection);

  await clickWithoutScrolling(page, "Pause automatic refresh");
  await expect(page.getByText("Automatic refresh: paused")).toBeVisible();
  const pausedCohort = cohort();
  const pausedDirect = direct();
  await page.clock.runFor(60_000);
  expect(cohort()).toBe(pausedCohort);
  expect(direct()).toBe(pausedDirect);

  await clickWithoutScrolling(page, "Resume automatic refresh");
  await expect.poll(() => cohort()).toBeGreaterThan(pausedCohort);
  const visibleCohort = cohort();
  const visibleDirect = direct();
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const hiddenCohort = cohort();
  const hiddenDirect = direct();
  await page.clock.runFor(60_000);
  expect(cohort()).toBe(hiddenCohort);
  expect(direct()).toBe(hiddenDirect);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => cohort()).toBeGreaterThan(visibleCohort);
});

test("a held scope change retires the previous token observation", async ({ page }) => {
  test.setTimeout(60_000);
  await applyMapScope(page);
  const slotBefore = await page.locator(".token-sankey-slot").boundingBox();
  writeFileSync(join(stateRoot, "hold-token-usage"), "hold\n");
  await clickWithoutScrolling(page, "Refresh current scope");
  await expect.poll(() => existsSync(join(stateRoot, "held-token-usage-started"))).toBe(true);
  unlinkSync(join(stateRoot, "held-token-usage-started"));
  const otherProject = '<img src=x onerror="window.__dashboard_xss=1">';
  await page.locator(".filters select").selectOption(otherProject);
  await page.waitForTimeout(450);
  await expect(page.locator("[data-token-project]")).toHaveCount(0);
  await expect(page.getByText("363", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Reading token usage…").or(page.getByText("Token read:")).first()).toBeVisible();
  const slotDuring = await page.locator(".token-sankey-slot").boundingBox();
  if (!slotBefore || !slotDuring) throw new Error("Sankey slot has no layout box.");
  expect(Math.abs(slotDuring.height - slotBefore.height)).toBeLessThanOrEqual(2);
  writeFileSync(join(stateRoot, "release-token-usage"), "release\n");
  await expect(page.getByText("363", { exact: true })).toHaveCount(0);
  const retry = page.getByRole("button", { name: "Retry token read" });
  if (await retry.isVisible()) await retry.click();
  await expect(page.locator("[data-token-project]")).toHaveAttribute("data-token-project", otherProject);
  await expect(page.getByText("363", { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __dashboard_xss?: number }).__dashboard_xss)).toBeUndefined();
});

test("replacing a 12-model run with a one-model run keeps panel geometry", async ({ page }) => {
  test.setTimeout(60_000);
  writeFileSync(join(stateRoot, "wide-direct-tokens"), "wide\n");
  await applyMapScope(page);
  await selectWithoutScrolling(page, "Select loaded run", "older-map-run");
  await expect(page.locator("[data-run-token-total]")).toHaveAttribute("data-run-token-total", "120");
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const before = await runGeometry(page);
  expect(before.chartHeight).toBeGreaterThanOrEqual(318);
  expect(before.chartHeight).toBeLessThanOrEqual(322);

  writeFileSync(join(stateRoot, "hold-token-usage"), "hold\n");
  await selectWithoutScrolling(page, "Select loaded run", "current-map-run");
  await expect.poll(() => existsSync(join(stateRoot, "held-token-usage-started"))).toBe(true);
  await expect(page.locator("[data-run-token-total]")).toHaveCount(0);
  await expect(page.getByText("120", { exact: true })).toHaveCount(0);
  const during = await runGeometry(page);
  expectHeldRunPanel(before, during);

  writeFileSync(join(stateRoot, "fail-token-usage"), "fail\n");
  writeFileSync(join(stateRoot, "release-token-usage"), "release\n");
  await expect(page.getByText("Selected-run token read:")).toBeVisible();
  await expect(page.locator("[data-run-token-total]")).toHaveCount(0);
  await expect(page.getByText("120", { exact: true })).toHaveCount(0);
  expectHeldRunPanel(before, await runGeometry(page));

  unlinkSync(join(stateRoot, "fail-token-usage"));
  unlinkSync(join(stateRoot, "release-token-usage"));
  unlinkSync(join(stateRoot, "held-token-usage-started"));
  await selectWithoutScrolling(page, "Select loaded run", "older-map-run");
  await expect.poll(() => existsSync(join(stateRoot, "held-token-usage-started"))).toBe(true);
  unlinkSync(join(stateRoot, "held-token-usage-started"));
  await selectWithoutScrolling(page, "Select loaded run", "current-map-run");
  await expect(page.locator("[data-run-token-total]")).toHaveCount(0);
  const heldReplacement = await runGeometry(page);
  expectHeldRunPanel(before, heldReplacement);
  writeFileSync(join(stateRoot, "release-token-usage"), "release\n");
  await expect(page.locator("[data-run-token-total]")).toHaveAttribute("data-run-token-total", "20");
  await expect(page.getByText("120", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Recorded token total is 0.")).toHaveCount(0);
  const applied = await runGeometry(page);
  expectHeldRunPanel(before, applied);
  expect(Math.abs(applied.panelHeight - during.panelHeight)).toBeLessThanOrEqual(2);
  expect(Math.abs(applied.chartHeight - during.chartHeight)).toBeLessThanOrEqual(2);
  expect(Math.abs(applied.height - during.height)).toBeLessThanOrEqual(4);
  expect(Math.abs(applied.scrollY - during.scrollY)).toBeLessThanOrEqual(4);
  expect(Math.abs(applied.viewportTop - during.viewportTop)).toBeLessThanOrEqual(4);
});
