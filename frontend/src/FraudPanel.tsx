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
 * What the fraud rails have seen, and what they flagged.
 *
 * The banner is the important part. A card rail with no model allows every
 * payment, and a card rail that is working but finding nothing shows exactly
 * the same counts - so the page says which, rather than leaving an empty queue
 * to be read either way.
 */
export function FraudPanel() {
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

  if (error) return <div className="empty">Could not reach the fraud rails: {error}</div>;
  if (!summary) return <div className="empty">Loading…</div>;
  if (!summary.enabled) return <div className="empty">Fraud detection is off for this session.</div>;

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
    <div className="fraud-panel">
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
        <Tile n={`${((summary.flag_rate ?? 0) * 100).toFixed(2)}%`} k="flag rate" />
        <Tile n={count(summary.blocked)} k="blocked" />
        <Tile n={count(summary.review)} k="for review" />
        <Tile n={count(summary.accounts_tracked)} k="accounts" />
      </div>

      <div className="fraud-model">
        <div className="fraud-model-head">
          <span>
            {retrain?.registry?.live_version
              ? `Model v${retrain.registry.live_version}`
              : "Model: the shipped one"}
            {retrain?.registry?.live_recall != null &&
              ` · caught ${(retrain.registry.live_recall * 100).toFixed(1)}% at a 2% flag rate`}
          </span>
          <span>
            <button onClick={startRetrain} disabled={busy || running}>
              {running ? "Retraining…" : "Retrain on collected traffic"}
            </button>
            {retrain?.registry?.can_rollback && (
              <button onClick={rollback} disabled={busy || running}>
                Roll back
              </button>
            )}
          </span>
        </div>

        <p className="fraud-note">
          Retraining does not let the model learn continuously from live
          traffic. It trains a candidate on what has been collected, scores the
          candidate <em>and</em> the current model on a period neither trained
          on, and promotes the candidate only if it wins. A model that learned
          from its own decisions could be taught by an attacker who got one
          attack through.
        </p>

        {retrain?.status === "declined" && (
          <div className="fraud-outcome">Declined — {retrain.error}</div>
        )}
        {retrain?.status === "failed" && (
          <div className="fraud-outcome bad">Failed — {retrain.error}</div>
        )}
        {retrain?.status === "done" && retrain.result && (
          <div className="fraud-outcome">
            {retrain.result.promoted ? "Promoted" : "Kept the current model"} —{" "}
            {retrain.result.reason}
          </div>
        )}

        {(retrain?.registry?.history?.length ?? 0) > 0 && (
          <table className="fraud-cases">
            <thead>
              <tr>
                <th>Version</th>
                <th>Trained on</th>
                <th>Recall</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {retrain!.registry.history.map((v) => (
                <tr key={v.version}>
                  <td className="mono">v{v.version}</td>
                  <td className="mono">
                    {v.rows.toLocaleString()} rows, {v.fraud} fraud
                  </td>
                  <td className="mono">{(v.recall_at_2pct * 100).toFixed(1)}%</td>
                  <td className="reason">{v.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="reviewer-bar">
        <label>
          Reviewer
          <input
            value={reviewer}
            placeholder="your name"
            onChange={(e) => setReviewer(e.target.value)}
          />
        </label>
        <span className="fraud-note">
          Every decision is recorded against this name. A freeze is a request,
          not an act — the engine holds the funds and acts on an instruction
          with a case behind it.
        </span>
      </div>

      {outcome && <div className="fraud-outcome">{outcome}</div>}

      {cases.length === 0 ? (
        <div className="empty">No cases raised.</div>
      ) : (
        <table className="fraud-cases">
          <thead>
            <tr>
              <th>Transaction</th>
              <th>Rail</th>
              <th>Action</th>
              <th>Score</th>
              <th>Amount</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.tx_id}>
                <td className="mono">{shortId(c.tx_id)}</td>
                <td>
                  <span className={`rail rail-${c.rail}`}>{c.rail}</span>
                </td>
                <td>{c.action}</td>
                <td className="mono">{c.score.toFixed(2)}</td>
                <td className="mono">{formatPaise(c.amount_paise)}</td>
                <td className="reason">
                  {c.reason}
                  {(c.chain?.length ?? 0) > 0 && (
                    <button
                      className="link"
                      onClick={() => setOpenCase(openCase === c.tx_id ? null : c.tx_id)}
                    >
                      {openCase === c.tx_id
                        ? "hide the route"
                        : `follow the money (${c.chain!.length} hop${c.chain!.length > 1 ? "s" : ""})`}
                    </button>
                  )}
                  {openCase === c.tx_id && <Chain hops={c.chain ?? []} />}
                  <div className="case-actions">
                    <button disabled={busy} onClick={() => act("freeze", c.tx_id)}>
                      Request freeze
                    </button>
                    {c.rail === "card" && (
                      <button disabled={busy} onClick={() => act("stepUp", c.tx_id)}>
                        Send OTP
                      </button>
                    )}
                    <button disabled={busy} onClick={() => act("clear", c.tx_id)}>
                      Clear
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** The route money took after a flagged transfer.
 *
 *  A case saying "this transfer looks unusual" is not reviewable. The decision
 *  is whether to freeze an account, and that needs the chain: which accounts,
 *  how much, how fast, and where it stopped moving. */
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
