import { useEffect, useState } from "react";
import { api, type RiskCase, type RiskSummary } from "./api";
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

  useEffect(() => {
    const controller = new AbortController();
    const poll = async () => {
      try {
        const [s, c] = await Promise.all([
          api.riskSummary(controller.signal),
          api.riskCases(controller.signal),
        ]);
        setSummary(s);
        setCases(c.cases);
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
