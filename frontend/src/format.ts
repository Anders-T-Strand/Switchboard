const intFmt = new Intl.NumberFormat("en-US");

export function compact(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "–";
  const abs = Math.abs(n);
  if (abs < 10_000) return intFmt.format(Math.round(n));
  if (abs < 1_000_000) return `${(n / 1_000).toFixed(abs < 100_000 ? 1 : 0)}K`;
  if (abs < 1_000_000_000) return `${(n / 1_000_000).toFixed(abs < 100_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

export function int(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  return intFmt.format(Math.round(n));
}

export function usd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  if (n === 0) return "$0";
  if (Math.abs(n) < 0.01) return "<$0.01";
  if (Math.abs(n) < 100) return `$${n.toFixed(2)}`;
  return `$${intFmt.format(Math.round(n))}`;
}

export function pct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  return `${Math.round(n * 100)}%`;
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86_400 && new Date().getDate() === d.getDate()) {
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function shortDate(isoDate: string): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], { month: "short", day: "numeric" });
}

export function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

const priceFmt = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 3 });

/** Per-million-token list price: $0.10, $0.125, $12.50 */
export function price(n: number): string {
  return `$${priceFmt.format(n)}`;
}

export const EFFORT_LABEL: Record<string, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Max",
};

export function effortLabel(e: string | null | undefined): string {
  return e ? EFFORT_LABEL[e] ?? e : "Default";
}
