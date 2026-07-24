"use client";

import type { CompareFinding } from "@/lib/api-types";
import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { conflictEntries, findingText, valueToText } from "./CompareShared";

/**
 * Presentational conflicts view: each engine's claim + value side by side.
 * Exported separately so it is unit-testable without fetching.
 */
export function ConflictList({ conflicts }: { conflicts: CompareFinding[] }) {
  if (conflicts.length === 0) {
    return <p className="text-sm text-slate-500">{t("conflicts.empty")}</p>;
  }
  return (
    <div className="space-y-4">
      {conflicts.map((conflict, i) => {
        const entries = conflictEntries(conflict);
        return (
          <section
            key={i}
            aria-label={findingText(conflict)}
            className="rounded border border-rose-200"
          >
            <h4 className="border-b border-rose-200 bg-rose-50 px-3 py-2 text-sm font-medium text-rose-900">
              {findingText(conflict)}
            </h4>
            {entries.length === 0 ? (
              <p className="px-3 py-2 text-sm text-slate-500">
                {t("common.empty")}
              </p>
            ) : (
              <div className="grid grid-cols-1 gap-px bg-slate-200 sm:grid-cols-2 lg:grid-cols-3">
                {entries.map((entry, j) => (
                  <div key={j} className="bg-white p-3">
                    <p className="mb-1 text-xs font-semibold text-slate-500">
                      {t("conflicts.engine")}:{" "}
                      <span className="text-slate-800">
                        {entry.engine_id ?? entry.run_id ?? t("common.unknown")}
                      </span>
                    </p>
                    {(entry.claim ?? entry.text) && (
                      <p className="mb-1 text-sm text-slate-800">
                        <span className="text-xs text-slate-500">
                          {t("conflicts.claim")}:{" "}
                        </span>
                        {entry.claim ?? entry.text}
                      </p>
                    )}
                    <p className="text-sm font-medium text-slate-900">
                      <span className="text-xs font-normal text-slate-500">
                        {t("conflicts.value")}:{" "}
                      </span>
                      {valueToText(entry.value)}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </section>
        );
      })}
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
      <div role="alert" className="text-sm text-rose-700">
        <p>
          {t("compare.loadFailed")}: {error}
        </p>
        <button
          type="button"
          onClick={reload}
          className="mt-2 rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
        >
          {t("common.reload")}
        </button>
      </div>
    );
  }

  const conflicts = Array.isArray(data?.conflicts) ? data.conflicts : [];
  return (
    <div>
      <h3 className="mb-1 text-sm font-semibold text-slate-900">
        {t("conflicts.title")}
      </h3>
      <p className="mb-3 text-xs text-slate-500">{t("conflicts.description")}</p>
      <ConflictList conflicts={conflicts} />
    </div>
  );
}
