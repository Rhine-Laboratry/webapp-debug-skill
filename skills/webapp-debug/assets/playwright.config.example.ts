import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './generated',
  outputDir: '../../.webapp-debug/artifacts/playwright',
  fullyParallel: false,
  workers: 1,
  retries: 1,
  timeout: 60_000,
  expect: {
    timeout: 10_000,
  },
  reporter: [
    ['list'],
    ['json', { outputFile: '../../.webapp-debug/state/playwright-results.json' }],
    ['html', { outputFolder: '../../.webapp-debug/artifacts/playwright-report', open: 'never' }],
  ],
  use: {
    baseURL: process.env.WEBAPP_DEBUG_BASE_URL,
    browserName: 'chromium',
    viewport: { width: 1440, height: 900 },
    locale: 'ja-JP',
    timezoneId: 'Asia/Tokyo',
    trace: 'retain-on-failure-and-retries',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
});
