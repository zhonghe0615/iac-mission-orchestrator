import { useEffect, useState } from "react";
import { api, AuditEvent, Config, Metrics, Mission, MissionDetail, Robot, RobotsResponse, RobotType } from "./api";
import { ErrorBox, label, Pill, time, useAction } from "./ui";

const DEFAULT_WORK_ORDER =
  "Operators report a suspected leak near the east weld seam of storage tank T-101. " +
  "Inspect the tank, collect evidence, and resolve the defect if one is confirmed.";

export function NewMissionForm({
  config,
  operator,
  onCreated,
}: {
  config: Config | null;
  operator: string;
  onCreated: (d: MissionDetail) => void;
}) {
  const [workOrder, setWorkOrder] = useState(DEFAULT_WORK_ORDER);
  const [planner, setPlanner] = useState("");
  const action = useAction();

  useEffect(() => {
    if (config && !planner) setPlanner(config.default_planner);
  }, [config, planner]);

  const llmAvailable = config?.planners.some((p) => p.mode === "llm");

  return (
    <section className="panel">
      <h3>New work order</h3>
      <textarea rows={5} value={workOrder} onChange={(e) => setWorkOrder(e.target.value)} />
      <label className="field">
        Planner
        <select value={planner} onChange={(e) => setPlanner(e.target.value)}>
          {config?.planners.map((p) => (
            <option key={p.mode} value={p.mode}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      {config && !llmAvailable && (
        <p className="hint warn-text">
          No LLM is configured (set LLM_API_KEY and LLM_MODEL). Only the deterministic fixture planner is available.
        </p>
      )}
      <button
        className="primary"
        disabled={action.busy || workOrder.trim().length < 5 || !planner}
        onClick={() =>
          action.run(async () => onCreated(await api.createMission(workOrder, planner, operator)))
        }
      >
        {action.busy ? "Planning…" : "Generate plan"}
      </button>
      <ErrorBox error={action.error} />
    </section>
  );
}

export function MissionList({
  missions,
  selected,
  onSelect,
}: {
  missions: Mission[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="panel">
      <h3>Missions</h3>
      {missions.length === 0 && <p className="hint">None yet.</p>}
      <ul className="mission-list">
        {missions.map((m) => (
          <li key={m.id}>
            <button className={`mission-item ${m.id === selected ? "selected" : ""}`} onClick={() => onSelect(m.id)}>
              <span className="mono">{m.id}</span>
              <Pill value={m.status} />
              <span className="mission-item-time">{time(m.created_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

const FAULTS: { fault: string; text: string; title: string }[] = [
  { fault: "fail_next", text: "Fail next", title: "Next command reports an explicit failure" },
  { fault: "hang_next", text: "Time out next", title: "Next command responds only after its deadline" },
  { fault: "duplicate_next", text: "Duplicate next", title: "Next completion event is delivered twice" },
  { fault: "reorder_next", text: "Reorder next", title: "Next 'accepted' event arrives after completion" },
];

// Types with more robots than this start collapsed and show a filtered, capped list.
const EXPANDED_LIMIT = 8;
const LIST_LIMIT = 50;

export function FleetPanel({
  robots,
  events,
  operator,
  onChange,
}: {
  robots: RobotsResponse;
  events: AuditEvent[];
  operator: string;
  onChange: () => void;
}) {
  const action = useAction();
  const fault = (robotId: string, f: string) =>
    action.run(async () => {
      await api.fault(robotId, f, operator);
      onChange();
    });
  const t101 = robots.simulator.assets["tank-T101"];

  return (
    <section className="panel">
      <h3>Fleet</h3>
      {robots.types.map((t) => (
        <RobotTypeGroup
          key={t.id}
          type={t}
          robots={robots.robots.filter((r) => r.type === t.id)}
          simulator={robots.simulator}
          onFault={fault}
        />
      ))}
      {t101 && (
        <label className="check">
          <input
            type="checkbox"
            checked={t101.defect_present && !t101.repaired}
            onChange={(e) =>
              action.run(async () => {
                await api.setDefect("tank-T101", e.target.checked);
                onChange();
              })
            }
          />
          Simulated defect present on T-101
        </label>
      )}
      <ErrorBox error={action.error} />
      {events.length > 0 && (
        <details className="fleet-events">
          <summary>Fleet events ({events.length})</summary>
          <ul>
            {events
              .slice()
              .reverse()
              .slice(0, 20)
              .map((e) => (
                <li key={e.seq}>
                  <span className="mono">{time(e.ts)}</span> {e.message}
                </li>
              ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function RobotTypeGroup({
  type,
  robots,
  simulator,
  onFault,
}: {
  type: RobotType;
  robots: Robot[];
  simulator: RobotsResponse["simulator"];
  onFault: (robotId: string, fault: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const large = robots.length > EXPANDED_LIMIT;
  const matching = robots.filter((r) => !filter || `${r.name} ${r.id}`.toLowerCase().includes(filter.toLowerCase()));
  const shown = matching.slice(0, LIST_LIMIT);
  const list = (
    <>
      {large && <input placeholder="Filter robots" value={filter} onChange={(e) => setFilter(e.target.value)} />}
      {shown.map((r) => (
        <RobotRow key={r.id} robot={r} sim={simulator.robots[r.id]} onFault={onFault} />
      ))}
      {matching.length > shown.length && (
        <p className="hint">
          Showing {shown.length} of {matching.length}. Filter to find others.
        </p>
      )}
    </>
  );
  return (
    <div className="robot-type">
      <strong>
        {type.name} <span className="hint">× {type.total}</span>
      </strong>
      <div className="type-counts">
        {Object.entries(type.availability).map(([status, n]) => (
          <span key={status}>
            <Pill value={status} /> {n}
          </span>
        ))}
      </div>
      <div className="hint">{type.capabilities.map(label).join(" · ")}</div>
      {large ? (
        <details>
          <summary>Robots</summary>
          {list}
        </details>
      ) : (
        list
      )}
    </div>
  );
}

function RobotRow({
  robot: r,
  sim,
  onFault,
}: {
  robot: Robot;
  sim: RobotsResponse["simulator"]["robots"][string];
  onFault: (robotId: string, fault: string) => void;
}) {
  return (
    <div className="robot">
      <div className="robot-head">
        <span>{r.name}</span>
        <Pill value={r.availability} />
      </div>
      {r.reservation && (
        <div className="hint">
          Reserved: {r.reservation.mission_id} / {r.reservation.task_id}
        </div>
      )}
      {r.health_reason && <div className="hint warn-text">{r.health_reason}</div>}
      {sim.pending_faults.length > 0 && (
        <div className="hint warn-text">Armed: {sim.pending_faults.map(label).join(", ")}</div>
      )}
      <div className="fault-row">
        {sim.online ? (
          <button className="small danger" onClick={() => onFault(r.id, "offline")}>
            Take offline
          </button>
        ) : (
          <button className="small" onClick={() => onFault(r.id, "online")}>
            Bring online
          </button>
        )}
        {r.availability === "unresponsive" && sim.online && (
          <button className="small" onClick={() => onFault(r.id, "online")}>
            Mark healthy
          </button>
        )}
        {FAULTS.map((f) => (
          <button key={f.fault} className="small" title={f.title} onClick={() => onFault(r.id, f.fault)}>
            {f.text}
          </button>
        ))}
      </div>
    </div>
  );
}

const METRIC_LABELS: [string, string][] = [
  ["missions_total", "Missions"],
  ["completion_rate", "Resolved / closed"],
  ["retries", "Retries"],
  ["reassignments", "Reassignments"],
  ["escalations", "Escalations"],
  ["operator_actions", "Operator actions"],
  ["mean_time_to_recover_s", "Mean time to recover (s)"],
  ["duplicate_events_ignored", "Duplicate events ignored"],
  ["stale_events_ignored", "Stale events ignored"],
];

export function MetricsPanel({ metrics }: { metrics: Metrics }) {
  return (
    <section className="panel">
      <h3>Metrics</h3>
      <dl className="metrics">
        {METRIC_LABELS.map(([key, text]) => (
          <div key={key}>
            <dt>{text}</dt>
            <dd>{metrics[key] === null || metrics[key] === undefined ? "–" : String(metrics[key])}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
