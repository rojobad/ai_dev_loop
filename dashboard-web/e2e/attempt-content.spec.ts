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

async function applyMapScope(page: Page): Promise<void> {
  await page.goto(`${baseUrl}/`);
  await page.locator(".filters select").selectOption("map-project");
  await page.getByLabel("IANA time zone").fill("UTC");
  await page.getByLabel("From").fill("01/01/2026 00:00");
  await page.getByLabel("Until (exclusive)").fill("05/01/2026 00:00");
  await expect(page.getByText("Loaded 28 of 28 started runs")).toBeVisible();
}

async function expectFocusInsideDialog(page: Page): Promise<void> {
  await expect.poll(() => page.evaluate(() => {
    const dialog = document.querySelector<HTMLElement>("[role='dialog']");
    return dialog?.contains(document.activeElement) ?? false;
  })).toBe(true);
}

test.beforeAll(async () => {
  const port = await reservePort();
  baseUrl = `http://127.0.0.1:${port}`;
  stateRoot = mkdtempSync(join(tmpdir(), "ai-dev-loop-phase265-e2e-"));
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

test("C265-1 deliberate controls preserve exact implementer and reviewer ownership", async ({
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
  await page.getByLabel("Select loaded run").selectOption("current-map-run");
  await page.locator(".attempt-details-disclosure > summary").click();
  await page.getByRole("button", { name: /Attempt ID: attempt-current-map-run-execution/ }).click();
  await expect(page.getByRole("dialog")).toContainText("codex.implementer_bootstrap");
  expect(
    operations.some((call) =>
      ["run.initial-prompt", "run.review", "run.review-content", "run.output"].includes(
        call.operation,
      ),
    ),
  ).toBe(false);

  await page.getByRole("button", { name: "Load initial run prompt (context)" }).click();
  await expect(page.getByText("fixture initial run prompt (context only)")).toBeVisible();
  expect(operations.filter((call) => call.operation === "run.initial-prompt")).toEqual([
    expect.objectContaining({ parameters: expect.objectContaining({ runId: "current-map-run" }) }),
  ]);
  expect(operations.some((call) => call.operation === "run.review-content")).toBe(false);
  await page.getByRole("button", { name: "Close attempt details" }).click();

  await page.getByLabel("Select loaded run").selectOption("older-map-run");
  await page.locator(".attempt-details-disclosure > summary").click();
  await page.getByRole("button", { name: "Load more attempts" }).click();
  await page.getByRole("button", { name: /Attempt ID: attempt-older-map-run-review-retry/ }).click();
  const sensitiveBeforeReviewerAction = operations.filter((call) =>
    ["run.review", "run.review-content", "run.output"].includes(call.operation),
  ).length;
  await expect(page.getByRole("dialog")).toContainText("codex.resume_review");
  expect(sensitiveBeforeReviewerAction).toBe(0);
  await page.getByRole("button", { name: "Load exact review prompt" }).click();
  await expect(page.getByText("exact reviewer prompt sentinel")).toBeVisible();
  expect(operations.filter((call) => call.operation === "run.review-content")).toEqual([
    expect.objectContaining({
      parameters: expect.objectContaining({
        runId: "older-map-run",
        attemptId: "attempt-older-map-run-review-retry",
        kind: "prompt",
      }),
    }),
  ]);
  await page.getByRole("button", { name: "Load review-owned fix prompt" }).click();
  await expect(page.getByText("review-owned fix prompt sentinel")).toBeVisible();
  await page.getByRole("button", { name: "Close attempt details" }).click();

  await page.getByRole("button", { name: /Attempt ID: attempt-older-map-run-active-correction/ }).click();
  await expect(page.getByText(/Exact correction prompt unavailable/)).toBeVisible();
  await page.getByRole("button", { name: "Load stderr" }).click();
  await expect(
    page.getByText("stderr sentinel for attempt-older-map-run-active-correction"),
  ).toBeVisible();
  expect(
    operations.filter(
      (call) =>
        call.operation === "run.review-content" &&
        call.parameters.attemptId === "attempt-older-map-run-active-correction",
    ),
  ).toHaveLength(0);
});

test("C265-3 Escape retires an in-flight sensitive body and late success cannot reopen", async ({
  page,
}) => {
  for (const name of [
    "hold-content-response",
    "release-content-response",
    "content-response-started",
  ]) {
    rmSync(join(stateRoot, name), { force: true });
  }
  await applyMapScope(page);
  await page.getByLabel("Select loaded run").selectOption("current-map-run");
  await page.locator(".attempt-details-disclosure > summary").click();
  const row = page.getByRole("button", { name: /Attempt ID: attempt-current-map-run-execution/ });
  await row.click();
  writeFileSync(join(stateRoot, "hold-content-response"), "hold\n", "utf-8");
  try {
    await page.getByRole("button", { name: "Load stdout" }).click();
    await expect.poll(() => existsSync(join(stateRoot, "content-response-started"))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(row).toBeFocused();
    writeFileSync(join(stateRoot, "release-content-response"), "release\n", "utf-8");
    await page.waitForTimeout(250);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(
      page.getByText("stdout sentinel for attempt-current-map-run-execution"),
    ).toHaveCount(0);
    expect(await page.evaluate(() => localStorage.getItem("ai-dev-loop-dashboard-theme")))
      .toMatch(/^(light|dark)$/);
    expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([
      "ai-dev-loop-dashboard-theme",
    ]);
  } finally {
    writeFileSync(join(stateRoot, "release-content-response"), "release\n", "utf-8");
    rmSync(join(stateRoot, "hold-content-response"), { force: true });
  }
});

test("C265-3 paging keeps focus contained through loading and terminal completion", async ({
  page,
}) => {
  const controlNames = [
    "paged-content",
    "hold-paging-response",
    "release-paging-response",
    "paging-response-started",
  ];
  for (const name of controlNames) rmSync(join(stateRoot, name), { force: true });
  writeFileSync(join(stateRoot, "paged-content"), "paged\n", "utf-8");
  writeFileSync(join(stateRoot, "hold-paging-response"), "hold\n", "utf-8");

  try {
    await applyMapScope(page);
    await page.getByLabel("Select loaded run").selectOption("current-map-run");
    await page.locator(".attempt-details-disclosure > summary").click();
    const row = page.getByRole("button", { name: /Attempt ID: attempt-current-map-run-execution/ });
    await row.click();
    const dialog = page.getByRole("dialog");
    await page.getByRole("button", { name: "Load stdout" }).click({ timeout: 5_000 });
    await expect(dialog.locator(".content-reader")).toContainText(
      "65,536 raw bytes in this window",
      { timeout: 5_000 },
    );
    await dialog.getByRole("button", { name: "Load more", exact: true }).click({ timeout: 5_000 });
    await expect.poll(
      () => existsSync(join(stateRoot, "paging-response-started")),
      { timeout: 5_000 },
    ).toBe(true);

    await expect(dialog.getByText("Reading one bounded content chunk…")).toBeVisible();
    await expectFocusInsideDialog(page);
    await page.keyboard.press("Tab");
    await expectFocusInsideDialog(page);
    await page.keyboard.press("Shift+Tab");
    await expectFocusInsideDialog(page);

    writeFileSync(join(stateRoot, "release-paging-response"), "release\n", "utf-8");
    await expect(dialog.locator(".content-text")).toContainText("terminal page sentinel");
    await expect(dialog.getByRole("button", { name: "Load more" })).toHaveCount(0);
    await expectFocusInsideDialog(page);
    await page.keyboard.press("Tab");
    await expectFocusInsideDialog(page);
    await page.keyboard.press("Shift+Tab");
    await expectFocusInsideDialog(page);

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(row).toBeFocused();
  } finally {
    if (existsSync(stateRoot)) {
      writeFileSync(join(stateRoot, "release-paging-response"), "release\n", "utf-8");
      for (const name of controlNames) rmSync(join(stateRoot, name), { force: true });
    }
  }
});
