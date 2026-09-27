import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router";
import {
  bootstrapApiBootstrapGet,
  preflightApiRunsPreflightGet,
  runSnapshotApiRunsRunIdGet,
  stopRunApiRunsRunIdStopPost,
} from "../../api/generated";
import { CurrentRunView } from "./CurrentRunView";

const terminalRunStates = new Set(["completed", "completed_with_warnings", "stopped", "failed"]);

function runUnavailableMessage(error: unknown): string {
  if (
    typeof error === "object" &&
    error !== null &&
    "detail" in error &&
    error.detail === "run not found"
  ) {
    return "This run is no longer available. Empty terminal runs remain visible only until Pensae Signal restarts or another run begins.";
  }
  return "The authoritative run snapshot could not be loaded.";
}

export function CurrentRunContainer() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [recentProgress, setRecentProgress] = useState<string[]>([]);
  const bootstrap = useQuery({
    queryKey: ["bootstrap"],
    queryFn: async () => {
      const response = await bootstrapApiBootstrapGet({ throwOnError: true });
      return response.data;
    },
    staleTime: Number.POSITIVE_INFINITY,
  });
  const run = useQuery({
    queryKey: ["run-snapshot", runId],
    queryFn: async () => {
      if (!runId) {
        throw new Error("Run identifier is unavailable");
      }
      const response = await runSnapshotApiRunsRunIdGet({
        path: { run_id: runId },
        throwOnError: true,
      });
      return response.data;
    },
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      if (query.state.status === "error") {
        return false;
      }
      const state = query.state.data?.state;
      return state && terminalRunStates.has(state) ? false : 1_000;
    },
  });
  const health = useQuery({
    queryKey: ["capability-preflight"],
    queryFn: async () => {
      const response = await preflightApiRunsPreflightGet({ throwOnError: true });
      return response.data;
    },
  });
  const snapshotId = run.data?.id;
  const snapshotState = run.data?.state;

  useEffect(() => {
    if (
      !runId ||
      run.isError ||
      snapshotId !== runId ||
      !snapshotState ||
      terminalRunStates.has(snapshotState) ||
      typeof EventSource === "undefined"
    ) {
      return;
    }
    const events = new EventSource(`/api/runs/${runId}/events`);
    const refresh = (event: Event) => {
      if (event.type === "snapshot_required") {
        setRecentProgress((current) =>
          [
            ...current.filter(
              (item) => item !== "Progress stream gap · authoritative snapshot refreshed",
            ),
            "Progress stream gap · authoritative snapshot refreshed",
          ].slice(-8),
        );
      }
      if (event instanceof MessageEvent) {
        try {
          const value = JSON.parse(event.data) as {
            run_id?: string;
            kind?: string;
            state?: string;
            stage?: string;
            counters?: Record<string, number>;
            committed_count?: number;
            warning_code?: string | null;
          };
          const summary = [value.kind, value.stage, value.warning_code]
            .filter((item): item is string => typeof item === "string" && item.length > 0)
            .join(" · ")
            .replaceAll("_", " ");
          if (summary) {
            setRecentProgress((current) =>
              [...current.filter((item) => item !== summary), summary].slice(-8),
            );
          }
        } catch {
          // The backend snapshot remains authoritative after malformed stream data.
        }
      }
      void queryClient.invalidateQueries({ queryKey: ["run-snapshot", runId] });
      void queryClient.invalidateQueries({ queryKey: ["active-run"] });
      void queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    };
    const eventNames = [
      "run_started",
      "stage_started",
      "stage_completed",
      "warning",
      "opportunity_committed",
      "stopping",
      "terminal",
      "snapshot_required",
    ];
    eventNames.forEach((name) => {
      events.addEventListener(name, refresh);
    });
    return () => {
      eventNames.forEach((name) => {
        events.removeEventListener(name, refresh);
      });
      events.close();
    };
  }, [queryClient, run.isError, runId, snapshotId, snapshotState]);

  const stop = useMutation({
    mutationFn: async () => {
      const nonce = bootstrap.data?.session_nonce;
      if (!nonce || !runId) {
        throw new Error("Run control is unavailable");
      }
      const response = await stopRunApiRunsRunIdStopPost({
        path: { run_id: runId },
        body: {},
        headers: {
          "Content-Type": "application/json",
          "X-Pensae-Session": nonce,
        },
        throwOnError: true,
      });
      return response.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["run-snapshot", runId] }),
  });

  if (run.isPending) {
    return (
      <main id="main-content" aria-busy="true">
        <h1>Loading current run</h1>
      </main>
    );
  }
  if (run.isError || !run.data) {
    return (
      <main id="main-content">
        <h1>Current run unavailable</h1>
        <p role="alert">{runUnavailableMessage(run.error)}</p>
        <button type="button" onClick={() => navigate("/")}>
          Return to readiness
        </button>
      </main>
    );
  }
  return (
    <CurrentRunView
      run={run.data}
      onStop={() => stop.mutate()}
      stopPending={stop.isPending}
      stopError={
        stop.isError
          ? "Stop request failed. The backend-owned run may still be active; refresh the authoritative snapshot and retry."
          : undefined
      }
      recentProgress={recentProgress}
      onOpenOpportunity={
        (run.data.committed_opportunity_ids?.length ?? 0) > 0 || run.data.opportunity_id
          ? (opportunityId) => navigate(`/opportunities/${opportunityId}`)
          : undefined
      }
      dependencyHealth={health.data}
    />
  );
}
