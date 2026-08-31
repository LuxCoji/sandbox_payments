import { useEffect, useRef, useState } from "react";
import { api, wsUrl, type AccountRow, type BranchNode, type BranchState, type SimEvent } from "./api";
import DagGraph from "./DagGraph";
import LiveFeed from "./LiveFeed";
import AccountsPanel from "./AccountsPanel";
import SandboxPanel from "./SandboxPanel";
import CheckpointsPanel from "./CheckpointsPanel";
import BlueTeamDashboard from "./BlueTeamDashboard";
import RedTeamDashboard from "./RedTeamDashboard";
import Sparkline from "./Sparkline";
import { formatSimTime, shortId } from "./eventStyle";

type Tab = "feed" | "agents" | "checkpoints" | "sandbox" | "historical";
type View = "sim" | "redteam" | "blueteam";

const MAX_FEED = 150;
const MAX_SPARK = 40;

export default function App() {
  const [branches, setBranches] = useState<BranchNode[]>([]);
  const [selectedBranch, setSelectedBranch] = useState("main");
  const [branchState, setBranchState] = useState<BranchState | null>(null);
  const [accounts, setAccounts] = useState<AccountRow[]>([]);
  const [selectedAccount, setSelectedAccount] = useState<string | null>(null);
  const [events, setEvents] = useState<SimEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [paused, setPaused] = useState(false);
  const [tab, setTab] = useState<Tab>("feed");
  const [view, setView] = useState<View>("sim");
  const [redteamPrefillCheckpoint, setRedteamPrefillCheckpoint] = useState<string | null>(null);
  const [redteamPrefillSessionId, setRedteamPrefillSessionId] = useState<string | null>(null);
  const [moneyHist, setMoneyHist] = useState<number[]>([]);
  const [txHist, setTxHist] = useState<number[]>([]);
  const [eventHist, setEventHist] = useState<number[]>([]);
  const lastTxCount = useRef(0);

  // WebSocket: live event feed + periodic state ticks for "main"
  useEffect(() => {
    let ws: WebSocket;
    let retry: number;
    let delay = 1500;
    function connect() {
      ws = new WebSocket(wsUrl());
      ws.onopen = () => {
        setConnected(true);
        delay = 1500;
      };
      ws.onclose = () => {
        setConnected(false);
        delay = Math.min(delay * 1.5, 30000);
        retry = window.setTimeout(connect, delay + Math.random() * 500);
      };
      ws.onmessage = (msg) => {
        const data = JSON.parse(msg.data);
        if (data.type === "event") {
          setEvents((prev) => [...prev.slice(-MAX_FEED + 1), data.event]);
          if (data.event.branch_id === selectedBranch) {
            setEventHist((prev) => [...prev.slice(-MAX_SPARK + 1), data.event.seq_num]);
          }
        } else if (data.type === "tick") {
          const st: BranchState = data.state;
          if (selectedBranch === "main") setBranchState(st);
          setMoneyHist((prev) => [...prev.slice(-MAX_SPARK + 1), st.money_supply_paise]);
          setTxHist((prev) => {
            const delta = st.tx_count - lastTxCount.current;
            lastTxCount.current = st.tx_count;
            return [...prev.slice(-MAX_SPARK + 1), Math.max(0, delta)];
          });
        }
      };
    }
    connect();
    return () => {
      window.clearTimeout(retry);
      ws?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Poll branch list (fork topology changes infrequently)
  useEffect(() => {
    let timerId: number;
    let cancelled = false;
    const load = async () => {
      try {
        const b = await api.branches();
        if (!cancelled) setBranches(b);
      } catch (e) {}
      if (!cancelled) timerId = window.setTimeout(load, 3000);
    };
    load();
    return () => {
      cancelled = true;
      clearTimeout(timerId);
    };
  }, []);

  // Load state + accounts whenever the selected branch changes (and refresh a paused branch's state)
  useEffect(() => {
    let timerId: number;
    let cancelled = false;
    const refresh = async () => {
      if (selectedBranch.startsWith("red-team/")) return;
      try {
        const s = await api.branchState(selectedBranch);
        if (!cancelled) setBranchState(s);
        const a = await api.accounts(selectedBranch);
        if (!cancelled) setAccounts(a);
      } catch (e) {}
      if (!cancelled) timerId = window.setTimeout(refresh, selectedBranch === "main" ? 5000 : 1500);
    };
    refresh();
    return () => {
      cancelled = true;
      clearTimeout(timerId);
    };
  }, [selectedBranch]);

  async function togglePause() {
    const next = !paused;
    setPaused(next);
    await fetch("/api/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused: next }),
    });
  }

  async function handleReset() {
    if (!confirm("Are you sure you want to completely reset the simulation? All branches and checkpoints will be lost.")) return;
    try {
      await api.resetSimulation();
      window.location.reload();
    } catch (e: any) {
      alert("Failed to reset: " + e.message);
    }
  }

  const shownEvents = events.filter((e) => e.branch_id === selectedBranch);

  return (
    <div className={`app ${view !== "sim" ? "no-statstrip" : ""}`}>
      <div className="topbar">
        <div className="brand">
          <svg className="brand-mark" width="32" height="20" viewBox="0 0 32 20">
            <defs>
              <clipPath id="mc-lens">
                <circle cx="12" cy="10" r="7" />
              </clipPath>
            </defs>
            <circle cx="12" cy="10" r="7" fill="#eb001b" />
            <circle cx="20" cy="10" r="7" fill="#f79e1b" />
            <circle cx="20" cy="10" r="7" fill="#ff5f00" clipPath="url(#mc-lens)" />
          </svg>
          <span className="brand-word">FINSIM</span>
          <span className="brand-sub">CHRONO</span>
        </div>
        <span className="credit">Spoider_Boys <i>·</i> IIT Kharagpur</span>
        <div className="view-switch">
          <button className={`view-btn ${view === "sim" ? "active" : ""}`} onClick={() => setView("sim")}>Simulation</button>
          <button className={`view-btn view-btn-red ${view === "redteam" ? "active" : ""}`} onClick={() => setView("redteam")}>🔴 Red Team</button>
          <button className={`view-btn view-btn-blue ${view === "blueteam" ? "active" : ""}`} onClick={() => setView("blueteam")}>🛡️ Blue Team</button>
        </div>
        <div className="topbar-spacer" />
        {view === "sim" && branchState && (
          <>
            <div className="pill">
              <span>clock</span>
              <b>{formatSimTime(branchState.sim_time_ns)}</b>
            </div>
            <div className="hash-chip">{shortId(branchState.state_hash, 16)}…</div>
          </>
        )}
        <div className="pill">
          <span className={`conn-dot ${connected ? "up" : "down"}`} />
          {connected ? "live" : "reconnecting"}
        </div>
        {view === "sim" && (
          <>
            <button className="btn small" onClick={togglePause}>{paused ? "▶ resume" : "⏸ pause"}</button>
            <button className="btn small danger" onClick={handleReset}>⟲ reset</button>
          </>
        )}
      </div>

      {view === "redteam" ? (
        <RedTeamDashboard
          initialCheckpointId={redteamPrefillCheckpoint}
          initialSessionId={redteamPrefillSessionId}
        />
      ) : view === "blueteam" ? (
        <BlueTeamDashboard />
      ) : (
      <div className="main">
        <div className="dag-pane">
          <div className="pane-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <span className="pane-title">ChronoDAG</span>
              <span style={{ color: "var(--text-faint)", fontSize: 11, marginLeft: 8 }}>
                — click a branch to inspect it
              </span>
            </div>
            <span className="credit">Spoider_Boys <i>·</i> IIT Kharagpur</span>
          </div>
          <DagGraph
            branches={branches}
            selectedBranch={selectedBranch}
            onSelect={(branchId) => {
              setSelectedBranch(branchId);
              // Red-team branches live in the real Postgres store, not the
              // demo SimSession — the Agents/Checkpoints tabs can't show
              // them (they'd just 404 against the demo backend), so route
              // straight to the Red Team view and select that session there.
              if (branchId.startsWith("red-team/")) {
                setRedteamPrefillSessionId(branchId.slice("red-team/".length));
                setView("redteam");
              }
            }}
            onCheckpointClick={() => setTab("checkpoints")}
          />
        </div>

        <div className="side-pane">
          <div className="tabs">
            <div className={`tab ${tab === "feed" ? "active" : ""}`} onClick={() => setTab("feed")}>Live Feed</div>
            <div className={`tab ${tab === "agents" ? "active" : ""}`} onClick={() => { setTab("agents"); setSelectedAccount(null); }}>Agents</div>
            <div className={`tab ${tab === "checkpoints" ? "active" : ""}`} onClick={() => setTab("checkpoints")}>Checkpoints</div>
            <div className={`tab ${tab === "sandbox" ? "active" : ""}`} onClick={() => setTab("sandbox")}>Sandbox</div>
            <div className={`tab ${tab === "historical" ? "active" : ""}`} onClick={() => setTab("historical")}>Historical</div>
          </div>
          <div className="tab-body">
            {tab === "feed" && <LiveFeed events={shownEvents} />}
            {tab === "agents" && (
              <AccountsPanel
                branchId={selectedBranch}
                accounts={accounts}
                selected={selectedAccount}
                onSelect={setSelectedAccount}
              />
            )}
            {tab === "checkpoints" && (
              <CheckpointsPanel
                branch={branches.find((b) => b.branch_id === selectedBranch)}
                onForked={(id) => {
                  setSelectedBranch(id);
                  setTab("agents");
                }}
                onUseForRedTeam={(checkpointId) => {
                  setRedteamPrefillCheckpoint(checkpointId);
                  setView("redteam");
                }}
              />
            )}
            {tab === "sandbox" && (
              <SandboxPanel
                branches={branches.filter(b => !b.branch_id.startsWith("red-team/"))}
                accounts={accounts}
                selectedBranch={selectedBranch}
                onForked={(id) => {
                  setSelectedBranch(id);
                  setTab("agents");
                }}
              />
            )}
            {tab === "historical" && (
              <div style={{ padding: 16 }}>
                <div style={{ marginBottom: 16, color: "var(--text-faint)", fontSize: 13, lineHeight: 1.4 }}>
                  Previous Red Team sessions saved in the Postgres database. 
                  These belong to past simulation runs.
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {branches.filter(b => b.branch_id.startsWith("red-team/")).length === 0 && (
                    <div className="empty-hint">No historical sessions found.</div>
                  )}
                  {branches.filter(b => b.branch_id.startsWith("red-team/")).map(b => (
                    <div 
                      key={b.branch_id} 
                      className="inspector-card"
                      style={{ cursor: "pointer" }}
                      onClick={() => {
                        setRedteamPrefillSessionId(b.branch_id.slice("red-team/".length));
                        setView("redteam");
                      }}
                    >
                      <div style={{ fontWeight: 600, color: "var(--accent)", marginBottom: 4 }}>{b.name}</div>
                      <div style={{ fontSize: 12, color: "var(--text-bright)", marginBottom: 6, fontStyle: "italic" }}>
                        "{(b as any).commit_reasoning || "No reasoning recorded"}"
                      </div>
                      <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
                        Forked at event #{b.fork_seq_num} • Head at #{b.head_seq_num}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
      )}

      {view === "sim" && (
      <div className="statstrip">
        <StatTile label="money supply" value={branchState ? "₹" + (branchState.money_supply_paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 }) : "—"} data={moneyHist} color="#ff8a3d" />
        <StatTile label="transactions" value={branchState ? branchState.tx_count.toLocaleString() : "—"} data={txHist} color="#2fe6d1" />
        <StatTile label="accounts" value={branchState ? String(branchState.account_count) : "—"} data={[]} color="#9b7bff" />
        <StatTile label="events / branch" value={branchState ? `#${branchState.head_seq_num}` : "—"} data={eventHist} color="#ffd23f" />
      </div>
      )}
    </div>
  );
}

function StatTile({ label, value, data, color }: { label: string; value: string; data: number[]; color: string }) {
  return (
    <div className="stat-tile">
      <div className="stat-meta">
        <div className="stat-label">{label}</div>
        <div className="stat-value mono">{value}</div>
      </div>
      {data.length > 1 && (
        <div className="stat-spark">
          <Sparkline values={data} color={color} />
        </div>
      )}
    </div>
  );
}
