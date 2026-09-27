import { useEffect, useRef } from "react";
import type { PortfolioItem, PortfolioSort } from "../../api/generated";
import styles from "./PortfolioView.module.css";

export type PortfolioViewProps = {
  readonly items: readonly PortfolioItem[];
  readonly industry: string;
  readonly sort: PortfolioSort;
  readonly onIndustryChange: (industry: string) => void;
  readonly onSortChange: (sort: PortfolioSort) => void;
  readonly onOpenOpportunity: (opportunityId: string) => void;
};

const sortOptions: ReadonlyArray<{ value: PortfolioSort; label: string }> = [
  { value: "default", label: "Verdict, weighted score, evidence, then newest" },
  { value: "newest", label: "Newest evaluation" },
  { value: "weighted_score_desc", label: "Highest weighted score" },
  { value: "evidence_score_desc", label: "Highest evidence score" },
];

function displayVerdict(verdict: string): string {
  return verdict.replaceAll("_", " ");
}

export function PortfolioView({
  items,
  industry,
  sort,
  onIndustryChange,
  onSortChange,
  onOpenOpportunity,
}: PortfolioViewProps) {
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => heading.current?.focus(), []);

  return (
    <main id="main-content" className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>Retained opportunity portfolio</p>
        <h1 ref={heading} tabIndex={-1}>
          Opportunities
        </h1>
        <p>
          Browse complete evaluated opportunities. Favorite and note metadata never changes
          discovery, scoring, classification, or ranking.
        </p>
      </header>

      <section className={styles.controls} aria-labelledby="portfolio-controls-heading">
        <h2 id="portfolio-controls-heading">Filter and sort</h2>
        <div className={styles.controlGrid}>
          <div className={styles.control}>
            <label htmlFor="portfolio-industry">Industry</label>
            <input
              id="portfolio-industry"
              type="search"
              value={industry}
              onChange={(event) => onIndustryChange(event.currentTarget.value)}
              placeholder="All industries"
              autoComplete="off"
              maxLength={240}
              aria-describedby="portfolio-industry-help"
            />
            <span id="portfolio-industry-help">
              Only the primary industry is filterable, up to 240 characters.
            </span>
          </div>
          <div className={styles.control}>
            <label htmlFor="portfolio-sort">Sort order</label>
            <select
              id="portfolio-sort"
              value={sort}
              onChange={(event) => onSortChange(event.currentTarget.value as PortfolioSort)}
              aria-describedby="portfolio-sort-help"
            >
              {sortOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <span id="portfolio-sort-help">Scores and verdicts are read-only policy results.</span>
          </div>
        </div>
      </section>

      <section className={styles.results} aria-labelledby="portfolio-results-heading">
        <div className={styles.resultsHeading}>
          <h2 id="portfolio-results-heading">Current portfolio</h2>
          <p role="status">
            {items.length} {items.length === 1 ? "opportunity" : "opportunities"}
            {industry.trim() ? ` in ${industry.trim()}` : ""}
          </p>
        </div>

        {items.length === 0 ? (
          <div className={styles.empty}>
            <h3>No retained opportunities found</h3>
            <p>
              {industry.trim()
                ? "No retained opportunity matches this industry. Clear the industry filter to see the complete portfolio."
                : "Completed opportunities will appear here after they pass retention rules and commit."}
            </p>
            {industry.trim() && (
              <button type="button" onClick={() => onIndustryChange("")}>
                Clear industry filter
              </button>
            )}
          </div>
        ) : (
          <ul className={styles.list}>
            {items.map((item) => (
              <li key={item.id}>
                <article className={styles.card}>
                  <div className={styles.cardHeading}>
                    <div>
                      <p className={styles.industry}>{item.primary_industry}</p>
                      <h3>{item.opportunity_name}</h3>
                    </div>
                    <p className={styles.verdict}>Verdict: {displayVerdict(item.verdict)}</p>
                  </div>
                  <p>{item.concise_summary}</p>
                  <dl className={styles.metrics}>
                    <div>
                      <dt>Weighted score</dt>
                      <dd>{item.weighted_score} / 5</dd>
                    </div>
                    <div>
                      <dt>Evidence score</dt>
                      <dd>{item.evidence_score} / 5</dd>
                    </div>
                    <div>
                      <dt>Evaluation date</dt>
                      <dd>{new Date(item.version_created_at).toLocaleDateString()}</dd>
                    </div>
                    <div>
                      <dt>Favorite</dt>
                      <dd>{item.favorite ? "Yes" : "No"}</dd>
                    </div>
                  </dl>
                  {item.note && <p className={styles.note}>Operator note: {item.note}</p>}
                  <button type="button" onClick={() => onOpenOpportunity(item.id)}>
                    Open {item.opportunity_name}
                  </button>
                </article>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
