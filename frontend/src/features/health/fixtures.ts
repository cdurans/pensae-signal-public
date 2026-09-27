import type { PreflightSnapshot } from "./model";

export const degradedPreflightFixture: PreflightSnapshot = {
  checkedAt: "2026-07-21T18:00:00Z",
  dependencies: [
    {
      id: "postgresql",
      label: "PostgreSQL and pgvector",
      state: "ready",
      detail: "Database connection and pgvector extension are available.",
    },
    {
      id: "redis",
      label: "Redis",
      state: "ready",
      detail: "Progress and cancellation control are available.",
    },
    {
      id: "searxng",
      label: "SearXNG",
      state: "unavailable",
      detail: "The local search endpoint did not answer its health check.",
    },
    {
      id: "chat",
      label: "Chat model",
      state: "starting",
      detail: "The Fedora launcher is waiting for the chat capability probe.",
    },
    {
      id: "embedding",
      label: "Embedding model",
      state: "unknown_listener",
      detail: "Port 8086 is occupied by a process Pensae Signal does not own.",
    },
  ],
  blockers: [
    {
      id: "searxng-unavailable",
      dependencyId: "searxng",
      summary: "Local search is unavailable.",
      action: "Check SearXNG with `make status`, then retry startup.",
    },
    {
      id: "chat-starting",
      dependencyId: "chat",
      summary: "The chat model is still starting.",
      action: "Wait for the bounded readiness check to finish.",
    },
    {
      id: "embedding-unknown-listener",
      dependencyId: "embedding",
      summary: "An unknown process is listening on embedding port 8086.",
      action:
        "Inspect the listener manually; Pensae Signal will not stop or replace an unowned process.",
    },
  ],
};
