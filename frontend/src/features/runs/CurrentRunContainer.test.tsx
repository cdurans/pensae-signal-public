import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CurrentRunContainer } from "./CurrentRunContainer";

const api = vi.hoisted(() => ({
  bootstrap: vi.fn(),
  preflight: vi.fn(),
  run: vi.fn(),
  stop: vi.fn(),
}));

vi.mock("../../api/generated", () => ({
  bootstrapApiBootstrapGet: api.bootstrap,
  preflightApiRunsPreflightGet: api.preflight,
  runSnapshotApiRunsRunIdGet: api.run,
  stopRunApiRunsRunIdStopPost: api.stop,
}));

class EventSourceStub {
  static instances: EventSourceStub[] = [];

  readonly url: string;
  readonly listeners = new Map<string, (event: Event) => void>();
  readonly addEventListener = vi.fn((name: string, listener: (event: Event) => void) => {
    this.listeners.set(name, listener);
  });
  readonly removeEventListener = vi.fn((name: string) => {
    this.listeners.delete(name);
  });
  readonly close = vi.fn();

  constructor(url: string | URL) {
    this.url = String(url);
    EventSourceStub.instances.push(this);
  }

  emit(name: string, payload: object) {
    this.listeners.get(name)?.(new MessageEvent(name, { data: JSON.stringify(payload) }));
  }
}

const runningSnapshot = {
  id: "run-1",
  state: "running",
  opportunity_id: null,
  effective_config: {},
  workflow_version: "phase2.fixed-graph.v1",
  schema_version: "phase2.run.v1",
  created_at: "2026-07-30T12:00:00Z",
  current_stage: "discovery_search",
  work_counters: {},
  warning_codes: [],
  model_usage: [],
  committed_count: 0,
};

function renderCurrentRun() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/runs/run-1"]}>
        <Routes>
          <Route path="/runs/:runId" element={<CurrentRunContainer />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  Object.values(api).forEach((mock) => {
    mock.mockReset();
  });
  EventSourceStub.instances = [];
  vi.stubGlobal("EventSource", EventSourceStub);
  api.bootstrap.mockResolvedValue({ data: { session_nonce: "nonce" } });
  api.preflight.mockResolvedValue({ data: { ready: true, checks: [], blockers: [] } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("CurrentRunContainer stream lifecycle", () => {
  it("does not open an EventSource when the initial run snapshot is unavailable", async () => {
    api.run.mockRejectedValue(new Error("404 run not found"));

    renderCurrentRun();
    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Current run unavailable" })).toBeVisible();
    });

    expect(EventSourceStub.instances).toHaveLength(0);
  });

  it("explains when an older empty terminal run is no longer in process memory", async () => {
    api.run.mockRejectedValue({ detail: "run not found" });

    renderCurrentRun();
    await vi.waitFor(() => expect(screen.getByRole("alert")).toBeVisible());
    const alert = screen.getByRole("alert");

    expect(alert).toHaveTextContent("no longer available");
    expect(alert).toHaveTextContent("until Pensae Signal restarts or another run begins");
  });

  it("stops periodic run refetches after the initial snapshot is unavailable", async () => {
    api.run.mockRejectedValue(new Error("404 run not found"));

    renderCurrentRun();
    await vi.waitFor(() => expect(api.run).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });

    expect(api.run).toHaveBeenCalledTimes(1);
  });

  it("opens one EventSource for a valid running snapshot and closes it on cleanup", async () => {
    api.run.mockResolvedValue({ data: runningSnapshot });

    const rendered = renderCurrentRun();
    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Research in progress" })).toBeVisible();
    });

    expect(EventSourceStub.instances).toHaveLength(1);
    expect(EventSourceStub.instances[0]?.url).toBe("/api/runs/run-1/events");

    rendered.unmount();
    expect(EventSourceStub.instances[0]?.close).toHaveBeenCalledOnce();
  });

  it("keeps the existing EventSource when polling returns a fresh equivalent running snapshot", async () => {
    api.run
      .mockResolvedValueOnce({ data: { ...runningSnapshot } })
      .mockResolvedValue({ data: { ...runningSnapshot } });

    renderCurrentRun();
    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Research in progress" })).toBeVisible();
    });
    expect(EventSourceStub.instances).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_100);
    });
    await vi.waitFor(() => expect(api.run).toHaveBeenCalledTimes(2));

    expect(EventSourceStub.instances).toHaveLength(1);
    expect(EventSourceStub.instances[0]?.close).not.toHaveBeenCalled();
  });

  it("closes the existing EventSource and stops polling when a running snapshot refetch fails", async () => {
    api.run
      .mockResolvedValueOnce({ data: runningSnapshot })
      .mockRejectedValue(new Error("404 run not found"));

    renderCurrentRun();
    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Research in progress" })).toBeVisible();
    });
    expect(EventSourceStub.instances).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_100);
    });
    await vi.waitFor(() => expect(api.run).toHaveBeenCalledTimes(2));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });

    expect(EventSourceStub.instances[0]?.close).toHaveBeenCalledOnce();
    expect(api.run).toHaveBeenCalledTimes(2);
  });

  it("closes the EventSource without reopening when a running snapshot becomes terminal", async () => {
    api.run.mockResolvedValueOnce({ data: runningSnapshot }).mockResolvedValue({
      data: { ...runningSnapshot, state: "completed", current_stage: "terminal_cleanup" },
    });

    renderCurrentRun();
    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Research in progress" })).toBeVisible();
    });
    expect(EventSourceStub.instances).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_100);
    });
    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Research run" })).toBeVisible();
    });

    expect(EventSourceStub.instances).toHaveLength(1);
    expect(EventSourceStub.instances[0]?.close).toHaveBeenCalledOnce();
  });

  it("refetches the complete backend terminal snapshot after an empty terminal event", async () => {
    api.run.mockResolvedValueOnce({ data: runningSnapshot }).mockResolvedValue({
      data: {
        ...runningSnapshot,
        state: "stopped",
        current_stage: "terminal_cleanup",
        work_counters: { queries: 8, search_results: 60, unique_urls: 60 },
        warning_codes: ["search_engine_warning", "work_limit_exceeded"],
      },
    });

    renderCurrentRun();
    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Research in progress" })).toBeVisible();
    });
    const events = EventSourceStub.instances[0];
    expect(events).toBeDefined();

    await act(async () => {
      events?.emit("terminal", {
        run_id: "run-1",
        kind: "terminal",
        state: "stopped",
        stage: "terminal_cleanup",
        counters: { queries: 8, search_results: 60, unique_urls: 60 },
        committed_count: 0,
        warning_code: "work_limit_exceeded",
      });
    });

    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Research run" })).toBeVisible();
    });
    expect(screen.getByRole("status")).toHaveTextContent("stopped · terminal cleanup");
    expect(screen.getByText("work limit exceeded")).toBeVisible();
    expect(screen.getByText("search engine warning")).toBeVisible();
    expect(screen.getByText(/protected work or token ceiling was reached/i)).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });

    expect(api.run).toHaveBeenCalledTimes(2);
    expect(events?.close).toHaveBeenCalledOnce();
  });

  it("does not open an EventSource for an already-terminal snapshot", async () => {
    api.run.mockResolvedValue({
      data: { ...runningSnapshot, state: "failed", current_stage: "terminal_cleanup" },
    });

    renderCurrentRun();
    await vi.waitFor(() => {
      expect(screen.getByRole("heading", { name: "Research run" })).toBeVisible();
    });

    expect(EventSourceStub.instances).toHaveLength(0);
  });

  it("does not periodically rerun capability preflight while the run page is mounted", async () => {
    api.run.mockResolvedValue({
      data: { ...runningSnapshot, state: "completed", current_stage: "terminal_cleanup" },
    });

    renderCurrentRun();
    await vi.waitFor(() => expect(api.preflight).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });

    expect(api.preflight).toHaveBeenCalledTimes(1);
  });
});
