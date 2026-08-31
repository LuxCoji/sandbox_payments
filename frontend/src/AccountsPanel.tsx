import { useEffect, useState } from "react";
import type { AccountRow, SimEvent } from "./api";
import { api } from "./api";
import { formatPaise, shortId } from "./eventStyle";

interface Props {
  branchId: string;
  accounts: AccountRow[];
  selected: string | null;
  onSelect: (id: string | null) => void;
}

export default function AccountsPanel({ branchId, accounts, selected, onSelect }: Props) {
  const [history, setHistory] = useState<SimEvent[]>([]);
  const acct = accounts.find((a) => a.account_id === selected) ?? null;

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    api.accountEvents(branchId, selected).then((h) => {
      if (!cancelled) setHistory(h);
    });
    return () => {
      cancelled = true;
    };
  }, [branchId, selected]);

  if (acct) {
    return (
      <div>
        <button className="btn small" onClick={() => onSelect(null)} style={{ marginBottom: 10 }}>
          ← back
        </button>
        <div className="inspector-card">
          <div className="kv"><span className="kv-key">account_id</span><span className="kv-val">{shortId(acct.account_id, 14)}…</span></div>
          <div className="kv"><span className="kv-key">owner_id</span><span className="kv-val">{shortId(acct.owner_id, 14)}…</span></div>
          <div className="kv"><span className="kv-key">type</span><span className="kv-val">{acct.account_type}</span></div>
          <div className="kv"><span className="kv-key">status</span>
            <span className={`badge ${acct.status.toLowerCase()}`}>{acct.status}</span>
          </div>
          <div className="kv"><span className="kv-key">balance</span><span className="kv-val" style={{ color: "var(--rail-cyan)" }}>{formatPaise(acct.balance_paise)}</span></div>
          <div className="kv"><span className="kv-key">kyc_level</span><span className="kv-val">{acct.kyc_level}</span></div>
          <div className="kv"><span className="kv-key">daily_tx_count</span><span className="kv-val">{acct.daily_tx_count}</span></div>
          {acct.merchant_category_code && (
            <div className="kv"><span className="kv-key">mcc</span><span className="kv-val">{acct.merchant_category_code}</span></div>
          )}
        </div>
        <div className="sandbox-label">Recent activity</div>
        {history.length === 0 && <div className="empty-hint">No recorded events yet.</div>}
        {[...history].reverse().map((e) => (
          <div key={e.event_id} className="feed-item">
            <div className="feed-bar" style={{ background: "#2fe6d1" }} />
            <div>
              <div className="feed-row1">
                <span className="feed-type" style={{ color: "#2fe6d1" }}>{e.event_type}</span>
                <span className="feed-seq">#{e.seq_num}</span>
              </div>
            </div>
          </div>
        ))}
      </div>
    );
  }

  const sorted = [...accounts].sort((a, b) => b.balance_paise - a.balance_paise);
  return (
    <div>
      {sorted.map((a) => (
        <div
          key={a.account_id}
          className={`acct-row ${selected === a.account_id ? "selected" : ""}`}
          onClick={() => onSelect(a.account_id)}
        >
          <span className={`badge ${a.status.toLowerCase()}`}>{a.account_type === "MERCHANT" ? "M" : "U"}</span>
          <span className="acct-id">{shortId(a.account_id, 18)}…</span>
          <span className="acct-balance">{formatPaise(a.balance_paise)}</span>
        </div>
      ))}
    </div>
  );
}
