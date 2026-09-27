import type { CapabilityPreflight as CapabilityPreflightResponse } from "../../api/generated";
import type { DependencyId, PreflightBlocker, PreflightSnapshot } from "./model";

export type { CapabilityPreflightResponse };

const dependencyLabels: Readonly<Record<DependencyId, string>> = {
  postgresql: "PostgreSQL and pgvector",
  redis: "Redis",
  searxng: "SearXNG",
  chat: "Chat model",
  embedding: "Embedding model",
};

function fallbackAction(blocker: CapabilityPreflightResponse["blockers"][number]): string {
  if (blocker.state === "unknown_listener") {
    return "Inspect the listener manually; Pensae Signal will not stop or replace an unowned process.";
  }

  return `Check ${
    dependencyLabels[blocker.dependency]
  } with \`make status\`, correct the local dependency, and retry.`;
}

export function adaptCapabilityPreflight(
  response: CapabilityPreflightResponse,
  checkedAt: string,
): PreflightSnapshot {
  const blockers: readonly PreflightBlocker[] = response.blockers.map((blocker, index) => ({
    id: `${blocker.dependency}-${blocker.state}-${index}`,
    dependencyId: blocker.dependency,
    summary: blocker.reason,
    action: blocker.action?.trim() || fallbackAction(blocker),
  }));

  return {
    checkedAt,
    dependencies: response.checks.map((check) => ({
      id: check.dependency,
      label: dependencyLabels[check.dependency],
      state: check.state,
      detail: check.summary,
    })),
    blockers,
  };
}
