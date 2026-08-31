import { useEffect, useState } from "react";
import {
  api,
  type ChainHop,
  type RetrainStatus,
  type RiskCase,
  type RiskSummary,
} from "./api";
import { formatPaise, shortId } from "./eventStyle";

/**
 * The defenders' view: what the fraud rails have seen, what they flagged,
 * and what a reviewer can do about it right now. Same shell as Red Team
 * (side = your identity and the model, main = the live queue) — this is
 * the same simulator, looked at from the other chair.
 *
 * The banner is the important part. A card rail with no model allows every
 * payment, and a card rail that is working but finding nothing shows exactly
 * the same counts — so the page says which, rather than leaving an empty
 * queue to be read either way.
 */
export default function BlueTeamDashboard() {
  const [summary, setSummary] = useState<RiskSummary | null>(null);
  const [cases, setCases] = useState<RiskCase[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [retrain, setRetrain] = useState<RetrainStatus | null>(null);
  const [busy, setBusy] = useState(false);
  // A reviewer's name is required on every action - "who decided this" is the
  // first question an audit asks, and defaulting it to "system" would make
  // every freeze unattributable.
  const [reviewer, setReviewer] = useState("");
  const [openCase, setOpenCase] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const poll = async () => {
      try {
        const [s, c, r] = await Promise.all([
          api.riskSummary(controller.signal),
          api.riskCases(controller.signal),
          api.retrainStatus(controller.signal),
        ]);
        setSummary(s);
        setCases(c.cases);
        setRetrain(r);
        setError(null);
      } catch (e) {
        if (!controller.signal.aborted) setError(String(e));
      }
    };
    poll();
    const timer = setInterval(poll, 2000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, []);

  const startRetrain = async () => {
    setBusy(true);
    try {
      await api.startRetrain();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const rollback = async () => {
    setBusy(true);
    try {
      await api.rollbackModel();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const running = retrain?.status === "running";

  const act = async (
    action: "freeze" | "clear" | "stepUp",
    caseId: string,
  ) => {
    if (!reviewer.trim()) {
      setOutcome("Enter your name first — every decision is recorded against it.");
      return;
    }
    setBusy(true);
    try {
      const call =
        action === "freeze"
          ? api.freezeCase
          : action === "clear"
            ? api.clearCase
            : api.stepUpCase;
      await call({ case_id: caseId, reviewer, reason: "" });
      setOutcome(
        action === "freeze"
          ? `Freeze requested on ${caseId} — recorded, not executed. The engine holds the funds.`
          : action === "clear"
            ? `Cleared ${caseId} — kept in the log and reopenable.`
            : `Challenge sent for ${caseId}.`,
      );
    } catch (e) {
      // The refusals here are the interesting ones: a freeze with no reviewer,
      // or a step-up on the wire rail, which would be tipping off.
      setOutcome(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rt-dashboard team-blue">
      <div className="rt-side">
        <div className="rt-side-icon">🛡️</div>
        <div className="callout">
          Every payment the engine scores lands here. A case is a claim, not a
          verdict — freeze holds funds without acting, clear keeps the case
          in the log, and a step-up challenge asks the account holder to
          prove it was them.
        </div>

        <div className="bt-side-section">
          <div className="sandbox-label">Reviewer</div>
          <div className="field">
            <label>your name — recorded against every decision</label>
            <input
              value={reviewer}
              placeholder="e.g. priya.s"
              onChange={(e) => setReviewer(e.target.value)}
            />
          </div>
        </div>

        {summary?.enabled && (
          <div className="bt-side-section">
            <div className="sandbox-label">Model</div>
            <div className="inspector-card" style={{ marginBottom: 8 }}>
              <div className="kv">
                <span className="kv-key">live version</span>
                <span className="kv-val">
                  {retrain?.registry?.live_version ? `v${retrain.registry.live_version}` : "shipped default"}
                </span>
              </div>
              {retrain?.registry?.live_recall != null && (
                <div className="kv">
                  <span className="kv-key">recall @ 2% flag</span>
                  <span className="kv-val">{(retrain.registry.live_recall * 100).toFixed(1)}%</span>
                </div>
              )}
              <div className="kv">
                <span className="kv-key">card model</span>
                <span className={`badge ${summary.card_model_loaded ? "active" : "closed"}`}>
                  {summary.card_model_loaded ? "loaded" : "not loaded"}
                </span>
              </div>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button className="btn small" disabled={busy || running} onClick={startRetrain} style={{ flex: 1 }}>
                {running ? "Retraining…" : "Retrain"}
              </button>
              {retrain?.registry?.can_rollback && (
                <button className="btn small" disabled={busy || running} onClick={rollback} style={{ flex: 1 }}>
                  Roll back
                </button>
              )}
            </div>
            <p className="fraud-note">
              Retraining scores a candidate against the live model on traffic
              neither trained on, and only promotes it if it wins — a model
              that learns from its own decisions could be taught by an
              attacker who got one attack through.
            </p>
            {retrain?.status === "declined" && <div className="fraud-outcome">Declined — {retrain.error}</div>}
            {retrain?.status === "failed" && <div className="fraud-outcome bad">Failed — {retrain.error}</div>}
            {retrain?.status === "done" && retrain.result && (
              <div className="fraud-outcome">
                {retrain.result.promoted ? "Promoted" : "Kept current model"} — {retrain.result.reason}
              </div>
            )}
          </div>
        )}

        {(retrain?.registry?.history?.length ?? 0) > 0 && (
          <div className="bt-side-section">
            <div className="sandbox-label">Version history</div>
            {[...retrain!.registry.history].reverse().map((v) => (
              <div key={v.version} className="kv" style={{ fontSize: 11 }}>
                <span className="kv-key mono">v{v.version} · {v.rows.toLocaleString()} rows</span>
                <span className="kv-val">{(v.recall_at_2pct * 100).toFixed(1)}%</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="rt-main">
        <div className="rt-header">
          <span className="rt-header-title">Case queue</span>
          {summary && (
            <span className="pill">flag rate <b>{((summary.flag_rate ?? 0) * 100).toFixed(2)}%</b></span>
          )}
          {summary && <span className="pill">for review <b>{count(summary.review)}</b></span>}
        </div>

        {error && <div className="callout" style={{ borderLeftColor: "var(--danger)", color: "var(--danger)" }}>Could not reach the fraud rails: {error}</div>}
        {!error && !summary && <div className="empty-hint">Loading…</div>}
        {!error && summary && !summary.enabled && <div className="empty-hint">Fraud detection is off for this session.</div>}

        {summary?.enabled && (
          <>
            {!summary.card_model_loaded && (
              <div className="fraud-banner">
                <strong>The card rail has no trained model.</strong> Every payment is
                being allowed. An empty card queue below means nothing is scoring
                them — not that nothing was found.
              </div>
            )}

            <div className="fraud-tiles">
              <Tile n={count(summary.scored)} k="scored" />
              <Tile n={count(summary.flagged)} k="flagged" />
              <Tile n={count(summary.blocked)} k="blocked" />
              <Tile n={count(summary.review)} k="for review" />
              <Tile n={count(summary.accounts_tracked)} k="accounts" />
            </div>

            {outcome && <div className="fraud-outcome" style={{ marginBottom: 14 }}>{outcome}</div>}

            <div className="sandbox-label" style={{ marginBottom: 10 }}>Cases</div>
            {cases.length === 0 && <div className="empty-hint">No cases raised.</div>}
            {cases.map((c) => (
              <div key={c.tx_id} className="bt-case-card">
                <div className="bt-case-head">
                  <span className="mono" style={{ fontSize: 12, color: "var(--text)" }}>{shortId(c.tx_id)}</span>
                  <span className={`rail rail-${c.rail}`}>{c.rail}</span>
                  <span className="pill" style={{ padding: "2px 8px" }}>{c.action}</span>
                  <span className="mono" style={{ fontSize: 11, color: "var(--text-dim)" }}>score {c.score.toFixed(2)}</span>
                  <span className="mono" style={{ fontSize: 11, color: "var(--rail-cyan)" }}>{formatPaise(c.amount_paise)}</span>
                  <span style={{ flex: 1 }} />
                  {(c.chain?.length ?? 0) > 0 ? (
                    <button className="bt-trace" onClick={() => setOpenCase(openCase === c.tx_id ? null : c.tx_id)}>
                      {openCase === c.tx_id ? "hide trail" : `🔎 trace money (${c.chain!.length})`}
                    </button>
                  ) : (
                    <span className="bt-trace-none" title={c.rail === "card" ? "Card payments settle in one hop — there's nothing downstream to trace." : "The money didn't move on past this transfer, so there's no chain to follow."}>
                      no trail
                    </span>
                  )}
                </div>
                <div className="bt-case-reason">{c.reason}</div>
                {openCase === c.tx_id && (c.chain?.length ?? 0) > 0 && <Chain hops={c.chain!} />}
                <div className="case-actions" style={{ marginTop: 9 }}>
                  <button className="bt-action freeze" disabled={busy} onClick={() => act("freeze", c.tx_id)}>
                    ❄️ Request freeze
                  </button>
                  {c.rail === "card" && (
                    <button className="bt-action stepup" disabled={busy} onClick={() => act("stepUp", c.tx_id)}>
                      📟 Send OTP
                    </button>
                  )}
                  <button className="bt-action clear" disabled={busy} onClick={() => act("clear", c.tx_id)}>
                    ✅ Clear
                  </button>
                </div>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

/** The route money took after a flagged transfer — surfaced inline under
 *  the case it belongs to, not buried behind a table cell. A case saying
 *  "this transfer looks unusual" isn't reviewable on its own: the decision
 *  is whether to freeze an account, and that needs the chain — which
 *  accounts, how much, how fast, and where it stopped moving. */
function Chain({ hops }: { hops: ChainHop[] }) {
  return (
    <div className="chain">
      {hops.map((h) => (
        <div key={h.hop} className="chain-hop">
          <span className="mono">{shortId(h.from_account)}</span>
          <span className="chain-arrow">→</span>
          <span className="mono">{shortId(h.to_account)}</span>
          <span className="chain-detail">
            {formatPaise(h.amount_paise)} over {h.transfers} transfer
            {h.transfers > 1 ? "s" : ""}
            {h.hours > 0 && ` in ${h.hours.toFixed(1)}h`}
            {" · forwarded on "}
            {(h.forwarded_on * 100).toFixed(0)}%
            {h.other_legs > 0 && ` · ${h.other_legs} other leg${h.other_legs > 1 ? "s" : ""} here`}
          </span>
        </div>
      ))}
      <div className="chain-end">
        the money stops here — this account kept most of what arrived
      </div>
    </div>
  );
}

/** A count that may not have arrived. An older server, a renamed field or a
 *  partial response should render "—", not take the tab down. */
function count(n: number | undefined): string {
  return n == null ? "—" : n.toLocaleString();
}

function Tile({ n, k }: { n: string; k: string }) {
  return (
    <div className="fraud-tile">
      <div className="n">{n}</div>
      <div className="k">{k}</div>
    </div>
  );
}
