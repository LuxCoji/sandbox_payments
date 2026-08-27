import { useMemo, useState } from "react";
import type { SimEvent } from "./api";
import { eventColor, formatPaise, formatSimTime, shortId } from "./eventStyle";

function detailFor(e: SimEvent): string {
  const p = e.payload;
  const acc = (p.account_id ?? p.source_account_id) as string | undefined;
  const dest = p.destination_account_id as string | undefined;
  if (acc && dest) return `${shortId(acc)} → ${shortId(dest)}`;
  if (acc) return shortId(acc);
  if (p.merchant_id) return shortId(p.merchant_id as string);
  if (p.device_id) return shortId(p.device_id as string);
  return e.actor_id ? shortId(e.actor_id) : "";
}

function amountFor(e: SimEvent): number | undefined {
  const p = e.payload;
  return (p.amount_paise ?? p.net_amount_paise ?? p.total_amount_paise) as number | undefined;
}

const CATEGORIES = ["Payment", "Account", "Transfer", "Device", "Merchant", "Settlement", "Other"] as const;

function categoryOf(eventType: string): (typeof CATEGORIES)[number] {
  for (const c of CATEGORIES) if (c !== "Other" && eventType.startsWith(c)) return c;
  return "Other";
}

interface Props {
  events: SimEvent[];
}

export default function LiveFeed({ events }: Props) {
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<string | null>(null);
  const [highlightCorrelation, setHighlightCorrelation] = useState<string | null>(null);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const e of events) c[categoryOf(e.event_type)] = (c[categoryOf(e.event_type)] ?? 0) + 1;
    return c;
  }, [events]);

  const shown = events.filter((e) => !hidden.has(categoryOf(e.event_type)));
  const byEventId = useMemo(() => new Map(events.map((e) => [e.event_id, e])), [events]);

  function toggleCategory(c: string) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(c)) next.delete(c);
      else next.add(c);
      return next;
    });
  }

  if (events.length === 0) {
    return <div className="empty-hint">Waiting for events…</div>;
  }

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginBottom: 10 }}>
        {CATEGORIES.filter((c) => counts[c]).map((c) => (
          <span
            key={c}
            onClick={() => toggleCategory(c)}
            className="pill"
            style={{
              cursor: "pointer",
              padding: "3px 8px",
              opacity: hidden.has(c) ? 0.35 : 1,
              borderColor: hidden.has(c) ? "var(--border)" : "var(--accent-dim)",
            }}
          >
            {c} <b>{counts[c]}</b>
          </span>
        ))}
        {highlightCorrelation && (
          <span className="pill" style={{ borderColor: "var(--amber)", color: "var(--amber)", cursor: "pointer" }}
            onClick={() => setHighlightCorrelation(null)}>
            ✕ correlation filter
          </span>
        )}
      </div>

      {[...shown].reverse()
        .filter((e) => !highlightCorrelation || e.correlation_id === highlightCorrelation)
        .map((e) => {
        const color = eventColor(e.event_type);
        const amt = amountFor(e);
        const isOpen = expanded === e.event_id;
        const caused = e.causation_id ? byEventId.get(e.causation_id) : undefined;
        return (
          <div key={e.event_id}>
            <div
              className="feed-item"
              style={{ cursor: "pointer" }}
              onClick={() => setExpanded(isOpen ? null : e.event_id)}
            >
              <div className="feed-bar" style={{ background: color }} />
              <div>
                <div className="feed-row1">
                  <span className="feed-type" style={{ color }}>{e.event_type}</span>
                  <span className="feed-seq">#{e.seq_num}</span>
                </div>
                <div className="feed-row1">
                  <span className="feed-detail">{detailFor(e)}</span>
                  {amt !== undefined && <span className="feed-amount">{formatPaise(amt)}</span>}
                </div>
              </div>
            </div>
            {isOpen && (
              <div className="inspector-card" style={{ margin: "0 0 8px 13px", padding: 10 }}>
                <div className="kv"><span className="kv-key">event_id</span><span className="kv-val">{shortId(e.event_id, 18)}…</span></div>
                <div className="kv"><span className="kv-key">sim_time</span><span className="kv-val">{formatSimTime(e.sim_time_ns)}</span></div>
                <div className="kv"><span className="kv-key">actor_id</span><span className="kv-val">{e.actor_id ? shortId(e.actor_id, 18) + "…" : "—"}</span></div>
                {e.causation_id && (
                  <div className="kv">
                    <span className="kv-key">caused_by</span>
                    <span className="kv-val">{caused ? `${caused.event_type} #${caused.seq_num}` : shortId(e.causation_id)}</span>
                  </div>
                )}
                {e.correlation_id && (
                  <div className="kv">
                    <span className="kv-key">correlation_id</span>
                    <span
                      className="kv-val"
                      style={{ color: "var(--amber)", cursor: "pointer" }}
                      onClick={(ev) => { ev.stopPropagation(); setHighlightCorrelation(e.correlation_id); }}
                    >
                      {shortId(e.correlation_id, 10)}… (filter)
                    </span>
                  </div>
                )}
                <div style={{ marginTop: 8, borderTop: "1px solid var(--border-soft)", paddingTop: 8 }}>
                  {Object.entries(e.payload).map(([k, v]) => (
                    <div className="kv" key={k}>
                      <span className="kv-key">{k}</span>
                      <span className="kv-val">{typeof v === "number" && k.includes("paise") ? formatPaise(v) : String(v)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
