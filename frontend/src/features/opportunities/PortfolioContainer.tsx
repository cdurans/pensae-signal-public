import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router";
import type { PortfolioSort } from "../../api/generated";
import { listOpportunitiesApiOpportunitiesGet } from "../../api/generated";
import { PortfolioView } from "./PortfolioView";

const supportedSorts = new Set<PortfolioSort>([
  "default",
  "newest",
  "weighted_score_desc",
  "evidence_score_desc",
]);

function selectedSort(value: string | null): PortfolioSort {
  return supportedSorts.has(value as PortfolioSort) ? (value as PortfolioSort) : "default";
}

export function PortfolioContainer() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const industry = searchParams.get("industry") ?? "";
  const sort = selectedSort(searchParams.get("sort"));
  const portfolio = useQuery({
    queryKey: ["portfolio", industry, sort],
    queryFn: async () => {
      const response = await listOpportunitiesApiOpportunitiesGet({
        query: {
          industry: industry.trim() || undefined,
          sort,
          limit: 50,
        },
        throwOnError: true,
      });
      return response.data;
    },
    placeholderData: (previousData) => previousData,
  });

  const updateUrl = (nextIndustry: string, nextSort: PortfolioSort) => {
    const next = new URLSearchParams();
    if (nextIndustry.trim()) {
      next.set("industry", nextIndustry);
    }
    if (nextSort !== "default") {
      next.set("sort", nextSort);
    }
    setSearchParams(next, { replace: true });
  };

  if (portfolio.isPending) {
    return (
      <main id="main-content" aria-busy="true">
        <h1>Loading opportunities</h1>
        <p role="status">Loading the bounded retained portfolio.</p>
      </main>
    );
  }
  if (portfolio.isError || !portfolio.data) {
    return (
      <main id="main-content">
        <h1>Opportunities unavailable</h1>
        <p role="alert">
          The retained portfolio could not be loaded. The UI remains available; check local
          dependency health and retry.
        </p>
        <button type="button" onClick={() => portfolio.refetch()}>
          Retry portfolio
        </button>
      </main>
    );
  }
  return (
    <PortfolioView
      items={portfolio.data.items}
      industry={industry}
      sort={sort}
      onIndustryChange={(value) => updateUrl(value, sort)}
      onSortChange={(value) => updateUrl(industry, value)}
      onOpenOpportunity={(id) => navigate(`/opportunities/${id}`)}
    />
  );
}
