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

const BASE = "/api";

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

export const api = {
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
  resetSimulation: (seed: number = 42, numUsers: number = 60, numMerchants: number = 8) =>
    fetch(`${BASE}/reset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seed, num_users: numUsers, num_merchants: numMerchants }),
    }).then((r) => j<void>(r)),
};

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/api/stream`;
}
