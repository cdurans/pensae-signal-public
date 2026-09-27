import { expect, test, type Page } from "@playwright/test";

const fiveOpportunityFocus = "Pensae Signal browser five-opportunity acceptance";
const shortfallFocus = "Pensae Signal browser honest-shortfall acceptance";
const opportunityIds = [
  "00000000-0000-0000-0000-000000000020",
  "00000000-0000-0000-0000-000000000023",
  "00000000-0000-0000-0000-000000000024",
  "00000000-0000-0000-0000-000000000025",
  "00000000-0000-0000-0000-000000000026",
];
const opportunityNames = [
  "Maintenance Request Triage Assistant",
  "Vendor Dispatch Evidence Desk",
  "Tenant Update Coordination Queue",
  "Inspection Follow-up Workbench",
  "Portfolio Repair Pattern Monitor",
];

async function bootstrap(page: Page) {
  const response = await page.request.get("/api/bootstrap");
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function setFocus(page: Page, focus: string) {
  const runtime = await bootstrap(page);
  const currentResponse = await page.request.get("/api/settings");
  expect(currentResponse.ok()).toBeTruthy();
  const current = await currentResponse.json();
  const saved = await page.request.put("/api/settings", {
    data: {
      ...current.values,
      research: {
        ...current.values.research,
        discovery_mode: "directed",
        focus,
      },
    },
    headers: {
      "Content-Type": "application/json",
      Origin: runtime.canonical_origin,
      "X-Pensae-Session": runtime.session_nonce,
    },
  });
  expect(saved.ok()).toBeTruthy();
}

async function resetSettings(page: Page) {
  const runtime = await bootstrap(page);
  const response = await page.request.post("/api/settings/reset", {
    data: { confirmation: "RESET TO PROTECTED DEFAULTS" },
    headers: {
      "Content-Type": "application/json",
      Origin: runtime.canonical_origin,
      "X-Pensae-Session": runtime.session_nonce,
    },
  });
  expect(response.ok()).toBeTruthy();
}

test("P7 commits five sequential opportunities with five unique inspectable IDs", async ({
  page,
}) => {
  try {
    await resetSettings(page);
    await setFocus(page, fiveOpportunityFocus);
    await page.goto("/");
    await expect(page).toHaveTitle("Pensae Signal | Local readiness");
    await expect(page.getByRole("link", { name: "Pensae Signal" })).toBeVisible();
    await page.getByRole("button", { name: "Start research" }).click();
    await expect(page.getByRole("heading", { name: "Research run" })).toBeVisible();
    await expect(page.getByRole("status").first()).toContainText("completed · terminal cleanup");

    const runId = new URL(page.url()).pathname.split("/").at(-1);
    const response = await page.request.get(`/api/runs/${runId}`);
    expect(response.ok()).toBeTruthy();
    const snapshot = await response.json();
    expect(snapshot.target_count).toBe(5);
    expect(snapshot.achieved_count).toBe(5);
    expect(snapshot.committed_count).toBe(5);
    expect(snapshot.committed_opportunity_ids).toEqual(opportunityIds);
    expect(new Set(snapshot.committed_opportunity_ids).size).toBe(5);
    expect(snapshot.shortfall_code).toBeNull();
    expect(snapshot.capacity.reserved.total_tokens).toBe(0);
    await expect(page.getByText(/target opportunities achieved/)).toContainText("5 of 5");

    for (let index = 0; index < opportunityIds.length; index += 1) {
      const button = page.getByRole("button", {
        name: `Open opportunity ${index + 1} of 5`,
      });
      await expect(button).toBeVisible();
      await button.click();
      await expect(page).toHaveURL(`/opportunities/${opportunityIds[index]}`);
      await expect(
        page.getByRole("heading", { name: opportunityNames[index] }),
      ).toBeVisible();
      await page.goBack();
      await expect(page.getByRole("heading", { name: "Research run" })).toBeVisible();
    }
  } finally {
    await resetSettings(page);
  }
});

test("P7 reports an honest shortfall and keeps all earlier commits inspectable", async ({ page }) => {
  try {
    await resetSettings(page);
    await setFocus(page, shortfallFocus);
    await page.goto("/");
    await page.getByRole("button", { name: "Start research" }).click();
    await expect(page.getByRole("heading", { name: "Research run" })).toBeVisible();
    await expect(page.getByRole("status").first()).toContainText(
      "completed with warnings · terminal cleanup",
    );

    const runId = new URL(page.url()).pathname.split("/").at(-1);
    const response = await page.request.get(`/api/runs/${runId}`);
    expect(response.ok()).toBeTruthy();
    const snapshot = await response.json();
    expect(snapshot.target_count).toBe(5);
    expect(snapshot.achieved_count).toBe(3);
    expect(snapshot.committed_count).toBe(3);
    expect(snapshot.committed_opportunity_ids).toEqual(opportunityIds.slice(0, 3));
    expect(snapshot.shortfall_code).toBe("insufficient_evidence");
    expect(snapshot.non_counting_outcomes.invalid_candidate).toBe(1);
    expect(snapshot.non_counting_outcomes.incomplete_candidate).toBe(1);
    await expect(page.getByText(/Honest shortfall:/)).toContainText("insufficient evidence");
    await expect(
      page.getByText(/Pensae Signal did not weaken an evidence gate or add filler/),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /Open opportunity/ })).toHaveCount(3);
    await page.getByRole("button", { name: "Open opportunity 3 of 3" }).click();
    await expect(page).toHaveURL(`/opportunities/${opportunityIds[2]}`);
    await expect(
      page.getByRole("heading", { name: opportunityNames[2] }),
    ).toBeVisible();
  } finally {
    await resetSettings(page);
  }
});
