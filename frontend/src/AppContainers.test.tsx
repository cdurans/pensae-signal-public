import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OpportunityDetailContainer } from "./features/opportunities/OpportunityDetailContainer";
import { PortfolioContainer } from "./features/opportunities/PortfolioContainer";
import { CurrentRunContainer } from "./features/runs/CurrentRunContainer";
import { SettingsContainer } from "./features/settings/SettingsContainer";

const api = vi.hoisted(() => ({
  activeRun: vi.fn(),
  bootstrap: vi.fn(),
  detail: vi.fn(),
  favorite: vi.fn(),
  list: vi.fn(),
  note: vi.fn(),
  preflight: vi.fn(),
  reset: vi.fn(),
  run: vi.fn(),
  save: vi.fn(),
  settings: vi.fn(),
  stop: vi.fn(),
}));

vi.mock("./api/generated", () => ({
  activeRunSnapshotApiRunsActiveGet: api.activeRun,
  bootstrapApiBootstrapGet: api.bootstrap,
  getSavedSettingsApiSettingsGet: api.settings,
  listOpportunitiesApiOpportunitiesGet: api.list,
  opportunityDetailApiOpportunitiesOpportunityIdGet: api.detail,
  preflightApiRunsPreflightGet: api.preflight,
  resetSettingsApiSettingsResetPost: api.reset,
  runSnapshotApiRunsRunIdGet: api.run,
  saveSettingsApiSettingsPut: api.save,
  setOpportunityFavoriteApiOpportunitiesOpportunityIdFavoritePut: api.favorite,
  setOpportunityNoteApiOpportunitiesOpportunityIdNotePut: api.note,
  stopRunApiRunsRunIdStopPost: api.stop,
}));

function pending() {
  return new Promise<never>(() => undefined);
}

function renderRoute(path: string, pattern: string, element: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={pattern} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  Object.values(api).forEach((mock) => {
    mock.mockReset();
  });
  api.bootstrap.mockResolvedValue({ data: { session_nonce: "nonce" } });
  api.preflight.mockResolvedValue({
    data: { ready: true, checks: [], blockers: [] },
  });
});

describe("route/query containers", () => {
  it("restores the authorized portfolio query from the URL", async () => {
    api.list.mockResolvedValue({
      data: {
        items: [],
        industry: "Property management",
        sort: "newest",
        limit: 50,
      },
    });

    renderRoute(
      "/opportunities?industry=Property%20management&sort=newest",
      "/opportunities",
      <PortfolioContainer />,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "Opportunities" })).toBeVisible();
    expect(screen.getByRole("searchbox", { name: "Industry" })).toHaveValue("Property management");
    expect(screen.getByRole("combobox", { name: "Sort order" })).toHaveValue("newest");
    expect(api.list).toHaveBeenCalledWith(
      expect.objectContaining({
        query: { industry: "Property management", sort: "newest", limit: 50 },
      }),
    );
  });

  it("renders bounded portfolio loading and error states", async () => {
    api.list.mockImplementation(pending);
    const loading = renderRoute("/opportunities", "/opportunities", <PortfolioContainer />);
    expect(screen.getByRole("heading", { name: "Loading opportunities" })).toBeVisible();
    loading.unmount();

    api.list.mockRejectedValue(new Error("offline"));
    renderRoute("/opportunities", "/opportunities", <PortfolioContainer />);
    expect(await screen.findByRole("heading", { name: "Opportunities unavailable" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("could not be loaded");
  });

  it("keeps the controlled industry input mounted and focused during a pending refetch", async () => {
    api.list
      .mockResolvedValueOnce({
        data: { items: [], industry: null, sort: "default", limit: 50 },
      })
      .mockImplementation(pending);
    const user = userEvent.setup();
    renderRoute("/opportunities", "/opportunities", <PortfolioContainer />);
    const industry = await screen.findByRole("searchbox", { name: "Industry" });

    await user.type(industry, "Property management");

    expect(industry).toHaveValue("Property management");
    expect(industry).toHaveFocus();
    expect(screen.getByRole("heading", { level: 1, name: "Opportunities" })).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Loading opportunities" }),
    ).not.toBeInTheDocument();
  });

  it("renders opportunity-detail loading and error states", async () => {
    api.detail.mockImplementation(pending);
    const loading = renderRoute(
      "/opportunities/opportunity-1",
      "/opportunities/:opportunityId",
      <OpportunityDetailContainer />,
    );
    expect(screen.getByRole("heading", { name: "Loading opportunity" })).toBeVisible();
    loading.unmount();

    api.detail.mockRejectedValue(new Error("missing"));
    renderRoute(
      "/opportunities/opportunity-1",
      "/opportunities/:opportunityId",
      <OpportunityDetailContainer />,
    );
    expect(await screen.findByRole("heading", { name: "Opportunity unavailable" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("could not be loaded");
  });

  it("renders settings loading and error states without hiding the shell", async () => {
    api.settings.mockImplementation(pending);
    const loading = renderRoute("/settings", "/settings", <SettingsContainer />);
    expect(screen.getByRole("heading", { name: "Loading settings" })).toBeVisible();
    loading.unmount();

    api.settings.mockRejectedValue(new Error("database unavailable"));
    renderRoute("/settings", "/settings", <SettingsContainer />);
    expect(await screen.findByRole("heading", { name: "Settings unavailable" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("could not be loaded");
  });

  it("renders authoritative current-run loading and error states", async () => {
    api.run.mockImplementation(pending);
    const loading = renderRoute("/runs/run-1", "/runs/:runId", <CurrentRunContainer />);
    expect(screen.getByRole("heading", { name: "Loading current run" })).toBeVisible();
    loading.unmount();

    api.run.mockRejectedValue(new Error("snapshot unavailable"));
    renderRoute("/runs/run-1", "/runs/:runId", <CurrentRunContainer />);
    expect(await screen.findByRole("heading", { name: "Current run unavailable" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("could not be loaded");
  });
});
