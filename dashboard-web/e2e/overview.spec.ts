import { expect, test } from "@playwright/test";
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

test.beforeAll(async () => {
  const port = await reservePort();
  baseUrl = `http://127.0.0.1:${port}`;
  stateRoot = mkdtempSync(join(tmpdir(), "ai-dev-loop-phase263-e2e-"));
  const repositoryRoot = resolve(process.cwd(), "..");
  const python = resolve(repositoryRoot, ".venv/bin/python");
  serverProcess = spawn(
    python,
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

test("C263-1..4 real server renders bounded overview with local-only assets", async ({ page }) => {
  const externalRequests: string[] = [];
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.protocol.startsWith("http") && url.hostname !== "127.0.0.1") {
      externalRequests.push(url.href);
      await route.abort();
      return;
    }
    await route.continue();
  });

  await page.goto(`${baseUrl}/`);
  await expect(page.getByRole("heading", { name: "Project overview" })).toBeVisible();
  await page.getByLabel("IANA time zone").fill("UTC");
  await page.getByLabel("From").fill("01/01/2026 00:00");
  await page.getByLabel("Until (exclusive)").fill("03/01/2026 00:00");
  await page.getByRole("button", { name: "Apply" }).click();

  await expect(page.getByLabel("Runs started: 3")).toBeVisible();
  await expect(page.getByText("64% remaining · 36% used")).toBeVisible();
  await expect(page.getByText("27% remaining · 73% used")).toBeVisible();
  await expect(page.getByText("Reset unavailable")).toBeVisible();
  const malicious = '<img src=x onerror="window.__dashboard_xss=1">';
  await expect(page.getByText(malicious, { exact: true }).last()).toBeVisible();
  expect(await page.locator('img[src="x"]').count()).toBe(0);
  expect(await page.evaluate(() => (window as Window & { __dashboard_xss?: number }).__dashboard_xss)).toBeUndefined();

  await page.getByRole("button", { name: "Use dark theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.setViewportSize({ width: 390, height: 844 });
  const dimensions = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
  expect(externalRequests).toEqual([]);
});
