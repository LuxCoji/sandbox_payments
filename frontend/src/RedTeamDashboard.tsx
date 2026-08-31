import { useEffect, useMemo, useRef, useState } from "react";
import { api, redteamWsUrl, type RedTeamSession, type RedTeamStep } from "./api";
import CopyChip from "./CopyChip";
import { shortId } from "./eventStyle";

// One fixed color per tool — identity should never repaint as data changes,
// so this table is the single source of truth reused by the step feed, the
// tool-usage chart, and anywhere else a tool shows up.
const TOOL_COLOR: Record<string, string> = {
  create_account: "#2fe6d1",
  transfer_funds: "#9b7bff",
  make_payment: "#ff8a3d",
  inspect_account: "#8b93a3",
  commit_strategy: "#ffd23f",
  fork_branch: "#ff5fa8",
  diff_branches: "#ff4757",
};
function toolColor(name: string): string {
  return TOOL_COLOR[name] ?? "#4a5162";
}

// Distinct identity channel from tool color — provider/model bars need
// their own fixed order so a model's color doesn't collide with a tool's.
const PROVIDER_COLORS = ["#2fe6d1", "#ff8a3d", "#ffd23f", "#9b7bff", "#ff5fa8", "#8b93a3"];

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
  const [formHighlighted, setFormHighlighted] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const startFormRef = useRef<HTMLDivElement | null>(null);

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
        setLive((prev) => {
          if (!prev) return prev;
          // The websocket replays a session's ENTIRE step history on every
          // new connection (api/main.py::redteam_stream, "catch up" for a
          // subscriber that connects mid- or post-session) — but the
          // initial `api.redteamSession(activeId)` fetch a few lines above
          // already loaded that same history via step_log. Without this
          // check, every step you'd already have gets appended a second
          // time (same `step` number, same key) — clicking any session
          // that already had steps showed its whole feed duplicated.
          if (prev.step_log.some((s) => s.step === step.step)) return prev;
          return { ...prev, steps_taken: step.step, step_log: [...prev.step_log, step] };
        });
      } else if (data.type === "done") {
        setLive((prev) =>
          prev
            ? {
                ...prev, status: data.status, committed: data.committed, error: data.error,
                end_checkpoint_id: data.end_checkpoint_id, commit_reasoning: data.commit_reasoning,
              }
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
          end_checkpoint_id: null, commit_reasoning: null, pool_from_branch_ids: [...poolFromIds], error: null,
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
    <div className="rt-dashboard team-red">
      <div className="rt-side">
        <div className="rt-side-icon">🎯</div>
        <div className="callout">
          Fork off a warm checkpoint, hand an LLM the account tools, and watch it decide —
          one action at a time — what it thinks a fraud system won't catch.
        </div>

        <div
          className="sandbox-section"
          ref={startFormRef}
          style={formHighlighted ? { outline: "2px solid var(--violet)", borderRadius: 8, transition: "outline 0.3s" } : undefined}
        >
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

        <div className="rt-side-footer credit">Spoider_Boys <i>·</i> IIT Kharagpur</div>
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
                      // This only fills in the start form — it doesn't launch
                      // anything by itself, and that form lives in the left
                      // sidebar, a separate scroll area from this step feed,
                      // so without this the fill-in was invisible and looked
                      // like the button did nothing.
                      startFormRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
                      setFormHighlighted(true);
                      setTimeout(() => setFormHighlighted(false), 1500);
                    }}
                    title="Fills in the start form with this session's end checkpoint — scroll to it, then click Start session to actually launch the continuation"
                  >
                    ▶ Continue from here (fills in the form below)
                  </button>
                </>
              )}
            </div>

            {/* Lineage — where this session actually started from. Was
                shown nowhere at all before: from_genesis/checkpoint_id and
                pool_from_branch_ids were fetched but never rendered, so
                there was no way to tell a fresh session apart from a
                continuation, or see what it pooled findings from, without
                reading raw API responses. */}
            <div className="rt-header" style={{ marginTop: -6, marginBottom: 14 }}>
              <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
                {live.from_genesis ? "started: fresh warmup" : "forked from checkpoint"}
              </span>
              {!live.from_genesis && live.checkpoint_id && (
                <CopyChip value={live.checkpoint_id} display={`${shortId(live.checkpoint_id, 13)}…`} />
              )}
              {live.pool_from_branch_ids.length > 0 && (
                <>
                  <span style={{ fontSize: 11, color: "var(--text-faint)" }}>· pooled findings from</span>
                  {live.pool_from_branch_ids.map((bid) => (
                    <CopyChip key={bid} value={bid} display={shortId(bid, 24)} />
                  ))}
                </>
              )}
            </div>

            {live.error && (
              <div className="callout" style={{ borderLeftColor: "var(--danger)", color: "var(--danger)" }}>
                {live.error}
              </div>
            )}

            {/* The actual finding, once committed — was written to Postgres
                branch metadata (_record_commit_reasoning) but never surfaced
                in the UI at all before this; the only way to see it was a
                raw SQL query against the branches table. */}
            {live.commit_reasoning && (
              <div className="callout" style={{ borderLeftColor: "var(--accent)", color: "var(--text)" }}>
                <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--accent)", fontWeight: 700, marginBottom: 4 }}>
                  📌 Committed finding
                </div>
                {live.commit_reasoning}
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
