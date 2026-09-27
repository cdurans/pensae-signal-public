import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { OpportunityDetail } from "../../api/generated";
import { OpportunityDetailView } from "./OpportunityDetailView";

const detail: OpportunityDetail = {
  id: "00000000-0000-0000-0000-000000000001",
  version_id: "00000000-0000-0000-0000-000000000002",
  version_number: 1,
  revision: 1,
  lifecycle_status: "active",
  classification: "possible_rediscovery",
  primary_industry: "Property management",
  report: {
    opportunity_name: "Maintenance Request Triage Assistant",
    concise_summary: "A focused intake and follow-up layer.",
    primary_industry: "Property management",
    problem_pattern: "Fragmented channels delay maintenance triage.",
    target_segment: "Small property managers",
    affected_user: "Property managers and maintenance coordinators",
    likely_buyer: "Head of property operations",
    recurring_workflow: "Receive, triage, assign, and follow up on requests",
    current_workaround: "Email, text messages, and shared spreadsheets",
    business_consequences: ["Delayed repairs", "Inconsistent tenant updates"],
    proposed_solution: "An auditable intake and follow-up queue.",
    delivery_model: "Locally operated web application",
    initial_market_reason: "The recurring coordination burden is well bounded.",
    frequency_value_hypothesis: "Daily delays consume staff time and tenant trust.",
    other_segments: ["Small facilities-management teams"],
    alternatives: ["Property-management suites", "Shared inbox and spreadsheet"],
    missing_capabilities: ["Direct work-order integration"],
    reusable_core_capabilities: ["Channel intake", "Triage queue", "Audit trail"],
    pricing_or_spend_signals: ["Teams already fund software and coordination time."],
    market_saturation: "Broad suites exist, but the focused workflow gap persists.",
    incumbent_response_risk: "Incumbents could add a similar focused workflow.",
    integration_customization_burden: "The first version avoids deep integrations.",
    preferred_technology_fit: "A local web app fits this auditable workflow.",
    trust_regulatory_constraints: ["Tenant messages may contain personal data."],
    operational_burden: "Source review and support must remain bounded.",
    commercial_analysis: "A buyer can connect faster triage to staff time.",
    feasibility_analysis: "The first product can remain narrow.",
    differentiation_analysis: "Evidence-linked output differs from generic inboxes.",
    risks: ["Incumbent suites could add similar automation."],
    unknowns: ["Willingness to pay needs direct validation."],
    reasons_not_to_pursue: [],
    supporting_evidence_ids: ["00000000-0000-0000-0000-000000000003"],
    negative_evidence_ids: [],
    conflicting_evidence_ids: [],
    next_research_questions: ["Which request volume creates budget urgency?"],
    claims: [
      {
        statement: "The workflow relies on manual copying.",
        label: "fact",
        evidence_ids: ["00000000-0000-0000-0000-000000000003"],
      },
      { statement: "A queue may reduce delay.", label: "inference", evidence_ids: [] },
      { statement: "Teams may pay for saved time.", label: "assumption", evidence_ids: [] },
      { statement: "Weekly savings are approximate.", label: "estimate", evidence_ids: [] },
      { statement: "The buyer may fund the workflow.", label: "assumption", evidence_ids: [] },
      { statement: "A narrow queue is viable.", label: "hypothesis", evidence_ids: [] },
      { statement: "Incumbents may cover it.", label: "conflict", evidence_ids: ["e1"] },
      { statement: "Willingness to pay is missing.", label: "missing_evidence", evidence_ids: [] },
    ],
    source_diversity_limitation: "The example has one retained source.",
    conflict_limitation: null,
    proposed_scores: {
      commercial_attractiveness: 4,
      commercial_explanation: "A plausible operations buyer exists.",
      evidence_strength: 4,
      evidence_explanation: "Independent evidence supports the workflow problem.",
      pensae_feasibility: 4,
      feasibility_explanation: "The initial workflow avoids deep integration.",
      differentiation: 3,
      differentiation_explanation: "Incumbent response remains a risk.",
    },
    strong_evidence: true,
    plausible_buyer: true,
    payment_or_value_path: true,
    critical_blocker: false,
    strong_negative_evidence: false,
    implausible_economics: false,
    excessive_customization_or_operations: false,
  },
  scores: {
    commercial_attractiveness: "4",
    evidence_strength: "4",
    pensae_feasibility: "4",
    differentiation: "3",
    weighted: "3.90",
  },
  verdict: "promising",
  evidence: [
    {
      id: "00000000-0000-0000-0000-000000000003",
      excerpt: "Staff copy maintenance status into a shared spreadsheet.",
      supported_claim: "The workflow relies on manual copying.",
      evidence_kind: "supporting",
      source_url: "https://example.test/source",
      source_title: "Synthetic maintenance workflow source",
      publisher: "Offline fixture",
      retrieved_at: "2026-07-22T12:00:00Z",
    },
  ],
  provenance: {
    workflow_version: "phase1.vertical-slice.v1",
    chat_model_id: "fake-chat",
  },
  favorite: false,
  note: "Check buyer urgency.",
  origin_pattern: {
    id: "pattern-1",
    summary: "Maintenance coordination is fragmented across channels.",
  },
  origin_signals: [
    {
      id: "signal-1",
      affected_user: "Maintenance coordinators",
      recurring_workflow: "Triage incoming repair requests",
      current_workaround: "Copy details between email and spreadsheets",
      business_consequence: "Repairs and updates are delayed",
      confidence: "high",
    },
  ],
  related: [
    {
      opportunity_id: "related-1",
      opportunity_name: "Facilities Intake Queue",
      primary_industry: "Facilities management",
      relation_kind: "possible_rediscovery",
      similarity: "0.93",
      revision: 2,
      lifecycle_status: "active",
    },
  ],
  versions: [
    {
      id: "00000000-0000-0000-0000-000000000002",
      version_number: 1,
      is_current: true,
      verdict: "promising",
      weighted_score: "3.90",
      evidence_score: 4,
      created_at: "2026-07-22T12:00:00Z",
    },
  ],
};

function renderDetail(view: ReactNode) {
  return render(<MemoryRouter>{view}</MemoryRouter>);
}

describe("OpportunityDetailView", () => {
  it("renders source-linked evidence, immutable scores, verdict, and provenance", () => {
    renderDetail(<OpportunityDetailView opportunity={detail} />);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Maintenance Request Triage Assistant",
    );
    expect(screen.getByText("Verdict: promising")).toBeVisible();
    expect(
      screen.getByText("Committed opportunity · Property management · Version 1"),
    ).toBeVisible();
    expect(
      screen.getByText(
        (_content, element) =>
          element?.tagName === "BLOCKQUOTE" &&
          element.textContent?.includes(
            "Staff copy maintenance status into a shared spreadsheet.",
          ) === true,
      ),
    ).toBeVisible();
    expect(screen.getByText("Scores and verdict are read-only policy results.")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Verdict policy factors" })).toBeVisible();
    expect(screen.getByText("Strong evidence").nextElementSibling).toHaveTextContent("Yes");
    expect(screen.getByText("Critical blocker").nextElementSibling).toHaveTextContent("No");
    expect(screen.getByText("Daily delays consume staff time and tenant trust.")).toBeVisible();
    expect(screen.getByText("A plausible operations buyer exists.")).toBeVisible();
    expect(screen.getByText("The first version avoids deep integrations.")).toBeVisible();
    const source = screen.getByRole("link", { name: /Synthetic maintenance workflow source/ });
    expect(source).toHaveAttribute("href", "https://example.test/source");
    const provenance = screen.getByText("Reproducibility provenance").closest("details");
    if (provenance === null) {
      throw new Error("Provenance details were not rendered");
    }
    expect(within(provenance).getByText("phase1.vertical-slice.v1")).toBeInTheDocument();
    expect(
      screen.getByText("Maintenance coordination is fragmented across channels."),
    ).toBeVisible();
    expect(screen.getByText(/Possible rediscovery · Similarity 0.93/)).toBeVisible();
    expect(screen.getByText(/Version 1 · Current/)).toBeVisible();
  });

  it("exposes typed favorite and note actions and announces mutation results", async () => {
    const user = userEvent.setup();
    const onFavoriteChange = vi.fn();
    const onNoteSave = vi.fn();
    renderDetail(
      <OpportunityDetailView
        opportunity={detail}
        onFavoriteChange={onFavoriteChange}
        onNoteSave={onNoteSave}
        mutationMessage="Operator note saved."
      />,
    );

    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Add to favorites" }));
    expect(onFavoriteChange).toHaveBeenCalledWith(true);

    const note = screen.getByRole("textbox", { name: "Operator note" });
    await user.clear(note);
    await user.type(note, "  Follow up with a buyer.  ");
    await user.click(screen.getByRole("button", { name: "Save operator note" }));
    expect(onNoteSave).toHaveBeenCalledWith("Follow up with a buyer.");
    expect(screen.getAllByRole("status").at(-1)).toHaveTextContent("Operator note saved.");
  });

  it("disables metadata controls and communicates a pending save", () => {
    renderDetail(
      <OpportunityDetailView
        opportunity={detail}
        onFavoriteChange={vi.fn()}
        onNoteSave={vi.fn()}
        metadataPending
      />,
    );

    expect(screen.getByRole("button", { name: "Add to favorites" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save operator note" })).toBeDisabled();
    expect(screen.getByText("Saving operator metadata…")).toBeVisible();
  });
});
