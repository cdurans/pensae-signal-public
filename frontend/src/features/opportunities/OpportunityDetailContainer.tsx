import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import {
  bootstrapApiBootstrapGet,
  decideOpportunityRediscoveryApiOpportunitiesOpportunityIdRediscoveryDecisionPost,
  deleteOpportunityPermanentlyApiOpportunitiesOpportunityIdDelete,
  type LifecycleTarget,
  mergeOpportunityApiOpportunitiesOpportunityIdMergePost,
  opportunityDetailApiOpportunitiesOpportunityIdGet,
  opportunityLifecycleTargetsApiOpportunitiesOpportunityIdLifecycleTargetsGet,
  opportunityVersionDetailApiOpportunitiesOpportunityIdVersionsVersionIdGet,
  reverseOpportunityMergeApiOpportunitiesOpportunityIdMergeReversePost,
  setOpportunityFavoriteApiOpportunitiesOpportunityIdFavoritePut,
  setOpportunityNoteApiOpportunitiesOpportunityIdNotePut,
} from "../../api/generated";
import { OpportunityDetailView } from "./OpportunityDetailView";
import { OpportunityLifecyclePanel } from "./OpportunityLifecyclePanel";

type LifecycleAction =
  | {
      readonly kind: "rediscovery";
      readonly decision: "related" | "rediscovered" | "updated";
      readonly target: LifecycleTarget;
    }
  | { readonly kind: "merge"; readonly target: LifecycleTarget }
  | { readonly kind: "reverse" }
  | { readonly kind: "delete"; readonly confirmation: string };

export function OpportunityDetailContainer() {
  const { opportunityId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const versionId = searchParams.get("version");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [mutationMessage, setMutationMessage] = useState<string>();
  const [mergeSearch, setMergeSearch] = useState("");
  const bootstrap = useQuery({
    queryKey: ["bootstrap"],
    queryFn: async () => {
      const response = await bootstrapApiBootstrapGet({ throwOnError: true });
      return response.data;
    },
    staleTime: Number.POSITIVE_INFINITY,
  });
  const detail = useQuery({
    queryKey: ["opportunity-detail", opportunityId, versionId],
    queryFn: async () => {
      if (!opportunityId) throw new Error("Opportunity identifier is unavailable");
      if (versionId) {
        return (
          await opportunityVersionDetailApiOpportunitiesOpportunityIdVersionsVersionIdGet({
            path: { opportunity_id: opportunityId, version_id: versionId },
            throwOnError: true,
          })
        ).data;
      }
      return (
        await opportunityDetailApiOpportunitiesOpportunityIdGet({
          path: { opportunity_id: opportunityId },
          throwOnError: true,
        })
      ).data;
    },
    enabled: Boolean(opportunityId),
  });
  const lifecycleTargets = useQuery({
    queryKey: ["lifecycle-targets", opportunityId, mergeSearch],
    queryFn: async () => {
      if (!opportunityId) throw new Error("Opportunity identifier is unavailable");
      return (
        await opportunityLifecycleTargetsApiOpportunitiesOpportunityIdLifecycleTargetsGet({
          path: { opportunity_id: opportunityId },
          query: { search: mergeSearch || undefined, limit: 100 },
          throwOnError: true,
        })
      ).data;
    },
    enabled: Boolean(opportunityId),
  });
  const secureHeaders = () => {
    const nonce = bootstrap.data?.session_nonce;
    if (!nonce) throw new Error("Local session bootstrap is unavailable");
    return { "Content-Type": "application/json", "X-Pensae-Session": nonce };
  };
  const refreshOpportunity = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["opportunity-detail"] }),
      queryClient.invalidateQueries({ queryKey: ["portfolio"] }),
      queryClient.invalidateQueries({ queryKey: ["lifecycle-targets"] }),
    ]);
  };
  const favorite = useMutation({
    mutationFn: async (value: boolean) => {
      if (!opportunityId) throw new Error("Opportunity identifier is unavailable");
      return (
        await setOpportunityFavoriteApiOpportunitiesOpportunityIdFavoritePut({
          path: { opportunity_id: opportunityId },
          body: { favorite: value },
          headers: secureHeaders(),
          throwOnError: true,
        })
      ).data;
    },
    onSuccess: async () => {
      setMutationMessage("Favorite preference saved.");
      await refreshOpportunity();
    },
    onError: () => setMutationMessage("Favorite preference could not be saved."),
  });
  const note = useMutation({
    mutationFn: async (value: string | null) => {
      if (!opportunityId) throw new Error("Opportunity identifier is unavailable");
      return (
        await setOpportunityNoteApiOpportunitiesOpportunityIdNotePut({
          path: { opportunity_id: opportunityId },
          body: { note: value },
          headers: secureHeaders(),
          throwOnError: true,
        })
      ).data;
    },
    onSuccess: async () => {
      setMutationMessage("Operator note saved.");
      await refreshOpportunity();
    },
    onError: () => setMutationMessage("Operator note could not be saved."),
  });
  const lifecycle = useMutation({
    mutationFn: async (action: LifecycleAction) => {
      if (!opportunityId || !detail.data) throw new Error("Opportunity state is unavailable");
      const common = {
        path: { opportunity_id: opportunityId },
        headers: secureHeaders(),
        throwOnError: true,
      } as const;
      if (action.kind === "rediscovery") {
        return (
          await decideOpportunityRediscoveryApiOpportunitiesOpportunityIdRediscoveryDecisionPost({
            ...common,
            body: {
              target_opportunity_id: action.target.id,
              decision: action.decision,
              expected_candidate_revision: detail.data.revision,
              expected_target_revision: action.target.revision,
            },
          })
        ).data;
      }
      if (action.kind === "merge") {
        return (
          await mergeOpportunityApiOpportunitiesOpportunityIdMergePost({
            ...common,
            body: {
              survivor_id: action.target.id,
              expected_revision: detail.data.revision,
              expected_survivor_revision: action.target.revision,
              confirmation: "MERGE",
            },
          })
        ).data;
      }
      if (action.kind === "reverse") {
        return (
          await reverseOpportunityMergeApiOpportunitiesOpportunityIdMergeReversePost({
            ...common,
            body: {
              expected_revision: detail.data.revision,
              confirmation: "REVERSE MERGE",
            },
          })
        ).data;
      }
      return (
        await deleteOpportunityPermanentlyApiOpportunitiesOpportunityIdDelete({
          ...common,
          body: {
            expected_revision: detail.data.revision,
            confirmation: action.confirmation,
          },
        })
      ).data;
    },
    onSuccess: async (_result, action) => {
      if (action.kind === "delete") {
        navigate("/opportunities", { replace: true });
        return;
      }
      setMutationMessage(
        action.kind === "merge"
          ? "Merge recorded without combining content."
          : action.kind === "reverse"
            ? "Merge reversed; the opportunity is independent again."
            : action.decision === "updated"
              ? "Updated immutable version created."
              : `Relationship recorded as ${action.decision.replaceAll("_", " ")}.`,
      );
      setSearchParams({}, { replace: true });
      await refreshOpportunity();
    },
    onError: () => {
      setMutationMessage(
        "Lifecycle action was not applied. The record may be stale or unavailable; refresh and review the current state.",
      );
      void refreshOpportunity();
    },
  });

  if (detail.isPending) {
    return (
      <main id="main-content" aria-busy="true">
        <h1>Loading opportunity</h1>
      </main>
    );
  }
  if (detail.isError || !detail.data) {
    return (
      <main id="main-content">
        <h1>Opportunity unavailable</h1>
        <p role="alert">
          The retained opportunity or requested immutable version could not be loaded.
        </p>
        <button type="button" onClick={() => navigate("/opportunities")}>
          Return to opportunities
        </button>
      </main>
    );
  }
  const historical = Boolean(
    versionId && !detail.data.versions?.find((item) => item.id === versionId)?.is_current,
  );
  return (
    <OpportunityDetailView
      opportunity={detail.data}
      historical={historical}
      onCurrentVersion={() => setSearchParams({}, { replace: true })}
      onFavoriteChange={(value) => favorite.mutate(value)}
      onNoteSave={(value) => note.mutate(value)}
      metadataPending={favorite.isPending || note.isPending}
      mutationMessage={mutationMessage}
      lifecyclePanel={
        <OpportunityLifecyclePanel
          opportunity={detail.data}
          mergeTargets={lifecycleTargets.data?.items ?? []}
          mergeSearch={mergeSearch}
          mergeHasMore={lifecycleTargets.data?.has_more}
          historical={historical}
          pending={lifecycle.isPending || lifecycleTargets.isPending}
          message={
            lifecycleTargets.isError
              ? "Lifecycle targets are unavailable. Retry by refreshing this page."
              : mutationMessage
          }
          onMergeSearch={setMergeSearch}
          onRediscovery={(decision, target) =>
            lifecycle.mutate({ kind: "rediscovery", decision, target })
          }
          onMerge={(target) => lifecycle.mutate({ kind: "merge", target })}
          onReverse={() => lifecycle.mutate({ kind: "reverse" })}
          onDelete={(confirmation) => lifecycle.mutate({ kind: "delete", confirmation })}
        />
      }
    />
  );
}
