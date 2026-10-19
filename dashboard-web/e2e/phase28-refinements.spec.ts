import { expect, test, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
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
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (serverProcess.exitCode !== null) {
      throw new Error(`Fixture server exited early with ${serverProcess.exitCode}.`);
    }
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // The owned server is still constructing its isolated large ledger.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 50));
  }
  throw new Error("Fixture dashboard server did not become ready.");
}

async function setScopeWithoutTypingDelay(page: Page): Promise<void> {
  await page.evaluate(() => {
    const update = (label: string, value: string) => {
      const control = [...document.querySelectorAll("label")]
        .find((candidate) => candidate.textContent?.trim().startsWith(label))
        ?.querySelector("input");
      if (!control) throw new Error(`Missing ${label} input.`);
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )?.set;
      setter?.call(control, value);
      control.dispatchEvent(new Event("input", { bubbles: true }));
    };
    update("IANA time zone", "UTC");
    update("From", "01/01/2026 00:00");
    update("Until (exclusive)", "08/01/2026 00:00");
  });
}

async function hoverTopCalendarCell(page: Page): Promise<void> {
  const chart = page.getByRole("img", { name: /Submissions activity calendar/ });
  await chart.scrollIntoViewIfNeeded();
  await chart.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  const point = await chart.evaluate((element) => {
    for (const canvas of element.querySelectorAll("canvas")) {
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) continue;
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      let minX = canvas.width;
      let maxX = -1;
      let minY = canvas.height;
      let maxY = -1;
      for (let y = 0; y < canvas.height * 0.7; y += 1) {
        for (let x = 0; x < canvas.width; x += 1) {
          const offset = (y * canvas.width + x) * 4;
          if (
            Math.abs(pixels[offset] - 47) <= 5 &&
            Math.abs(pixels[offset + 1] - 118) <= 5 &&
            Math.abs(pixels[offset + 2] - 210) <= 5 &&
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
    throw new Error("The first-row activity cell was not found on the real canvas.");
  });
  await page.mouse.move(point.x, point.y);
}

async function hoverLastRunMapCell(page: Page): Promise<void> {
  const scroll = page.locator(".run-map-chart-scroll");
  await scroll.scrollIntoViewIfNeeded();
  await scroll.evaluate((element) => {
    element.scrollLeft = element.scrollWidth;
  });
  const chart = page.getByRole("img", {
    name: "Selectable chart cells for authoritative run IDs",
  });
  await chart.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  const point = await chart.evaluate((element) => {
    for (const canvas of element.querySelectorAll("canvas")) {
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) continue;
      const sampleWidth = Math.min(canvas.width, 320);
      const sampleLeft = canvas.width - sampleWidth;
      const image = context.getImageData(sampleLeft, 0, sampleWidth, canvas.height);
      let maxX = -1;
      for (let y = 0; y < canvas.height; y += 1) {
        for (let x = 0; x < sampleWidth; x += 1) {
          const offset = (y * sampleWidth + x) * 4;
          if (
            Math.abs(image.data[offset] - 47) <= 5 &&
            Math.abs(image.data[offset + 1] - 118) <= 5 &&
            Math.abs(image.data[offset + 2] - 210) <= 5 &&
            image.data[offset + 3] >= 240
          ) {
            maxX = Math.max(maxX, x);
          }
        }
      }
      if (maxX >= 0) {
        let minX = maxX;
        let minY = canvas.height;
        let maxY = -1;
        for (let y = 0; y < canvas.height; y += 1) {
          for (let x = Math.max(0, maxX - 36); x <= maxX; x += 1) {
            const offset = (y * sampleWidth + x) * 4;
            if (
              Math.abs(image.data[offset] - 47) <= 5 &&
              Math.abs(image.data[offset + 1] - 118) <= 5 &&
              Math.abs(image.data[offset + 2] - 210) <= 5 &&
              image.data[offset + 3] >= 240
            ) {
              minX = Math.min(minX, x);
              minY = Math.min(minY, y);
              maxY = Math.max(maxY, y);
            }
          }
        }
        const box = canvas.getBoundingClientRect();
        return {
          x: box.left + ((sampleLeft + (minX + maxX) / 2) / canvas.width) * box.width,
          y: box.top + (((minY + maxY) / 2) / canvas.height) * box.height,
        };
      }
    }
    throw new Error("The final large-cohort run cell was not found on the real canvas.");
  });
  await page.mouse.move(point.x, point.y);
}

async function expectViewportTooltip(
  page: Page,
  hover: (page: Page) => Promise<void>,
  expectedText: string,
): Promise<void> {
  await hover(page);
  const tooltip = page.locator("body > div:not(#root)").filter({ hasText: expectedText }).last();
  await expect(tooltip).toBeVisible();
  await expect.poll(async () => {
    const box = await tooltip.boundingBox();
    const viewport = page.viewportSize();
    return Boolean(
      box &&
      viewport &&
      box.x >= 0 &&
      box.y >= 0 &&
      box.x + box.width <= viewport.width + 1 &&
      box.y + box.height <= viewport.height + 1
    );
  }).toBe(true);
  const box = await tooltip.boundingBox();
  const viewport = page.viewportSize();
  if (!box || !viewport) throw new Error("The ECharts tooltip has no viewport box.");
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 1);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 1);
  expect(await tooltip.evaluate((element) => element.parentElement === document.body)).toBe(true);
  expect(Number(await tooltip.evaluate((element) => getComputedStyle(element).zIndex))).toBe(900);
}

async function expectChartTooltipsInViewport(page: Page): Promise<void> {
  await expectViewportTooltip(page, hoverTopCalendarCell, "05/01/2026");
  await expectViewportTooltip(page, hoverLastRunMapCell, "Large alias 1004");
}

test.beforeAll(async () => {
  const port = await reservePort();
  baseUrl = `http://127.0.0.1:${port}`;
  stateRoot = mkdtempSync(join(tmpdir(), "ai-dev-loop-phase283-e2e-"));
  const repositoryRoot = resolve(process.cwd(), "..");
  serverProcess = spawn(
    resolve(repositoryRoot, ".venv/bin/python"),
    [
      resolve(process.cwd(), "e2e/fixture_server.py"),
      "--port",
      String(port),
      "--state-root",
      stateRoot,
      "--large-cohort",
      "1005",
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

test("C283-1..5 automatic complete cohort, information controls, and viewport tooltips", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1095, height: 884 });
  const requests: Array<{
    operation: string;
    parameters: Record<string, unknown>;
    status: number;
  }> = [];
  page.on("response", (response) => {
    if (!response.url().endsWith("/api/dashboard/read")) return;
    const request = response.request();
    const body = JSON.parse(request.postData() ?? "{}") as {
      operation?: string;
      parameters?: Record<string, unknown>;
    };
    if (body.operation) {
      requests.push({
        operation: body.operation,
        parameters: body.parameters ?? {},
        status: response.status(),
      });
    }
  });

  await page.goto(`${baseUrl}/`);
  await expect(page.getByRole("button", { name: "Apply" })).toHaveCount(0);
  await expect(page.getByLabel("Runs started: 0")).toBeVisible();
  await expect(page.getByText("64% remaining · 36% used")).toBeVisible();
  await expect(page.getByText("0/0 loaded")).toBeVisible();
  await setScopeWithoutTypingDelay(page);
  await page.waitForTimeout(300);
  expect(
    requests.filter(
      (request) =>
        request.operation === "dashboard.summary" && request.parameters.timeZone === "UTC",
    ),
  ).toHaveLength(0);

  await expect(page.getByText("1036/1036 loaded")).toBeVisible();
  await expect(page.getByLabel("Runs started: 1036")).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more runs" })).toHaveCount(0);
  const runOptions = page.getByLabel("Select loaded run").locator("option");
  await expect(runOptions).toHaveCount(1037);
  expect(
    await runOptions.evaluateAll((options) =>
      new Set(options.slice(1).map((option) => (option as HTMLOptionElement).value)).size,
    ),
  ).toBe(1036);
  await expect(runOptions.filter({ hasText: "older-map-run" })).toHaveCount(1);
  await expect(runOptions.filter({ hasText: "Large alias 1004" })).toHaveCount(1);

  const runPages = requests.filter(
    (request) =>
      request.operation === "dashboard.runs" &&
      request.parameters.timeZone === "UTC" &&
      request.parameters.projectName === undefined,
  );
  expect(runPages.slice(0, 4).map(({ parameters, status }) => ({
    offset: parameters.offset,
    limit: parameters.limit,
    status,
  }))).toEqual([
    { offset: 0, limit: 1000, status: 413 },
    { offset: 0, limit: 500, status: 200 },
    { offset: 500, limit: 500, status: 200 },
    { offset: 1000, limit: 500, status: 200 },
  ]);

  const informationLabels = [
    "Inspection scope",
    "Started runs by project and state",
    "Project workload",
    "Current Codex capacity",
    "Sequence and run map",
    "Attempt timeline",
    "Activity calendar",
  ];
  for (const label of informationLabels) {
    await expect(page.getByRole("button", { name: `Information about ${label}` }))
      .toHaveAttribute("aria-expanded", "false");
  }
  await expect(page.getByText(/Typed date and time-zone edits commit 400 ms/)).toHaveCount(0);
  const scopeInformation = page.getByRole("button", { name: "Information about Inspection scope" });
  await scopeInformation.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "Inspection scope" })).toContainText(
    "Typed date and time-zone edits commit 400 ms",
  );
  const informationClose = page.getByRole("button", { name: "Close" });
  await expect(informationClose).toBeFocused();
  await expect(page.locator("#root")).toHaveAttribute("inert", "");
  await page.keyboard.press("Tab");
  await expect(informationClose).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(informationClose).toBeFocused();
  await page.locator("button.theme-toggle").evaluate((element: HTMLElement) => element.focus());
  await expect(informationClose).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(scopeInformation).toBeFocused();
  await expect(page.locator("#root")).not.toHaveAttribute("inert", "");

  const dailyValues = page.locator(".activity-days-disclosure");
  await expect(dailyValues).not.toHaveAttribute("open", "");
  await dailyValues.getByText("Show daily values").click();
  await expect(page.getByLabel("Submissions daily values")).toContainText("05/01/20261005");

  await expectChartTooltipsInViewport(page);
  await page.getByRole("button", { name: "Use dark theme" }).click();
  await expectChartTooltipsInViewport(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await expectChartTooltipsInViewport(page);
  await page.getByRole("button", { name: "Use light theme" }).click();
  await expectChartTooltipsInViewport(page);
  await page.getByLabel("Activity metric").selectOption("process-hours");
  await expect(page.locator(".activity-total strong")).toHaveText(/^\d+ h \d+ m$/);
  await expect(page.locator(".activity-panel .coverage")).toContainText(
    "missing durations",
  );
  await expect(page.getByLabel("Process hours daily values")).toContainText(/\d+ h \d+ m/);

  const summariesBeforeProject = requests.filter(
    (request) => request.operation === "dashboard.summary",
  ).length;
  await page.locator(".filters select").selectOption("large-project");
  await expect.poll(
    () => requests.filter(
      (request) =>
        request.operation === "dashboard.summary" &&
        request.parameters.projectName === "large-project",
    ).length,
  ).toBeGreaterThan(0);
  expect(requests.filter((request) => request.operation === "dashboard.summary").length)
    .toBeGreaterThan(summariesBeforeProject);
  await page.locator(".filters select").selectOption("");
  await expect(page.getByLabel("Runs started: 1036")).toBeVisible();

  const summariesBeforeInvalid = requests.filter(
    (request) => request.operation === "dashboard.summary",
  ).length;
  await page.getByLabel("From").fill("incomplete");
  await expect(page.getByRole("alert")).toContainText(
    "previous valid results remain unchanged",
  );
  expect(requests.filter((request) => request.operation === "dashboard.summary").length)
    .toBe(summariesBeforeInvalid);
  await expect(page.getByText("1036/1036 loaded")).toBeVisible();
});
