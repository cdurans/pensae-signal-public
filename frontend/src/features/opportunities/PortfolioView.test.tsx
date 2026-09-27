import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { PortfolioItem } from "../../api/generated";
import { PortfolioView } from "./PortfolioView";

const item: PortfolioItem = {
  id: "opportunity-1",
  version_id: "version-1",
  opportunity_name: "Maintenance Request Triage Assistant",
  concise_summary: "A focused intake and follow-up layer.",
  primary_industry: "Property management",
  verdict: "needs_more_evidence",
  weighted_score: "3.90",
  evidence_score: 4,
  version_created_at: "2026-07-22T12:00:00Z",
  favorite: true,
  note: "Review with an operations buyer.",
  revision: 1,
  lifecycle_status: "active",
  classification: "possible_rediscovery",
};

describe("PortfolioView", () => {
  it("renders retained values and exposes only the supported filter and sort controls", () => {
    render(
      <PortfolioView
        items={[item]}
        industry=""
        sort="default"
        onIndustryChange={vi.fn()}
        onSortChange={vi.fn()}
        onOpenOpportunity={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { level: 1, name: "Opportunities" })).toHaveFocus();
    expect(screen.getByRole("searchbox", { name: "Industry" })).toBeVisible();
    expect(screen.getByRole("searchbox", { name: "Industry" })).toHaveAttribute("maxlength", "240");
    expect(screen.getByText(/up to 240 characters/i)).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Sort order" })).toHaveLength(4);
    expect(screen.getByText("Verdict: needs more evidence")).toBeVisible();
    expect(screen.getByText("Favorite").nextElementSibling).toHaveTextContent("Yes");
    expect(screen.queryByLabelText(/tag|folder|score editor/i)).not.toBeInTheDocument();
  });

  it("sends route-owned filter, sort, and selection actions", async () => {
    const user = userEvent.setup();
    const onIndustryChange = vi.fn();
    const onSortChange = vi.fn();
    const onOpenOpportunity = vi.fn();
    render(
      <PortfolioView
        items={[item]}
        industry=""
        sort="default"
        onIndustryChange={onIndustryChange}
        onSortChange={onSortChange}
        onOpenOpportunity={onOpenOpportunity}
      />,
    );

    fireEvent.change(screen.getByRole("searchbox", { name: "Industry" }), {
      target: { value: "Retail" },
    });
    expect(onIndustryChange).toHaveBeenCalledWith("Retail");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Sort order" }),
      "weighted_score_desc",
    );
    expect(onSortChange).toHaveBeenCalledWith("weighted_score_desc");
    await user.click(screen.getByRole("button", { name: `Open ${item.opportunity_name}` }));
    expect(onOpenOpportunity).toHaveBeenCalledWith(item.id);
  });

  it("explains the filtered empty state and can clear the industry", async () => {
    const user = userEvent.setup();
    const onIndustryChange = vi.fn();
    render(
      <PortfolioView
        items={[]}
        industry="Manufacturing"
        sort="newest"
        onIndustryChange={onIndustryChange}
        onSortChange={vi.fn()}
        onOpenOpportunity={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "No retained opportunities found" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("0 opportunities in Manufacturing");
    await user.click(screen.getByRole("button", { name: "Clear industry filter" }));
    expect(onIndustryChange).toHaveBeenCalledWith("");
  });
});
