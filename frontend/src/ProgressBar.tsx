import { MissionDetail, SiteContext, Task } from "./api";

type StepState = "done" | "current" | "waiting" | "failed" | "skipped" | "upcoming";

interface Step {
  label: string;
  state: StepState;
  note: string;
}

const ICON: Record<StepState, string> = {
  done: "✓",
  current: "●",
  waiting: "◐",
  failed: "✗",
  skipped: "–",
  upcoming: "",
};

const TERMINAL = ["resolved", "unresolved", "no_action_needed", "cancelled"];

interface Item {
  status: string;
  role: string;
  requires_approval: boolean;
  task?: Task;
}

/** Step state for a group of tasks with the same role. */
function groupStep(label: string, items: Item[], names: Map<string, string>): Step {
  const statuses = items.map((i) => i.status);
  const tasks = items.flatMap((i) => (i.task ? [i.task] : []));
  const retried = tasks.some((t) => t.attempts.length > 1);
  const retryNote = retried ? " ↻ retried" : "";
  const runningOn = tasks
    .flatMap((t) => t.attempts.filter((a) => a.status === "running").map((a) => names.get(a.robot_id) ?? a.robot_id))
    .join(", ");
  const findings = tasks.map((t) => t.finding).filter(Boolean);

  if (statuses.some((s) => s === "failed" || s === "needs_intervention"))
    return { label, state: "failed", note: "needs your decision" };
  if (statuses.includes("running")) return { label, state: "current", note: `${runningOn || "running"}${retryNote}` };
  if (statuses.includes("waiting_resource")) return { label, state: "waiting", note: "no robot free" };
  if (statuses.length && statuses.every((s) => s === "skipped" || s === "cancelled" || s === "rejected")) {
    // "Not needed" only when a condition was not met (no defect); otherwise it was blocked.
    const conditionUnmet =
      tasks.length > 0 &&
      tasks.every((t) => t.status_reason.startsWith("Condition not met") || t.status_reason.endsWith("was skipped"));
    const note = statuses.includes("cancelled")
      ? "cancelled"
      : statuses.includes("rejected")
        ? "rejected"
        : conditionUnmet || tasks.length === 0
          ? "not needed"
          : "skipped";
    return { label, state: "skipped", note };
  }
  if (statuses.length && statuses.every((s) => s === "succeeded" || s === "skipped")) {
    if (findings.includes("defect_persists")) return { label, state: "failed", note: "defect persists" };
    const note = findings.includes("defect_detected")
      ? "defect found"
      : findings.includes("repair_verified")
        ? "verified"
        : findings.includes("repair_applied")
          ? "applied"
          : findings.includes("no_defect")
            ? "no defect"
            : "done";
    return { label, state: "done", note: note + retryNote };
  }
  return { label, state: "upcoming", note: retried ? retryNote.trim() : "" };
}

export function buildSteps(detail: MissionDetail, site: SiteContext | null, names: Map<string, string>): Step[] {
  const m = detail.mission;
  const roleOf = new Map((site?.actions ?? []).map((a) => [a.name, a] as const));
  const started = detail.tasks.length > 0;
  const items: Item[] = started
    ? detail.tasks.map((t) => ({ status: t.status, role: t.role, requires_approval: t.requires_approval, task: t }))
    : (detail.current_plan?.plan.tasks ?? []).map((t) => ({
        status: "pending",
        role: roleOf.get(t.action)?.role ?? "",
        requires_approval: roleOf.get(t.action)?.requires_approval ?? false,
      }));

  const steps: Step[] = [];
  const source = detail.current_plan?.source;
  if (m.status === "planning") steps.push({ label: "Plan", state: "current", note: "generating" });
  else if (m.status === "planning_failed" && !detail.current_plan)
    steps.push({ label: "Plan", state: "failed", note: "planning failed" });
  else steps.push({ label: "Plan", state: "done", note: source === "llm" ? "by LLM" : source === "fixture" ? "fixture" : "edited" });

  if (!started) {
    const ready = m.status === "ready";
    steps.push({ label: "Start", state: ready ? "waiting" : "upcoming", note: ready ? "review, then start" : "" });
  } else {
    steps.push({ label: "Start", state: "done", note: "" });
  }

  const byRole = (role: string) => items.filter((i) => i.role === role);
  if (byRole("inspection").length) steps.push(groupStep("Inspect", byRole("inspection"), names));

  const gated = items.filter((i) => i.requires_approval);
  if (gated.length) {
    const statuses = gated.map((i) => i.status);
    const decided = detail.approvals.filter((a) => gated.some((g) => g.task?.task_id === a.task_id));
    const last = decided[decided.length - 1];
    let step: Step;
    if (statuses.includes("awaiting_approval")) step = { label: "Approval", state: "waiting", note: "waiting for you" };
    else if (statuses.includes("rejected")) step = { label: "Approval", state: "failed", note: `rejected by ${last?.actor ?? "operator"}` };
    else if (last?.decision === "approve") step = { label: "Approval", state: "done", note: `approved by ${last.actor}` };
    else if (statuses.length && statuses.every((s) => s === "skipped" || s === "cancelled"))
      step = {
        label: "Approval",
        state: "skipped",
        note: gated.every((g) => g.task?.status_reason.startsWith("Condition not met")) ? "not needed" : "skipped",
      };
    else step = { label: "Approval", state: "upcoming", note: "" };
    steps.push(step);
  }

  const interventions = byRole("intervention").map((i) =>
    // Waiting on approval is shown on the Approval step, not as repair progress.
    i.status === "awaiting_approval" ? { ...i, status: "pending" } : i,
  );
  if (interventions.length) steps.push(groupStep("Repair", interventions, names));
  if (byRole("verification").length) steps.push(groupStep("Verify", byRole("verification"), names));

  if (TERMINAL.includes(m.status)) {
    const state: StepState = m.status === "unresolved" ? "failed" : m.status === "cancelled" ? "skipped" : "done";
    steps.push({ label: "Outcome", state, note: m.status.replace(/_/g, " ") });
  } else {
    steps.push({ label: "Outcome", state: "upcoming", note: "" });
  }
  return steps;
}

export function ProgressBar({ steps }: { steps: Step[] }) {
  return (
    <ol className="progress" aria-label="Mission progress">
      {steps.map((s, i) => (
        <li key={s.label} className={`step step-${s.state}`} aria-current={s.state === "current" || s.state === "waiting"}>
          <span className="step-dot">{ICON[s.state] || i + 1}</span>
          <span className="step-text">
            <span className="step-label">{s.label}</span>
            {s.note && <span className="step-note">{s.note}</span>}
          </span>
        </li>
      ))}
    </ol>
  );
}
