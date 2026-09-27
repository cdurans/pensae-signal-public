import styles from "./HealthDashboard.module.css";
import type { DependencyHealth, PreflightSnapshot } from "./model";
import { getStartEligibility, healthStateLabels, healthStateSymbols } from "./model";

interface HealthDashboardProps {
  readonly snapshot: PreflightSnapshot;
  readonly onRetryPreflight?: () => void;
  readonly preflightPending?: boolean;
  readonly onStartResearch: () => void;
  readonly startPending?: boolean;
  readonly startError?: string;
}

function DependencyRow({ dependency }: { readonly dependency: DependencyHealth }) {
  const stateLabel = healthStateLabels[dependency.state];

  return (
    <li className={styles.healthItem}>
      <p className={styles.dependencyName}>{dependency.label}</p>
      <span className={`${styles.statusBadge} ${styles[dependency.state]}`}>
        <span className={styles.statusSymbol} aria-hidden="true">
          {healthStateSymbols[dependency.state]}
        </span>
        {stateLabel}
      </span>
      <p className={styles.detail}>{dependency.detail}</p>
    </li>
  );
}

export function HealthDashboard({
  snapshot,
  onRetryPreflight,
  preflightPending = false,
  onStartResearch,
  startPending = false,
  startError,
}: HealthDashboardProps) {
  const eligibility = getStartEligibility(snapshot);
  const blockerHeadingId = "preflight-blockers-heading";
  const buttonDescriptionId = "start-research-description";
  const checkedAt = new Date(snapshot.checkedAt);
  const timestamp = Number.isNaN(checkedAt.valueOf())
    ? snapshot.checkedAt
    : checkedAt.toLocaleString();

  return (
    <main id="main-content" className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>Local research workspace</p>
        <h1 className={styles.title}>Pensae Signal readiness</h1>
        <p className={styles.intro}>
          Review every local dependency before beginning an evidence-first research run. Pensae
          Signal stays available for diagnosis when a dependency is degraded.
        </p>
      </header>

      <div className={styles.layout}>
        <section className={styles.panel} aria-labelledby="dependency-heading">
          <div className={styles.panelHeader}>
            <h2 id="dependency-heading" className={styles.panelTitle}>
              Required dependencies
            </h2>
            <p className={styles.timestamp}>Checked {timestamp}</p>
          </div>
          <ul className={styles.healthList} aria-live="polite">
            {snapshot.dependencies.map((dependency) => (
              <DependencyRow key={dependency.id} dependency={dependency} />
            ))}
          </ul>
        </section>

        <section
          className={`${styles.panel} ${styles.sidePanel}`}
          aria-labelledby="readiness-heading"
          aria-live="polite"
        >
          <h2 id="readiness-heading" className={styles.panelTitle}>
            Research preflight
          </h2>
          <p className={styles.readinessLine} role="status">
            <span
              className={`${styles.readinessIcon} ${
                eligibility.canStart ? styles.readinessIconReady : ""
              }`}
              aria-hidden="true"
            >
              {eligibility.canStart ? "✓" : "!"}
            </span>
            {eligibility.canStart ? "Ready to start" : "Start blocked"}
          </p>
          <p id={buttonDescriptionId} className={styles.summary}>
            {eligibility.canStart
              ? "All required local capabilities passed preflight."
              : `${eligibility.reasons.length} blocking ${
                  eligibility.reasons.length === 1 ? "reason must" : "reasons must"
                } be resolved.`}
          </p>

          {!eligibility.canStart && (
            <div>
              <h3 id={blockerHeadingId} className={styles.blockerHeading}>
                Blocking reasons
              </h3>
              <ul className={styles.blockerList} aria-labelledby={blockerHeadingId}>
                {eligibility.reasons.map((reason, index) => {
                  const blocker = snapshot.blockers[index];
                  return (
                    <li className={styles.blockerItem} key={blocker?.id ?? reason}>
                      {blocker === undefined ? (
                        <p className={styles.blockerSummary}>{reason}</p>
                      ) : (
                        <>
                          <p className={styles.blockerSummary}>{blocker.summary}</p>
                          <p className={styles.blockerAction}>{blocker.action}</p>
                        </>
                      )}
                    </li>
                  );
                })}
              </ul>
              {onRetryPreflight && (
                <button type="button" onClick={onRetryPreflight} disabled={preflightPending}>
                  {preflightPending ? "Checking dependencies" : "Retry preflight"}
                </button>
              )}
            </div>
          )}

          <button
            className={styles.startButton}
            type="button"
            disabled={!eligibility.canStart || startPending}
            aria-describedby={buttonDescriptionId}
            onClick={onStartResearch}
          >
            {startPending ? "Starting research" : "Start research"}
          </button>
          {startError && <p role="alert">Research could not start: {startError}</p>}
        </section>
      </div>
    </main>
  );
}
