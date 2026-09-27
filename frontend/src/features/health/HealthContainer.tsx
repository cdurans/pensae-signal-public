import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useNavigate } from "react-router";
import {
  activeRunSnapshotApiRunsActiveGet,
  bootstrapApiBootstrapGet,
  preflightApiRunsPreflightGet,
  startRunApiRunsPost,
} from "../../api/generated";
import { adaptCapabilityPreflight } from "./apiAdapter";
import { HealthDashboard } from "./HealthDashboard";
import type { DependencyId, PreflightSnapshot } from "./model";

const requiredDependencies: readonly DependencyId[] = [
  "postgresql",
  "redis",
  "searxng",
  "chat",
  "embedding",
];

function unavailableSnapshot(message: string): PreflightSnapshot {
  return {
    checkedAt: new Date().toISOString(),
    dependencies: requiredDependencies.map((id) => ({
      id,
      label: id === "postgresql" ? "PostgreSQL and pgvector" : id,
      state: "unavailable",
      detail: message,
    })),
    blockers: requiredDependencies.map((id) => ({
      id: `${id}-unavailable`,
      dependencyId: id,
      summary: message,
      action: "Run `make status` in Fedora and repair the local dependency.",
    })),
  };
}

const loadingSnapshot: PreflightSnapshot = {
  checkedAt: new Date(0).toISOString(),
  dependencies: requiredDependencies.map((id) => ({
    id,
    label: id === "postgresql" ? "PostgreSQL and pgvector" : id,
    state: "starting",
    detail: "Checking the local capability…",
  })),
  blockers: [],
};

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : undefined;
}

function startErrorMessage(error: unknown): string | undefined {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === "string") {
    return error;
  }
  const response = recordValue(error);
  if (!response) {
    return undefined;
  }
  const parts: string[] = [];
  const detail = response.detail;
  if (typeof detail === "string") {
    parts.push(detail);
  } else {
    const structured = recordValue(detail);
    if (typeof structured?.message === "string") {
      parts.push(structured.message);
    }
    if (Array.isArray(structured?.blockers)) {
      for (const blocker of structured.blockers.slice(0, requiredDependencies.length)) {
        const value = recordValue(blocker);
        if (typeof value?.reason === "string") {
          parts.push(value.reason);
        }
        if (typeof value?.action === "string") {
          parts.push(value.action);
        }
      }
    }
  }
  if (typeof response.run_id === "string") {
    parts.push(`Run ${response.run_id}.`);
  }
  return parts.length > 0 ? parts.join(" ") : "The local API rejected the start request.";
}

export function HealthContainer() {
  const navigate = useNavigate();
  const bootstrap = useQuery({
    queryKey: ["bootstrap"],
    queryFn: async () => {
      const response = await bootstrapApiBootstrapGet({ throwOnError: true });
      return response.data;
    },
    staleTime: Number.POSITIVE_INFINITY,
  });
  const preflight = useQuery({
    queryKey: ["capability-preflight"],
    queryFn: async () => {
      const response = await preflightApiRunsPreflightGet({ throwOnError: true });
      return response.data;
    },
  });
  const activeRun = useQuery({
    queryKey: ["active-run"],
    queryFn: async () => {
      const response = await activeRunSnapshotApiRunsActiveGet({ throwOnError: true });
      if (response.response.status === 204) {
        return null;
      }
      return response.data ?? null;
    },
    refetchOnWindowFocus: true,
  });

  useEffect(() => {
    if (activeRun.data?.id) {
      navigate(`/runs/${activeRun.data.id}`, { replace: true });
    }
  }, [activeRun.data, navigate]);

  const start = useMutation({
    mutationFn: async () => {
      const freshPreflight = await preflight.refetch();
      if (!freshPreflight.data?.ready) {
        throw (
          freshPreflight.error ??
          new Error("Research is blocked by the latest capability preflight.")
        );
      }
      const nonce = bootstrap.data?.session_nonce;
      if (!nonce) {
        throw new Error("Local session bootstrap is unavailable");
      }
      const response = await startRunApiRunsPost({
        body: {},
        headers: {
          "Content-Type": "application/json",
          "X-Pensae-Session": nonce,
        },
        throwOnError: true,
      });
      return response.data;
    },
    onSuccess: (result) => navigate(`/runs/${result.run_id}`),
  });

  let snapshot = loadingSnapshot;
  if (preflight.data) {
    snapshot = adaptCapabilityPreflight(
      preflight.data,
      new Date(preflight.dataUpdatedAt).toISOString(),
    );
  } else if (preflight.isError) {
    snapshot = unavailableSnapshot("The Fedora health API is unavailable.");
  }

  return (
    <HealthDashboard
      snapshot={snapshot}
      onRetryPreflight={() => void preflight.refetch()}
      preflightPending={preflight.isFetching}
      onStartResearch={() => start.mutate()}
      startPending={start.isPending}
      startError={startErrorMessage(start.error)}
    />
  );
}
