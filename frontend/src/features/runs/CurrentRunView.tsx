import type { CapabilityPreflight, RunDetail } from "../../api/generated";
import styles from "./CurrentRunView.module.css";

const terminalStates = new Set(["completed", "completed_with_warnings", "stopped", "failed"]);

const warningExplanations: Record<string, string> = {
  search_engine_warning:
    "The local search engine reported that some provider results were unavailable or unusable; research continued under the normal evidence gates, and zero usable results remain possible.",
  source_unavailable:
    "A source could not be retrieved within the safety and availability rules; research continued with the remaining evidence.",
  signal_discarded:
    "A proposed problem signal failed its schema or source contract and was discarded without inventing a replacement.",
  evidence_integrity_failure:
    "Selected text could not be mechanically verified against the retrieved source, so the affected evidence was discarded.",
  pattern_synthesis_discarded:
    "A problem-pattern response failed its contract and was discarded; other valid patterns could continue.",
  segment_mapping_discarded:
    "A segment mapping failed its contract and was discarded before the preliminary gate.",
  no_gate_survivor:
    "No preliminary candidate passed the deterministic evidence and viability gate, so no solution was advanced.",
  concept_design_discarded:
    "A solution concept failed its contract or changed the approved buyer/segment and was discarded.",
  focused_evidence_unavailable:
    "Focused validation did not retain sufficient verified evidence for one concept, so that concept could not advance.",
  final_candidate_discarded:
    "A final report candidate failed its schema, evidence, or gate contract and was not committed.",
  final_candidates_unavailable:
    "That candidate did not retain enough verified evidence to commit an opportunity; other valid candidates could continue.",
  work_limit_exceeded:
    "A protected work or token ceiling was reached; the run stopped new work and retained only earlier complete commits.",
  stop_requested:
    "The operator requested Stop; new work ceased cooperatively and earlier complete commits were retained.",
  exact_rediscovery:
    "An exact retained identity was rediscovered, so no duplicate opportunity was committed.",
  redis_unavailable:
    "Transient progress or cancellation storage became unavailable; the run failed closed instead of continuing unsafely.",
  run_execution_failed:
    "The backend run task failed; incomplete work was discarded and earlier complete commits, if any, remain available.",
  workflow_contract_failure:
    "A required workflow value or stage violated its validated contract, so incomplete work was discarded.",
  workflow_execution_failed:
    "An unexpected workflow-stage failure stopped research; incomplete work was discarded.",
  dependency_unavailable:
    "A required local dependency failed or timed out, so research stopped safely at a work boundary.",
  opportunity_commit_failed:
    "A later opportunity commit failed; earlier independently committed opportunities remain available.",
  progress_snapshot_failed:
    "The authoritative progress snapshot could not be recorded, so the run failed closed.",
  opportunity_target_shortfall:
    "The run exhausted its bounded eligible work before reaching five complete opportunities; no filler was created.",
  focused_evidence_prompt_truncated:
    "Focused evidence was packed to the protected prompt limit; research continued with the source-fair spans that fit.",
  focused_evidence_prompt_overflow:
    "No focused evidence span fit the protected prompt envelope, so the candidate was discarded without inventing support.",
};

function label(value: string) {
  return value.replaceAll("_", " ");
}

export function CurrentRunView({
  run,
  onStop,
  stopPending = false,
  stopError,
  recentProgress = [],
  onOpenOpportunity,
  dependencyHealth,
}: {
  readonly run: RunDetail;
  readonly onStop: () => void;
  readonly stopPending?: boolean;
  readonly stopError?: string;
  readonly recentProgress?: readonly string[];
  readonly onOpenOpportunity?: (opportunityId: string) => void;
  readonly dependencyHealth?: CapabilityPreflight;
}) {
  const counters = Object.entries(run.work_counters ?? {});
  const usage = run.model_usage ?? [];
  const tokenTotal = usage.reduce(
    (total, call) => total + call.input_tokens + call.output_tokens,
    0,
  );
  const byRole = usage.reduce<Record<string, number>>((totals, call) => {
    totals[call.role] = (totals[call.role] ?? 0) + call.input_tokens + call.output_tokens;
    return totals;
  }, {});
  const byModel = usage.reduce<Record<string, number>>((totals, call) => {
    totals[call.model] = (totals[call.model] ?? 0) + call.input_tokens + call.output_tokens;
    return totals;
  }, {});
  const terminal = terminalStates.has(run.state);
  const explicitOpportunityIds = run.committed_opportunity_ids ?? [];
  const committedOpportunityIds =
    explicitOpportunityIds.length > 0
      ? explicitOpportunityIds
      : run.opportunity_id
        ? [run.opportunity_id]
        : [];
  const nonCountingOutcomes = Object.entries(
    run.non_counting_outcomes ?? {
      automatic_exact_rediscovery: 0,
      updated_version: 0,
      unresolved_possible_rediscovery: 0,
      invalid_candidate: 0,
      incomplete_candidate: 0,
    },
  );
  const capacityGroups = run.capacity
    ? ([
        ["Available for optional work", run.capacity.available],
        ["Reserved for remaining slots", run.capacity.reserved],
        ["Consumed", run.capacity.consumed],
        ["Remaining total", run.capacity.remaining],
      ] as const)
    : [];

  return (
    <main id="main-content" className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>Backend-owned research run</p>
        <h1>{terminal ? "Research run" : "Research in progress"}</h1>
        <p role="status" aria-live="polite">
          {label(run.state)} · {label(run.current_stage ?? "created")}
        </p>
        <p className={styles.runId}>Run {run.id}</p>
        {!terminal && (
          <>
            <button type="button" onClick={onStop} disabled={stopPending}>
              {stopPending ? "Stop requested" : "Stop research"}
            </button>
            {stopError && <p role="alert">{stopError}</p>}
          </>
        )}
        {onOpenOpportunity && committedOpportunityIds.length > 0 && (
          <nav aria-label="Committed opportunities">
            <ol>
              {committedOpportunityIds.map((opportunityId, index) => (
                <li key={opportunityId}>
                  <button type="button" onClick={() => onOpenOpportunity(opportunityId)}>
                    Open opportunity {index + 1} of {committedOpportunityIds.length}
                  </button>
                </li>
              ))}
            </ol>
          </nav>
        )}
      </header>

      <div className={styles.grid}>
        <section className={styles.card} aria-labelledby="work-heading">
          <h2 id="work-heading">Bounded work</h2>
          <p>
            <strong>{run.achieved_count ?? 0}</strong> of <strong>{run.target_count ?? 0}</strong>{" "}
            target opportunities achieved
          </p>
          <dl>
            <div>
              <dt>admitted candidates</dt>
              <dd>{run.admitted_count ?? 0}</dd>
            </div>
            <div>
              <dt>evaluated candidates</dt>
              <dd>{run.evaluated_count ?? 0}</dd>
            </div>
            <div>
              <dt>durable commits</dt>
              <dd>{run.committed_count}</dd>
            </div>
            {counters.map(([name, value]) => (
              <div key={name}>
                <dt>{label(name)}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className={styles.card} aria-labelledby="non-counting-heading">
          <h2 id="non-counting-heading">Non-counting outcomes</h2>
          <p>These outcomes do not advance the protected opportunity target.</p>
          <dl>
            {nonCountingOutcomes.map(([name, value]) => (
              <div key={name}>
                <dt>{label(name)}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className={styles.card} aria-labelledby="capacity-heading">
          <h2 id="capacity-heading">Protected capacity</h2>
          {capacityGroups.length > 0 ? (
            capacityGroups.map(([heading, values]) => (
              <div key={heading}>
                <h3>{heading}</h3>
                <dl>
                  {Object.entries(values).map(([name, value]) => (
                    <div key={name}>
                      <dt>{label(name)}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            ))
          ) : (
            <p>Capacity diagnostics are unavailable for this historical run snapshot.</p>
          )}
        </section>

        <section className={styles.card} aria-labelledby="tokens-heading">
          <h2 id="tokens-heading">Model accounting</h2>
          <p>{tokenTotal} total tokens reported</p>
          <h3>By model</h3>
          <dl>
            {Object.entries(byModel).map(([model, tokens]) => (
              <div key={model}>
                <dt>{label(model)}</dt>
                <dd>{tokens} tokens</dd>
              </div>
            ))}
          </dl>
          <h3>By role</h3>
          <dl>
            {Object.entries(byRole).map(([role, tokens]) => (
              <div key={role}>
                <dt>{label(role)}</dt>
                <dd>{tokens} tokens</dd>
              </div>
            ))}
          </dl>
          <details>
            <summary>{usage.length} model calls</summary>
            <ol>
              {usage.map((call) => (
                <li key={`${call.call_index}-${call.role}`}>
                  {label(call.role)}: {call.input_tokens} in / {call.output_tokens} out
                  {call.repair ? " · repair" : ""}
                </li>
              ))}
            </ol>
          </details>
        </section>
      </div>

      {(run.limit_code || run.shortfall_code) && (
        <section className={styles.card} aria-labelledby="outcome-heading">
          <h2 id="outcome-heading">Run outcome diagnostics</h2>
          {run.limit_code && (
            <p>
              Exact protected limit: <strong>{label(run.limit_code)}</strong>
              {run.limit_stage ? ` during ${label(run.limit_stage)}` : ""}.
            </p>
          )}
          {run.shortfall_code && (
            <p role="status">
              Honest shortfall: <strong>{label(run.shortfall_code)}</strong>. {run.shortfall_detail}
            </p>
          )}
        </section>
      )}

      {(run.warning_codes?.length ?? 0) > 0 && (
        <section className={styles.card} aria-labelledby="warnings-heading">
          <h2 id="warnings-heading">Run warnings</h2>
          <ul>
            {run.warning_codes?.map((warning) => (
              <li key={warning}>
                <strong>{label(warning)}</strong>:{" "}
                {warningExplanations[warning] ??
                  "The backend reported a bounded run warning; inspect the authoritative run state before retrying."}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className={styles.card} aria-labelledby="dependency-health-heading">
        <h2 id="dependency-health-heading">Dependency health</h2>
        <p>
          Live health reflects the currently saved local endpoints. This run continues to use the
          immutable endpoint snapshot captured when it started.
        </p>
        {dependencyHealth ? (
          <ul>
            {dependencyHealth.checks.map((check) => (
              <li key={check.dependency}>
                <strong>{label(check.dependency)}</strong>: {label(check.state)} — {check.summary}
              </li>
            ))}
          </ul>
        ) : (
          <p>
            Dependency health is temporarily unavailable; the run snapshot remains authoritative.
          </p>
        )}
      </section>

      <section className={styles.card} aria-labelledby="progress-heading">
        <h2 id="progress-heading">Recent progress</h2>
        {recentProgress.length > 0 ? (
          <ol>
            {recentProgress.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ol>
        ) : (
          <p>No recent stream events are available; the snapshot above is authoritative.</p>
        )}
      </section>
    </main>
  );
}
