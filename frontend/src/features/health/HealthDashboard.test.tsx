import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { adaptCapabilityPreflight } from "./apiAdapter";
import { HealthDashboard } from "./HealthDashboard";
import type { HealthState, PreflightSnapshot } from "./model";
import { healthStateSymbols } from "./model";

const stateCases: ReadonlyArray<{
  readonly state: HealthState;
  readonly label: string;
}> = [
  { state: "unavailable", label: "Unavailable" },
  { state: "incompatible", label: "Incompatible" },
  { state: "starting", label: "Starting" },
  { state: "ready", label: "Ready" },
  { state: "unknown_listener", label: "Unknown listener" },
];

const allStateSnapshot: PreflightSnapshot = {
  checkedAt: "2026-07-21T18:00:00Z",
  dependencies: [
    {
      id: "postgresql",
      label: "PostgreSQL and pgvector",
      state: "unavailable",
      detail: "The database did not answer.",
    },
    {
      id: "redis",
      label: "Redis",
      state: "incompatible",
      detail: "The service does not satisfy the required contract.",
    },
    {
      id: "searxng",
      label: "SearXNG",
      state: "starting",
      detail: "The local search service is still starting.",
    },
    {
      id: "chat",
      label: "Chat model",
      state: "ready",
      detail: "Structured chat capability passed.",
    },
    {
      id: "embedding",
      label: "Embedding model",
      state: "unknown_listener",
      detail: "The configured port belongs to an unowned process.",
    },
  ],
  blockers: [
    {
      id: "database-unavailable",
      dependencyId: "postgresql",
      summary: "The database is unavailable.",
      action: "Start PostgreSQL and confirm pgvector is installed.",
    },
    {
      id: "redis-incompatible",
      dependencyId: "redis",
      summary: "Redis is incompatible.",
      action: "Restore the pinned local Redis configuration.",
    },
    {
      id: "search-starting",
      dependencyId: "searxng",
      summary: "Local search is starting.",
      action: "Wait for its bounded health check to finish.",
    },
    {
      id: "embedding-unknown",
      dependencyId: "embedding",
      summary: "An unknown listener occupies port 8086.",
      action: "Inspect it manually; Pensae Signal will never stop an unowned process.",
    },
  ],
};

const readySnapshot: PreflightSnapshot = {
  checkedAt: "2026-07-21T18:00:00Z",
  dependencies: [
    {
      id: "postgresql",
      label: "PostgreSQL and pgvector",
      state: "ready",
      detail: "Database capability passed.",
    },
    {
      id: "redis",
      label: "Redis",
      state: "ready",
      detail: "Progress and cancellation capability passed.",
    },
    {
      id: "searxng",
      label: "SearXNG",
      state: "ready",
      detail: "Search capability passed.",
    },
    {
      id: "chat",
      label: "Chat model",
      state: "ready",
      detail: "Structured chat capability passed.",
    },
    {
      id: "embedding",
      label: "Embedding model",
      state: "ready",
      detail: "Embedding dimension capability passed.",
    },
  ],
  blockers: [],
};

describe("HealthDashboard", () => {
  it("renders the Pensae Signal product identity", () => {
    render(<HealthDashboard snapshot={readySnapshot} onStartResearch={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Pensae Signal readiness" })).toBeVisible();
  });

  it("renders every health state with text and a non-color symbol", () => {
    render(<HealthDashboard snapshot={allStateSnapshot} onStartResearch={vi.fn()} />);

    for (const stateCase of stateCases) {
      const status = screen.getByText(stateCase.label);
      expect(within(status).getByText(healthStateSymbols[stateCase.state])).toBeInTheDocument();
    }
  });

  it("renders every actionable blocker and disables research start", () => {
    const onStartResearch = vi.fn();
    render(<HealthDashboard snapshot={allStateSnapshot} onStartResearch={onStartResearch} />);

    for (const blocker of allStateSnapshot.blockers) {
      expect(screen.getByText(blocker.summary)).toBeVisible();
      expect(screen.getByText(blocker.action)).toBeVisible();
    }

    const startButton = screen.getByRole("button", { name: "Start research" });
    expect(startButton).toBeDisabled();
    fireEvent.click(startButton);
    expect(onStartResearch).not.toHaveBeenCalled();
  });

  it("offers an explicit retry without enabling research while blocked", async () => {
    const retry = vi.fn();
    const user = userEvent.setup();
    render(
      <HealthDashboard
        snapshot={allStateSnapshot}
        onRetryPreflight={retry}
        onStartResearch={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Retry preflight" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Start research" })).toBeDisabled();
  });

  it("enables keyboard-operable research start only when preflight is fully ready", async () => {
    const onStartResearch = vi.fn();
    const user = userEvent.setup();
    render(<HealthDashboard snapshot={readySnapshot} onStartResearch={onStartResearch} />);

    expect(screen.getByText("Ready to start")).toBeVisible();
    const startButton = screen.getByRole("button", { name: "Start research" });
    expect(startButton).toBeEnabled();
    startButton.focus();
    await user.keyboard("{Enter}");
    expect(onStartResearch).toHaveBeenCalledTimes(1);
  });

  it("keeps research start disabled when the API reports any blocker", () => {
    const blockedSnapshot: PreflightSnapshot = {
      ...readySnapshot,
      blockers: [
        {
          id: "settings-invalid",
          summary: "Saved research settings are invalid.",
          action: "Open Settings and correct every invalid value.",
        },
      ],
    };

    render(<HealthDashboard snapshot={blockedSnapshot} onStartResearch={vi.fn()} />);

    expect(screen.getByText("Saved research settings are invalid.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Start research" })).toBeDisabled();
  });

  it("fails closed when a required dependency is missing despite no API blockers", () => {
    const incompleteSnapshot: PreflightSnapshot = {
      ...readySnapshot,
      dependencies: readySnapshot.dependencies.filter(
        (dependency) => dependency.id !== "embedding",
      ),
    };

    render(<HealthDashboard snapshot={incompleteSnapshot} onStartResearch={vi.fn()} />);

    expect(screen.getByText("Start blocked")).toBeVisible();
    expect(
      screen.getByText(
        "Required dependency embedding has no health result. Run make status and retry preflight.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Start research" })).toBeDisabled();
  });

  it("maps the generated-client preflight shape without performing network work", () => {
    const snapshot = adaptCapabilityPreflight(
      {
        ready: false,
        checks: allStateSnapshot.dependencies.map((dependency) => ({
          dependency: dependency.id,
          state: dependency.state,
          summary: dependency.detail,
          action: null,
        })),
        blockers: [
          {
            dependency: "embedding",
            state: "unknown_listener",
            reason: "An unknown listener occupies port 8086.",
            action: null,
          },
        ],
      },
      "2026-07-21T19:00:00Z",
    );

    expect(snapshot.checkedAt).toBe("2026-07-21T19:00:00Z");
    expect(snapshot.dependencies).toHaveLength(5);
    expect(snapshot.blockers).toEqual([
      {
        id: "embedding-unknown_listener-0",
        dependencyId: "embedding",
        summary: "An unknown listener occupies port 8086.",
        action:
          "Inspect the listener manually; Pensae Signal will not stop or replace an unowned process.",
      },
    ]);
  });
});
