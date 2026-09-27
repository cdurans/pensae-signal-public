import { useEffect, useMemo, useRef, useState } from "react";
import type { LifecycleTarget, OpportunityDetail } from "../../api/generated";
import styles from "./OpportunityLifecyclePanel.module.css";

type Decision = "related" | "rediscovered" | "updated";
type DialogAction =
  | { readonly kind: "rediscovery"; readonly decision: Decision }
  | { readonly kind: "merge" }
  | { readonly kind: "reverse" }
  | { readonly kind: "delete" };

export type OpportunityLifecyclePanelProps = {
  readonly opportunity: OpportunityDetail;
  readonly mergeTargets: readonly LifecycleTarget[];
  readonly mergeSearch: string;
  readonly mergeHasMore?: boolean;
  readonly historical: boolean;
  readonly pending?: boolean;
  readonly message?: string;
  readonly onMergeSearch: (search: string) => void;
  readonly onRediscovery: (decision: Decision, target: LifecycleTarget) => void;
  readonly onMerge: (survivor: LifecycleTarget) => void;
  readonly onReverse: () => void;
  readonly onDelete: (confirmation: string) => void;
};

export function OpportunityLifecyclePanel({
  opportunity,
  mergeTargets,
  mergeSearch,
  mergeHasMore = false,
  historical,
  pending = false,
  message,
  onMergeSearch,
  onRediscovery,
  onMerge,
  onReverse,
  onDelete,
}: OpportunityLifecyclePanelProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const dialogPanel = useRef<HTMLDivElement>(null);
  const [dialog, setDialog] = useState<DialogAction>();
  const [targetId, setTargetId] = useState("");
  const [typedDelete, setTypedDelete] = useState("");
  const relatedTargets = useMemo(
    () =>
      (opportunity.related ?? [])
        .filter((relation) => relation.lifecycle_status === "active")
        .map((relation) => ({
          id: relation.opportunity_id,
          opportunity_name: relation.opportunity_name,
          primary_industry: relation.primary_industry,
          revision: relation.revision,
          lifecycle_status: relation.lifecycle_status,
        })),
    [opportunity.related],
  );
  const selectedPool = dialog?.kind === "merge" ? mergeTargets : relatedTargets;
  const selectedTarget = selectedPool.find((item) => item.id === targetId) ?? selectedPool[0];
  const deletePhrase = `DELETE ${opportunity.id}`;

  useEffect(() => {
    if (!dialog) return;
    setTargetId("");
    setTypedDelete("");
    const panel = dialogPanel.current;
    const focusableSelector = "select:not(:disabled), input:not(:disabled), button:not(:disabled)";
    panel?.querySelector<HTMLElement>(focusableSelector)?.focus();
    const handleDialogKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setDialog(undefined);
        trigger.current?.focus();
      } else if (event.key === "Tab" && panel) {
        const focusable = Array.from(panel.querySelectorAll<HTMLElement>(focusableSelector));
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", handleDialogKeyDown);
    return () => document.removeEventListener("keydown", handleDialogKeyDown);
  }, [dialog]);

  const close = () => {
    setDialog(undefined);
    trigger.current?.focus();
  };
  const open = (action: DialogAction, button: HTMLButtonElement) => {
    trigger.current = button;
    setDialog(action);
  };
  const submit = () => {
    if (!dialog) return;
    if (dialog.kind === "rediscovery" && selectedTarget) {
      onRediscovery(dialog.decision, selectedTarget);
    } else if (dialog.kind === "merge" && selectedTarget) {
      onMerge(selectedTarget);
    } else if (dialog.kind === "reverse") {
      onReverse();
    } else if (dialog.kind === "delete") {
      onDelete(typedDelete);
    }
    close();
  };

  return (
    <section className={styles.panel} aria-labelledby="lifecycle-heading">
      <h2 id="lifecycle-heading">Lifecycle actions</h2>
      <p>
        Lifecycle policy is application-owned. Semantic similarity never updates or merges an
        opportunity automatically, and every accepted mutation refreshes the authoritative record.
      </p>
      <ul className={styles.statusGrid}>
        <li>
          <strong>Classification</strong>
          <br />
          {(opportunity.classification ?? "new").replaceAll("_", " ")}
        </li>
        <li>
          <strong>Stable revision</strong>
          <br />
          {opportunity.revision}
        </li>
        <li>
          <strong>Record status</strong>
          <br />
          {opportunity.lifecycle_status === "merged"
            ? `Merged into ${opportunity.merge_target_id}`
            : "Independent active record"}
        </li>
      </ul>
      {historical ? (
        <p className={styles.warning}>
          You are inspecting an immutable historical version. Return to the current version before
          taking a lifecycle action.
        </p>
      ) : (
        <div className={styles.actions}>
          {opportunity.lifecycle_status === "merged" ? (
            <button
              ref={trigger}
              type="button"
              disabled={pending}
              onClick={(event) => open({ kind: "reverse" }, event.currentTarget)}
            >
              Reverse merge
            </button>
          ) : (
            <>
              {opportunity.classification === "possible_rediscovery" &&
              relatedTargets.length > 0 ? (
                <>
                  <button
                    ref={trigger}
                    type="button"
                    disabled={pending}
                    onClick={(event) =>
                      open({ kind: "rediscovery", decision: "related" }, event.currentTarget)
                    }
                  >
                    Leave relationship Related
                  </button>
                  <button
                    type="button"
                    disabled={pending}
                    onClick={(event) =>
                      open({ kind: "rediscovery", decision: "rediscovered" }, event.currentTarget)
                    }
                  >
                    Confirm Rediscovered
                  </button>
                  <button
                    type="button"
                    disabled={pending}
                    onClick={(event) =>
                      open({ kind: "rediscovery", decision: "updated" }, event.currentTarget)
                    }
                  >
                    Create immutable Updated version
                  </button>
                </>
              ) : opportunity.classification === "possible_rediscovery" ? (
                <p>No Related or Possible rediscovery target is available for review.</p>
              ) : (
                <p>This opportunity already has a recorded lifecycle classification.</p>
              )}
              <button
                ref={relatedTargets.length === 0 ? trigger : undefined}
                type="button"
                disabled={pending || mergeTargets.length === 0}
                onClick={(event) => open({ kind: "merge" }, event.currentTarget)}
              >
                Merge into selected survivor
              </button>
            </>
          )}
          <button
            type="button"
            className={styles.danger}
            disabled={pending}
            onClick={(event) => open({ kind: "delete" }, event.currentTarget)}
          >
            Permanently delete opportunity
          </button>
        </div>
      )}
      <p className={styles.liveMessage} role="status" aria-live="polite" aria-atomic="true">
        {message ?? (pending ? "Applying lifecycle action…" : "")}
      </p>

      {dialog && (
        <div className={styles.dialogBackdrop}>
          <div
            ref={dialogPanel}
            className={styles.dialog}
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="lifecycle-dialog-title"
            aria-describedby="lifecycle-dialog-description"
          >
            <h2 id="lifecycle-dialog-title">
              {dialog.kind === "delete"
                ? "Permanently delete this opportunity?"
                : "Confirm lifecycle action"}
            </h2>
            <p id="lifecycle-dialog-description">
              {dialog.kind === "rediscovery" &&
                "This records an operator decision. Updated creates a new immutable version on the selected stable opportunity; favorite and note stay with that stable record."}
              {dialog.kind === "merge" &&
                "The selected survivor remains independent. This record and all versions, evidence, favorite, and note remain preserved without automatic combination."}
              {dialog.kind === "reverse" &&
                "Reversal restores this record as independent and records a durable lifecycle event."}
              {dialog.kind === "delete" &&
                "Deletion is permanent. This stable record, versions, relations, favorite, note, and exclusively owned support are removed. Shared support remains referenced by retained opportunities."}
            </p>
            {(dialog.kind === "rediscovery" || dialog.kind === "merge") && (
              <div className={styles.field}>
                {dialog.kind === "merge" && (
                  <>
                    <label htmlFor="merge-target-search">Search all active opportunities</label>
                    <input
                      id="merge-target-search"
                      value={mergeSearch}
                      onChange={(event) => onMergeSearch(event.currentTarget.value)}
                      autoComplete="off"
                    />
                    {mergeHasMore && (
                      <small>More matches exist. Refine the name or stable UUID search.</small>
                    )}
                  </>
                )}
                <label htmlFor="lifecycle-target">Selected stable opportunity</label>
                <select
                  id="lifecycle-target"
                  value={selectedTarget?.id ?? ""}
                  onChange={(event) => setTargetId(event.currentTarget.value)}
                >
                  {selectedPool.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.opportunity_name} · Revision {item.revision}
                    </option>
                  ))}
                </select>
              </div>
            )}
            {dialog.kind === "delete" && (
              <div className={styles.field}>
                <label htmlFor="delete-confirmation">
                  Type <strong>{deletePhrase}</strong> to confirm
                </label>
                <input
                  id="delete-confirmation"
                  value={typedDelete}
                  onChange={(event) => setTypedDelete(event.currentTarget.value)}
                  autoComplete="off"
                />
              </div>
            )}
            <div className={styles.dialogActions}>
              <button
                type="button"
                className={dialog.kind === "delete" ? styles.danger : undefined}
                disabled={
                  pending ||
                  ((dialog.kind === "rediscovery" || dialog.kind === "merge") && !selectedTarget) ||
                  (dialog.kind === "delete" && typedDelete !== deletePhrase)
                }
                onClick={submit}
              >
                {dialog.kind === "delete" ? "Delete permanently" : "Confirm action"}
              </button>
              <button type="button" className={styles.secondary} onClick={close}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
