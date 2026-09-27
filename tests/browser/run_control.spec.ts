// Supported production run, progress, Stop, and reconnect flows.
import { expect, test, type Page } from "@playwright/test";

const zeroCommitFocus = "Pensae Signal browser zero-commit regression";

async function resetSettings(page: Page) {
  const bootstrapResponse = await page.request.get("/api/bootstrap");
  expect(bootstrapResponse.ok()).toBeTruthy();
  const bootstrap = await bootstrapResponse.json();
  const reset = await page.request.post("/api/settings/reset", {
    data: { confirmation: "RESET TO PROTECTED DEFAULTS" },
    headers: {
      "Content-Type": "application/json",
      Origin: bootstrap.canonical_origin,
      "X-Pensae-Session": bootstrap.session_nonce,
    },
  });
  expect(reset.ok()).toBeTruthy();
}

test("start streams bounded progress and opens the committed detail", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveTitle("Pensae Signal | Local readiness");
  await expect(page.getByRole("link", { name: "Pensae Signal" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Pensae Signal readiness" })).toBeVisible();
  const start = page.getByRole("button", { name: "Start research" });
  await expect(start).toBeEnabled();
  await start.click();

  await expect(page.getByRole("heading", { name: "Research in progress" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText(/discovery|signal|focused|similarity/);

  await page.getByRole("button", { name: "Open opportunity 1 of 1" }).click();
  await expect(
    page.getByRole("heading", { name: "Maintenance Request Triage Assistant" }),
  ).toBeVisible();
  await expect(page.getByText("Verdict: promising", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Verified evidence chain" })).toBeVisible();
  const source = page.getByRole("link", { name: /Synthetic maintenance workflow source/ });
  await expect(source).toHaveAttribute("href", "https://example.test/source");
  await expect(page.getByText("Scores and verdict are read-only policy results.")).toBeVisible();
  await expect(page.getByText("Daily delays consume staff time and tenant trust.")).toBeVisible();
  await expect(page.getByText("Teams already fund software and coordination time.")).toBeVisible();
  await expect(page.getByText("Incumbents could add a similar focused workflow.")).toBeVisible();
  await expect(page.getByText("The first version avoids deep integrations.")).toBeVisible();
  await expect(page.getByText("A local web app fits this auditable workflow.")).toBeVisible();
  await expect(page.getByText("Tenant messages may contain personal data.")).toBeVisible();
  await expect(page.getByText("A plausible operations buyer exists.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Claim labels and evidence limitations" })).toBeVisible();
});

test("stop after an earlier commit preserves it and reaches stopped", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Start research" }).click();
  await expect(page.getByText(/target opportunities achieved/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recent progress" })).toBeVisible();
  await expect(page.getByText(/stage completed · similarity commit/)).toBeVisible();
  const runId = new URL(page.url()).pathname.split("/").at(-1);
  expect(runId).toBeTruthy();

  await page.getByRole("button", { name: "Stop research" }).click();
  await page.getByRole("button", { name: "Open opportunity 1 of 1" }).click();
  await expect(
    page.getByRole("heading", { name: "Maintenance Request Triage Assistant" }),
  ).toBeVisible();
  const snapshot = await page.request.get(`/api/runs/${runId}`);
  expect(snapshot.ok()).toBeTruthy();
  expect((await snapshot.json()).state).toBe("stopped");
  expect((await page.request.get("/api/opportunities/00000000-0000-0000-0000-000000000099")).status()).toBe(404);
});

test("refresh restores the authoritative active snapshot and reconnects", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Start research" }).click();
  await expect(page.getByRole("heading", { name: "Research in progress" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText(/discovery search|signal extraction/);

  await page.reload();

  await expect(page.getByRole("heading", { name: "Research in progress" })).toBeVisible();
  await expect(page.getByText(/target opportunities achieved/)).toBeVisible();
  await page.getByRole("button", { name: "Open opportunity 1 of 1" }).click();
  await expect(
    page.getByRole("heading", { name: "Maintenance Request Triage Assistant" }),
  ).toBeVisible();
});

test("saved settings survive navigation and an empty terminal run stays explainable without HTTP errors", async ({
  page,
}) => {
  const failedResponses: string[] = [];
  const consoleErrors: string[] = [];
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push(`${response.status()} ${response.url()}`);
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });

  await expect
    .poll(async () => (await page.request.get("/api/runs/active")).status())
    .toBe(204);

  try {
    await resetSettings(page);
    await page.goto("/settings");
    await page.getByRole("combobox", { name: "Discovery mode" }).selectOption("directed");
    await page.getByRole("textbox", { name: "Research focus" }).fill(zeroCommitFocus);
    await page.getByRole("button", { name: "Save future-run settings" }).click();
    await expect(page.locator("form").getByRole("status")).toContainText("Settings saved");

    const savedResponse = await page.request.get("/api/settings");
    expect(savedResponse.ok()).toBeTruthy();
    const saved = await savedResponse.json();

    await page.getByRole("link", { name: "Opportunities" }).click();
    await expect(page.getByRole("heading", { name: "Opportunities" })).toBeVisible();
    await page.getByRole("link", { name: "Settings" }).click();
    await expect(page.getByRole("textbox", { name: "Research focus" })).toHaveValue(
      zeroCommitFocus,
    );
    await expect(
      page.getByText(`Future-run configuration · Revision ${saved.revision}`),
    ).toBeVisible();

    await page.getByRole("link", { name: "Current run" }).click();
    await page.getByRole("button", { name: "Start research" }).click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+$/);
    const runId = new URL(page.url()).pathname.split("/").at(-1);
    expect(runId).toBeTruthy();

    await expect(page.getByRole("heading", { name: "Research run" })).toBeVisible();
    await expect(page.getByRole("status")).toContainText(
      "completed with warnings · terminal cleanup",
    );
    await expect(page.getByText("no gate survivor", { exact: true })).toBeVisible();
    await expect(page.getByText(/No preliminary candidate passed/)).toBeVisible();

    const terminalSnapshot = await page.request.get(`/api/runs/${runId}`);
    expect(terminalSnapshot.ok()).toBeTruthy();
    const terminal = await terminalSnapshot.json();
    expect(terminal.committed_count).toBe(0);
    expect(terminal.effective_config.settings_revision).toBe(saved.revision);
    expect(terminal.effective_config.saved_settings.research.focus).toBe(zeroCommitFocus);

    await page.waitForTimeout(3_500);
    expect(failedResponses).toEqual([]);
    expect(consoleErrors.filter((message) => message.includes("Failed to load resource"))).toEqual(
      [],
    );
  } finally {
    await resetSettings(page);
  }
});
