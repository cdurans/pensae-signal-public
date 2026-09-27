// Supported portfolio, settings, and degraded-health flows.
import { expect, test, type Locator } from "@playwright/test";

async function expectDefinitionRowsNotToOverlap(list: Locator) {
  const overlapCount = await list.locator(":scope > div").evaluateAll((rows) =>
    rows.filter((row) => {
      const term = row.querySelector("dt")?.getBoundingClientRect();
      const description = row.querySelector("dd")?.getBoundingClientRect();
      if (term === undefined || description === undefined) {
        return true;
      }
      return (
        term.left < description.right &&
        term.right > description.left &&
        term.top < description.bottom &&
        term.bottom > description.top
      );
    }).length,
  );
  expect(overlapCount).toBe(0);
}

test("portfolio URL state restores and stable metadata survives detail navigation", async ({
  page,
}) => {
  await page.goto("/opportunities?industry=Property%20management&sort=newest");

  const industry = page.getByRole("searchbox", { name: "Industry" });
  const sort = page.getByRole("combobox", { name: "Sort order" });
  await expect(industry).toHaveValue("Property management");
  await expect(sort).toHaveValue("newest");
  await expect(page.getByRole("status")).toContainText("1 opportunity in Property management");

  await page.reload();
  await expect(industry).toHaveValue("Property management");
  await expect(sort).toHaveValue("newest");

  await page
    .getByRole("button", { name: "Open Maintenance Request Triage Assistant" })
    .click();
  await expect(page).toHaveURL(/\/opportunities\/00000000-0000-0000-0000-000000000020$/);
  await expect(
    page.getByText("Committed opportunity · Property management · Version 2"),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Origin problem research" })).toBeVisible();
  await expect(page.getByText("Triage incoming maintenance requests")).toBeVisible();
  await expect(page.getByText("Facilities Intake Queue")).toBeVisible();
  await expect(page.getByText(/Version 2 · Current/)).toBeVisible();

  await page.getByRole("button", { name: "Add to favorites" }).click();
  const metadataStatus = page
    .getByRole("region", { name: "Operator metadata" })
    .getByRole("status");
  await expect(metadataStatus).toContainText("Favorite preference saved");
  await expect(page.getByText("Current value: Favorite")).toBeVisible();

  const note = page.getByRole("textbox", { name: "Operator note" });
  await note.fill("Review with the operations lead.");
  await page.getByRole("button", { name: "Save operator note" }).click();
  await expect(metadataStatus).toContainText("Operator note saved");

  await page.goto("/opportunities?sort=weighted_score_desc");
  await expect(page.getByText("Operator note: Review with the operations lead.")).toBeVisible();
  const retainedCard = page
    .locator("article")
    .filter({ hasText: "Maintenance Request Triage Assistant" });
  await expect(retainedCard.getByText("Favorite").locator("xpath=following-sibling::*[1]")).toHaveText(
    "Yes",
  );
});

test("long score explanations and provenance values do not overlap their labels", async ({
  page,
}) => {
  const opportunityId = "00000000-0000-0000-0000-000000000020";
  await page.route(`**/api/opportunities/${opportunityId}`, async (route) => {
    const upstream = await route.fetch();
    const body = await upstream.json();
    body.report.proposed_scores.commercial_explanation =
      "The B2C market for career tools is growing, and the pain point of information overload is significant. However, monetization may be challenging due to free alternatives.";
    body.provenance.prompt_versions = {
      planner: "research-planner.v1",
      problem_analyst: "problem-analyst.v1",
      product_strategist: "product-strategist.v1",
      opportunity_analyst: "opportunity-analyst.v1",
    };
    body.provenance.source_fingerprints = {
      "00000000-0000-0000-0000-000000000041":
        "8945ff700d42b511cbc4502d9b93c7abdbf21c455e9be758f385ad424d539865",
      "00000000-0000-0000-0000-000000000042":
        "d0996bebf05bd0fb60511f3ed6cff8878676bee0f9928d8c43f9af265f5313ce",
    };
    await route.fulfill({ response: upstream, json: body });
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`/opportunities/${opportunityId}`);

  const scoreExplanations = page
    .getByRole("heading", { name: "Score explanations" })
    .locator("xpath=following-sibling::dl[1]");
  await expectDefinitionRowsNotToOverlap(scoreExplanations);

  const provenance = page
    .locator("details")
    .filter({ hasText: "Reproducibility provenance" });
  await provenance.getByText("Reproducibility provenance").click();
  await expectDefinitionRowsNotToOverlap(provenance.locator("dl"));

  await page.setViewportSize({ width: 390, height: 900 });
  await expectDefinitionRowsNotToOverlap(scoreExplanations);
  await expectDefinitionRowsNotToOverlap(provenance.locator("dl"));
});

test("settings validate, save for future runs, and reset with reversible focus flow", async ({
  page,
}) => {
  await page.goto("/settings");
  expect(
    await page
      .getByRole("heading", { level: 1, name: "Settings" })
      .evaluate((element) => element === document.activeElement),
  ).toBe(true);
  await expect(page.getByText(/apply only to future research runs/i)).toBeVisible();

  await page.getByRole("combobox", { name: "Discovery mode" }).selectOption("directed");
  await page.getByRole("button", { name: "Save future-run settings" }).click();
  expect(
    await page
      .getByRole("textbox", { name: "Research focus" })
      .evaluate((element) => element === document.activeElement),
  ).toBe(true);
  await expect(page.getByText("Directed discovery requires a research focus.")).toBeVisible();

  await page.getByRole("textbox", { name: "Research focus" }).fill("Construction estimating");
  await page.getByRole("button", { name: "Save future-run settings" }).click();
  const settingsStatus = page.locator("form").getByRole("status");
  await expect(settingsStatus).toContainText(
    "Settings saved. They apply only to future research runs.",
  );

  const reset = page.getByRole("button", { name: "Reset to protected defaults" });
  await reset.focus();
  await reset.press("Enter");
  const dialog = page.getByRole("alertdialog", { name: "Reset future-run settings?" });
  await expect(dialog).toBeVisible();
  expect(
    await dialog
      .getByRole("button", { name: "Confirm reset to protected defaults" })
      .evaluate((element) => element === document.activeElement),
  ).toBe(true);
  await dialog.getByRole("button", { name: "Keep current settings" }).click();
  expect(await reset.evaluate((element) => element === document.activeElement)).toBe(true);

  await reset.click();
  await dialog.getByRole("button", { name: "Confirm reset to protected defaults" }).click();
  await expect(settingsStatus).toContainText("Protected shipped defaults restored");
  await expect(page.getByRole("combobox", { name: "Discovery mode" })).toHaveValue("broad");
  await expect(page.getByRole("textbox", { name: "Research focus" })).toHaveValue("");
});

test("degraded dependency health keeps the UI usable and explains blocked start", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.route("**/api/runs/preflight", async (route) => {
    const upstream = await route.fetch();
    const body = await upstream.json();
    body.ready = false;
    body.checks = body.checks.map((check: { dependency: string; state: string; summary: string }) =>
      check.dependency === "chat"
        ? { ...check, state: "unavailable", summary: "Chat endpoint is unavailable." }
        : check,
    );
    body.blockers = [
      {
        dependency: "chat",
        reason: "The configured chat endpoint did not respond.",
        action: "Start the launcher-owned chat server, then retry preflight.",
      },
    ];
    await route.fulfill({ response: upstream, json: body });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Pensae Signal readiness" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start research" })).toBeDisabled();
  await expect(page.getByRole("heading", { name: "Blocking reasons" })).toBeVisible();
  await expect(page.getByText("The configured chat endpoint did not respond.")).toBeVisible();
  await expect(page.getByText("Start the launcher-owned chat server, then retry preflight.")).toBeVisible();

  await page.keyboard.press("Tab");
  const focusedOutline = await page.evaluate(() => getComputedStyle(document.activeElement!).outlineStyle);
  expect(focusedOutline).not.toBe("none");
  const reducedDuration = await page.evaluate(
    () => getComputedStyle(document.querySelector("button")!).transitionDuration,
  );
  expect(["0.01ms", "1e-05s"]).toContain(reducedDuration);
});
