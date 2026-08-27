export function eventColor(eventType: string): string {
  if (eventType.startsWith("Payment")) return "#5ef2b5";
  if (eventType.startsWith("Account")) return "#6fb7ff";
  if (eventType.startsWith("Transfer")) return "#b78bff";
  if (eventType.startsWith("Device")) return "#ffb454";
  if (eventType.startsWith("Merchant")) return "#ff9ecb";
  if (eventType.startsWith("Settlement")) return "#8892a3";
  if (eventType.includes("Declined") || eventType.includes("Rejected") || eventType.includes("Failed")) return "#ff6d6d";
  return "#4d5567";
}

export function formatPaise(paise: number | undefined | null): string {
  if (paise === undefined || paise === null) return "—";
  const rupees = paise / 100;
  return "₹" + rupees.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export function formatSimTime(ns: number): string {
  const totalSeconds = ns / 1e9;
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const mins = Math.floor((totalSeconds % 3600) / 60);
  const secs = Math.floor(totalSeconds % 60);
  return `D${days} ${String(hours).padStart(2, "0")}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function shortId(id: string, n = 8): string {
  return id.length > n ? id.slice(0, n) : id;
}
