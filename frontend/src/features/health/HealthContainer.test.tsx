import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HealthContainer } from "./HealthContainer";

const api = vi.hoisted(() => ({
  activeRun: vi.fn(),
  bootstrap: vi.fn(),
  preflight: vi.fn(),
  start: vi.fn(),
}));

vi.mock("../../api/generated", () => ({
  activeRunSnapshotApiRunsActiveGet: api.activeRun,
  bootstrapApiBootstrapGet: api.bootstrap,
  preflightApiRunsPreflightGet: api.preflight,
  startRunApiRunsPost: api.start,
}));

function renderHealthContainer() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HealthContainer />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

beforeEach(() => {
  Object.values(api).forEach((mock) => {
    mock.mockReset();
  });
  api.activeRun.mockResolvedValue({
    data: {},
    response: new Response(null, { status: 204 }),
  });
  api.bootstrap.mockResolvedValue({ data: { session_nonce: "nonce" } });
  api.preflight.mockResolvedValue({
    data: {
      ready: true,
      checks: [
        { dependency: "postgresql", state: "ready", summary: "PostgreSQL is ready." },
        { dependency: "redis", state: "ready", summary: "Redis is ready." },
        { dependency: "searxng", state: "ready", summary: "SearXNG is ready." },
        { dependency: "chat", state: "ready", summary: "Chat is ready." },
        { dependency: "embedding", state: "ready", summary: "Embedding is ready." },
      ],
      blockers: [],
    },
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("HealthContainer start errors", () => {
  it("normalizes the generated client's empty 204 object to no active run", async () => {
    const { queryClient } = renderHealthContainer();

    await screen.findByText("Ready to start");
    expect(queryClient.getQueryData(["active-run"])).toBeNull();
  });

  it("shows a structured capability-preflight conflict instead of silently discarding it", async () => {
    api.start.mockRejectedValue({
      detail: {
        message: "research start is blocked by capability preflight",
        blockers: [
          {
            dependency: "chat",
            state: "incompatible",
            reason: "The configured chat endpoint failed structured-output validation.",
            action: "Restore the compatible launcher-owned chat endpoint and retry preflight.",
          },
        ],
      },
    });
    const user = userEvent.setup();
    renderHealthContainer();

    await screen.findByText("Ready to start");
    const start = screen.getByRole("button", { name: "Start research" });
    expect(start).toBeEnabled();
    await user.click(start);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("research start is blocked by capability preflight");
    expect(alert).toHaveTextContent(
      "The configured chat endpoint failed structured-output validation.",
    );
    expect(alert).toHaveTextContent(
      "Restore the compatible launcher-owned chat endpoint and retry preflight.",
    );
  });

  it("shows a structured active-run conflict instead of silently discarding it", async () => {
    api.start.mockRejectedValue({
      detail: "a research run is already active",
      run_id: "00000000-0000-0000-0000-000000000010",
    });
    const user = userEvent.setup();
    renderHealthContainer();

    await screen.findByText("Ready to start");
    const start = screen.getByRole("button", { name: "Start research" });
    expect(start).toBeEnabled();
    await user.click(start);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("a research run is already active");
    expect(alert).toHaveTextContent("00000000-0000-0000-0000-000000000010");
  });

  it("stops periodic capability preflight requests after readiness", async () => {
    vi.useFakeTimers();
    renderHealthContainer();

    await vi.waitFor(() => expect(api.preflight).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });

    expect(api.preflight).toHaveBeenCalledTimes(1);
  });

  it("does not poll a blocked preflight and retries only on explicit operator action", async () => {
    api.preflight
      .mockResolvedValueOnce({
        data: {
          ready: false,
          checks: [
            { dependency: "postgresql", state: "ready", summary: "PostgreSQL is ready." },
            { dependency: "redis", state: "ready", summary: "Redis is ready." },
            { dependency: "searxng", state: "ready", summary: "SearXNG is ready." },
            { dependency: "chat", state: "starting", summary: "Chat is starting." },
            { dependency: "embedding", state: "ready", summary: "Embedding is ready." },
          ],
          blockers: [
            {
              dependency: "chat",
              state: "starting",
              reason: "Chat is starting.",
              action: "Wait for the launcher-owned model, then retry preflight.",
            },
          ],
        },
      })
      .mockResolvedValue({
        data: {
          ready: true,
          checks: [
            { dependency: "postgresql", state: "ready", summary: "PostgreSQL is ready." },
            { dependency: "redis", state: "ready", summary: "Redis is ready." },
            { dependency: "searxng", state: "ready", summary: "SearXNG is ready." },
            { dependency: "chat", state: "ready", summary: "Chat is ready." },
            { dependency: "embedding", state: "ready", summary: "Embedding is ready." },
          ],
          blockers: [],
        },
      });
    vi.useFakeTimers();
    renderHealthContainer();

    await vi.waitFor(() => expect(screen.getByText("Start blocked")).toBeVisible());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(api.preflight).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Retry preflight" }));
    await vi.waitFor(() => expect(api.preflight).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(screen.getByText("Ready to start")).toBeVisible());
  });

  it("runs a fresh capability preflight immediately before starting research", async () => {
    api.start.mockResolvedValue({ data: { run_id: "run-1" } });
    const user = userEvent.setup();
    renderHealthContainer();

    await screen.findByText("Ready to start");
    expect(api.preflight).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Start research" }));

    await waitFor(() => expect(api.preflight).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(api.start).toHaveBeenCalledOnce());
    expect(api.preflight.mock.invocationCallOrder[1]).toBeLessThan(
      api.start.mock.invocationCallOrder[0] ?? Number.POSITIVE_INFINITY,
    );
  });
});
