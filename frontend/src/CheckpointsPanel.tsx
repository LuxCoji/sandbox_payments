import { useEffect, useState } from "react";
import type { BranchNode, Checkpoint } from "./api";
import { api } from "./api";
import { formatSimTime, shortId } from "./eventStyle";

interface Props {
  branch: BranchNode | undefined;
  onForked: (branchId: string) => void;
}

export default function CheckpointsPanel({ branch, onForked }: Props) {
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [busy, setBusy] = useState(false);
  const [nameById, setNameById] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!branch) return;
    const controller = new AbortController();
    let cancelled = false;
    let timer: number;

    const load = () => {
      api.checkpoints(branch.branch_id, { signal: controller.signal })
        .then((cs) => {
          if (!cancelled) {
            setCheckpoints(cs);
            timer = setTimeout(load, 4000) as unknown as number;
          }
        })
        .catch((e) => {
          if (!cancelled && e.name !== "AbortError") {
            console.error("Checkpoints fetch failed:", e);
            timer = setTimeout(load, 4000) as unknown as number;
          }
        });
    };
    load();

    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [branch?.branch_id]);

  if (!branch) return <div className="empty-hint">Select a branch.</div>;

  async function snapshotNow() {
    if (!branch) return;
    setBusy(true);
    try {
      const cp = await api.makeCheckpoint(branch.branch_id);
      setCheckpoints((prev) => [...prev, cp]);
    } catch (e) {
      console.error("Failed to snapshot:", e);
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function forkFrom(cp: Checkpoint) {
    setBusy(true);
    try {
      const name = nameById[cp.checkpoint_id] || `${branch!.name}-@${cp.event_number}`;
      const b = await api.fork(branch!.branch_id, name, cp.checkpoint_id);
      onForked(b.branch_id);
    } catch (e) {
      console.error("Failed to fork:", e);
      alert(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="callout">
        Checkpoints are periodic state snapshots on this branch. Forking from one
        rebuilds a fresh engine from <i>that exact moment</i> — real time travel, not just
        branching off whatever's live right now.
      </div>

      <div className="inspector-card">
        <div className="kv"><span className="kv-key">branch</span><span className="kv-val">{branch.name}</span></div>
        <div className="kv"><span className="kv-key">seed_offset</span><span className="kv-val">{branch.seed_offset}</span></div>
        {branch.parent_branch_id && (
          <div className="kv"><span className="kv-key">forked from</span><span className="kv-val">{branch.parent_branch_id} @ #{branch.fork_seq_num}</span></div>
        )}
        <div className="kv"><span className="kv-key">head</span><span className="kv-val">#{branch.head_seq_num}</span></div>
      </div>

      <button className="btn primary" disabled={busy} onClick={snapshotNow} style={{ width: "100%", marginBottom: 14 }}>
        📸 Checkpoint now (@ current head)
      </button>

      <div className="sandbox-label">Snapshots ({checkpoints.length})</div>
      {checkpoints.length === 0 && <div className="empty-hint">No checkpoints yet — main auto-checkpoints periodically.</div>}
      {[...checkpoints].reverse().map((cp) => (
        <div key={cp.checkpoint_id} className="inspector-card" style={{ padding: 10, marginBottom: 8 }}>
          <div className="kv"><span className="kv-key">event</span><span className="kv-val">#{cp.event_number}</span></div>
          <div className="kv"><span className="kv-key">sim time</span><span className="kv-val">{formatSimTime(cp.sim_time_ns)}</span></div>
          <div className="kv"><span className="kv-key">state hash</span><span className="kv-val">{shortId(cp.state_hash, 12)}…</span></div>
          <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
            <input
              placeholder="fork name"
              style={{ flex: 1, background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)", padding: "5px 8px", fontFamily: "var(--mono)", fontSize: 11 }}
              value={nameById[cp.checkpoint_id] ?? ""}
              onChange={(e) => setNameById((prev) => ({ ...prev, [cp.checkpoint_id]: e.target.value }))}
            />
            <button className="btn small" disabled={busy} onClick={() => forkFrom(cp)}>Fork from here</button>
          </div>
        </div>
      ))}
    </div>
  );
}
