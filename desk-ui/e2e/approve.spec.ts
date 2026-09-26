import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const code = () => readFileSync(fileURLToPath(new URL("./.enrol-code", import.meta.url)), "utf8").trim();

/** A platform authenticator that verifies the user, as Face ID / Touch ID would. */
async function virtualPasskey(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
}

async function hold(page: Page, name: RegExp, ms = 1300) {
  const button = page.getByRole("button", { name });
  const box = await button.boundingBox();
  if (!box) throw new Error("hold button not visible");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(ms);
  await page.mouse.up();
}

// One server for both projects: each test leaves the book as the next expects,
// so the phone run approves and the desktop run finds the plan already sent.
test.describe.configure({ mode: "serial" });

test("enrol a passkey, approve the plan by holding, then halt and resume", async ({ page }, info) => {
  test.skip(info.project.name !== "phone", "the approval runs once, on the phone");
  await virtualPasskey(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Order plan · standard" })).toBeVisible();
  await expect(page.getByText("ACAD")).toBeVisible();
  await page.screenshot({ path: "test-results/phone-today-plan.png" });
  // Token alone cannot approve: the app asks for a passkey first.
  await page.getByRole("button", { name: "Enrol a passkey to approve" }).click();

  await expect(page.getByRole("heading", { name: "Security" })).toBeVisible();
  await page.getByLabel(/Enrolment code/).fill(code());
  await page.getByRole("button", { name: "Create passkey" }).click();
  await expect(page.getByText(/Passkey enrolled/)).toBeVisible();

  await page.getByRole("link", { name: "Today" }).click();
  // Letting go early does nothing.
  await hold(page, /Hold to approve 3 orders/, 250);
  await expect(page.getByRole("heading", { name: "Order plan · standard" })).toBeVisible();
  await hold(page, /Hold to approve 3 orders/);
  await expect(page.getByText(/sent 3 of 3 orders/)).toBeVisible();
  await expect(page.getByText(/No order plan waiting/)).toBeVisible();

  // Halt needs no passkey; resuming does.
  await page.getByRole("button", { name: /Halt/ }).first().click();
  await page.getByLabel("Reason (optional)").fill("e2e");
  await page.getByRole("button", { name: "Halt trading" }).click();
  await expect(page.getByText(/Trading halted \(e2e\)/)).toBeVisible();
  await expect(page.getByText("Trading is halted")).toBeVisible();
  await page.screenshot({ path: "test-results/phone-today-halted.png" });
  await page.getByRole("link", { name: "Book" }).first().click();
  await page.getByRole("button", { name: "Resume trading" }).click();
  await expect(page.getByText(/Trading resumed/)).toBeVisible();
  await expect(page.getByText("ALLOWED")).toBeVisible();
});

test("an enrolment code works once", async ({ page }, info) => {
  test.skip(info.project.name !== "phone", "runs after the enrolment above");
  await virtualPasskey(page);
  await page.goto("/security");
  await page.getByLabel(/Enrolment code/).fill(code());
  await page.getByRole("button", { name: "Create passkey" }).click();
  await expect(page.getByText(/wrong or has expired/)).toBeVisible();
});

test("the desktop layout: the book, the cohort the phone bought, the sidebar", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "desktop layout");
  await page.goto("/book");
  await expect(page.getByRole("heading", { name: "Book" })).toBeVisible();
  await expect(page.getByText("ALLOWED")).toBeVisible();
  await expect(page.getByText(/Bought/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Halt trading/ })).toBeVisible();
  await page.screenshot({ path: "test-results/desktop-book.png", fullPage: true });
  await page.goto("/");
  await page.screenshot({ path: "test-results/desktop-today.png", fullPage: true });
});
