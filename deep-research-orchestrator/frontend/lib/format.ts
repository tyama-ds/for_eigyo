/** Formatting / defensive metric-reading helpers. */

export function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** Read the first finite-number value among candidate keys in a metrics dict. */
export function readMetricNumber(
  metrics: Record<string, unknown> | null | undefined,
  keys: string[],
): number | null {
  if (!metrics) return null;
  for (const key of keys) {
    const v = metrics[key];
    if (isFiniteNumber(v)) return v;
    if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) {
      return Number(v);
    }
  }
  return null;
}

export function readMetricValue(
  metrics: Record<string, unknown> | null | undefined,
  keys: string[],
): unknown {
  if (!metrics) return undefined;
  for (const key of keys) {
    if (metrics[key] !== undefined && metrics[key] !== null) return metrics[key];
  }
  return undefined;
}

export function formatInteger(n: number): string {
  return n.toLocaleString("ja-JP");
}

export function formatUsd(n: number): string {
  const digits = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2;
  return `$${n.toFixed(digits)}`;
}

/** mm:ss or h:mm:ss */
export function formatElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function formatDateTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function parseIsoMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? null : ms;
}

/** Convert an unknown warning entry (string or object) to display text. */
export function warningToText(w: unknown): string {
  if (typeof w === "string") return w;
  if (w && typeof w === "object") {
    const obj = w as Record<string, unknown>;
    for (const key of ["message", "reason", "text", "detail", "warning"]) {
      if (typeof obj[key] === "string") return obj[key] as string;
    }
    try {
      return JSON.stringify(w);
    } catch {
      return String(w);
    }
  }
  return String(w);
}

/** Find a warning whose text mentions one of the given keywords. */
export function findWarningReason(
  warnings: unknown[] | null | undefined,
  keywords: string[],
): string | null {
  if (!warnings) return null;
  for (const w of warnings) {
    const text = warningToText(w);
    const lower = text.toLowerCase();
    if (keywords.some((k) => lower.includes(k.toLowerCase()))) return text;
  }
  return null;
}
