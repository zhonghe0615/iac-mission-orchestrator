import { useState } from "react";
import { ApiError } from "./api";

const TONE: Record<string, string> = {
  planning: "info",
  ready: "info",
  running: "info",
  pending: "muted",
  waiting_approval: "warn",
  awaiting_approval: "warn",
  waiting_resource: "warn",
  needs_intervention: "danger",
  planning_failed: "danger",
  failed: "danger",
  timed_out: "danger",
  lost: "danger",
  cancel_requested: "warn",
  cancel_unconfirmed: "danger",
  resolved: "ok",
  succeeded: "ok",
  no_action_needed: "ok",
  unresolved: "danger",
  rejected: "danger",
  skipped: "muted",
  cancelled: "muted",
  available: "ok",
  busy: "info",
  offline: "danger",
  unresponsive: "danger",
};

export function label(value: string): string {
  return value.replace(/_/g, " ");
}

export function Pill({ value }: { value: string }) {
  return <span className={`pill tone-${TONE[value] ?? "muted"}`}>{label(value)}</span>;
}

export function time(ts: number | null | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function seconds(from: number, to: number | null): string {
  return to ? `${(to - from).toFixed(1)}s` : "";
}

/** Run an API action, tracking busy state and surfacing errors next to the control. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; errors: string[] } | null>(null);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      return true;
    } catch (e) {
      setError(
        e instanceof ApiError ? { message: e.message, errors: e.errors } : { message: String(e), errors: [] },
      );
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, clear: () => setError(null) };
}

export function ErrorBox({ error }: { error: { message: string; errors: string[] } | null }) {
  if (!error) return null;
  return (
    <div className="error-box" role="alert">
      <strong>{error.message}</strong>
      {error.errors.length > 0 && (
        <ul>
          {error.errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
