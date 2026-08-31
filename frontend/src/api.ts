export interface BranchNode {
  branch_id: string;
  name: string;
  parent_branch_id: string | null;
  parent_checkpoint_id: string | null;
  fork_seq_num: number;
  head_seq_num: number;
  live: boolean;
  seed_offset: number;
  created_at_ns: number;
  checkpoint_seq_nums: number[];
  pool_from_branch_ids?: string[];
}

export interface Checkpoint {
  checkpoint_id: string;
  branch_id: string;
  event_number: number;
  sim_time_ns: number;
  state_hash: string;
  has_snapshot?: boolean;
}

export interface BranchState {
  branch_id: string;
  name: string;
  live: boolean;
  sim_time_ns: number;
  state_hash: string;
  money_supply_paise: number;
  account_count: number;
  tx_count: number;
  step_count: number;
  head_seq_num: number;
  parent_branch_id: string | null;
  parent_checkpoint_id: string | null;
}

export interface AccountRow {
  account_id: string;
  account_type: string;
  balance_paise: number;
  status: string;
  kyc_level: number;
  owner_id: string;
  daily_tx_count: number;
  daily_tx_volume_paise: number;
  merchant_category_code: string | null;
}

export interface SimEvent {
  event_id: string;
  event_type: string;
  sim_time_ns: number;
  actor_id: string | null;
  branch_id: string;
  seq_num: number;
  payload: Record<string, unknown>;
  causation_id: string | null;
  correlation_id: string | null;
}

export interface DiffResult {
  branch_a_id: string;
  branch_b_id: string;
  at_event: number;
  events_only_in_a: number;
  events_only_in_b: number;
  added: { entity_type: string; entity_id: string }[];
  modified: { entity_type: string; entity_id: string }[];
}

export interface RedTeamStep {
  step: number;
  tool_name: string;
  parameters: Record<string, unknown>;
  reasoning: string;
  success: boolean;
  error_code: string | null;
  error_message: string | null;
  provider_model: string | null;
  latency_ms: number | null;
}

export interface RedTeamSession {
  session_id: string;
  status: "running" | "done" | "error";
  from_genesis: boolean;
  checkpoint_id: string | null;
  use_graph: boolean;
  branch_id: string | null;
  steps_taken: number;
  max_steps: number;
  committed: boolean;
  end_checkpoint_id: string | null;
  commit_reasoning: string | null;
  pool_from_branch_ids: string[];
  error: string | null;
  started_at: number;
  step_log: RedTeamStep[];
}

const BASE = "/api";

/** POST JSON and surface the server's own message on a refusal.
 *
 *  The refusals here are meaningful - "a freeze needs a named reviewer",
 *  "telling a customer they are under AML review is a criminal offence" - so
 *  swallowing them into a generic error would hide the reason the action was
 *  not taken. */
async function post(path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(detail?.detail ?? `HTTP ${response.status}`);
  }
  return response.json();
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

export const api = {
  freezeCase: (body: CaseDecision) => post("/risk/cases/freeze", body),
  clearCase: (body: CaseDecision) => post("/risk/cases/clear", body),
  stepUpCase: (body: CaseDecision) => post("/risk/cases/step-up", body),
  startRetrain: () =>
    fetch(`${BASE}/risk/retrain`, { method: "POST" }).then((r) => j<{ status: string }>(r)),
  retrainStatus: (signal?: AbortSignal) =>
    fetch(`${BASE}/risk/retrain`, { signal }).then((r) => j<RetrainStatus>(r)),
  rollbackModel: () =>
    fetch(`${BASE}/risk/rollback`, { method: "POST" }).then((r) => j<unknown>(r)),
  riskSummary: (signal?: AbortSignal) =>
    fetch(`${BASE}/risk/summary`, { signal }).then((r) => j<RiskSummary>(r)),
  riskCases: (signal?: AbortSignal) =>
    fetch(`${BASE}/risk/cases`, { signal }).then((r) => j<{ cases: RiskCase[] }>(r)),
  branches: (init?: RequestInit) => fetch(`${BASE}/branches`, init).then((r) => j<BranchNode[]>(r)),
  branchState: (id: string, init?: RequestInit) => fetch(`${BASE}/branches/${id}/state`, init).then((r) => j<BranchState>(r)),
  accounts: (id: string, init?: RequestInit) => fetch(`${BASE}/branches/${id}/accounts`, init).then((r) => j<AccountRow[]>(r)),
  accountEvents: (branchId: string, accountId: string, init?: RequestInit) =>
    fetch(`${BASE}/branches/${branchId}/accounts/${accountId}/events`, init).then((r) => j<SimEvent[]>(r)),
  fork: (parentBranchId: string, name: string, checkpointId?: string) =>
    fetch(`${BASE}/branches/fork`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent_branch_id: parentBranchId, name, checkpoint_id: checkpointId ?? null }),
    }).then((r) => j<BranchState>(r)),
  checkpoints: (id: string, init?: RequestInit) => fetch(`${BASE}/branches/${id}/checkpoints`, init).then((r) => j<Checkpoint[]>(r)),
  makeCheckpoint: (id: string) =>
    fetch(`${BASE}/branches/${id}/checkpoint`, { method: "POST" }).then((r) => j<Checkpoint>(r)),
  breakdown: (id: string) => fetch(`${BASE}/branches/${id}/breakdown`).then((r) => j<Record<string, number>>(r)),
  chaos: (branchId: string, action: string, params: Record<string, unknown>) =>
    fetch(`${BASE}/branches/${branchId}/chaos`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, params }),
    }).then((r) => j<BranchState>(r)),
  diff: (a: string, b: string) => fetch(`${BASE}/diff?branch_a=${a}&branch_b=${b}`).then((r) => j<DiffResult>(r)),
  deleteBranch: (id: string) => fetch(`${BASE}/branches/${id}`, { method: "DELETE" }).then((r) => j<void>(r)),
  // Demo checkpoints live only in the in-process demo DAG, not the real
  // Postgres store the red-team harness forks from — this materializes an
  // equivalent checkpoint there and hands back its (real) checkpoint_id.
  exportForRedTeam: (checkpointId: string) =>
    fetch(`${BASE}/checkpoints/${checkpointId}/export-for-redteam`, { method: "POST" }).then((r) => j<{ checkpoint_id: string }>(r)),
  resetSimulation: (seed: number = 42, numUsers: number = 60, numMerchants: number = 8) =>
    fetch(`${BASE}/reset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seed, num_users: numUsers, num_merchants: numMerchants }),
    }).then((r) => j<void>(r)),
  startRedteamSession: (opts: {
    fromGenesis: boolean; checkpointId?: string; seed?: number; useGraph?: boolean; poolFromBranchIds?: string[];
  }) =>
    fetch(`${BASE}/redteam/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        from_genesis: opts.fromGenesis,
        checkpoint_id: opts.checkpointId ?? null,
        seed: opts.seed ?? 42,
        use_graph: opts.useGraph ?? false,
        pool_from_branch_ids: opts.poolFromBranchIds ?? [],
      }),
    }).then((r) => j<{ session_id: string }>(r)),
  redteamSessions: () => fetch(`${BASE}/redteam/sessions`).then((r) => j<RedTeamSession[]>(r)),
  redteamSession: (id: string) => fetch(`${BASE}/redteam/sessions/${id}`).then((r) => j<RedTeamSession>(r)),
};

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/stream`;
}

export function redteamWsUrl(sessionId: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/redteam/stream/${sessionId}`;
}

/** What the fraud rails report about the live session. */
export type RiskSummary = {
  enabled: boolean;
  /** Whether a trained card model is loaded. Without it the rail allows
   *  everything, and its counts look identical to a rail finding nothing. */
  card_model_loaded: boolean;
  assessed: number;
  scored: number;
  allowed: number;
  stepped_up: number;
  blocked: number;
  review: number;
  flagged: number;
  flag_rate: number;
  by_rail: Record<string, number>;
  open_cases: number;
  accounts_tracked: number;
};

/** One flagged transaction, with the evidence that raised it. */
export type RiskCase = {
  tx_id: string;
  rail: "card" | "wire";
  action: string;
  score: number;
  reason: string;
  amount_paise: number;
  source_account_id: string;
  destination_account_id: string;
  sim_time_ns: number;
  /** Where the money went after this leg. Empty on the card rail, and absent
   *  entirely from a server older than chain tracing - which is why it is
   *  optional rather than an empty array. */
  chain?: ChainHop[];
};

/** A reviewer's answer to a case. The reviewer's name is required - "who
 *  decided this" is the first question an audit asks. */
export type CaseDecision = {
  case_id: string;
  reviewer: string;
  reason?: string;
  second_reviewer?: string | null;
};

/** One hop of the route money took after a flagged transfer. */
export type ChainHop = {
  hop: number;
  from_account: string;
  to_account: string;
  amount_paise: number;
  transfers: number;
  hours: number;
  forwarded_on: number;
  other_legs: number;
};

/** A model version and what it measured when it was considered. */
export type ModelVersion = {
  version: number;
  trained_at: string;
  rows: number;
  fraud: number;
  recall_at_2pct: number;
  promoted: boolean;
  reason: string;
};

/** Where the last retrain got to. "declined" is a success, not a failure:
 *  it means the candidate did not beat what is already live. */
export type RetrainStatus = {
  status: "idle" | "running" | "done" | "declined" | "failed";
  error: string | null;
  result: {
    promoted: boolean;
    reason: string;
    version: number;
    candidate_recall: number;
    live_recall_on_same_holdout: number | null;
    rows: number;
    fraud: number;
  } | null;
  registry: {
    versions: number;
    live_version: number | null;
    live_recall: number | null;
    can_rollback: boolean;
    history: ModelVersion[];
  };
};
