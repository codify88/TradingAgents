import { expect, test } from "@playwright/test";

test("browse an agent's tools, then save a variant with an edit for it", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "the lab is a desktop screen");
  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: "Agent lab" })).toBeVisible();
  await page.getByRole("button", { name: "News Analyst" }).click();
  await expect(page.getByText("get_macro_indicators")).toBeVisible();
  await expect(page.getByText("get_congress_trades")).toBeVisible(); // listed as unused

  await page.getByRole("button", { name: /Edit in a variant/ }).click();
  await page.getByLabel("Name").fill("flat-book");
  await page.getByLabel("Edit 1: text").fill("The book is in cash; rate the next 5 trading days against SPY.");
  await page.getByRole("button", { name: "Save variant" }).click();
  await expect(page.getByText(/Saved flat-book, version 1/)).toBeVisible();
  await expect(page.getByRole("button", { name: /flat-book v1 · 1 edit/ })).toBeVisible();

  await page.getByRole("tab", { name: /Runs/ }).click();
  await expect(page.getByText("Test a variant")).toBeVisible();
});
