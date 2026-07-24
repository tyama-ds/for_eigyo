import type { CompareEntry, CompareFinding } from "@/lib/api-types";
import { t } from "@/lib/i18n";

/** Best-effort text extraction from a loosely shaped compare finding. */
export function findingText(f: CompareFinding | string | unknown): string {
  if (typeof f === "string") return f;
  if (f && typeof f === "object") {
    const obj = f as CompareFinding;
    for (const key of ["text", "statement", "description", "topic", "key", "claim"]) {
      const v = obj[key];
      if (typeof v === "string" && v.trim() !== "") return v;
    }
    try {
      return JSON.stringify(f);
    } catch {
      return String(f);
    }
  }
  return String(f);
}

/** Engines associated with a finding, as display strings. */
export function findingEngines(f: CompareFinding | unknown): string[] {
  if (!f || typeof f !== "object") return [];
  const obj = f as CompareFinding;
  if (Array.isArray(obj.engines)) {
    return obj.engines.map((e) =>
      typeof e === "string" ? e : findingText(e),
    );
  }
  if (obj.engines && typeof obj.engines === "object") {
    return Object.keys(obj.engines as Record<string, unknown>);
  }
  if (Array.isArray(obj.entries)) {
    return obj.entries
      .map((en) => en.engine_id ?? en.run_id ?? "")
      .filter((s): s is string => Boolean(s));
  }
  return [];
}

/**
 * Normalize a conflict item into per-engine entries (claim + value),
 * accepting `entries[]`, `values{engine: value}` or `engines{engine: value}`.
 */
export function conflictEntries(f: CompareFinding | unknown): CompareEntry[] {
  if (!f || typeof f !== "object") return [];
  const obj = f as CompareFinding;
  // 正式なバックエンド形状: claims[] (engine_id / text / value)
  if (Array.isArray(obj.claims)) {
    return (obj.claims as Record<string, unknown>[]).map((c) => ({
      engine_id: typeof c.engine_id === "string" ? c.engine_id : undefined,
      run_id: typeof c.run_id === "string" ? c.run_id : undefined,
      claim: typeof c.text === "string" ? c.text : undefined,
      value: c.value,
    }));
  }
  if (Array.isArray(obj.entries)) return obj.entries;
  const map =
    (obj.values && typeof obj.values === "object" && !Array.isArray(obj.values)
      ? obj.values
      : null) ??
    (obj.engines && typeof obj.engines === "object" && !Array.isArray(obj.engines)
      ? (obj.engines as Record<string, unknown>)
      : null);
  if (map) {
    return Object.entries(map).map(([engine, value]) => {
      if (value && typeof value === "object") {
        const v = value as Record<string, unknown>;
        return {
          engine_id: engine,
          claim: typeof v.claim === "string" ? v.claim : typeof v.text === "string" ? v.text : undefined,
          value: v.value ?? value,
        };
      }
      return { engine_id: engine, value };
    });
  }
  return [];
}

export function valueToText(v: unknown): string {
  if (v === null || v === undefined) return t("common.unknown");
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export function FindingTable({
  items,
  emptyText,
}: {
  items: unknown[] | undefined;
  emptyText: string;
}) {
  if (!items || items.length === 0) {
    return <p className="text-sm text-slate-500">{emptyText}</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs text-slate-500">
            <th scope="col" className="py-1.5 pr-3 font-medium">
              {t("compare.findingColumn")}
            </th>
            <th scope="col" className="py-1.5 font-medium">
              {t("compare.enginesColumn")}
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item, i) => (
            <tr key={i} className="border-b border-slate-100 align-top">
              <td className="py-2 pr-3">{findingText(item)}</td>
              <td className="py-2 text-xs text-slate-600">
                {findingEngines(item).join(", ") || t("common.unknown")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
