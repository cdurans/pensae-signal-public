import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import type { OpportunityDetail } from "../../api/generated";
import styles from "./OpportunityDetailView.module.css";

function textField(report: Record<string, unknown>, key: string): string {
  const value = report[key];
  return typeof value === "string" ? value : "Not available";
}

function textList(report: Record<string, unknown>, key: string): readonly string[] {
  const value = report[key];
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function objectField(report: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = report[key];
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

const verdictFactors = [
  ["strong_evidence", "Strong evidence"],
  ["plausible_buyer", "Plausible buyer"],
  ["payment_or_value_path", "Payment or value path"],
  ["critical_blocker", "Critical blocker"],
  ["strong_negative_evidence", "Strong negative evidence"],
  ["implausible_economics", "Implausible economics"],
  ["excessive_customization_or_operations", "Excessive customization or operations"],
] as const;

function ReportList({
  report,
  field,
  empty = "None recorded",
}: {
  readonly report: Record<string, unknown>;
  readonly field: string;
  readonly empty?: string;
}) {
  const items = textList(report, field);
  return items.length === 0 ? (
    <p>{empty}</p>
  ) : (
    <ul>
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

export type OpportunityDetailViewProps = {
  readonly opportunity: OpportunityDetail;
  readonly onFavoriteChange?: (favorite: boolean) => void;
  readonly onNoteSave?: (note: string | null) => void;
  readonly metadataPending?: boolean;
  readonly mutationMessage?: string;
  readonly lifecyclePanel?: ReactNode;
  readonly historical?: boolean;
  readonly onCurrentVersion?: () => void;
};

export function OpportunityDetailView({
  opportunity,
  onFavoriteChange,
  onNoteSave,
  metadataPending = false,
  mutationMessage,
  lifecyclePanel,
  historical = false,
  onCurrentVersion,
}: OpportunityDetailViewProps) {
  const report = opportunity.report as unknown as Record<string, unknown>;
  const proposedScores = objectField(report, "proposed_scores");
  const provenance = Object.entries(opportunity.provenance);
  const claims = Array.isArray(report.claims)
    ? report.claims.filter(
        (item): item is Record<string, unknown> => typeof item === "object" && item !== null,
      )
    : [];
  const heading = useRef<HTMLHeadingElement>(null);
  const [note, setNote] = useState(opportunity.note ?? "");

  useEffect(() => heading.current?.focus(), []);
  useEffect(() => setNote(opportunity.note ?? ""), [opportunity.note]);

  const saveNote = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = note.trim();
    onNoteSave?.(normalized ? normalized : null);
  };

  return (
    <main id="main-content" className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>
          {historical ? "Historical immutable version" : "Committed opportunity"} ·{" "}
          {opportunity.primary_industry} · Version {opportunity.version_number}
        </p>
        <h1 ref={heading} tabIndex={-1}>
          {textField(report, "opportunity_name")}
        </h1>
        <p className={styles.summary}>{textField(report, "concise_summary")}</p>
        <div className={styles.verdict} role="status">
          Verdict: {opportunity.verdict.replaceAll("_", " ")}
        </div>
        {historical && onCurrentVersion && (
          <button type="button" onClick={onCurrentVersion}>
            Return to current version
          </button>
        )}
      </header>

      <section className={styles.card} aria-labelledby="operator-metadata-heading">
        <h2 id="operator-metadata-heading">Operator metadata</h2>
        <p className={styles.immutable}>
          Favorite and note apply to the stable opportunity only. They never affect discovery,
          scoring, classification, or ranking.
        </p>
        <div className={styles.metadataGrid}>
          <div>
            <h3>Favorite</h3>
            <p>Current value: {opportunity.favorite ? "Favorite" : "Not favorite"}</p>
            {onFavoriteChange && (
              <button
                type="button"
                disabled={metadataPending}
                onClick={() => onFavoriteChange(!opportunity.favorite)}
              >
                {opportunity.favorite ? "Remove from favorites" : "Add to favorites"}
              </button>
            )}
          </div>
          <form onSubmit={saveNote}>
            <label htmlFor="opportunity-note">Operator note</label>
            <textarea
              id="opportunity-note"
              value={note}
              onChange={(event) => setNote(event.currentTarget.value)}
              rows={5}
              maxLength={4_000}
              disabled={metadataPending || !onNoteSave}
            />
            <span className={styles.help}>One free-text note, up to 4,000 characters.</span>
            {onNoteSave && (
              <button type="submit" disabled={metadataPending}>
                Save operator note
              </button>
            )}
          </form>
        </div>
        <p className={styles.liveMessage} role="status" aria-live="polite" aria-atomic="true">
          {mutationMessage ?? (metadataPending ? "Saving operator metadata…" : "")}
        </p>
      </section>

      {lifecyclePanel}

      <div className={styles.layout}>
        <section className={styles.card} aria-labelledby="problem-heading">
          <h2 id="problem-heading">Problem and proposed response</h2>
          <h3>Evidence-backed problem pattern</h3>
          <p>{textField(report, "problem_pattern")}</p>
          <h3>Target segment</h3>
          <p>{textField(report, "target_segment")}</p>
          <h3>Why this initial market</h3>
          <p>{textField(report, "initial_market_reason")}</p>
          <h3>Other plausible segments</h3>
          <ReportList report={report} field="other_segments" />
          <h3>Affected user and buyer</h3>
          <p>
            {textField(report, "affected_user")} · {textField(report, "likely_buyer")}
          </p>
          <h3>Recurring workflow and workaround</h3>
          <p>{textField(report, "recurring_workflow")}</p>
          <p>{textField(report, "current_workaround")}</p>
          <h3>Business consequences</h3>
          <ReportList report={report} field="business_consequences" />
          <h3>Frequency and value hypothesis</h3>
          <p>{textField(report, "frequency_value_hypothesis")}</p>
          <h3>Proposed solution</h3>
          <p>{textField(report, "proposed_solution")}</p>
          <p>Delivery: {textField(report, "delivery_model")}</p>
          <h3>Missing capabilities</h3>
          <ReportList report={report} field="missing_capabilities" />
          <h3>Reusable core capabilities</h3>
          <ReportList report={report} field="reusable_core_capabilities" />
        </section>

        <section className={styles.card} aria-labelledby="scores-heading">
          <h2 id="scores-heading">Application-calculated scores</h2>
          <dl className={styles.scoreGrid}>
            {Object.entries(opportunity.scores).map(([name, score]) => (
              <div key={name}>
                <dt>{name.replaceAll("_", " ")}</dt>
                <dd>{score}</dd>
              </div>
            ))}
          </dl>
          <h3>Score explanations</h3>
          <dl className={styles.provenance}>
            <div>
              <dt>Commercial attractiveness</dt>
              <dd>{textField(proposedScores, "commercial_explanation")}</dd>
            </div>
            <div>
              <dt>Evidence strength</dt>
              <dd>{textField(proposedScores, "evidence_explanation")}</dd>
            </div>
            <div>
              <dt>Pensae feasibility</dt>
              <dd>{textField(proposedScores, "feasibility_explanation")}</dd>
            </div>
            <div>
              <dt>Differentiation</dt>
              <dd>{textField(proposedScores, "differentiation_explanation")}</dd>
            </div>
          </dl>
          <h3>Verdict policy factors</h3>
          <dl className={styles.provenance}>
            {verdictFactors.map(([key, factorLabel]) => (
              <div key={key}>
                <dt>{factorLabel}</dt>
                <dd>{report[key] === true ? "Yes" : "No"}</dd>
              </div>
            ))}
          </dl>
          <p className={styles.immutable}>Scores and verdict are read-only policy results.</p>
        </section>
      </div>

      <div className={styles.layout}>
        <section className={styles.card} aria-labelledby="origin-heading">
          <h2 id="origin-heading">Origin problem research</h2>
          {opportunity.origin_pattern ? (
            <>
              <h3>Problem pattern</h3>
              <p>{opportunity.origin_pattern.summary}</p>
            </>
          ) : (
            <p>No retained origin pattern is available.</p>
          )}
          <h3>Problem signals</h3>
          {opportunity.origin_signals && opportunity.origin_signals.length > 0 ? (
            <ul className={styles.signalList}>
              {opportunity.origin_signals.map((signal) => (
                <li key={signal.id}>
                  <p>
                    <strong>{signal.affected_user}</strong> · Confidence {signal.confidence}
                  </p>
                  <p>{signal.recurring_workflow}</p>
                  <p>Workaround: {signal.current_workaround}</p>
                  <p>Consequence: {signal.business_consequence}</p>
                </li>
              ))}
            </ul>
          ) : (
            <p>No retained origin signals are available.</p>
          )}
        </section>

        <section className={styles.card} aria-labelledby="relations-heading">
          <h2 id="relations-heading">Relations and version history</h2>
          <h3>Related opportunities</h3>
          {opportunity.related && opportunity.related.length > 0 ? (
            <ul className={styles.relationList}>
              {opportunity.related.map((relation) => (
                <li key={relation.opportunity_id}>
                  <strong>{relation.opportunity_name}</strong>
                  <span>
                    {relation.relation_kind === "possible_rediscovery"
                      ? "Possible rediscovery"
                      : "Related"}
                    {` · Similarity ${relation.similarity} · ${relation.primary_industry}`}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p>No related opportunity is recorded.</p>
          )}
          <h3>Immutable versions</h3>
          {opportunity.versions && opportunity.versions.length > 0 ? (
            <ol className={styles.versionList}>
              {opportunity.versions.map((version) => (
                <li key={version.id}>
                  <strong>
                    <Link
                      to={
                        version.is_current
                          ? `/opportunities/${opportunity.id}`
                          : `/opportunities/${opportunity.id}?version=${version.id}`
                      }
                    >
                      Version {version.version_number}
                      {version.is_current ? " · Current" : ""}
                    </Link>
                  </strong>
                  <span>
                    {version.verdict.replaceAll("_", " ")} · Weighted {version.weighted_score} ·
                    Evidence {version.evidence_score} ·{" "}
                    {new Date(version.created_at).toLocaleDateString()}
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p>Version {opportunity.version_number} is the current immutable evaluation.</p>
          )}
        </section>
      </div>

      <section className={styles.card} aria-labelledby="analysis-heading">
        <h2 id="analysis-heading">Market, feasibility, and negative case</h2>
        <h3>Alternatives</h3>
        <ReportList report={report} field="alternatives" />
        <h3>Pricing or spend signals</h3>
        <ReportList report={report} field="pricing_or_spend_signals" />
        <h3>Market saturation</h3>
        <p>{textField(report, "market_saturation")}</p>
        <h3>Incumbent response risk</h3>
        <p>{textField(report, "incumbent_response_risk")}</p>
        <h3>Commercial analysis</h3>
        <p>{textField(report, "commercial_analysis")}</p>
        <h3>Feasibility analysis</h3>
        <p>{textField(report, "feasibility_analysis")}</p>
        <h3>Integration and customization burden</h3>
        <p>{textField(report, "integration_customization_burden")}</p>
        <h3>Preferred technology fit</h3>
        <p>{textField(report, "preferred_technology_fit")}</p>
        <h3>Trust and regulatory constraints</h3>
        <ReportList report={report} field="trust_regulatory_constraints" />
        <h3>Operational burden</h3>
        <p>{textField(report, "operational_burden")}</p>
        <h3>Differentiation analysis</h3>
        <p>{textField(report, "differentiation_analysis")}</p>
        <div className={styles.analysisGrid}>
          <div>
            <h3>Risks</h3>
            <ReportList report={report} field="risks" />
          </div>
          <div>
            <h3>Unknowns</h3>
            <ReportList report={report} field="unknowns" />
          </div>
          <div>
            <h3>Reasons not to pursue</h3>
            <ReportList report={report} field="reasons_not_to_pursue" />
          </div>
          <div>
            <h3>Next research questions</h3>
            <ReportList report={report} field="next_research_questions" />
          </div>
        </div>
      </section>

      <section className={styles.card} aria-labelledby="claims-heading">
        <h2 id="claims-heading">Claim labels and evidence limitations</h2>
        <dl className={styles.provenance}>
          {claims.map((claim) => (
            <div key={`${String(claim.label)}-${String(claim.statement)}`}>
              <dt>{String(claim.label)}</dt>
              <dd>{String(claim.statement)}</dd>
            </div>
          ))}
        </dl>
        <h3>Source diversity limitation</h3>
        <p>{textField(report, "source_diversity_limitation")}</p>
        <h3>Conflict limitation</h3>
        <p>{textField(report, "conflict_limitation")}</p>
      </section>

      <section className={styles.card} aria-labelledby="evidence-heading">
        <h2 id="evidence-heading">Verified evidence chain</h2>
        <ol className={styles.evidenceList}>
          {opportunity.evidence.map((item) => (
            <li key={item.id}>
              <p className={styles.claim}>{item.supported_claim}</p>
              <blockquote>“{item.excerpt}”</blockquote>
              <a href={item.source_url} target="_blank" rel="noreferrer">
                {item.source_title}
                {item.publisher ? ` — ${item.publisher}` : ""}
              </a>
              <p className={styles.meta}>
                {item.evidence_kind} · Retrieved {new Date(item.retrieved_at).toLocaleString()}
              </p>
              {item.material_conflict && <p>{item.material_conflict}</p>}
            </li>
          ))}
        </ol>
      </section>

      <details className={styles.card}>
        <summary>Reproducibility provenance</summary>
        <dl className={styles.provenance}>
          {provenance.map(([name, value]) => (
            <div key={name}>
              <dt>{name.replaceAll("_", " ")}</dt>
              <dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd>
            </div>
          ))}
        </dl>
      </details>
    </main>
  );
}
