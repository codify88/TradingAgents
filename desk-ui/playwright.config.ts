import { defineConfig, devices } from "@playwright/test";

// End to end against the real Desk API (e2e/server.py: fake broker, temporary
// book) serving the built app. Chromium only: its virtual WebAuthn
// authenticator stands in for Face ID / Touch ID.
export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:8799", trace: "retain-on-failure" },
  webServer: {
    command: "../.venv/bin/python e2e/server.py --port 8799",
    url: "http://localhost:8799/api/v1/session",
    reuseExistingServer: false,
    timeout: 60_000,
  },
  projects: [
    { name: "phone", use: { ...devices["Pixel 7"] } },
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 860 } } },
  ],
});
