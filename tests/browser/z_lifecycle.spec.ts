import { expect, test } from "@playwright/test";

const opportunityId = "00000000-0000-0000-0000-000000000020";

test("lifecycle review, history, merge reversal, and permanent deletion stay explicit", async ({
  page,
}) => {
  await page.goto(`/opportunities/${opportunityId}`);
  const lifecycle = page.getByRole("region", { name: "Lifecycle actions" });

  await page.getByRole("link", { name: "Version 1" }).click();
  await expect(page).toHaveURL(/version=00000000-0000-0000-0000-000000000022/);
  await expect(page.getByText("Historical immutable version")).toBeVisible();
  await expect(page.getByText(/Return to the current version before taking/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Merge into selected survivor" })).toHaveCount(0);
  await page.getByRole("button", { name: "Return to current version" }).click();

  const mergeEndpoint = `**/api/opportunities/${opportunityId}/merge`;
  await page.route(mergeEndpoint, async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        detail: "opportunity revision changed; refresh before retrying",
        code: "stale_revision",
      }),
    });
  });
  await page.getByRole("button", { name: "Merge into selected survivor" }).click();
  await page
    .getByRole("alertdialog", { name: "Confirm lifecycle action" })
    .getByRole("button", { name: "Confirm action" })
    .click();
  await expect(lifecycle).toContainText(
    "record may be stale or unavailable; refresh and review the current state",
  );
  await page.unroute(mergeEndpoint);

  const updated = page.getByRole("button", { name: "Create immutable Updated version" });
  await updated.focus();
  await updated.press("Enter");
  const updateDialog = page.getByRole("alertdialog", { name: "Confirm lifecycle action" });
  await expect(updateDialog.getByRole("combobox", { name: "Selected stable opportunity" })).toBeFocused();
  await updateDialog.press("Escape");
  await expect(updated).toBeFocused();
  await updated.click();
  await updateDialog.getByRole("button", { name: "Confirm action" }).click();
  await expect(lifecycle.getByRole("status")).toContainText("Updated immutable version created");
  await expect(lifecycle).toContainText("updated");

  await page.getByRole("button", { name: "Merge into selected survivor" }).click();
  const mergeDialog = page.getByRole("alertdialog", { name: "Confirm lifecycle action" });
  await expect(mergeDialog).toContainText("remain preserved without automatic combination");
  const targetSearch = mergeDialog.getByRole("textbox", {
    name: "Search all active opportunities",
  });
  await expect(targetSearch).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(mergeDialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(targetSearch).toBeFocused();
  await targetSearch.fill("Facilities");
  await expect(
    mergeDialog.getByRole("combobox", { name: "Selected stable opportunity" }),
  ).toContainText("Facilities Intake Queue");
  await mergeDialog.getByRole("button", { name: "Confirm action" }).click();
  await expect(lifecycle.getByRole("status")).toContainText(
    "Merge recorded without combining content",
  );
  await expect(page.getByText(/Merged into 00000000-0000-0000-0000-000000000050/)).toBeVisible();

  await page.getByRole("button", { name: "Reverse merge" }).click();
  await page
    .getByRole("alertdialog", { name: "Confirm lifecycle action" })
    .getByRole("button", { name: "Confirm action" })
    .click();
  await expect(lifecycle.getByRole("status")).toContainText("independent again");
  await expect(page.getByText("Independent active record")).toBeVisible();

  await page.getByRole("button", { name: "Permanently delete opportunity" }).click();
  const deleteDialog = page.getByRole("alertdialog", {
    name: "Permanently delete this opportunity?",
  });
  const confirmDelete = deleteDialog.getByRole("button", { name: "Delete permanently" });
  await expect(confirmDelete).toBeDisabled();
  const deleteInput = deleteDialog.getByRole("textbox", { name: /Type DELETE/ });
  await expect(deleteInput).toBeFocused();
  await deleteInput.pressSequentially(`DELETE ${opportunityId}`);
  await expect(confirmDelete).toBeEnabled();
  await confirmDelete.click();

  await expect(page).toHaveURL(/\/opportunities$/);
  await expect(page.getByText("Maintenance Request Triage Assistant")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Facilities Intake Queue" })).toBeVisible();
  await page.goto(`/opportunities/${opportunityId}`);
  await expect(page.getByRole("heading", { name: "Opportunity unavailable" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("could not be loaded");
});
