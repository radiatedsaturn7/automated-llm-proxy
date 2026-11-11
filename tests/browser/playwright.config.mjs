import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.mjs',
  workers: 1,
  retries: 0, // Bootstrap is intentionally one-shot; retries require a fresh database.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  use: {
    baseURL: process.env.GATEWAY_TEST_URL || 'http://127.0.0.1:8080',
    browserName: 'chromium',
    // Never capture administrator credentials in trace/screenshot/video artifacts.
    trace: 'off', screenshot: 'off', video: 'off',
    launchOptions: process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
  },
});
