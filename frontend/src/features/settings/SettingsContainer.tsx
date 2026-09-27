import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SavedSettings } from "../../api/generated";
import {
  bootstrapApiBootstrapGet,
  getSavedSettingsApiSettingsGet,
  preflightApiRunsPreflightGet,
  resetSettingsApiSettingsResetPost,
  saveSettingsApiSettingsPut,
} from "../../api/generated";
import { SettingsView } from "./SettingsView";

export function SettingsContainer() {
  const queryClient = useQueryClient();
  const [mutationMessage, setMutationMessage] = useState<string>();
  const bootstrap = useQuery({
    queryKey: ["bootstrap"],
    queryFn: async () => {
      const response = await bootstrapApiBootstrapGet({ throwOnError: true });
      return response.data;
    },
    staleTime: Number.POSITIVE_INFINITY,
  });
  const settings = useQuery({
    queryKey: ["saved-settings"],
    queryFn: async ({ signal }) => {
      const response = await getSavedSettingsApiSettingsGet({ signal, throwOnError: true });
      return response.data;
    },
  });
  const health = useQuery({
    queryKey: ["capability-preflight"],
    queryFn: async () => {
      const response = await preflightApiRunsPreflightGet({ throwOnError: true });
      return response.data;
    },
  });
  const secureHeaders = () => {
    const nonce = bootstrap.data?.session_nonce;
    if (!nonce) {
      throw new Error("Local session bootstrap is unavailable");
    }
    return { "Content-Type": "application/json", "X-Pensae-Session": nonce };
  };
  const save = useMutation({
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ["saved-settings"] });
    },
    mutationFn: async (values: SavedSettings) =>
      (
        await saveSettingsApiSettingsPut({
          body: values,
          headers: secureHeaders(),
          throwOnError: true,
        })
      ).data,
    onSuccess: async (response) => {
      queryClient.setQueryData(["saved-settings"], response);
      setMutationMessage("Settings saved. They apply only to future research runs.");
      await queryClient.invalidateQueries({ queryKey: ["capability-preflight"] });
    },
    onError: () => setMutationMessage("Settings could not be saved. Review every field."),
  });
  const reset = useMutation({
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ["saved-settings"] });
    },
    mutationFn: async () =>
      (
        await resetSettingsApiSettingsResetPost({
          body: { confirmation: "RESET TO PROTECTED DEFAULTS" },
          headers: secureHeaders(),
          throwOnError: true,
        })
      ).data,
    onSuccess: async (response) => {
      queryClient.setQueryData(["saved-settings"], response);
      setMutationMessage("Protected shipped defaults restored for future runs.");
      await queryClient.invalidateQueries({ queryKey: ["capability-preflight"] });
    },
    onError: () => setMutationMessage("Settings could not be reset."),
  });

  if (settings.isPending) {
    return (
      <main id="main-content" aria-busy="true">
        <h1>Loading settings</h1>
      </main>
    );
  }
  if (settings.isError || !settings.data) {
    return (
      <main id="main-content">
        <h1>Settings unavailable</h1>
        <p role="alert">Saved future-run settings could not be loaded.</p>
        <button type="button" onClick={() => settings.refetch()}>
          Retry settings
        </button>
      </main>
    );
  }
  return (
    <SettingsView
      response={settings.data}
      health={health.data}
      healthUnavailable={health.isError}
      onHealthRetry={() => health.refetch()}
      onSave={(values) => save.mutate(values)}
      onReset={() => reset.mutate()}
      savePending={save.isPending}
      resetPending={reset.isPending}
      mutationMessage={mutationMessage}
    />
  );
}
