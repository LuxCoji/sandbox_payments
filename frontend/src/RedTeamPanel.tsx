import { useEffect, useRef, useState } from "react";
import { api, redteamWsUrl, type RedTeamSession, type RedTeamStep } from "./api";
import CopyChip from "./CopyChip";
import { shortId } from "./eventStyle";

const TOOL_COLOR: Record<string, string> = {
  create_account: "#6fb7ff",
  transfer_funds: "#b78bff",
  make_payment: "#5ef2b5",
  inspect_account: "#8892a3",
  commit_strategy: "#ffb454",
  fork_branch: "#ff9ecb",
  diff_branches: "#ff9ecb",
};

function toolColor(name: string): string {
  return TOOL_COLOR[name] ?? "#4d5567";
}

interface Props {
  initialCheckpointId?: string | null;
}

export default function RedTeamPanel({ initialCheckpointId }: Props) {
  const [sessions, setSessions] = useState<RedTeamSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [live, setLive] = useState<RedTeamSession | null>(null);
  const [starting, setStarting] = useState(false);
  const [fromGenesis, setFromGenesis] = useState(!initialCheckpointId);
  const [checkpointId, setCheckpointId] = useState(initialCheckpointId ?? "");
  const [useGraph, setUseGraph] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  // A checkpoint handed over from the Checkpoints tab ("Use for Red Team")
  // should immediately switch this form to fork-from-checkpoint mode, even
  // if the panel was already mounted with different state.
  useEffect(() => {
    if (initialCheckpointId) {
      setCheckpointId(initialCheckpointId);
      setFromGenesis(false);
    }
  }, [initialCheckpointId]);

  // Refresh the session list periodically — cheap poll, sessions are rare
  // events compared to the ChronoDAG feed.
  useEffect(() => {
    let timerId: number;
    let cancelled = false;
    const load = async () => {
      try {
        const s = await api.redteamSessions();
        if (!cancelled) setSessions(s);
      } catch {
        // API not reachable / redteam extra not installed — leave list empty
      }
      if (!cancelled) timerId = window.setTimeout(load, 4000);
    };
    load();
    return () => {
      cancelled = true;
      clearTimeout(timerId);
    };
  }, []);

  // Live-stream the selected session's steps.
  useEffect(() => {
    wsRef.current?.close();
    if (!activeId) {
      setLive(null);
      return;
    }
    let cancelled = false;
    api.redteamSession(activeId).then((s) => !cancelled && setLive(s)).catch(() => {});

    const ws = new WebSocket(redteamWsUrl(activeId));
    wsRef.current = ws;
    ws.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (data.type === "step") {
        const step: RedTeamStep = data;
        setLive((prev) =>
          prev ? { ...prev, steps_taken: step.step, step_log: [...prev.step_log, step] } : prev
        );
      } else if (data.type === "done") {
        setLive((prev) => (prev ? { ...prev, status: data.status, committed: data.committed, error: data.error } : prev));
      }
    };
    return () => {
      cancelled = true;
      ws.close();
    };
  }, [activeId]);

  async function handleStart() {
    setStarting(true);
    try {
      const { session_id } = await api.startRedteamSession({
        fromGenesis, checkpointId: checkpointId || undefined, useGraph,
      });
      setActiveId(session_id);
      setSessions((prev) => [
        {
          session_id, status: "running", from_genesis: fromGenesis, checkpoint_id: checkpointId || null,
          use_graph: useGraph, branch_id: null, steps_taken: 0, committed: false, error: null,
          started_at: Date.now() / 1000, step_log: [],
        },
        ...prev,
      ]);
    } catch (e) {
      alert("Failed to start session: " + String(e));
    } finally {
      setStarting(false);
    }
  }

  return (
    <div>
      <div className="callout">
        Fork off a warm checkpoint, hand an LLM the account tools, and watch it decide —
        one action at a time — what it thinks a fraud system won't catch. Routing across
        the provider pool and each step's reasoning show up live below.
      </div>

      <div className="sandbox-section">
        <div className="sandbox-label">1 · Start a session</div>
        <div className="field">
          <label>onset</label>
          <select value={fromGenesis ? "genesis" : "checkpoint"} onChange={(e) => setFromGenesis(e.target.value === "genesis")}>
            <option value="genesis">fresh warmup (--from-genesis)</option>
            <option value="checkpoint">fork existing checkpoint</option>
          </select>
        </div>
        {!fromGenesis && (
          <div className="field">
            <label>checkpoint id</label>
            <input value={checkpointId} onChange={(e) => setCheckpointId(e.target.value)} placeholder="checkpoint uuid" />
          </div>
        )}
        <div className="field">
          <label>orchestration</label>
          <select value={useGraph ? "graph" : "loop"} onChange={(e) => setUseGraph(e.target.value === "graph")}>
            <option value="loop">bare lockstep loop</option>
            <option value="graph">LangGraph StateGraph</option>
          </select>
        </div>
        <button
          className="btn primary"
          disabled={starting || (!fromGenesis && !checkpointId)}
          onClick={handleStart}
          style={{ width: "100%" }}
        >
          {starting ? "starting…" : "Start session"}
        </button>
      </div>

      {sessions.length > 0 && (
        <div className="sandbox-section">
          <div className="sandbox-label">2 · Sessions</div>
          {sessions.map((s) => (
            <div
              key={s.session_id}
              className={`acct-row ${activeId === s.session_id ? "selected" : ""}`}
              onClick={() => setActiveId(s.session_id)}
            >
              <span className={`badge ${s.status === "done" ? "active" : s.status === "error" ? "closed" : "pending_kyc"}`}>
                {s.status}
              </span>
              <span className="acct-id">{s.session_id}</span>
              <span className="acct-balance mono" style={{ color: "var(--text-dim)", fontWeight: 500 }}>
                {s.steps_taken} steps
              </span>
            </div>
          ))}
        </div>
      )}

      {live && (
        <div className="sandbox-section">
          <div className="sandbox-label">3 · Live steps</div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10, alignItems: "center" }}>
            <span className="pill">
              status <b style={{ color: live.status === "error" ? "var(--danger)" : live.status === "done" ? "var(--accent)" : "var(--amber)" }}>{live.status}</b>
            </span>
            <CopyChip value={live.session_id} display={`session ${live.session_id}`} />
            {live.branch_id && <CopyChip value={live.branch_id} display={`branch ${shortId(live.branch_id, 24)}`} />}
            <span className="pill">committed <b>{live.committed ? "yes" : "no"}</b></span>
          </div>

          {live.error && (
            <div className="callout" style={{ borderLeftColor: "var(--danger)", color: "var(--danger)" }}>
              {live.error}
            </div>
          )}

          {live.step_log.length === 0 && live.status === "running" && (
            <div className="empty-hint">Warming up — first step can take a little while…</div>
          )}

          {[...live.step_log].reverse().map((step) => (
            <div key={step.step} className="feed-item">
              <div className="feed-bar" style={{ background: toolColor(step.tool_name) }} />
              <div>
                <div className="feed-row1">
                  <span className="feed-type" style={{ color: toolColor(step.tool_name) }}>{step.tool_name}</span>
                  <span className={`badge ${step.success ? "active" : "closed"}`}>
                    {step.success ? "ok" : step.error_code ?? "fail"}
                  </span>
                </div>
                {step.reasoning && (
                  <div className="feed-detail" style={{ fontStyle: "italic", color: "var(--text-dim)" }}>
                    “{step.reasoning}”
                  </div>
                )}
                <div className="feed-row1" style={{ marginTop: 3 }}>
                  <span className="feed-seq">step #{step.step}</span>
                  {step.provider_model && (
                    <span className="hash-chip" style={{ color: "var(--violet)", borderColor: "var(--violet)" }}>
                      {step.provider_model}
                      {step.latency_ms != null ? ` · ${Math.round(step.latency_ms)}ms` : ""}
                    </span>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {!live && sessions.length === 0 && (
        <div className="empty-hint">No red-team sessions yet — start one above.</div>
      )}
    </div>
  );
}
