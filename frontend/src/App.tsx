import { useEffect, useRef, useState } from "react";
import { api, wsUrl, type AccountRow, type BranchNode, type BranchState, type SimEvent } from "./api";
import DagGraph from "./DagGraph";
import LiveFeed from "./LiveFeed";
import AccountsPanel from "./AccountsPanel";
import SandboxPanel from "./SandboxPanel";
import CheckpointsPanel from "./CheckpointsPanel";
import Sparkline from "./Sparkline";
import { formatSimTime, shortId } from "./eventStyle";

type Tab = "feed" | "agents" | "checkpoints" | "sandbox";

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
  const [moneyHist, setMoneyHist] = useState<number[]>([]);
  const [txHist, setTxHist] = useState<number[]>([]);
  const [eventHist, setEventHist] = useState<number[]>([]);
  const lastTxCount = useRef(0);

  // WebSocket: live event feed + periodic state ticks for "main"
  useEffect(() => {
    let ws: WebSocket;
    let retry: number;
    function connect() {
      ws = new WebSocket(wsUrl());
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        retry = window.setTimeout(connect, 1500);
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
    const load = () => api.branches().then(setBranches).catch(() => {});
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  // Load state + accounts whenever the selected branch changes (and refresh a paused branch's state)
  useEffect(() => {
    let cancelled = false;
    function refresh() {
      api.branchState(selectedBranch).then((s) => !cancelled && setBranchState(s)).catch(() => {});
      api.accounts(selectedBranch).then((a) => !cancelled && setAccounts(a)).catch(() => {});
    }
    refresh();
    const id = setInterval(refresh, selectedBranch === "main" ? 5000 : 1500);
    return () => {
      cancelled = true;
      clearInterval(id);
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

  const shownEvents = events.filter((e) => e.branch_id === selectedBranch);

  return (
    <div className="app">
      <div className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          FINSIM <span className="brand-sub">// CHRONO</span>
        </div>
        <div className="topbar-spacer" />
        {branchState && (
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
        <button className="btn small" onClick={togglePause}>{paused ? "▶ resume" : "⏸ pause"}</button>
      </div>

      <div className="main">
        <div className="dag-pane">
          <div className="pane-header">
            <span className="pane-title">ChronoDAG</span>
            <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
              — click a branch to inspect it
            </span>
          </div>
          <DagGraph
            branches={branches}
            selectedBranch={selectedBranch}
            onSelect={setSelectedBranch}
            onCheckpointClick={() => setTab("checkpoints")}
          />
        </div>

        <div className="side-pane">
          <div className="tabs">
            <div className={`tab ${tab === "feed" ? "active" : ""}`} onClick={() => setTab("feed")}>Live Feed</div>
            <div className={`tab ${tab === "agents" ? "active" : ""}`} onClick={() => { setTab("agents"); setSelectedAccount(null); }}>Agents</div>
            <div className={`tab ${tab === "checkpoints" ? "active" : ""}`} onClick={() => setTab("checkpoints")}>Checkpoints</div>
            <div className={`tab ${tab === "sandbox" ? "active" : ""}`} onClick={() => setTab("sandbox")}>Sandbox</div>
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
              />
            )}
            {tab === "sandbox" && (
              <SandboxPanel
                branches={branches}
                accounts={accounts}
                selectedBranch={selectedBranch}
                onForked={(id) => {
                  setSelectedBranch(id);
                  setTab("agents");
                }}
              />
            )}
          </div>
        </div>
      </div>

      <div className="statstrip">
        <StatTile label="money supply" value={branchState ? "₹" + (branchState.money_supply_paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 }) : "—"} data={moneyHist} color="#5ef2b5" />
        <StatTile label="transactions" value={branchState ? branchState.tx_count.toLocaleString() : "—"} data={txHist} color="#6fb7ff" />
        <StatTile label="accounts" value={branchState ? String(branchState.account_count) : "—"} data={[]} color="#b78bff" />
        <StatTile label="events / branch" value={branchState ? `#${branchState.head_seq_num}` : "—"} data={eventHist} color="#ffb454" />
      </div>
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
