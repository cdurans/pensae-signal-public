import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type {
  CapabilityPreflight,
  SavedSettingsResponse,
  SettingsFieldMetadata,
} from "../../api/generated";
import { SettingsView } from "./SettingsView";

function metadata(
  key: string,
  label: string,
  options: Partial<SettingsFieldMetadata> = {},
): SettingsFieldMetadata {
  return {
    key,
    label,
    validation: "Enter a valid saved value.",
    protected_default_explanation: "Protected shipped value used by Reset.",
    ...options,
  };
}

const response: SavedSettingsResponse = {
  revision: 3,
  values: {
    research: {
      focus: null,
      country: "United States",
      language: "English",
      discovery_mode: "broad",
      preferred_technologies: ["Python", "TypeScript"],
    },
    endpoints: {
      searxng_url: "http://127.0.0.1:8888",
      chat_url: "http://127.0.0.1:8085",
      embedding_url: "http://127.0.0.1:8086",
    },
    workflow: {
      discovery_queries: 8,
      run_queries: 40,
      first_pass_pages: 24,
      run_pages: 96,
      preliminary_survivors: 12,
      opportunities: 5,
      concepts: 8,
      patterns: 10,
      segments_per_pattern: 3,
      focused_queries_per_concept: 3,
      focused_pages_per_concept: 6,
      search_retries: 1,
      model_calls: 160,
      total_run_tokens: 1_500_000,
      planner_output_max_tokens: 2_048,
      problem_analyst_output_max_tokens: 4_096,
      product_strategist_output_max_tokens: 4_096,
      opportunity_analyst_output_max_tokens: 6_144,
    },
    logging: { rotation_size_mib: 10 },
  },
  fields: [
    metadata("research.focus", "Research focus"),
    metadata("research.discovery_mode", "Discovery mode", { choices: ["broad", "directed"] }),
    metadata("research.preferred_technologies", "Preferred technologies"),
    metadata("endpoints.chat_url", "Chat model endpoint"),
    metadata("workflow.discovery_queries", "Discovery queries", { minimum: 1, maximum: 20 }),
    metadata("workflow.run_queries", "Total run queries", { minimum: 1, maximum: 40 }),
    metadata("workflow.first_pass_pages", "First-pass retrieved pages", {
      minimum: 1,
      maximum: 48,
    }),
    metadata("workflow.run_pages", "Total run retrieved pages", { minimum: 1, maximum: 96 }),
    metadata("workflow.preliminary_survivors", "Preliminary survivors", {
      minimum: 1,
      maximum: 12,
    }),
    metadata("workflow.concepts", "Solution concepts", { minimum: 1, maximum: 8 }),
    metadata("workflow.patterns", "Problem patterns", { minimum: 1, maximum: 10 }),
    metadata("workflow.segments_per_pattern", "Segments per pattern", {
      minimum: 1,
      maximum: 3,
    }),
    metadata("workflow.focused_queries_per_concept", "Focused queries per concept", {
      minimum: 1,
      maximum: 3,
    }),
    metadata("workflow.focused_pages_per_concept", "Focused pages per concept", {
      minimum: 1,
      maximum: 6,
    }),
    metadata("workflow.search_retries", "Search retries", { minimum: 0, maximum: 1 }),
    metadata("workflow.model_calls", "Total model calls", { minimum: 1, maximum: 256 }),
    metadata("workflow.total_run_tokens", "Total run tokens", {
      minimum: 1,
      maximum: 1_500_000,
    }),
    metadata("workflow.planner_output_max_tokens", "Planner output tokens", {
      minimum: 1,
      maximum: 2_048,
    }),
    metadata("workflow.problem_analyst_output_max_tokens", "Problem Analyst output tokens", {
      minimum: 1,
      maximum: 4_096,
    }),
    metadata("workflow.product_strategist_output_max_tokens", "Product Strategist output tokens", {
      minimum: 1,
      maximum: 4_096,
    }),
    metadata(
      "workflow.opportunity_analyst_output_max_tokens",
      "Opportunity Analyst output tokens",
      { minimum: 1, maximum: 6_144 },
    ),
    metadata("logging.rotation_size_mib", "Log rotation size", { minimum: 1, maximum: 10 }),
  ],
};

const blockedHealth: CapabilityPreflight = {
  ready: false,
  checks: [
    {
      dependency: "postgresql",
      state: "ready",
      summary: "PostgreSQL and pgvector are ready.",
    },
    {
      dependency: "chat",
      state: "incompatible",
      summary: "The chat endpoint did not satisfy the structured-output contract.",
      action: "Restart the configured local chat endpoint.",
    },
  ],
  blockers: [
    {
      dependency: "chat",
      state: "incompatible",
      reason: "Structured output capability is unavailable.",
      action: "Check the pinned model and launcher status.",
    },
  ],
};

function renderSettings(options: Partial<React.ComponentProps<typeof SettingsView>> = {}) {
  const onSave = vi.fn();
  const onReset = vi.fn();
  render(
    <SettingsView
      response={response}
      health={blockedHealth}
      onSave={onSave}
      onReset={onReset}
      savePending={false}
      resetPending={false}
      {...options}
    />,
  );
  return { onSave, onReset };
}

describe("SettingsView", () => {
  it("renders readable settings metadata and non-color-only dependency reasons", () => {
    renderSettings();

    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toHaveFocus();
    expect(screen.getByText("Research start is blocked")).toBeVisible();
    expect(screen.getByText("State: incompatible")).toBeVisible();
    expect(screen.getByText(/Structured output capability is unavailable/)).toBeVisible();
    expect(screen.getAllByText(/Protected shipped value used by Reset/).length).toBeGreaterThan(0);
    expect(screen.getByText(/There is no temporary per-run override/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "Protected opportunity target" })).toBeVisible();
    expect(
      screen.queryByRole("spinbutton", { name: "Final opportunities" }),
    ).not.toBeInTheDocument();
  });

  it("keeps settings usable and provides an action when health is unavailable", async () => {
    const user = userEvent.setup();
    const onHealthRetry = vi.fn();
    renderSettings({ health: undefined, healthUnavailable: true, onHealthRetry });

    expect(screen.getByText("Health check unavailable")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Saved future-run settings remain usable");
    await user.click(screen.getByRole("button", { name: "Retry health check" }));
    expect(onHealthRetry).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Save future-run settings" })).toBeEnabled();
  });

  it("validates visible bounds, endpoints, and directed focus before save", async () => {
    const user = userEvent.setup();
    const { onSave } = renderSettings();

    await user.selectOptions(screen.getByRole("combobox", { name: "Discovery mode" }), "directed");
    const endpoint = screen.getByRole("textbox", { name: "Chat model endpoint" });
    await user.clear(endpoint);
    await user.type(endpoint, "https://example.test:8085");
    const queries = screen.getByRole("spinbutton", { name: "Discovery queries" });
    await user.clear(queries);
    await user.type(queries, "21");
    await user.click(screen.getByRole("button", { name: "Save future-run settings" }));

    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("Directed discovery requires a research focus.")).toBeVisible();
    expect(screen.getByText(/Use an HTTP IPv4 loopback URL/)).toBeVisible();
    expect(screen.getByText("Enter 20 or less.")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Research focus" })).toHaveFocus();
  });

  it("submits generated-client SavedSettings values for future runs", async () => {
    const user = userEvent.setup();
    const { onSave } = renderSettings();

    const focus = screen.getByRole("textbox", { name: "Research focus" });
    await user.type(focus, "Property management maintenance");
    const technologies = screen.getByRole("textbox", { name: "Preferred technologies" });
    await user.clear(technologies);
    await user.type(technologies, "Python, C++");
    const queries = screen.getByRole("spinbutton", { name: "Discovery queries" });
    await user.clear(queries);
    await user.type(queries, "6");
    await user.click(screen.getByRole("button", { name: "Save future-run settings" }));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        research: expect.objectContaining({
          focus: "Property management maintenance",
          preferred_technologies: ["Python", "C++"],
        }),
        workflow: expect.objectContaining({ discovery_queries: 6 }),
      }),
    );
  });

  it("preserves dirty form values when the server refetches the same settings revision", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    const onReset = vi.fn();
    const { rerender } = render(
      <SettingsView
        response={response}
        health={blockedHealth}
        onSave={onSave}
        onReset={onReset}
        savePending={false}
        resetPending={false}
      />,
    );
    const focus = screen.getByRole("textbox", { name: "Research focus" });
    await user.type(focus, "Unsaved operator draft");

    rerender(
      <SettingsView
        response={{
          ...response,
          values: structuredClone(response.values),
          fields: [...response.fields],
        }}
        health={blockedHealth}
        onSave={onSave}
        onReset={onReset}
        savePending={false}
        resetPending={false}
      />,
    );

    expect(focus).toHaveValue("Unsaved operator draft");
  });

  it("replaces dirty form values when a newer authoritative settings revision arrives", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    const onReset = vi.fn();
    const { rerender } = render(
      <SettingsView
        response={response}
        health={blockedHealth}
        onSave={onSave}
        onReset={onReset}
        savePending={false}
        resetPending={false}
      />,
    );
    const focus = screen.getByRole("textbox", { name: "Research focus" });
    await user.type(focus, "Unsaved operator draft");

    rerender(
      <SettingsView
        response={{
          ...response,
          revision: response.revision + 1,
          values: {
            ...response.values,
            research: {
              ...response.values.research,
              focus: "Authoritative saved focus",
            },
          },
        }}
        health={blockedHealth}
        onSave={onSave}
        onReset={onReset}
        savePending={false}
        resetPending={false}
      />,
    );

    expect(focus).toHaveValue("Authoritative saved focus");
  });

  it("ignores an older settings revision after a newer revision was applied", () => {
    const onSave = vi.fn();
    const onReset = vi.fn();
    const authoritative = {
      ...response,
      revision: response.revision + 1,
      values: {
        ...response.values,
        research: { ...response.values.research, focus: "Authoritative saved focus" },
      },
    };
    const { rerender } = render(
      <SettingsView
        response={authoritative}
        health={blockedHealth}
        onSave={onSave}
        onReset={onReset}
        savePending={false}
        resetPending={false}
      />,
    );

    rerender(
      <SettingsView
        response={{
          ...response,
          values: {
            ...response.values,
            research: { ...response.values.research, focus: "Stale server focus" },
          },
        }}
        health={blockedHealth}
        onSave={onSave}
        onReset={onReset}
        savePending={false}
        resetPending={false}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Research focus" })).toHaveValue(
      "Authoritative saved focus",
    );
  });

  it("explains and focuses cross-field workflow relationship errors", async () => {
    const user = userEvent.setup();
    const { onSave } = renderSettings();
    const runQueries = screen.getByRole("spinbutton", { name: "Total run queries" });
    await user.clear(runQueries);
    await user.type(runQueries, "1");
    const runPages = screen.getByRole("spinbutton", { name: "Total run retrieved pages" });
    await user.clear(runPages);
    await user.type(runPages, "1");
    const concepts = screen.getByRole("spinbutton", { name: "Solution concepts" });
    await user.clear(concepts);
    await user.type(concepts, "1");

    await user.click(screen.getByRole("button", { name: "Save future-run settings" }));

    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("Discovery queries cannot exceed Total run queries.")).toBeVisible();
    expect(
      screen.getByText("First-pass retrieved pages cannot exceed Total run retrieved pages."),
    ).toBeVisible();
    expect(
      screen.getByText("Solution concepts must be at least the protected target of 5."),
    ).toBeVisible();
    expect(screen.getByRole("spinbutton", { name: "Discovery queries" })).toHaveFocus();
  });

  it("rejects model and token totals that cannot preserve five finalization slots", async () => {
    const user = userEvent.setup();
    const { onSave } = renderSettings();
    const calls = screen.getByRole("spinbutton", { name: "Total model calls" });
    const tokens = screen.getByRole("spinbutton", { name: "Total run tokens" });
    await user.clear(calls);
    await user.type(calls, "91");
    await user.clear(tokens);
    await user.type(tokens, "1120255");

    await user.click(screen.getByRole("button", { name: "Save future-run settings" }));

    expect(onSave).not.toHaveBeenCalled();
    expect(
      screen.getByText("Total model calls must be at least 92 for the protected target."),
    ).toBeVisible();
    expect(
      screen.getByText("Total run tokens must be at least 1,120,256 for the protected target."),
    ).toBeVisible();
    expect(calls).toHaveFocus();
  });

  it("rejects pattern and segment settings that cannot yield five candidates", async () => {
    const user = userEvent.setup();
    const { onSave } = renderSettings();
    const patterns = screen.getByRole("spinbutton", { name: "Problem patterns" });
    await user.clear(patterns);
    await user.type(patterns, "1");

    await user.click(screen.getByRole("button", { name: "Save future-run settings" }));

    expect(onSave).not.toHaveBeenCalled();
    expect(
      screen.getByText(
        "Problem patterns must provide at least 5 candidate segments with the saved segments-per-pattern value.",
      ),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Patterns multiplied by segments per pattern must reach the protected target of 5.",
      ),
    ).toBeVisible();
    expect(patterns).toHaveFocus();
  });

  it("rejects a blank zero-minimum numeric field", async () => {
    const user = userEvent.setup();
    const { onSave } = renderSettings();
    const retries = screen.getByRole("spinbutton", { name: "Search retries" });
    await user.clear(retries);

    await user.click(screen.getByRole("button", { name: "Save future-run settings" }));

    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("Enter a whole number.")).toBeVisible();
    expect(retries).toHaveFocus();
  });

  it("describes Reset as reversible, confirms explicitly, and returns focus", async () => {
    const user = userEvent.setup();
    const { onReset } = renderSettings();
    const trigger = screen.getByRole("button", { name: "Reset to protected defaults" });

    await user.click(trigger);
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("This reversible action");
    expect(
      screen.getByRole("button", { name: "Confirm reset to protected defaults" }),
    ).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Keep current settings" }));
    expect(onReset).not.toHaveBeenCalled();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Confirm reset to protected defaults" }));
    expect(onReset).toHaveBeenCalledOnce();
    expect(trigger).toHaveFocus();
  });

  it("disables conflicting actions while a mutation is pending and announces the result", () => {
    renderSettings({ savePending: true, mutationMessage: "Settings saved for future runs." });

    expect(screen.getByRole("button", { name: "Saving settings…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reset to protected defaults" })).toBeDisabled();
    expect(screen.getByText("Settings saved for future runs.")).toBeVisible();
  });
});
