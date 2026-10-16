import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "**/installed-acceptance.spec.ts",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: "line",
  use: {
    headless: true,
    trace: "off",
    screenshot: "off",
    video: "off",
  },
});
