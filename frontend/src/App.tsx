import { useCallback, useEffect, useRef, useState } from "react";
import { api, Config, Metrics, Mission, MissionDetail, RobotsResponse, AuditEvent, SiteContext } from "./api";
import { FleetPanel, MetricsPanel, MissionList, NewMissionForm, WorkOrderDraft } from "./Sidebar";
import { MissionView } from "./MissionView";

const OPERATOR_KEY = "orchestrator.operator";

function loadOperator(): string {
  try {
    return localStorage.getItem(OPERATOR_KEY) || "operator";
  } catch {
    return "operator";
  }
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [site, setSite] = useState<SiteContext | null>(null);
  const [missions, setMissions] = useState<Mission[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<MissionDetail | null>(null);
  const [robots, setRobots] = useState<RobotsResponse | null>(null);
  const [fleetEvents, setFleetEvents] = useState<AuditEvent[]>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [operator, setOperator] = useState(loadOperator);
  const [connected, setConnected] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [copiedDraft, setCopiedDraft] = useState<(WorkOrderDraft & { nonce: number }) | null>(null);
  const selectedRef = useRef(selected);
  const refreshSeq = useRef(0);
  selectedRef.current = selected;

  const refresh = useCallback(async () => {
    const seq = ++refreshSeq.current;
    try {
      const [m, r, f, met] = await Promise.all([api.missions(), api.robots(), api.fleetEvents(), api.metrics()]);
      const id = selectedRef.current ?? m[0]?.id ?? null;
      const latestDetail = id ? await api.mission(id) : null;
      if (seq !== refreshSeq.current) return;
      setMissions(m);
      setRobots(r);
      setFleetEvents(f);
      setMetrics(met);
      if (id !== selectedRef.current) setSelected(id);
      setDetail(latestDetail);
      setLoadError(null);
    } catch (e) {
      if (seq === refreshSeq.current) setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    api.config().then(setConfig).catch((e) => setLoadError(String(e)));
    api.site().then(setSite).catch((e) => setLoadError(String(e)));
  }, []);

  useEffect(() => {
    refresh();
  }, [selected, refresh]);

  // Live updates: the backend pushes a "changed" notice; the UI re-reads backend state.
  // A slow poll covers dropped connections.
  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(refresh, 100);
    };
    const source = new EventSource("/api/stream");
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = schedule;
    const poll = window.setInterval(refresh, 5000);
    return () => {
      source.close();
      window.clearInterval(poll);
      window.clearTimeout(timer);
    };
  }, [refresh]);

  const changeOperator = (name: string) => {
    setOperator(name);
    try {
      localStorage.setItem(OPERATOR_KEY, name);
    } catch {
      // Name is still used for this session.
    }
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-title">Mission Orchestrator</span>
          <span className="badge badge-sim">Robot execution simulated</span>
        </div>
        <div className="topbar-right">
          <span className={`live ${connected ? "on" : "off"}`}>{connected ? "Live" : "Reconnecting"}</span>
          <label className="operator">
            Operator
            <input value={operator} onChange={(e) => changeOperator(e.target.value)} maxLength={64} />
          </label>
        </div>
      </header>
      {loadError && <div className="error-bar">Backend unreachable: {loadError}</div>}
      <div className="layout">
        <aside className="sidebar">
          <NewMissionForm
            key={copiedDraft?.nonce ?? 0}
            config={config}
            site={site}
            operator={operator}
            draft={copiedDraft ?? undefined}
            onCreated={(d) => {
              // Update the ref first: refresh() reads it before React re-renders.
              selectedRef.current = d.mission.id;
              setSelected(d.mission.id);
              setDetail(d);
              refresh();
            }}
          />
          <MissionList missions={missions} selected={selected} onSelect={setSelected} />
          {robots && <FleetPanel robots={robots} events={fleetEvents} operator={operator} onChange={refresh} />}
          {metrics && <MetricsPanel metrics={metrics} />}
        </aside>
        <main className="main">
          {detail ? (
            <MissionView
              key={detail.mission.id}
              detail={detail}
              robots={robots?.robots ?? []}
              site={site}
              operator={operator}
              onChange={refresh}
              onCopy={() => {
                const m = detail.mission;
                setCopiedDraft((previous) => ({
                  nonce: (previous?.nonce ?? 0) + 1,
                  workOrder: m.work_order,
                  target: m.target ?? "tank-T101",
                  allowRepair: Boolean(m.allow_repair),
                  planner: m.planner_mode,
                }));
                document.getElementById("new-work-order")?.scrollIntoView({ behavior: "smooth" });
              }}
            />
          ) : (
            <div className="empty">
              <h2>No mission selected</h2>
              <p>Submit a work order to generate a plan. Nothing executes until you review the plan and start it.</p>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
