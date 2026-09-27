import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { LifecycleTarget, OpportunityDetail } from "../../api/generated";
import { OpportunityLifecyclePanel } from "./OpportunityLifecyclePanel";

const OPPORTUNITY_ID = "00000000-0000-0000-0000-000000000001";
const TARGET_ID = "00000000-0000-0000-0000-000000000002";

const opportunity = {
  id: OPPORTUNITY_ID,
  revision: 4,
  lifecycle_status: "active",
  classification: "possible_rediscovery",
  related: [
    {
      opportunity_id: TARGET_ID,
      opportunity_name: "Existing stable opportunity",
      primary_industry: "Facilities management",
      similarity: "0.94",
      relation_kind: "possible_rediscovery",
      revision: 3,
      lifecycle_status: "active",
    },
  ],
} as OpportunityDetail;

const target = {
  id: TARGET_ID,
  opportunity_name: "Existing stable opportunity",
  primary_industry: "Facilities management",
  revision: 3,
  lifecycle_status: "active",
} as LifecycleTarget;

function renderPanel(overrides: Partial<OpportunityDetail> = {}) {
  const handlers = {
    onRediscovery: vi.fn(),
    onMerge: vi.fn(),
    onReverse: vi.fn(),
    onDelete: vi.fn(),
    onMergeSearch: vi.fn(),
  };
  render(
    <OpportunityLifecyclePanel
      opportunity={{ ...opportunity, ...overrides }}
      mergeTargets={[target]}
      mergeSearch=""
      historical={false}
      {...handlers}
    />,
  );
  return handlers;
}

describe("OpportunityLifecyclePanel", () => {
  it("confirms semantic decisions and returns focus after keyboard cancellation", async () => {
    const user = userEvent.setup();
    const handlers = renderPanel();
    const rediscovered = screen.getByRole("button", { name: "Confirm Rediscovered" });
    await user.click(rediscovered);
    expect(screen.getByRole("alertdialog")).toBeVisible();
    expect(screen.getByLabelText("Selected stable opportunity")).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(rediscovered).toHaveFocus();

    await user.click(rediscovered);
    await user.click(screen.getByRole("button", { name: "Confirm action" }));
    expect(handlers.onRediscovery).toHaveBeenCalledWith("rediscovered", target);

    await user.click(screen.getByRole("button", { name: /create immutable updated/i }));
    await user.click(screen.getByRole("button", { name: "Confirm action" }));
    expect(handlers.onRediscovery).toHaveBeenCalledWith("updated", target);
  });

  it("requires exact typed deletion and explains non-destructive merge", async () => {
    const user = userEvent.setup();
    const handlers = renderPanel();
    await user.click(screen.getByRole("button", { name: /merge into selected survivor/i }));
    expect(screen.getByText(/remain preserved without automatic combination/i)).toBeVisible();
    const mergeSearch = screen.getByLabelText(/search all active opportunities/i);
    expect(mergeSearch).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Confirm action" }));
    expect(handlers.onMerge).toHaveBeenCalledWith(target);

    await user.click(screen.getByRole("button", { name: /permanently delete/i }));
    const confirm = screen.getByRole("button", { name: "Delete permanently" });
    expect(confirm).toBeDisabled();
    const deleteInput = screen.getByLabelText(/Type DELETE/i);
    expect(deleteInput).toHaveFocus();
    await user.type(deleteInput, `DELETE ${OPPORTUNITY_ID}`);
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(handlers.onDelete).toHaveBeenCalledWith(`DELETE ${OPPORTUNITY_ID}`);
  });

  it("offers reversal for merged records and blocks actions on historical versions", async () => {
    const user = userEvent.setup();
    const handlers = renderPanel({ lifecycle_status: "merged", merge_target_id: TARGET_ID });
    await user.click(screen.getByRole("button", { name: "Reverse merge" }));
    await user.click(screen.getByRole("button", { name: "Confirm action" }));
    expect(handlers.onReverse).toHaveBeenCalledOnce();

    const { unmount } = render(
      <OpportunityLifecyclePanel
        opportunity={opportunity}
        mergeTargets={[target]}
        mergeSearch=""
        historical
        onMergeSearch={vi.fn()}
        onRediscovery={vi.fn()}
        onMerge={vi.fn()}
        onReverse={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByText(/inspecting an immutable historical version/i)).toBeVisible();
    unmount();
  });

  it("renders unavailable and pending states without relying on color", () => {
    render(
      <OpportunityLifecyclePanel
        opportunity={{ ...opportunity, related: [] }}
        mergeTargets={[]}
        mergeSearch=""
        historical={false}
        pending
        message="Lifecycle targets are unavailable."
        onMergeSearch={vi.fn()}
        onRediscovery={vi.fn()}
        onMerge={vi.fn()}
        onReverse={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByText(/no related or possible rediscovery target/i)).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Lifecycle targets are unavailable");
    expect(screen.getByRole("button", { name: /merge into/i })).toBeDisabled();
  });
});
