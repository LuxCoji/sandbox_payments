import { useEffect, useMemo, useRef, useState } from "react";
import { api, redteamWsUrl, type RedTeamSession, type RedTeamStep } from "./api";
import CopyChip from "./CopyChip";
import { shortId } from "./eventStyle";

// One fixed color per tool — identity should never repaint as data changes,
// so this table is the single source of truth reused by the step feed, the
// tool-usage chart, and anywhere else a tool shows up.
const TOOL_COLOR: Record<string, string> = {
  create_account: "#6fb7ff",
  transfer_funds: "#b78bff",
  make_payment: "#5ef2b5",
  inspect_account: "#8892a3",
  commit_strategy: "#ffb454",
  fork_branch: "#ff9ecb",
  diff_branches: "#e6a8ff",
};
function toolColor(name: string): string {
  return TOOL_COLOR[name] ?? "#4d5567";
}

// Distinct identity channel from tool color — provider/model bars need
// their own fixed order so a model's color doesn't collide with a tool's.
const PROVIDER_COLORS = ["#6fb7ff", "#5ef2b5", "#ffb454", "#b78bff", "#ff9ecb", "#8892a3"];

interface Props {
  initialCheckpointId?: string | null;
  initialSessionId?: string | null;
}

export default function RedTeamDashboard({ initialCheckpointId, initialSessionId }: Props) {
  const [sessions, setSessions] = useState<RedTeamSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [live, setLive] = useState<RedTeamSession | null>(null);
  const [starting, setStarting] = useState(false);
  const [fromGenesis, setFromGenesis] = useState(!initialCheckpointId);
  const [checkpointId, setCheckpointId] = useState(initialCheckpointId ?? "");
  const [useGraph, setUseGraph] = useState(false);
  // Sessions to pool target_notes/commit_reasoning from, in addition to
  // whichever branch checkpointId itself was forked from — lets several
  // independent sessions' findings combine into one continuing session
  // instead of only ever inheriting from direct lineage.
  const [poolFromIds, setPoolFromIds] = useState<Set<string>>(new Set());
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (initialCheckpointId) {
      setCheckpointId(initialCheckpointId);
      setFromGenesis(false);
    }
  }, [initialCheckpointId]);

  // A branch clicked in the main ChronoDAG graph (a "red-team/<id>" lane)
  // should immediately select that session's live feed here.
  useEffect(() => {
    if (initialSessionId) setActiveId(initialSessionId);
  }, [initialSessionId]);

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
        setLive((prev) =>
          prev
            ? { ...prev, status: data.status, committed: data.committed, error: data.error, end_checkpoint_id: data.end_checkpoint_id }
            : prev
        );
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
        poolFromBranchIds: [...poolFromIds],
      });
      setActiveId(session_id);
      setSessions((prev) => [
        {
          session_id, status: "running", from_genesis: fromGenesis, checkpoint_id: checkpointId || null,
          use_graph: useGraph, branch_id: null, steps_taken: 0, max_steps: 0, committed: false,
          end_checkpoint_id: null, error: null,
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

  // ── derived chart data (pure function of live.step_log — no extra fetches) ──

  const toolCounts = useMemo(() => {
    if (!live) return [];
    const counts = new Map<string, number>();
    for (const s of live.step_log) counts.set(s.tool_name, (counts.get(s.tool_name) ?? 0) + 1);
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([tool_name, count]) => ({ label: tool_name, value: count, color: toolColor(tool_name) }));
  }, [live]);

  const providerStats = useMemo(() => {
    if (!live) return [];
    const byModel = new Map<string, { calls: number; totalMs: number }>();
    for (const s of live.step_log) {
      if (!s.provider_model) continue;
      const cur = byModel.get(s.provider_model) ?? { calls: 0, totalMs: 0 };
      cur.calls += 1;
      cur.totalMs += s.latency_ms ?? 0;
      byModel.set(s.provider_model, cur);
    }
    return [...byModel.entries()]
      .sort((a, b) => b[1].calls - a[1].calls)
      .map(([model, { calls, totalMs }], i) => ({
        label: model,
        value: Math.round(totalMs / calls),
        sublabel: `${calls}×`,
        color: PROVIDER_COLORS[i % PROVIDER_COLORS.length],
      }));
  }, [live]);

  const successCount = live ? live.step_log.filter((s) => s.success).length : 0;
  const failCount = live ? live.step_log.length - successCount : 0;
  const successRate = live && live.step_log.length > 0 ? Math.round((successCount / live.step_log.length) * 100) : null;

  return (
    <div className="rt-dashboard">
      <div className="rt-side">
        <div className="callout">
          Fork off a warm checkpoint, hand an LLM the account tools, and watch it decide —
          one action at a time — what it thinks a fraud system won't catch.
        </div>

        <div className="sandbox-section">
          <div className="sandbox-label">Start a session</div>
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
          {sessions.some((s) => s.status === "done" && s.branch_id) && (
            <div className="field">
              <label>pool findings from (optional)</label>
              <div style={{ maxHeight: 120, overflowY: "auto", display: "flex", flexDirection: "column", gap: 5 }}>
                {sessions.filter((s) => s.status === "done" && s.branch_id).map((s) => (
                  <label
                    key={s.session_id}
                    style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 11, fontFamily: "var(--mono)", color: "var(--text-dim)", cursor: "pointer" }}
                  >
                    <input
                      type="checkbox"
                      checked={poolFromIds.has(s.branch_id!)}
                      onChange={(e) => {
                        setPoolFromIds((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(s.branch_id!);
                          else next.delete(s.branch_id!);
                          return next;
                        });
                      }}
                    />
                    {s.session_id} {s.committed && <span style={{ color: "var(--accent)" }}>✓ committed</span>}
                  </label>
                ))}
              </div>
            </div>
          )}
          <button
            className="btn primary"
            disabled={starting || (!fromGenesis && !checkpointId)}
            onClick={handleStart}
            style={{ width: "100%" }}
          >
            {starting ? "starting…" : "Start session"}
          </button>
        </div>

        <div className="sandbox-section">
          <div className="sandbox-label">Sessions ({sessions.length})</div>
          {sessions.length === 0 && <div className="empty-hint">None yet.</div>}
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
                {s.steps_taken}{s.max_steps ? `/${s.max_steps}` : ""}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="rt-main">
        {!live && (
          <div className="empty-hint" style={{ marginTop: 60 }}>
            Start a session, or pick one from the list, to see it here.
          </div>
        )}

        {live && (
          <>
            <div className="rt-header">
              <span className="rt-header-title">Session</span>
              <CopyChip value={live.session_id} display={live.session_id} />
              <span className={`badge ${live.status === "done" ? "active" : live.status === "error" ? "closed" : "pending_kyc"}`}>
                {live.status}
              </span>
              {live.branch_id && <CopyChip value={live.branch_id} display={`branch ${shortId(live.branch_id, 24)}`} />}
              <span className="pill">committed <b>{live.committed ? "yes" : "no"}</b></span>
              <span className="pill">orchestration <b>{live.use_graph ? "LangGraph" : "lockstep loop"}</b></span>
              {live.end_checkpoint_id && (
                <>
                  <CopyChip value={live.end_checkpoint_id} display={`checkpoint ${shortId(live.end_checkpoint_id, 13)}…`} />
                  <button
                    className="btn small"
                    style={{ borderColor: "var(--violet)", color: "var(--violet)" }}
                    onClick={() => {
                      setCheckpointId(live.end_checkpoint_id!);
                      setFromGenesis(false);
                    }}
                    title="Fork a new session from this one's end state — it also inherits this session's save_note/commit_strategy findings, not just the account balances"
                  >
                    ▶ Continue from here
                  </button>
                </>
              )}
            </div>

            {live.error && (
              <div className="callout" style={{ borderLeftColor: "var(--danger)", color: "var(--danger)" }}>
                {live.error}
              </div>
            )}

            <div className="rt-stats">
              <div className="rt-stat">
                <div className="rt-stat-label">steps</div>
                <div className="rt-stat-value">{live.steps_taken}{live.max_steps ? ` / ${live.max_steps}` : ""}</div>
                {live.max_steps > 0 && (
                  <div className="rt-meter-track">
                    <div
                      className="rt-meter-fill"
                      style={{ width: `${Math.min(100, (live.steps_taken / live.max_steps) * 100)}%` }}
                    />
                  </div>
                )}
              </div>
              <div className="rt-stat">
                <div className="rt-stat-label">success rate</div>
                <div className="rt-stat-value" style={{ color: successRate == null ? "var(--text)" : successRate >= 70 ? "var(--accent)" : successRate >= 40 ? "var(--amber)" : "var(--danger)" }}>
                  {successRate == null ? "—" : `${successRate}%`}
                </div>
              </div>
              <div className="rt-stat">
                <div className="rt-stat-label">ok / failed</div>
                <div className="rt-stat-value">{successCount} / {failCount}</div>
              </div>
              <div className="rt-stat">
                <div className="rt-stat-label">providers used</div>
                <div className="rt-stat-value">{providerStats.length}</div>
              </div>
            </div>

            <div className="rt-charts">
              <div className="rt-chart">
                <div className="rt-chart-title">Tool usage</div>
                {toolCounts.length === 0 && <div className="empty-hint">No steps yet.</div>}
                {toolCounts.map((d) => (
                  <BarRow key={d.label} {...d} maxValue={toolCounts[0]?.value ?? 1} />
                ))}
              </div>
              <div className="rt-chart">
                <div className="rt-chart-title">Provider routing — avg latency</div>
                {providerStats.length === 0 && <div className="empty-hint">No steps yet.</div>}
                {providerStats.map((d) => (
                  <BarRow key={d.label} {...d} maxValue={Math.max(...providerStats.map((p) => p.value), 1)} valueSuffix="ms" />
                ))}
              </div>
            </div>

            <div className="sandbox-label" style={{ marginBottom: 10 }}>Live steps</div>
            {live.step_log.length === 0 && live.status === "running" && (
              <div className="empty-hint">Warming up — first step can take a little while…</div>
            )}
            {[...live.step_log].reverse().map((step) => (
              <div key={step.step} className="feed-item">
                <div className="feed-bar" style={{ background: toolColor(step.tool_name) }} />
                <div>
                  <div className="feed-row1">
                    <span className="feed-type" style={{ color: toolColor(step.tool_name) }}>{step.tool_name}</span>
                    <span className={`badge ${step.success ? "active" : step.error_code === "INTERNAL_ERROR" ? "bug" : "closed"}`}>
                      {step.success ? "ok" : step.error_code === "INTERNAL_ERROR" ? "🐛 bug" : step.error_code ?? "fail"}
                    </span>
                  </div>
                  {step.reasoning && (
                    <div className="feed-detail" style={{ fontStyle: "italic", color: "var(--text-dim)" }}>
                      “{step.reasoning}”
                    </div>
                  )}
                  {/* error_message: the actual failure text, not just the code — the whole
                      point is telling a real bug (🐛, logged server-side, see
                      sim/gateway/errors.py) apart from an expected business rejection
                      (LIMIT_EXCEEDED etc, working as intended). */}
                  {!step.success && step.error_message && (
                    <div
                      className="feed-detail"
                      style={{ color: step.error_code === "INTERNAL_ERROR" ? "var(--danger)" : "var(--text-dim)" }}
                    >
                      {step.error_message}
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
          </>
        )}
      </div>
    </div>
  );
}

function BarRow({
  label, value, color, maxValue, sublabel, valueSuffix,
}: { label: string; value: number; color: string; maxValue: number; sublabel?: string; valueSuffix?: string }) {
  const pct = Math.max(2, (value / maxValue) * 100);
  return (
    <div className="rt-bar-row">
      <span className="rt-bar-label" title={label}>{label}</span>
      <div className="rt-bar-track">
        <div className="rt-bar-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="rt-bar-value">
        {value}{valueSuffix ?? ""}{sublabel ? ` · ${sublabel}` : ""}
      </span>
    </div>
  );
}
