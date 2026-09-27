import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "../tests/browser",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:8000",
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    },
    trace: "retain-on-failure",
  },
});
