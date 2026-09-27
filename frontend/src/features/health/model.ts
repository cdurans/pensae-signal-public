export const dependencyIds = ["postgresql", "redis", "searxng", "chat", "embedding"] as const;

export type DependencyId = (typeof dependencyIds)[number];

export const healthStates = [
  "unavailable",
  "incompatible",
  "starting",
  "ready",
  "unknown_listener",
] as const;

export type HealthState = (typeof healthStates)[number];

export interface DependencyHealth {
  readonly id: DependencyId;
  readonly label: string;
  readonly state: HealthState;
  readonly detail: string;
}

export interface PreflightBlocker {
  readonly id: string;
  readonly dependencyId?: DependencyId;
  readonly summary: string;
  readonly action: string;
}

export interface PreflightSnapshot {
  readonly checkedAt: string;
  readonly dependencies: readonly DependencyHealth[];
  readonly blockers: readonly PreflightBlocker[];
}

export const healthStateLabels: Readonly<Record<HealthState, string>> = {
  unavailable: "Unavailable",
  incompatible: "Incompatible",
  starting: "Starting",
  ready: "Ready",
  unknown_listener: "Unknown listener",
};

export const healthStateSymbols: Readonly<Record<HealthState, string>> = {
  unavailable: "!",
  incompatible: "×",
  starting: "…",
  ready: "✓",
  unknown_listener: "?",
};

export function getStartEligibility(snapshot: PreflightSnapshot): {
  readonly canStart: boolean;
  readonly reasons: readonly string[];
} {
  const reasons = snapshot.blockers.map((blocker) => `${blocker.summary} ${blocker.action}`);

  const dependenciesById = new Map(
    snapshot.dependencies.map((dependency) => [dependency.id, dependency]),
  );

  for (const id of dependencyIds) {
    const dependency = dependenciesById.get(id);
    if (dependency === undefined) {
      reasons.push(
        `Required dependency ${id} has no health result. Run make status and retry preflight.`,
      );
    } else if (
      dependency.state !== "ready" &&
      !snapshot.blockers.some((blocker) => blocker.dependencyId === id)
    ) {
      const label = healthStateLabels[dependency.state];
      const action =
        dependency.state === "unknown_listener"
          ? "Inspect the listener manually; Pensae Signal will not stop or replace an unowned process."
          : "Run make status, correct the local dependency, and retry preflight.";
      const fallbackReason = `${dependency.label} is ${label.toLowerCase()}. ${
        dependency.detail
      } ${action}`;
      reasons.push(fallbackReason);
    }
  }

  return {
    canStart: reasons.length === 0,
    reasons,
  };
}
