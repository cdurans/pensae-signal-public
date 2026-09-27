import { type FormEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import type {
  CapabilityPreflight,
  SavedSettings,
  SavedSettingsResponse,
  SettingsFieldMetadata,
} from "../../api/generated";
import styles from "./SettingsView.module.css";

export type SettingsViewProps = {
  readonly response: SavedSettingsResponse;
  readonly health?: CapabilityPreflight;
  readonly healthUnavailable?: boolean;
  readonly onHealthRetry?: () => void;
  readonly onSave: (values: SavedSettings) => void;
  readonly onReset: () => void;
  readonly savePending: boolean;
  readonly resetPending: boolean;
  readonly mutationMessage?: string;
};

type FieldValues = Record<string, string>;
type FieldErrors = Record<string, string>;

const groupLabels: Record<string, string> = {
  research: "Research focus",
  endpoints: "Local service endpoints",
  workflow: "Workflow bounds",
  logging: "Local log rotation",
};

const protectedPromptInputMaxTokens = 8_192;
const protectedRunRepairs = 8;

function fieldId(key: string): string {
  return `settings-${key.replaceAll(".", "-")}`;
}

function valueAtPath(values: SavedSettings, key: string): unknown {
  let current: unknown = values;
  for (const part of key.split(".")) {
    if (typeof current !== "object" || current === null) {
      return undefined;
    }
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function initialFieldValues(response: SavedSettingsResponse): FieldValues {
  return Object.fromEntries(
    response.fields.map((field) => {
      const value = valueAtPath(response.values, field.key);
      if (Array.isArray(value)) {
        return [field.key, value.join(", ")];
      }
      return [field.key, value === null || value === undefined ? "" : String(value)];
    }),
  );
}

function isNumericField(field: SettingsFieldMetadata): boolean {
  return field.minimum !== null && field.minimum !== undefined;
}

function endpointError(value: string): string | undefined {
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "http:" ||
      parsed.hostname !== "127.0.0.1" ||
      !parsed.port ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      return "Use an HTTP IPv4 loopback URL with an explicit port and no credentials, query, or fragment.";
    }
  } catch {
    return "Enter a valid local HTTP endpoint URL.";
  }
  return undefined;
}

function validateField(
  field: SettingsFieldMetadata,
  value: string,
  values: FieldValues,
): string | undefined {
  if (isNumericField(field)) {
    if (!value.trim()) {
      return "Enter a whole number.";
    }
    const number = Number(value);
    if (!Number.isInteger(number)) {
      return "Enter a whole number.";
    }
    if (field.minimum !== null && field.minimum !== undefined && number < field.minimum) {
      return `Enter ${field.minimum} or greater.`;
    }
    if (field.maximum !== null && field.maximum !== undefined && number > field.maximum) {
      return `Enter ${field.maximum} or less.`;
    }
  }
  if (field.choices && field.choices.length > 0 && !field.choices.includes(value)) {
    return "Choose one of the supported values.";
  }
  if (field.key.startsWith("endpoints.")) {
    return endpointError(value);
  }
  if (field.key === "research.preferred_technologies") {
    const items = value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    if (items.length === 0 || items.length > 12) {
      return "Enter between 1 and 12 comma-separated technologies.";
    }
    if (items.some((item) => item.length > 80)) {
      return "Each technology must be 80 characters or fewer.";
    }
  }
  if (field.key === "research.focus" && value.length > 2_000) {
    return "Research focus must be 2,000 characters or fewer.";
  }
  if (
    field.key === "research.focus" &&
    values["research.discovery_mode"] === "directed" &&
    !value.trim()
  ) {
    return "Directed discovery requires a research focus.";
  }
  return undefined;
}

function validateRelationships(values: FieldValues, protectedTarget: number): FieldErrors {
  const errors: FieldErrors = {};
  const requireAtMost = (left: string, right: string, leftLabel: string, rightLabel: string) => {
    const leftValue = Number(values[left]);
    const rightValue = Number(values[right]);
    if (Number.isInteger(leftValue) && Number.isInteger(rightValue) && leftValue > rightValue) {
      errors[left] = `${leftLabel} cannot exceed ${rightLabel}.`;
      errors[right] = `${rightLabel} must be at least ${leftLabel}.`;
    }
  };
  requireAtMost(
    "workflow.discovery_queries",
    "workflow.run_queries",
    "Discovery queries",
    "Total run queries",
  );
  requireAtMost(
    "workflow.first_pass_pages",
    "workflow.run_pages",
    "First-pass retrieved pages",
    "Total run retrieved pages",
  );
  const numberAt = (key: string): number | undefined => {
    const value = Number(values[key]);
    return Number.isInteger(value) ? value : undefined;
  };
  const concepts = numberAt("workflow.concepts");
  const survivors = numberAt("workflow.preliminary_survivors");
  if (concepts !== undefined && survivors !== undefined && survivors < concepts) {
    errors["workflow.preliminary_survivors"] =
      `Preliminary survivors must be at least ${concepts} to cover every solution concept.`;
    errors["workflow.concepts"] = "Solution concepts cannot exceed the preliminary-survivor pool.";
  }
  if (concepts !== undefined && concepts < protectedTarget) {
    errors["workflow.concepts"] =
      `Solution concepts must be at least the protected target of ${protectedTarget}.`;
  }

  const discoveryQueries = numberAt("workflow.discovery_queries");
  const focusedQueries = numberAt("workflow.focused_queries_per_concept");
  const runQueries = numberAt("workflow.run_queries");
  if (discoveryQueries !== undefined && focusedQueries !== undefined && runQueries !== undefined) {
    const required = discoveryQueries + protectedTarget * focusedQueries;
    if (runQueries < required) {
      errors["workflow.run_queries"] =
        `Total run queries must be at least ${required} to preserve ${protectedTarget} focused slots.`;
    }
  }

  const firstPassPages = numberAt("workflow.first_pass_pages");
  const focusedPages = numberAt("workflow.focused_pages_per_concept");
  const runPages = numberAt("workflow.run_pages");
  if (firstPassPages !== undefined && focusedPages !== undefined && runPages !== undefined) {
    const required = firstPassPages + protectedTarget * focusedPages;
    if (runPages < required) {
      errors["workflow.run_pages"] =
        `Total run pages must be at least ${required} to preserve ${protectedTarget} focused slots.`;
    }
  }

  const patterns = numberAt("workflow.patterns");
  const segmentsPerPattern = numberAt("workflow.segments_per_pattern");
  if (
    patterns !== undefined &&
    segmentsPerPattern !== undefined &&
    patterns * segmentsPerPattern < protectedTarget
  ) {
    errors["workflow.patterns"] =
      `Problem patterns must provide at least ${protectedTarget} candidate segments with the saved segments-per-pattern value.`;
    errors["workflow.segments_per_pattern"] =
      `Patterns multiplied by segments per pattern must reach the protected target of ${protectedTarget}.`;
  }

  const modelCalls = numberAt("workflow.model_calls");
  if (
    firstPassPages !== undefined &&
    patterns !== undefined &&
    concepts !== undefined &&
    modelCalls !== undefined
  ) {
    const upstream = firstPassPages + concepts + 2 + 3 * patterns;
    const required = upstream + protectedTarget * 4 + protectedRunRepairs;
    if (modelCalls < required) {
      errors["workflow.model_calls"] =
        `Total model calls must be at least ${required} for the protected target.`;
    }
  }

  const plannerTokens = numberAt("workflow.planner_output_max_tokens");
  const problemTokens = numberAt("workflow.problem_analyst_output_max_tokens");
  const productTokens = numberAt("workflow.product_strategist_output_max_tokens");
  const opportunityTokens = numberAt("workflow.opportunity_analyst_output_max_tokens");
  const totalTokens = numberAt("workflow.total_run_tokens");
  if (
    firstPassPages !== undefined &&
    plannerTokens !== undefined &&
    problemTokens !== undefined &&
    productTokens !== undefined &&
    opportunityTokens !== undefined &&
    patterns !== undefined &&
    concepts !== undefined &&
    totalTokens !== undefined
  ) {
    const upstreamCalls = firstPassPages + concepts + 2 + 3 * patterns;
    const upstream =
      upstreamCalls * protectedPromptInputMaxTokens +
      plannerTokens +
      firstPassPages * problemTokens +
      patterns * (2 * problemTokens + productTokens) +
      concepts * productTokens;
    const perSlot =
      4 * protectedPromptInputMaxTokens + plannerTokens + problemTokens + opportunityTokens;
    const largestRepair =
      protectedPromptInputMaxTokens + Math.max(plannerTokens, problemTokens, opportunityTokens);
    const required = upstream + protectedTarget * perSlot + protectedRunRepairs * largestRepair;
    if (totalTokens < required) {
      errors["workflow.total_run_tokens"] =
        `Total run tokens must be at least ${required.toLocaleString("en-US")} for the protected target.`;
    }
  }
  return errors;
}

function setValueAtPath(target: Record<string, unknown>, key: string, value: unknown): void {
  const parts = key.split(".");
  let current = target;
  for (const part of parts.slice(0, -1)) {
    const next = current[part];
    if (typeof next === "object" && next !== null && !Array.isArray(next)) {
      current = next as Record<string, unknown>;
    } else {
      const created: Record<string, unknown> = {};
      current[part] = created;
      current = created;
    }
  }
  current[parts.at(-1) ?? key] = value;
}

function savedSettingsFromFields(
  response: SavedSettingsResponse,
  values: FieldValues,
): SavedSettings {
  const next = structuredClone(response.values) as Record<string, unknown>;
  for (const field of response.fields) {
    const value = values[field.key] ?? "";
    let typedValue: unknown = value;
    if (isNumericField(field)) {
      typedValue = Number(value);
    } else if (field.key === "research.preferred_technologies") {
      typedValue = value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
    } else if (field.key === "research.focus") {
      typedValue = value.trim() || null;
    }
    setValueAtPath(next, field.key, typedValue);
  }
  return next as SavedSettings;
}

function SettingsField({
  field,
  value,
  error,
  onChange,
}: {
  readonly field: SettingsFieldMetadata;
  readonly value: string;
  readonly error?: string;
  readonly onChange: (value: string) => void;
}) {
  const id = fieldId(field.key);
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const common = {
    id,
    value,
    onChange: (
      event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>,
    ) => onChange(event.currentTarget.value),
    "aria-invalid": Boolean(error),
    "aria-describedby": `${helpId}${error ? ` ${errorId}` : ""}`,
  };

  return (
    <div className={styles.field}>
      <label htmlFor={id}>{field.label}</label>
      {field.choices && field.choices.length > 0 ? (
        <select {...common}>
          {field.choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
      ) : field.key === "research.focus" || field.key === "research.preferred_technologies" ? (
        <textarea {...common} rows={field.key === "research.focus" ? 4 : 2} />
      ) : (
        <input
          {...common}
          type={
            isNumericField(field) ? "number" : field.key.startsWith("endpoints.") ? "url" : "text"
          }
          min={field.minimum ?? undefined}
          max={field.maximum ?? undefined}
          step={isNumericField(field) ? 1 : undefined}
          required={field.key !== "research.focus"}
        />
      )}
      <div id={helpId} className={styles.fieldHelp}>
        <span>{field.validation}</span>
        <span>Protected default: {field.protected_default_explanation}</span>
      </div>
      {error && (
        <p id={errorId} className={styles.fieldError} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function SettingsView({
  response,
  health,
  healthUnavailable = false,
  onHealthRetry,
  onSave,
  onReset,
  savePending,
  resetPending,
  mutationMessage,
}: SettingsViewProps) {
  const heading = useRef<HTMLHeadingElement>(null);
  const resetTrigger = useRef<HTMLButtonElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const appliedRevision = useRef(response.revision);
  const [values, setValues] = useState<FieldValues>(() => initialFieldValues(response));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [confirmReset, setConfirmReset] = useState(false);
  const protectedTarget = response.values.workflow?.opportunities ?? 5;

  useEffect(() => heading.current?.focus(), []);
  useEffect(() => {
    if (response.revision <= appliedRevision.current) {
      return;
    }
    appliedRevision.current = response.revision;
    setValues(initialFieldValues(response));
    setErrors({});
  }, [response]);
  useEffect(() => {
    if (confirmReset) {
      confirmButton.current?.focus();
    }
  }, [confirmReset]);

  const groupedFields = useMemo(() => {
    const groups = new Map<string, SettingsFieldMetadata[]>();
    for (const field of response.fields) {
      const group = field.key.split(".")[0] ?? "settings";
      groups.set(group, [...(groups.get(group) ?? []), field]);
    }
    return [...groups.entries()];
  }, [response.fields]);

  const closeReset = () => {
    setConfirmReset(false);
    resetTrigger.current?.focus();
  };

  const handleDialogKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeReset();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    if (event.shiftKey && document.activeElement === confirmButton.current) {
      event.preventDefault();
      cancelButton.current?.focus();
    } else if (!event.shiftKey && document.activeElement === cancelButton.current) {
      event.preventDefault();
      confirmButton.current?.focus();
    }
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextErrors: FieldErrors = {
      ...validateRelationships(values, protectedTarget),
      ...Object.fromEntries(
        response.fields.flatMap((field) => {
          const error = validateField(field, values[field.key] ?? "", values);
          return error ? [[field.key, error]] : [];
        }),
      ),
    };
    setErrors(nextErrors);
    const firstInvalid = response.fields.find((field) => nextErrors[field.key]);
    if (firstInvalid) {
      document.getElementById(fieldId(firstInvalid.key))?.focus();
      return;
    }
    onSave(savedSettingsFromFields(response, values));
  };

  return (
    <main id="main-content" className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>Future-run configuration · Revision {response.revision}</p>
        <h1 ref={heading} tabIndex={-1}>
          Settings
        </h1>
        <p>
          Saved values apply only to future research runs. There is no temporary per-run override,
          and protected security, scoring, model, and launcher policy is not editable here.
        </p>
      </header>

      <section className={styles.health} aria-labelledby="settings-health-heading">
        <div className={styles.sectionHeading}>
          <h2 id="settings-health-heading">Required-service health</h2>
          <p className={styles.healthSummary} role="status">
            {healthUnavailable
              ? "Health check unavailable"
              : health
                ? health.ready
                  ? "Ready for research"
                  : "Research start is blocked"
                : "Health is loading"}
          </p>
        </div>
        {healthUnavailable ? (
          <div className={styles.blockers} role="alert">
            <h3>Dependency health could not be loaded</h3>
            <p>
              Saved future-run settings remain usable. Run <code>make status</code> in Fedora,
              repair the local health API, then retry this check.
            </p>
            {onHealthRetry && (
              <button type="button" onClick={onHealthRetry}>
                Retry health check
              </button>
            )}
          </div>
        ) : !health ? (
          <p>Dependency checks are not available yet. Saved settings remain readable.</p>
        ) : (
          <>
            <ul className={styles.healthList}>
              {health.checks.map((check) => (
                <li key={check.dependency}>
                  <strong>{check.dependency.replaceAll("_", " ")}</strong>
                  <span>State: {check.state.replaceAll("_", " ")}</span>
                  <span>{check.summary}</span>
                  {check.action && <span>Action: {check.action}</span>}
                </li>
              ))}
            </ul>
            {health.blockers.length > 0 && (
              <div className={styles.blockers} role="alert">
                <h3>Why research cannot start</h3>
                <ul>
                  {health.blockers.map((blocker) => (
                    <li key={`${blocker.dependency}-${blocker.reason}`}>
                      <strong>{blocker.dependency.replaceAll("_", " ")}:</strong> {blocker.reason}
                      {blocker.action ? ` Action: ${blocker.action}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </section>

      <form className={styles.form} onSubmit={submit} noValidate>
        <section className={styles.group} aria-labelledby="protected-target-heading">
          <h2 id="protected-target-heading">Protected opportunity target</h2>
          <p>
            <strong>{protectedTarget}</strong> distinct, complete opportunities per representative
            sufficient-evidence run. This target is read-only; editable work bounds must preserve
            capacity for every remaining slot.
          </p>
        </section>
        {Object.keys(errors).length > 0 && (
          <div className={styles.errorSummary} role="alert">
            <h2>Review settings</h2>
            <p>{Object.keys(errors).length} fields need attention before settings can be saved.</p>
          </div>
        )}
        {groupedFields.map(([group, fields]) => (
          <fieldset key={group} className={styles.group}>
            <legend>{groupLabels[group] ?? group}</legend>
            <div className={styles.fieldGrid}>
              {fields.map((field) => (
                <SettingsField
                  key={field.key}
                  field={field}
                  value={values[field.key] ?? ""}
                  error={errors[field.key]}
                  onChange={(value) => {
                    setValues((current) => ({ ...current, [field.key]: value }));
                    setErrors((current) => {
                      const next = { ...current };
                      delete next[field.key];
                      return next;
                    });
                  }}
                />
              ))}
            </div>
          </fieldset>
        ))}

        <div className={styles.actions}>
          <button type="submit" disabled={savePending || resetPending}>
            {savePending ? "Saving settings…" : "Save future-run settings"}
          </button>
          <button
            ref={resetTrigger}
            type="button"
            className={styles.secondaryButton}
            disabled={savePending || resetPending}
            onClick={() => setConfirmReset(true)}
          >
            Reset to protected defaults
          </button>
        </div>
        <p className={styles.liveMessage} role="status" aria-live="polite" aria-atomic="true">
          {mutationMessage ?? (resetPending ? "Restoring protected defaults…" : "")}
        </p>
      </form>

      {confirmReset && (
        <div className={styles.dialogBackdrop}>
          <div
            className={styles.dialog}
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="reset-dialog-title"
            aria-describedby="reset-dialog-description"
            onKeyDown={handleDialogKeyDown}
          >
            <h2 id="reset-dialog-title">Reset future-run settings?</h2>
            <p id="reset-dialog-description">
              This reversible action copies the protected shipped defaults into saved settings.
              Existing runs and retained opportunities are not changed. You can edit and save the
              settings again later.
            </p>
            <div className={styles.actions}>
              <button
                ref={confirmButton}
                type="button"
                onClick={() => {
                  onReset();
                  closeReset();
                }}
              >
                Confirm reset to protected defaults
              </button>
              <button
                ref={cancelButton}
                type="button"
                className={styles.secondaryButton}
                onClick={closeReset}
              >
                Keep current settings
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
