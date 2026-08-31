import { useEffect, useState } from "react";
import { api, type RetrainStatus, type RiskCase, type RiskSummary } from "./api";
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
        <Tile n={summary.scored.toLocaleString()} k="scored" />
        <Tile n={summary.flagged.toLocaleString()} k="flagged" />
        <Tile n={`${(summary.flag_rate * 100).toFixed(2)}%`} k="flag rate" />
        <Tile n={summary.blocked.toLocaleString()} k="blocked" />
        <Tile n={summary.review.toLocaleString()} k="for review" />
        <Tile n={summary.accounts_tracked.toLocaleString()} k="accounts" />
      </div>

      <div className="fraud-model">
        <div className="fraud-model-head">
          <span>
            {retrain?.registry.live_version
              ? `Model v${retrain.registry.live_version}`
              : "Model: the shipped one"}
            {retrain?.registry.live_recall != null &&
              ` · caught ${(retrain.registry.live_recall * 100).toFixed(1)}% at a 2% flag rate`}
          </span>
          <span>
            <button onClick={startRetrain} disabled={busy || running}>
              {running ? "Retraining…" : "Retrain on collected traffic"}
            </button>
            {retrain?.registry.can_rollback && (
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

        {retrain && retrain.registry.history.length > 0 && (
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
              {retrain.registry.history.map((v) => (
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
                <td className="reason">{c.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Tile({ n, k }: { n: string; k: string }) {
  return (
    <div className="fraud-tile">
      <div className="n">{n}</div>
      <div className="k">{k}</div>
    </div>
  );
}
