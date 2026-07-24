import { defineConfig } from "@playwright/test";

// E2Eはフルスタック (PG/Redis/API/worker/mock runner/frontend) を前提とする。
// 起動は ../scripts/e2e_stack.sh を使用: `npm run e2e` が一括実行する。
export default defineConfig({
  testDir: "./e2e",
  timeout: 180_000,
  expect: { timeout: 30_000 },
  retries: 0,
  workers: 1, // ジョブ実行順序を安定させるため直列
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:3000",
    trace: "retain-on-failure",
  },
});
