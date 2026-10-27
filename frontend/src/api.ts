// Typed client for the orchestrator backend. All state shown in the UI comes from these calls.

export type MissionStatus =
  | "planning"
  | "planning_failed"
  | "ready"
  | "running"
  | "waiting_approval"
  | "waiting_resource"
  | "needs_intervention"
  | "resolved"
  | "unresolved"
  | "no_action_needed"
  | "cancelled";

export interface Mission {
  id: string;
  work_order: string;
  target: string | null;
  allow_repair: number;
  status: MissionStatus;
  status_reason: string;
  planner_mode: string;
  plan_version: number;
  created_at: number;
  updated_at: number;
}

export interface PlanTask {
  id: string;
  action: string;
  args: Record<string, string>;
  depends_on: string[];
  condition?: { task: string; finding: string } | null;
  rationale?: string;
}

export interface Plan {
  summary: string;
  target: string;
  tasks: PlanTask[];
}

export interface PlanVersion {
  mission_id: string;
  version: number;
  plan: Plan;
  source: string;
  author: string;
  created_at: number;
}

export interface Attempt {
  id: string;
  task_id: string;
  attempt_no: number;
  robot_id: string;
  robot_type: string;
  status: string;
  detail: string;
  started_at: number;
  deadline_at: number;
  ended_at: number | null;
}

export interface Task {
  task_id: string;
  action: string;
  args: Record<string, string>;
  depends_on: string[];
  condition: { task: string; finding: string } | null;
  rationale: string;
  status: string;
  status_reason: string;
  finding: string | null;
  attempt_count: number;
  plan_version: number;
  requires_approval: boolean;
  risk: string;
  role: string;
  attempts: Attempt[];
}

export interface Approval {
  id: string;
  task_id: string;
  plan_version: number;
  decision: string;
  actor: string;
  comment: string;
  created_at: number;
  consumed_by: string | null;
}

export interface Evidence {
  id: string;
  task_id: string;
  attempt_id: string;
  robot_id: string;
  finding: string | null;
  data: Record<string, unknown>;
  provenance: string;
  created_at: number;
}

export interface AuditEvent {
  seq: number;
  mission_id: string | null;
  ts: number;
  kind: string;
  message: string;
  data: Record<string, unknown>;
}

export interface MissionDetail {
  mission: Mission;
  intake_conflict: string | null;
  plan_versions: PlanVersion[];
  current_plan: PlanVersion | null;
  tasks: Task[];
  approvals: Approval[];
  evidence: Evidence[];
  events: AuditEvent[];
}

export interface Robot {
  id: string;
  name: string;
  type: string;
  type_name: string;
  capabilities: string[];
  availability: "available" | "busy" | "offline" | "unreachable" | "unresponsive";
  health_reason: string;
  reservation: { attempt_id: string; mission_id: string; task_id: string } | null;
}

export interface RobotType {
  id: string;
  name: string;
  capabilities: string[];
  total: number;
  availability: Partial<Record<Robot["availability"], number>>;
}

export interface RobotsResponse {
  types: RobotType[];
  robots: Robot[];
  simulator: {
    robots: Record<string, { online: boolean; silent: boolean; pending_faults: string[]; active_commands: string[] }>;
    assets: Record<string, { defect_present: boolean; repaired: boolean }>;
  };
}

export interface Config {
  planners: { mode: string; label: string }[];
  default_planner: string;
  sim_time_scale: number;
}

export type Metrics = Record<string, unknown>;

export interface SiteContext {
  assets: { id: string; name: string; description: string }[];
  actions: { name: string; role: string; requires_approval: boolean }[];
}

export class ApiError extends Error {
  constructor(message: string, public errors: string[] = []) {
    super(message);
  }
}

function newKey(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
}

const RETRIES = 2;

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  // Operator commands carry one idempotency key across retries, so a request that reached the
  // server before the connection dropped is replayed rather than applied twice.
  if (method !== "GET") headers["Idempotency-Key"] = newKey();
  let res: Response | undefined;
  for (let attempt = 0; ; attempt++) {
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      if (method === "GET" || attempt >= RETRIES) throw e;
      continue;
    }
    // 409 with the same key means the first try is still running on the server; wait and ask again.
    if (method !== "GET" && res.status === 409 && attempt < RETRIES) {
      const peek = await res.clone().json().catch(() => ({}));
      if (typeof peek.detail === "string" && peek.detail.includes("Idempotency-Key")) {
        await new Promise((r) => setTimeout(r, 500));
        continue;
      }
    }
    break;
  }
  const data = await res!.json().catch(() => ({}));
  if (!res!.ok) {
    const detail = typeof data.detail === "string" ? data.detail : `HTTP ${res!.status}`;
    const errors: string[] = Array.isArray(data.errors)
      ? data.errors
      : Array.isArray(data.detail)
        ? data.detail.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).join(".")}: ${d.msg}`)
        : [];
    throw new ApiError(detail, errors);
  }
  return data as T;
}

export const api = {
  config: () => request<Config>("GET", "/api/config"),
  site: () => request<SiteContext>("GET", "/api/site"),
  robots: () => request<RobotsResponse>("GET", "/api/robots"),
  fleetEvents: () => request<AuditEvent[]>("GET", "/api/fleet-events"),
  missions: () => request<Mission[]>("GET", "/api/missions"),
  mission: (id: string) => request<MissionDetail>("GET", `/api/missions/${id}`),
  metrics: () => request<Metrics>("GET", "/api/metrics"),
  createMission: (work_order: string, target: string, allow_repair: boolean, planner: string, actor: string) =>
    request<MissionDetail>("POST", "/api/missions", { work_order, target, allow_repair, planner, actor }),
  editPlan: (id: string, plan: unknown, actor: string, expectedPlanVersion: number) =>
    request<MissionDetail>("PUT", `/api/missions/${id}/plan`, { plan, actor, expected_plan_version: expectedPlanVersion }),
  start: (id: string, actor: string, expectedPlanVersion: number) =>
    request<MissionDetail>("POST", `/api/missions/${id}/start`, { actor, expected_plan_version: expectedPlanVersion }),
  decide: (
    id: string, taskId: string, decision: "approve" | "reject", actor: string, comment: string,
    expectedPlanVersion: number, expectedRequestSeq: number,
  ) => request<MissionDetail>("POST", `/api/missions/${id}/tasks/${taskId}/decision`, {
    decision, actor, comment, expected_plan_version: expectedPlanVersion, expected_request_seq: expectedRequestSeq,
  }),
  retry: (id: string, taskId: string, actor: string) =>
    request<MissionDetail>("POST", `/api/missions/${id}/tasks/${taskId}/retry`, { actor }),
  cancel: (id: string, actor: string) => request<MissionDetail>("POST", `/api/missions/${id}/cancel`, { actor }),
  fault: (robotId: string, fault: string, actor: string) =>
    request<RobotsResponse>("POST", `/api/sim/robots/${robotId}/fault`, { fault, actor }),
  setDefect: (assetId: string, defect_present: boolean) =>
    request<unknown>("POST", `/api/sim/assets/${assetId}`, { defect_present }),
};
