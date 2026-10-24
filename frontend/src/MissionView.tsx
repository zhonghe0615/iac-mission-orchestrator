import { useState } from "react";
import { api, AuditEvent, Evidence, MissionDetail, Plan, Robot, SiteContext, Task } from "./api";
import { buildSteps, ProgressBar } from "./ProgressBar";
import { ErrorBox, label, Pill, seconds, time, useAction } from "./ui";

const PRE_START = ["ready", "planning_failed"];
const TERMINAL = ["resolved", "unresolved", "no_action_needed", "cancelled"];

/** What is happening, why, and what the operator can do, in plain words. */
function explain(detail: MissionDetail): { headline: string; reason: string; next: string } {
  const m = detail.mission;
  const reason = m.status_reason;
  switch (m.status) {
    case "planning":
      return { headline: "Generating a plan", reason: "The planner is working on the work order.", next: "Wait." };
    case "planning_failed":
      return {
        headline: "Planning failed",
        reason,
        next: "Submit the work order again, or write a plan in the editor below. It will be validated.",
      };
    case "ready":
      return {
        headline: `Plan v${m.plan_version} is valid and waiting for review`,
        reason: "Nothing has been sent to any robot.",
        next: "Review or edit the plan, then start the mission.",
      };
    case "running":
      return { headline: "Executing", reason, next: "No action needed. Inject a fault from the Fleet panel to test recovery." };
    case "waiting_approval":
      return { headline: "Waiting for your approval", reason, next: "Approve or reject the high-risk step below." };
    case "waiting_resource":
      return {
        headline: "Waiting for an eligible robot",
        reason,
        next: "Bring a capable robot online, or cancel the mission. Work resumes automatically.",
      };
    case "needs_intervention":
      return { headline: "Operator intervention required", reason, next: "Review the task below, then retry it or cancel the mission." };
    case "resolved":
      return { headline: "Resolved", reason, next: "Mission record is complete." };
    case "unresolved":
      return { headline: "Closed with the defect unresolved", reason, next: "Follow up outside this mission." };
    case "no_action_needed":
      return { headline: "Closed, no action needed", reason, next: "Mission record is complete." };
    case "cancelled":
      return { headline: "Cancelled", reason, next: "Check that robots confirmed cancellation in the attempts below." };
  }
}

export function MissionView({
  detail,
  robots,
  site,
  operator,
  onChange,
}: {
  detail: MissionDetail;
  robots: Robot[];
  site: SiteContext | null;
  operator: string;
  onChange: () => void;
}) {
  const m = detail.mission;
  const action = useAction();
  const info = explain(detail);
  const awaiting = detail.tasks.filter((t) => t.status === "awaiting_approval");
  const blocked = detail.tasks.filter((t) => t.status === "failed" || t.status === "needs_intervention");
  const source = detail.current_plan?.source;

  return (
    <div className="mission">
      <div className="mission-head">
        <div>
          <h2>
            <span className="mono">{m.id}</span> <Pill value={m.status} />
          </h2>
          <p className="work-order">{m.work_order}</p>
        </div>
        <div className="mission-actions">
          {PRE_START.includes(m.status) && m.plan_version > 0 && (
            <button
              className="primary"
              disabled={action.busy || m.status !== "ready"}
              onClick={() => action.run(async () => { await api.start(m.id, operator); onChange(); })}
            >
              Start mission
            </button>
          )}
          {!TERMINAL.includes(m.status) && m.status !== "planning" && (
            <button
              className="danger"
              disabled={action.busy}
              onClick={() => {
                if (confirm("Cancel this mission? Running robots are asked to stop; confirmation is tracked."))
                  action.run(async () => { await api.cancel(m.id, operator); onChange(); });
              }}
            >
              Cancel mission
            </button>
          )}
        </div>
      </div>
      <ErrorBox error={action.error} />

      <ProgressBar steps={buildSteps(detail, site, new Map(robots.map((r) => [r.id, r.name])))} />

      <div className={`banner status-${m.status}`}>
        <strong>{info.headline}</strong>
        {info.reason && <p>{info.reason}</p>}
        <p className="next">Next: {info.next}</p>
      </div>

      {awaiting.map((t) => (
        <ApprovalCard key={t.task_id} detail={detail} task={t} operator={operator} onChange={onChange} />
      ))}
      {blocked.map((t) => (
        <InterventionCard key={t.task_id} missionId={m.id} task={t} operator={operator} onChange={onChange} />
      ))}

      {PRE_START.includes(m.status) ? (
        <PlanReview detail={detail} operator={operator} onChange={onChange} />
      ) : (
        <TaskBoard detail={detail} robots={robots} />
      )}

      {detail.current_plan && !PRE_START.includes(m.status) && (
        <p className="hint">
          Executing plan v{detail.current_plan.version} from {sourceLabel(source)}: {detail.current_plan.plan.summary}
        </p>
      )}

      <EvidenceList evidence={detail.evidence} />
      <Timeline events={detail.events} />
    </div>
  );
}

function sourceLabel(source: string | undefined): string {
  if (source === "llm") return "the LLM planner";
  if (source === "fixture") return "the deterministic fixture planner (not an LLM)";
  if (source === "operator") return "an operator edit";
  return "unknown source";
}

function ApprovalCard({
  detail,
  task,
  operator,
  onChange,
}: {
  detail: MissionDetail;
  task: Task;
  operator: string;
  onChange: () => void;
}) {
  const [comment, setComment] = useState("");
  const action = useAction();
  const decide = (decision: "approve" | "reject") =>
    action.run(async () => {
      await api.decide(detail.mission.id, task.task_id, decision, operator, comment);
      onChange();
    });
  const basis = task.condition ? detail.evidence.filter((e) => e.task_id === task.condition!.task) : [];

  return (
    <section className="card approval">
      <h3>Approval required: {label(task.action)}</h3>
      <p>
        Task <span className="mono">{task.task_id}</span> on <span className="mono">{task.args.target}</span>
        {task.args.procedure && <> using procedure <span className="mono">{task.args.procedure}</span></>}. Risk:{" "}
        <strong>{task.risk}</strong>. Approval covers one dispatch of this action in plan v{task.plan_version}.
      </p>
      {task.rationale && <p className="hint">Planner rationale: {task.rationale}</p>}
      {basis.map((e) => (
        <p key={e.id} className="hint">
          Basis: {e.task_id} reported <strong>{e.finding}</strong> ({formatData(e.data)})
        </p>
      ))}
      <input placeholder="Comment (optional)" value={comment} onChange={(e) => setComment(e.target.value)} />
      <div className="row">
        <button className="primary" disabled={action.busy} onClick={() => decide("approve")}>
          Approve repair
        </button>
        <button className="danger" disabled={action.busy} onClick={() => decide("reject")}>
          Reject
        </button>
      </div>
      <ErrorBox error={action.error} />
    </section>
  );
}

function InterventionCard({
  missionId,
  task,
  operator,
  onChange,
}: {
  missionId: string;
  task: Task;
  operator: string;
  onChange: () => void;
}) {
  const action = useAction();
  return (
    <section className="card intervention">
      <h3>
        {label(task.action)} ({task.task_id}) <Pill value={task.status} />
      </h3>
      <p>{task.status_reason}</p>
      {task.requires_approval && (
        <p className="warn-text">
          This is a physical high-risk action. Retrying will ask for a new approval. Confirm the asset's real state
          first; a timed-out repair may already have been applied.
        </p>
      )}
      <button
        disabled={action.busy}
        onClick={() => action.run(async () => { await api.retry(missionId, task.task_id, operator); onChange(); })}
      >
        Retry task
      </button>
      <ErrorBox error={action.error} />
    </section>
  );
}

function PlanReview({ detail, operator, onChange }: { detail: MissionDetail; operator: string; onChange: () => void }) {
  const plan = detail.current_plan?.plan;
  const [editing, setEditing] = useState(!plan);
  const [text, setText] = useState(() => JSON.stringify(plan ?? { summary: "", target: "tank-T101", tasks: [] }, null, 2));
  const action = useAction();

  const save = () =>
    action.run(async () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch (e) {
        throw new Error(`Plan is not valid JSON: ${e instanceof Error ? e.message : e}`);
      }
      await api.editPlan(detail.mission.id, parsed, operator);
      setEditing(false);
      onChange();
    });

  return (
    <section className="card">
      <div className="card-head">
        <h3>
          Plan {plan ? `v${detail.current_plan!.version}` : "(none)"}
          {plan && <span className="hint"> from {sourceLabel(detail.current_plan!.source)}</span>}
        </h3>
        <button className="small" onClick={() => setEditing(!editing)}>
          {editing ? "Show table" : "Edit plan"}
        </button>
      </div>
      {plan && !editing && (
        <>
          <p>{plan.summary}</p>
          <PlanTable plan={plan} />
        </>
      )}
      {editing && (
        <>
          <p className="hint">
            Edits are validated by the backend before they can run: unsupported actions, bad arguments, missing
            dependencies, cycles and unsafe repair sequencing are rejected. Approval is enforced at runtime and cannot be
            set in the plan.
          </p>
          <textarea className="mono plan-editor" rows={22} value={text} onChange={(e) => setText(e.target.value)} />
          <div className="row">
            <button className="primary" disabled={action.busy} onClick={save}>
              Validate and save as new version
            </button>
            {plan && (
              <button
                onClick={() => {
                  setText(JSON.stringify(plan, null, 2));
                  action.clear();
                }}
              >
                Reset
              </button>
            )}
          </div>
          <ErrorBox error={action.error} />
        </>
      )}
      {detail.plan_versions.length > 1 && (
        <p className="hint">
          Versions:{" "}
          {detail.plan_versions.map((v) => `v${v.version} (${v.source}, ${v.author})`).join(" · ")}
        </p>
      )}
    </section>
  );
}

function PlanTable({ plan }: { plan: Plan }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Task</th>
          <th>Action</th>
          <th>Arguments</th>
          <th>Depends on</th>
          <th>Runs only if</th>
          <th>Rationale</th>
        </tr>
      </thead>
      <tbody>
        {plan.tasks.map((t) => (
          <tr key={t.id}>
            <td className="mono">{t.id}</td>
            <td>
              {label(t.action)}
              {t.action === "repair" && <span className="badge badge-risk">approval required</span>}
            </td>
            <td className="mono">{Object.entries(t.args).map(([k, v]) => `${k}=${v}`).join(", ")}</td>
            <td className="mono">{t.depends_on.join(", ") || "–"}</td>
            <td className="mono">{t.condition ? `${t.condition.task} = ${t.condition.finding}` : "–"}</td>
            <td className="hint">{t.rationale}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TaskBoard({ detail, robots }: { detail: MissionDetail; robots: Robot[] }) {
  const names = new Map(robots.map((r) => [r.id, r.name]));
  return (
    <section className="card">
      <h3>Tasks and attempts</h3>
      <table>
        <thead>
          <tr>
            <th>Task</th>
            <th>Action</th>
            <th>Status</th>
            <th>Finding</th>
            <th>Attempts</th>
          </tr>
        </thead>
        <tbody>
          {detail.tasks.map((t) => (
            <tr key={t.task_id}>
              <td className="mono">{t.task_id}</td>
              <td>
                {label(t.action)}
                {t.requires_approval && <span className="badge badge-risk">approval required</span>}
                <div className="hint mono">{t.args.target}</div>
              </td>
              <td>
                <Pill value={t.status} />
                <div className="hint">{t.status_reason}</div>
              </td>
              <td>{t.finding ? label(t.finding) : "–"}</td>
              <td>
                {t.attempts.length === 0 && <span className="hint">none</span>}
                {t.attempts.map((a) => (
                  <div key={a.id} className="attempt">
                    #{a.attempt_no} {names.get(a.robot_id) ?? a.robot_id}{" "}
                    <span className="hint">({label(a.robot_type)})</span> <Pill value={a.status} />{" "}
                    <span className="hint">{seconds(a.started_at, a.ended_at)}</span>
                    {a.detail && a.status !== "succeeded" && <div className="hint">{a.detail}</div>}
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function formatData(data: Record<string, unknown>): string {
  return Object.entries(data)
    .filter(([k]) => k !== "image")
    .map(([k, v]) => `${label(k)}: ${v}`)
    .join(", ");
}

function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  if (evidence.length === 0) return null;
  return (
    <section className="card">
      <h3>Evidence</h3>
      <ul className="evidence">
        {evidence.map((e) => (
          <li key={e.id}>
            <span className="mono">{e.task_id}</span> <strong>{e.finding ? label(e.finding) : ""}</strong>{" "}
            {formatData(e.data)}
            <div className="hint">
              {e.provenance} at {time(e.created_at)}
              {typeof e.data.image === "string" && <> · artifact {e.data.image}</>}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

const QUIET = new Set(["command_accepted", "mission_status"]);

function Timeline({ events }: { events: AuditEvent[] }) {
  const [all, setAll] = useState(false);
  const shown = (all ? events : events.filter((e) => !QUIET.has(e.kind))).slice().reverse();
  return (
    <section className="card">
      <div className="card-head">
        <h3>Timeline</h3>
        <label className="check">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> Show every event
        </label>
      </div>
      <ol className="timeline">
        {shown.map((e) => (
          <li key={e.seq} className={`ev ev-${e.kind}`}>
            <span className="mono ev-time">{time(e.ts)}</span>
            <span className="ev-kind">{label(e.kind)}</span>
            <span className="ev-msg">{e.message}</span>
            {(e.kind === "policy_decision" || e.kind === "planner_call") && <EventDetail event={e} />}
          </li>
        ))}
      </ol>
    </section>
  );
}

function EventDetail({ event }: { event: AuditEvent }) {
  if (event.kind === "policy_decision") {
    const checks = (event.data.checks as { rule: string; passed: boolean; detail: string }[]) ?? [];
    return (
      <ul className="checks">
        {checks.map((c) => (
          <li key={c.rule} className={c.passed ? "ok-text" : "danger-text"}>
            {c.passed ? "pass" : "fail"} · {label(c.rule)}: {c.detail}
          </li>
        ))}
      </ul>
    );
  }
  const { output, errors, latency_ms, model } = event.data as {
    output?: string;
    errors?: string[];
    latency_ms?: number;
    model?: string;
  };
  if (!output && !errors) return null;
  return (
    <details className="planner-call">
      <summary>
        {model} {latency_ms !== undefined && `· ${latency_ms} ms`}
      </summary>
      {errors && (
        <ul>
          {errors.map((e, i) => (
            <li key={i} className="danger-text">
              {e}
            </li>
          ))}
        </ul>
      )}
      {output && <pre>{output}</pre>}
    </details>
  );
}
