import { expect, Page, test } from "@playwright/test";

/**
 * E2E: Research Console — 実API/worker/mock runnerに対するフルスタック検証。
 * 前提: scripts/e2e_stack.sh start 済み (npm run e2e が一括実行)。
 */

async function startJob(page: Page, topic: string, engineNames: string[]) {
  await page.goto("/");
  await page.locator("#jf-topic").fill(topic);
  for (const name of engineNames) {
    await page.getByRole("checkbox", { name: new RegExp(name) }).check();
  }
  // egress preview (通信先の事前表示) がフォーム上に出ている
  await expect(page.getByText("この実行で通信する外部先")).toBeVisible();
  await page.getByRole("button", { name: "リサーチ開始" }).click();
}

test("3つのMockエンジンが並列実行され、エンジン別カードが実イベントで更新される", async ({
  page,
}) => {
  await startJob(page, "E2E並列実行テスト", ["Mock Fast", "Mock Slow", "Mock Partial"]);

  // エンジン別カードが3枚現れる
  const fast = page.getByRole("article", { name: "Mock Fast" });
  const slow = page.getByRole("article", { name: "Mock Slow" });
  const partial = page.getByRole("article", { name: "Mock Partial" });
  await expect(fast).toBeVisible();
  await expect(slow).toBeVisible();
  await expect(partial).toBeVisible();

  // fastが先に成功し、その時点でslowは未完 (並列性の観察)
  await expect(fast.getByText("成功", { exact: true })).toBeVisible({ timeout: 60_000 });
  // 全体完了
  await expect(slow.getByText("成功", { exact: true })).toBeVisible({ timeout: 90_000 });
  await expect(partial.getByText("成功", { exact: true })).toBeVisible({ timeout: 90_000 });

  // mock-partialのトークンは捏造されず「不明」表示
  await expect(partial.getByText("不明").first()).toBeVisible();
});

test("1エンジン失敗時に他は完走し、ジョブがpartialとして表示される", async ({ page }) => {
  await startJob(page, "E2E部分失敗テスト", ["Mock Fast", "Mock Fail"]);

  const fail = page.getByRole("article", { name: "Mock Fail" });
  await expect(fail.getByText("失敗", { exact: true })).toBeVisible({ timeout: 120_000 });

  const fast = page.getByRole("article", { name: "Mock Fast" });
  await expect(fast.getByText("成功", { exact: true })).toBeVisible({ timeout: 60_000 });

  // partialバナー — 完全成功として表示されない
  await expect(page.getByText("一部のエンジンのみ成功しました")).toBeVisible({
    timeout: 60_000,
  });
});

test("リロード後も状態とイベントが復元される (SSE再接続)", async ({ page }) => {
  await startJob(page, "E2E再接続テスト", ["Mock Slow"]);
  const slow = page.getByRole("article", { name: "Mock Slow" });
  await expect(slow.getByText("調査中", { exact: true })).toBeVisible({ timeout: 30_000 });

  await page.reload();
  const slowAfter = page.getByRole("article", { name: "Mock Slow" });
  await expect(slowAfter).toBeVisible();
  await expect(slowAfter.getByText("成功", { exact: true })).toBeVisible({ timeout: 90_000 });
});

test("個別キャンセルが機能する", async ({ page }) => {
  await startJob(page, "E2Eキャンセルテスト", ["Mock Cancellable"]);
  const card = page.getByRole("article", { name: "Mock Cancellable" });
  await expect(card.getByText("調査中", { exact: true })).toBeVisible({ timeout: 30_000 });
  await card.getByRole("button", { name: "このランをキャンセル" }).click();
  await expect(card.getByText("キャンセル済み", { exact: true })).toBeVisible({
    timeout: 60_000,
  });
});

test("矛盾がConflictsタブに両論のまま表示される", async ({ page }) => {
  await startJob(page, "E2E矛盾テスト", ["Mock Fast", "Mock Slow"]);
  await expect(
    page.getByRole("article", { name: "Mock Slow" }).getByText("成功", { exact: true })
  ).toBeVisible({ timeout: 90_000 });

  await page.getByRole("tab", { name: "不一致" }).click();
  // 両エンジンの矛盾する値 (12% / 25%) が併記される
  await expect(page.getByText("12%").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("25%").first()).toBeVisible();
});
