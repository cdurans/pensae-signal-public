import { render, screen } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CurrentRunView } from "./CurrentRunView";

describe("CurrentRunView", () => {
  it("shows bounded counters, per-call accounting, warnings, and cooperative Stop", async () => {
    const stop = vi.fn();
    render(
      <CurrentRunView
        run={{
          id: "00000000-0000-0000-0000-000000000001",
          state: "running",
          opportunity_id: null,
          effective_config: {},
          workflow_version: "phase2.fixed-graph.v1",
          schema_version: "phase2.run.v1",
          created_at: "2026-07-22T12:00:00Z",
          current_stage: "focused_retrieval",
          target_count: 5,
          admitted_count: 2,
          evaluated_count: 1,
          achieved_count: 1,
          committed_count: 1,
          non_counting_outcomes: {
            automatic_exact_rediscovery: 0,
            updated_version: 0,
            unresolved_possible_rediscovery: 0,
            invalid_candidate: 1,
            incomplete_candidate: 0,
          },
          capacity: {
            available: {
              queries: 5,
              pages: 10,
              bytes: 1_024,
              model_calls: 12,
              repairs: 2,
              total_tokens: 100_000,
            },
            reserved: {
              queries: 12,
              pages: 24,
              bytes: 2_048,
              model_calls: 20,
              repairs: 4,
              total_tokens: 200_000,
            },
            consumed: {
              queries: 11,
              pages: 25,
              bytes: 4_096,
              model_calls: 1,
              repairs: 0,
              total_tokens: 162,
            },
            remaining: {
              queries: 29,
              pages: 71,
              bytes: 8_192,
              model_calls: 159,
              repairs: 8,
              total_tokens: 899_838,
            },
          },
          work_counters: { queries: 11, retrieved_pages: 25 },
          warning_codes: [
            "search_engine_warning",
            "source_unavailable",
            "final_candidates_unavailable",
          ],
          model_usage: [
            {
              model: "Qwen3.6-35B-A3B-UD-IQ4_XS",
              role: "problem_analyst",
              call_index: 1,
              attempts: 1,
              repair: false,
              input_tokens: 120,
              output_tokens: 42,
            },
          ],
        }}
        onStop={stop}
        stopError="Stop request failed. Refresh the authoritative snapshot and retry."
        recentProgress={["stage completed · focused retrieval"]}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("running · focused retrieval");
    expect(
      screen.getByText((_, element) =>
        Boolean(element?.tagName === "P" && element.textContent?.includes("1 of 5 target")),
      ),
    ).toBeVisible();
    expect(screen.getByText("invalid candidate")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Protected capacity" })).toBeVisible();
    expect(screen.getByText("162 total tokens reported")).toBeVisible();
    expect(screen.getByText("Qwen3.6-35B-A3B-UD-IQ4 XS")).toBeVisible();
    expect(screen.getByText("source unavailable")).toBeVisible();
    expect(screen.getByText("search engine warning")).toBeVisible();
    expect(screen.getByText(/zero usable results remain possible/i)).toBeVisible();
    expect(
      screen.getByText(/could not be retrieved within the safety and availability rules/i),
    ).toBeVisible();
    expect(screen.getByText(/continued with the remaining evidence/i)).toBeVisible();
    expect(screen.getByText(/other valid candidates could continue/i)).toBeVisible();
    expect(screen.getByText("stage completed · focused retrieval")).toBeVisible();
    expect(screen.getByText(/immutable endpoint snapshot captured when it started/i)).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Stop request failed. Refresh the authoritative snapshot and retry.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Stop research" }));
    expect(stop).toHaveBeenCalledOnce();
  });
});
