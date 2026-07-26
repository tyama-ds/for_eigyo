"use client";

import type { CompareEntry, CompareFinding } from "@/lib/api-types";
import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { EngineAvatar } from "@/lib/engine-meta";
import { conflictEntries, findingText, valueToText } from "./CompareShared";

/**
 * エンジン間不一致のピボット表。
 * 行 = 不一致項目 (クラスタ)、列 = エンジン。セルに値 + 主張 + 根拠数を表示する。
 * Exported separately so it is unit-testable without fetching.
 */
const VALUE_CHIP_PALETTE = [
  "bg-sky-500/15 text-sky-100 ring-sky-400/40",
  "bg-rose-500/15 text-rose-100 ring-rose-400/40",
  "bg-amber-500/15 text-amber-100 ring-amber-400/40",
  "bg-emerald-500/15 text-emerald-100 ring-emerald-400/40",
];

export function ConflictList({
  conflicts,
  engineOrder,
}: {
  conflicts: CompareFinding[];
  engineOrder?: string[];
}) {
  if (conflicts.length === 0) {
    return <p className="text-sm text-slate-500">{t("conflicts.empty")}</p>;
  }

  // 列 = 全conflictに現れるエンジンの和集合 (engineOrderがあればその順を優先)
  const perConflict: CompareEntry[][] = conflicts.map((c) => conflictEntries(c));
  const seen = new Set<string>();
  const engines: string[] = [];
  for (const id of engineOrder ?? []) {
    if (!seen.has(id)) {
      seen.add(id);
      engines.push(id);
    }
  }
  for (const entries of perConflict) {
    for (const e of entries) {
      const id = e.engine_id ?? e.run_id ?? "";
      if (id && !seen.has(id)) {
        seen.add(id);
        engines.push(id);
      }
    }
  }

  return (
    <div className="overflow-x-auto rounded-2xl ring-1 ring-white/10">
      <table className="w-full min-w-[640px] border-collapse text-left text-sm">
        <thead>
          <tr className="bg-white/5 text-xs text-slate-400">
            <th
              scope="col"
              className="sticky left-0 z-10 min-w-[180px] bg-slate-900/95 px-4 py-3 font-medium backdrop-blur"
            >
              {t("conflicts.itemColumn")}
            </th>
            {engines.map((engineId) => (
              <th
                key={engineId}
                scope="col"
                className="min-w-[220px] px-4 py-3 font-medium"
              >
                <span className="flex items-center gap-2">
                  <EngineAvatar engineId={engineId} size="h-6 w-6" />
                  <span className="font-semibold text-slate-200">{engineId}</span>
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {conflicts.map((conflict, i) => {
            const entries = perConflict[i];
            const byEngine = new Map<string, CompareEntry[]>();
            for (const e of entries) {
              const id = e.engine_id ?? e.run_id ?? "";
              const list = byEngine.get(id) ?? [];
              list.push(e);
              byEngine.set(id, list);
            }
            // 同じ値=同じ色 (行内で値のユニーク順にパレットを割り当てる)
            const valueColor = new Map<string, string>();
            for (const e of entries) {
              const v = valueToText(e.value);
              if (!valueColor.has(v)) {
                valueColor.set(
                  v,
                  VALUE_CHIP_PALETTE[valueColor.size % VALUE_CHIP_PALETTE.length],
                );
              }
            }
            return (
              <tr
                key={i}
                className="border-t border-white/5 align-top transition-colors hover:bg-white/[0.03]"
              >
                <th
                  scope="row"
                  className="sticky left-0 z-10 bg-slate-900/95 px-4 py-3 font-medium backdrop-blur"
                >
                  <span className="block text-sm text-rose-200">
                    {findingText(conflict)}
                  </span>
                  <span className="mt-1 inline-flex items-center gap-1 rounded-full bg-rose-500/10 px-2 py-0.5 text-[11px] text-rose-300 ring-1 ring-inset ring-rose-400/30">
                    {t("conflicts.conflictBadge", {
                      count: String(
                        new Set(
                          entries
                            .map((e) => valueToText(e.value))
                            .filter((v) => v !== t("common.unknown")),
                        ).size,
                      ),
                    })}
                  </span>
                </th>
                {engines.map((engineId) => {
                  const cell = byEngine.get(engineId);
                  if (!cell || cell.length === 0) {
                    return (
                      <td key={engineId} className="px-4 py-3 text-slate-600">
                        <span aria-label={t("conflicts.notReported")}>—</span>
                      </td>
                    );
                  }
                  return (
                    <td key={engineId} className="px-4 py-3">
                      {cell.map((entry, j) => (
                        <div key={j} className={j > 0 ? "mt-3" : undefined}>
                          <span
                            className={`inline-flex max-w-full items-center rounded-lg px-2 py-1 text-sm font-semibold tabular-nums ring-1 ring-inset ${
                              valueColor.get(valueToText(entry.value)) ??
                              VALUE_CHIP_PALETTE[0]
                            }`}
                          >
                            <span className="truncate">{valueToText(entry.value)}</span>
                          </span>
                          {(entry.claim ?? entry.text) && (
                            <p className="mt-1.5 text-xs leading-relaxed text-slate-400">
                              {entry.claim ?? entry.text}
                            </p>
                          )}
                          {typeof entry.evidence_count === "number" && (
                            <p className="mt-1 text-[11px] text-slate-500">
                              {t("conflicts.evidenceCount", {
                                count: String(entry.evidence_count),
                              })}
                            </p>
                          )}
                        </div>
                      ))}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ConflictsTab({ jobId }: { jobId: string }) {
  const { data, loading, error, reload } = useFetch(
    () => api.getCompare(jobId),
    [jobId],
  );

  if (loading) return <p className="text-sm text-slate-500">{t("common.loading")}</p>;
  if (error) {
    return (
      <div role="alert" className="text-sm text-rose-300">
        <p>
          {t("compare.loadFailed")}: {error}
        </p>
        <button
          type="button"
          onClick={reload}
          className="mt-2 rounded-lg bg-white/5 px-2.5 py-1 text-xs text-slate-300 ring-1 ring-inset ring-white/15 transition-colors hover:bg-white/10"
        >
          {t("common.reload")}
        </button>
      </div>
    );
  }

  const conflicts = Array.isArray(data?.conflicts) ? data.conflicts : [];
  const engineOrder = Array.isArray(data?.engines_compared)
    ? (data.engines_compared as string[])
    : undefined;
  return (
    <div>
      <h3 className="mb-1 text-sm font-semibold text-white">
        {t("conflicts.title")}
      </h3>
      <p className="mb-3 text-xs text-slate-500">{t("conflicts.description")}</p>
      <ConflictList conflicts={conflicts} engineOrder={engineOrder} />
    </div>
  );
}
