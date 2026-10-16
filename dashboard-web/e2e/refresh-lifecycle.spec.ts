import { expect, test, type Page, type Route } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
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

async function applyMapScope(page: Page): Promise<void> {
  await page.locator(".filters select").selectOption("map-project");
  await page.getByLabel("IANA time zone").fill("UTC");
  await page.getByLabel("From").fill("2026-01-01T00:00");
  await page.getByLabel("Until (exclusive)").fill("2026-01-05T00:00");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText(/Loaded 25 of 28 started runs/)).toBeVisible();
}

function operation(route: Route): string | null {
  const payload = JSON.parse(route.request().postData() ?? "{}") as { operation?: string };
  return payload.operation ?? null;
}

test.beforeAll(async () => {
  const port = await reservePort();
  baseUrl = `http://127.0.0.1:${port}`;
  stateRoot = mkdtempSync(join(tmpdir(), "ai-dev-loop-phase266-e2e-"));
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

test("C266-1 real producer renders provider-aware activity metrics", async ({ page }) => {
  const calls: string[] = [];
  await page.route("**/api/dashboard/read", async (route) => {
    const name = operation(route);
    if (name) calls.push(name);
    await route.continue();
  });
  await page.goto(`${baseUrl}/`);
  await applyMapScope(page);

  await expect(page.getByText("Submissions total:")).toBeVisible();
  await expect(page.getByText("28", { exact: true }).last()).toBeVisible();
  await page.getByLabel("Activity metric").selectOption("review-rounds");
  await expect(page.getByText("Review rounds total:")).toBeVisible();
  await expect(page.getByLabel("Review rounds daily values")).toContainText("2026-01-031");
  await page.getByLabel("Activity metric").selectOption("process-hours");
  await expect(page.getByText("Process hours total:")).toBeVisible();
  await expect(page.locator(".activity-total").getByText("4.917 hours")).toBeVisible();
  expect(calls.filter((name) => name === "dashboard.activity").length).toBeGreaterThanOrEqual(3);
  expect(
    calls.some((name) =>
      ["run.initial-prompt", "run.review", "run.review-content", "run.output"].includes(name),
    ),
  ).toBe(false);
});

test("C266-2 actual HTTP reads obey queue, Pause, visibility, and cadence", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-01-05T12:00:00Z") });
  let active = 0;
  let maximumActive = 0;
  let releaseInitial!: () => void;
  let holding = true;
  const released = new Promise<void>((resolveRelease) => {
    releaseInitial = resolveRelease;
  });
  const calls: string[] = [];
  await page.route("**/api/dashboard/read", async (route) => {
    const name = operation(route);
    if (name) calls.push(name);
    active += 1;
    maximumActive = Math.max(maximumActive, active);
    if (holding) await released;
    active -= 1;
    await route.continue();
  });
  await page.goto(`${baseUrl}/`);
  await expect.poll(() => active).toBe(2);
  expect(maximumActive).toBe(2);
  holding = false;
  releaseInitial();
  await applyMapScope(page);
  await expect(page.getByText("Submissions total:")).toBeVisible();
  await expect(page.getByText("64% remaining · 36% used")).toBeVisible();

  const count = (name: string) => calls.filter((call) => call === name).length;
  const recurring = [
    "dashboard.summary",
    "dashboard.runs",
    "dashboard.activity",
    "machine.codex-capacity",
  ];
  await page.getByRole("button", { name: "Pause automatic refresh" }).click();
  const pausedCounts = Object.fromEntries(recurring.map((name) => [name, count(name)]));
  await page.clock.runFor(120_000);
  for (const name of recurring) expect(count(name)).toBe(pausedCounts[name]);

  await page.getByRole("button", { name: "Refresh applied scope" }).click();
  await expect.poll(() => count("dashboard.activity")).toBe(pausedCounts["dashboard.activity"] + 1);
  await expect.poll(() => count("dashboard.summary")).toBe(pausedCounts["dashboard.summary"] + 1);
  await expect.poll(() => count("dashboard.runs")).toBe(pausedCounts["dashboard.runs"] + 1);
  expect(count("machine.codex-capacity")).toBe(pausedCounts["machine.codex-capacity"]);
  const manualCounts = Object.fromEntries(recurring.map((name) => [name, count(name)]));
  await page.clock.runFor(120_000);
  for (const name of recurring) expect(count(name)).toBe(manualCounts[name]);

  await page.getByRole("button", { name: "Resume automatic refresh" }).click();
  for (const name of recurring) {
    await expect.poll(() => count(name)).toBe(manualCounts[name] + 1);
  }

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const hiddenCounts = Object.fromEntries(recurring.map((name) => [name, count(name)]));
  await page.clock.runFor(120_000);
  for (const name of recurring) expect(count(name)).toBe(hiddenCounts[name]);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => count("dashboard.summary")).toBe(hiddenCounts["dashboard.summary"] + 1);

  const beforeLeave = calls.length;
  await page.goto("about:blank");
  await page.clock.runFor(120_000);
  expect(calls).toHaveLength(beforeLeave);
  expect(maximumActive).toBeLessThanOrEqual(2);
  expect(
    calls.some((name) =>
      ["run.initial-prompt", "run.review", "run.review-content", "run.output"].includes(name),
    ),
  ).toBe(false);
});

test("F-01 paused manual Refresh retains compatible activity and first-page success on failure", async ({
  page,
}) => {
  let failRefresh = false;
  const calls: string[] = [];
  await page.route("**/api/dashboard/read", async (route) => {
    const name = operation(route);
    if (name) calls.push(name);
    if (failRefresh && (name === "dashboard.activity" || name === "dashboard.runs")) {
      await route.abort("connectionfailed");
      return;
    }
    await route.continue();
  });
  await page.goto(`${baseUrl}/`);
  await applyMapScope(page);
  await expect(page.getByText("Submissions total:")).toBeVisible();
  await page.getByRole("button", { name: "Load more runs" }).click();
  await expect(page.getByText("Loaded 28 of 28 started runs")).toBeVisible();
  const activitySuccess = await page.locator(".activity-panel .observation-line").textContent();
  const mapSuccess = await page.locator(".run-map-panel .observation-line").textContent();
  const activityStableDates = activitySuccess?.replace(/ · \d+[smh] old$/, "") ?? "";
  const firstPageSuccess = mapSuccess?.match(/offset 0 · \S+ received \S+/)?.[0] ?? "";

  await page.getByRole("button", { name: "Pause automatic refresh" }).click();
  failRefresh = true;
  const activityCalls = calls.filter((name) => name === "dashboard.activity").length;
  const runCalls = calls.filter((name) => name === "dashboard.runs").length;
  await page.getByRole("button", { name: "Refresh applied scope" }).click();
  await expect.poll(
    () => calls.filter((name) => name === "dashboard.activity").length,
  ).toBe(activityCalls + 1);
  await expect.poll(
    () => calls.filter((name) => name === "dashboard.runs").length,
  ).toBe(runCalls + 1);

  await expect(page.locator(".activity-panel [role=alert]")).toContainText(
    "showing the last successful observation",
  );
  await expect(page.locator(".run-map-panel [role=alert]")).toContainText(
    "showing the last successful visible pages",
  );
  await expect(page.locator(".activity-panel .observation-line")).toContainText(activityStableDates);
  await expect(page.locator(".run-map-panel .observation-line")).toContainText(firstPageSuccess);
  await expect(page.getByText("Loaded 25 of 28 started runs")).toBeVisible();
  await expect(page.getByText("No started runs match the applied scope.")).toHaveCount(0);
  await expect(page.getByText("Automatic refresh: paused")).toBeVisible();
});

test("F-02 held initial capacity cannot gate a paused manual map refresh", async ({ page }) => {
  let releaseCapacity!: () => void;
  let markCapacityStarted!: () => void;
  let markCapacityFinished!: () => void;
  const capacityRelease = new Promise<void>((resolveRelease) => {
    releaseCapacity = resolveRelease;
  });
  const capacityStarted = new Promise<void>((resolveStarted) => {
    markCapacityStarted = resolveStarted;
  });
  const capacityFinished = new Promise<void>((resolveFinished) => {
    markCapacityFinished = resolveFinished;
  });
  let holdCapacity = true;
  let runCalls = 0;
  await page.route("**/api/dashboard/read", async (route) => {
    const name = operation(route);
    if (name === "dashboard.runs") runCalls += 1;
    if (name === "machine.codex-capacity" && holdCapacity) {
      holdCapacity = false;
      markCapacityStarted();
      try {
        await capacityRelease;
        await route.continue();
      } catch {
        // Pause aborts the owned fetch while this synthetic route is held.
      } finally {
        markCapacityFinished();
      }
      return;
    }
    await route.continue();
  });

  try {
    await page.goto(`${baseUrl}/`);
    await capacityStarted;
    await page.getByRole("button", { name: "Pause automatic refresh" }).click();
    await applyMapScope(page);
    const callsBeforeRefresh = runCalls;
    await page.getByRole("button", { name: "Refresh applied scope" }).click();
    await expect.poll(() => runCalls).toBe(callsBeforeRefresh + 1);
    await expect(page.getByText("Loaded 25 of 28 started runs")).toBeVisible();
    await expect(page.getByText("Automatic refresh: paused")).toBeVisible();
    await expect(page.getByRole("button", { name: "Resume automatic refresh" })).toBeVisible();
  } finally {
    releaseCapacity();
    await capacityFinished;
  }
});

test("C266-3 failure recovery and filter retirement keep only current memory ownership", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-01-05T12:00:00Z") });
  let dropActivity = false;
  await page.route("**/api/dashboard/read", async (route) => {
    if (dropActivity && operation(route) === "dashboard.activity") {
      await route.abort("connectionfailed");
      return;
    }
    await route.continue();
  });
  await page.goto(`${baseUrl}/`);
  await applyMapScope(page);
  const lastSuccess = await page.locator(".activity-panel .observation-line").textContent();
  expect(lastSuccess).toContain("Last success");
  dropActivity = true;
  await page.clock.runFor(30_000);
  await expect(page.locator(".activity-panel [role=alert]")).toContainText(
    "showing the last successful observation",
  );
  await expect(page.locator(".activity-panel .observation-line")).toContainText(
    "source observed",
  );
  expect(await page.locator(".activity-panel .observation-line").textContent()).toContain(
    lastSuccess?.split(" · received")[0],
  );
  dropActivity = false;
  await page.clock.runFor(30_000);
  await expect(page.locator(".activity-panel [role=alert]")).toHaveCount(0);

  const repositoryRoot = resolve(process.cwd(), "..");
  const mutation = spawnSync(
    resolve(repositoryRoot, ".venv/bin/python"),
    [resolve(process.cwd(), "e2e/mutate_fixture.py"), "--state-root", stateRoot],
    {
      cwd: repositoryRoot,
      env: {
        ...process.env,
        PYTHONPATH: `${resolve(repositoryRoot, "src")}:${repositoryRoot}`,
      },
      encoding: "utf-8",
    },
  );
  expect(mutation.status, mutation.stderr).toBe(0);
  await page.clock.runFor(30_000);
  await expect(page.getByLabel("Runs started: 29")).toBeVisible();
  await expect(page.getByText("Submissions total:")).toBeVisible();
  await expect(page.getByText("29", { exact: true }).last()).toBeVisible();

  await page.getByRole("button", { name: /^current-map-run codex/ }).click();
  await expect(page.getByText(/Selected run:/)).toContainText("current-map-run");
  await page.getByRole("button", { name: /^attempt-current-map-run-execution Codex implementer/ }).click();
  const control = (name: string) => join(stateRoot, name);
  for (const name of ["hold-content-response", "release-content-response", "content-response-started"]) {
    rmSync(control(name), { force: true });
  }
  writeFileSync(control("hold-content-response"), "hold\n", "utf-8");
  await page.getByRole("button", { name: "Load stdout" }).click();
  await expect.poll(() => existsSync(control("content-response-started"))).toBe(true);

  const projectSelect = page.locator(".filters select");
  await projectSelect.selectOption({ label: '<img src=x onerror="window.__dashboard_xss=1">' });
  // The held content request deliberately keeps the modal backdrop mounted.
  // Dispatch to the real Apply handler so this test can exercise an external
  // scope change while that old sensitive reply is still in flight.
  await page.getByRole("button", { name: "Apply" }).dispatchEvent("click");
  await expect(page.getByText("Select an exact run cell or companion row to inspect it.")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  writeFileSync(control("release-content-response"), "release\n", "utf-8");
  await page.clock.runFor(1_000);
  await expect(page.getByText(/stdout sentinel for attempt-current-map-run-execution/)).toHaveCount(0);
  await expect(page.getByLabel("Runs started: 1")).toBeVisible();
});
