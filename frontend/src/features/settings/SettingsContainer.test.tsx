import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SavedSettingsResponse } from "../../api/generated";
import { SettingsContainer } from "./SettingsContainer";

const api = vi.hoisted(() => ({
  bootstrap: vi.fn(),
  getSettings: vi.fn(),
  preflight: vi.fn(),
  reset: vi.fn(),
  save: vi.fn(),
}));

vi.mock("../../api/generated", () => ({
  bootstrapApiBootstrapGet: api.bootstrap,
  getSavedSettingsApiSettingsGet: api.getSettings,
  preflightApiRunsPreflightGet: api.preflight,
  resetSettingsApiSettingsResetPost: api.reset,
  saveSettingsApiSettingsPut: api.save,
}));

const initialResponse: SavedSettingsResponse = {
  revision: 3,
  values: { research: { focus: null } },
  fields: [
    {
      key: "research.focus",
      label: "Research focus",
      validation: "Optional plain-language focus.",
      protected_default_explanation: "No focus.",
    },
  ],
};

const savedResponse: SavedSettingsResponse = {
  ...initialResponse,
  revision: 4,
  values: { research: { focus: "Saved operator focus" } },
};

const resetResponse: SavedSettingsResponse = {
  ...initialResponse,
  revision: 5,
};

beforeEach(() => {
  Object.values(api).forEach((mock) => {
    mock.mockReset();
  });
  api.bootstrap.mockResolvedValue({ data: { session_nonce: "nonce" } });
  api.preflight.mockResolvedValue({ data: { ready: true, checks: [], blockers: [] } });
  api.getSettings
    .mockResolvedValueOnce({ data: initialResponse })
    .mockImplementation(() => new Promise<never>(() => undefined));
  api.save.mockResolvedValue({ data: savedResponse });
  api.reset.mockResolvedValue({ data: resetResponse });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("SettingsContainer", () => {
  it("puts a successful save response into the saved-settings cache immediately", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <SettingsContainer />
      </QueryClientProvider>,
    );
    const focus = await screen.findByRole("textbox", { name: "Research focus" });
    await user.type(focus, "Saved operator focus");
    await user.click(screen.getByRole("button", { name: "Save future-run settings" }));
    await waitFor(() => expect(api.save).toHaveBeenCalledOnce());

    expect(queryClient.getQueryData(["saved-settings"])).toEqual(savedResponse);
  });

  it("puts a successful reset response into the saved-settings cache immediately", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <SettingsContainer />
      </QueryClientProvider>,
    );
    await screen.findByRole("textbox", { name: "Research focus" });
    await waitFor(() => expect(api.preflight).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Reset to protected defaults" }));
    await user.click(screen.getByRole("button", { name: "Confirm reset to protected defaults" }));
    await waitFor(() => expect(api.reset).toHaveBeenCalledOnce());

    expect(queryClient.getQueryData(["saved-settings"])).toEqual(resetResponse);
    await waitFor(() => expect(api.preflight).toHaveBeenCalledTimes(2));
  });

  it("does not let an in-flight stale settings response overwrite a successful save", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    let resolveStale: ((value: { data: SavedSettingsResponse }) => void) | undefined;
    api.getSettings.mockImplementationOnce(
      () =>
        new Promise<{ data: SavedSettingsResponse }>((resolve) => {
          resolveStale = resolve;
        }),
    );
    render(
      <QueryClientProvider client={queryClient}>
        <SettingsContainer />
      </QueryClientProvider>,
    );
    const focus = await screen.findByRole("textbox", { name: "Research focus" });

    const staleRefetch = queryClient.refetchQueries({ queryKey: ["saved-settings"] });
    await waitFor(() => expect(api.getSettings).toHaveBeenCalledTimes(2));
    await user.type(focus, "Saved operator focus");
    await user.click(screen.getByRole("button", { name: "Save future-run settings" }));
    await waitFor(() => expect(api.save).toHaveBeenCalledOnce());

    const staleOptions = api.getSettings.mock.calls[1]?.[0];
    expect(staleOptions.signal).toBeInstanceOf(AbortSignal);
    expect(staleOptions.signal.aborted).toBe(true);
    resolveStale?.({ data: initialResponse });
    await staleRefetch;

    expect(queryClient.getQueryData(["saved-settings"])).toEqual(savedResponse);
    expect(focus).toHaveValue("Saved operator focus");
  });

  it("does not periodically rerun the expensive capability preflight", async () => {
    vi.useFakeTimers();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <SettingsContainer />
      </QueryClientProvider>,
    );

    await vi.waitFor(() => expect(api.preflight).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });

    expect(api.preflight).toHaveBeenCalledTimes(1);
  });
});
