import { useState } from "react";
import type { AccountRow, BranchNode, DiffResult } from "./api";
import { api } from "./api";
import { shortId } from "./eventStyle";

interface Props {
  branches: BranchNode[];
  accounts: AccountRow[];
  onForked: (branchId: string) => void;
  selectedBranch: string;
}

export default function SandboxPanel({ branches, accounts, onForked, selectedBranch }: Props) {
  const [forkName, setForkName] = useState("red-team");
  const [busy, setBusy] = useState(false);
  const [targetAccount, setTargetAccount] = useState("");
  const [action, setAction] = useState("freeze_account");
  const [amount, setAmount] = useState("100000000");
  const [diffResult, setDiffResult] = useState<DiffResult | null>(null);
  const [diffB, setDiffB] = useState("");

  const forkedBranches = branches.filter((b) => !b.live);
  const sandboxBranch = branches.find((b) => b.branch_id === selectedBranch && !b.live)
    ? selectedBranch
    : forkedBranches[forkedBranches.length - 1]?.branch_id ?? "";

  async function doFork() {
    setBusy(true);
    try {
      const b = await api.fork("main", forkName || "chaos-branch");
      onForked(b.branch_id);
    } finally {
      setBusy(false);
    }
  }

  async function doChaos() {
    if (!sandboxBranch || !targetAccount) return;
    setBusy(true);
    try {
      if (action === "override_balance") {
        await api.chaos(sandboxBranch, action, { account_id: targetAccount, balance_paise: Number(amount) });
      } else {
        await api.chaos(sandboxBranch, action, { account_id: targetAccount });
      }
    } finally {
      setBusy(false);
    }
  }

  async function doDiff() {
    if (!sandboxBranch || !diffB) return;
    setBusy(true);
    try {
      setDiffResult(await api.diff(diffB, sandboxBranch));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="callout">
        Fork main into a paused timeline, then inject chaos — freeze an account, shove in
        cash, force a transfer — and diff it against a live branch to see exactly what changed.
      </div>

      <div className="sandbox-section">
        <div className="sandbox-label">1 · Fork a timeline</div>
        <div className="field">
          <label>branch name</label>
          <input value={forkName} onChange={(e) => setForkName(e.target.value)} placeholder="red-team" />
        </div>
        <button className="btn primary" disabled={busy} onClick={doFork} style={{ width: "100%" }}>
          Fork from main
        </button>
      </div>

      {forkedBranches.length > 0 && (
        <div className="sandbox-section">
          <div className="sandbox-label">2 · Inject chaos on {shortId(sandboxBranch)}</div>
          <div className="field">
            <label>action</label>
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="freeze_account">freeze_account</option>
              <option value="unfreeze_account">unfreeze_account</option>
              <option value="override_balance">override_balance</option>
            </select>
          </div>
          <div className="field">
            <label>target account_id</label>
            <select value={targetAccount} onChange={(e) => setTargetAccount(e.target.value)}>
              <option value="">select account…</option>
              {accounts.map((a) => (
                <option key={a.account_id} value={a.account_id}>
                  {shortId(a.account_id, 14)}… ({a.account_type})
                </option>
              ))}
            </select>
          </div>
          {action === "override_balance" && (
            <div className="field">
              <label>new balance (paise)</label>
              <input value={amount} onChange={(e) => setAmount(e.target.value)} />
            </div>
          )}
          <button className="btn danger" disabled={busy || !targetAccount} onClick={doChaos} style={{ width: "100%" }}>
            Apply to {shortId(sandboxBranch)}
          </button>
        </div>
      )}

      {forkedBranches.length > 0 && (
        <div className="sandbox-section">
          <div className="sandbox-label">3 · Diff against another branch</div>
          <div className="field">
            <label>compare to</label>
            <select value={diffB} onChange={(e) => setDiffB(e.target.value)}>
              <option value="">select branch…</option>
              {branches.filter((b) => b.branch_id !== sandboxBranch).map((b) => (
                <option key={b.branch_id} value={b.branch_id}>{b.name}</option>
              ))}
            </select>
          </div>
          <button className="btn" disabled={busy || !diffB} onClick={doDiff} style={{ width: "100%" }}>
            Diff
          </button>
          {diffResult && (
            <div className="inspector-card" style={{ marginTop: 10 }}>
              <div className="kv"><span className="kv-key">events only in {shortId(diffResult.branch_a_id)}</span><span className="kv-val">{diffResult.events_only_in_a}</span></div>
              <div className="kv"><span className="kv-key">events only in {shortId(diffResult.branch_b_id)}</span><span className="kv-val">{diffResult.events_only_in_b}</span></div>
              {diffResult.added.map((d) => (
                <div key={d.entity_id} className="kv"><span className="kv-key">+ {d.entity_type}</span><span className="kv-val">{shortId(d.entity_id)}</span></div>
              ))}
              {diffResult.modified.map((d) => (
                <div key={d.entity_id} className="kv"><span className="kv-key">~ {d.entity_type}</span><span className="kv-val">{shortId(d.entity_id)}</span></div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
