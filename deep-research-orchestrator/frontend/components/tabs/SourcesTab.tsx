"use client";

import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";

export function SourcesTab({
  jobId,
  engineNames,
}: {
  jobId: string;
  engineNames: Record<string, string>;
}) {
  const { data, loading, error, reload } = useFetch(
    () => api.getSources(jobId),
    [jobId],
  );

  if (loading) return <p className="text-sm text-slate-500">{t("common.loading")}</p>;
  if (error) {
    return (
      <div role="alert" className="text-sm text-rose-300">
        <p>
          {t("sources.loadFailed")}: {error}
        </p>
        <button
          type="button"
          onClick={reload}
          className="mt-2 rounded-lg border border-white/15 px-2 py-1 text-xs text-slate-300 hover:bg-white/5"
        >
          {t("common.reload")}
        </button>
      </div>
    );
  }
  if (!data || data.length === 0) {
    return <p className="text-sm text-slate-500">{t("sources.empty")}</p>;
  }

  // Dedup indicator: count sources sharing the same canonical_url.
  const canonicalCounts = new Map<string, number>();
  for (const s of data) {
    canonicalCounts.set(
      s.canonical_url,
      (canonicalCounts.get(s.canonical_url) ?? 0) + 1,
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-white/10 text-xs text-slate-500">
            <th scope="col" className="py-1.5 pr-3 font-medium">
              {t("sources.url")}
            </th>
            <th scope="col" className="py-1.5 pr-3 font-medium">
              {t("sources.title")}
            </th>
            <th scope="col" className="py-1.5 pr-3 font-medium">
              {t("sources.engine")}
            </th>
            <th scope="col" className="py-1.5 font-medium">
              {t("sources.fetchedAt")}
            </th>
          </tr>
        </thead>
        <tbody>
          {data.map((source) => {
            const dupCount = canonicalCounts.get(source.canonical_url) ?? 1;
            return (
              <tr key={source.id} className="border-b border-white/5 align-top">
                <td className="max-w-[24rem] py-2 pr-3">
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block truncate text-indigo-300 underline underline-offset-2 hover:text-indigo-200"
                    title={source.url}
                  >
                    {source.url}
                  </a>
                  {dupCount > 1 && (
                    <span
                      className="mt-0.5 inline-block rounded-lg bg-violet-500/20 px-1.5 py-0.5 text-[11px] font-medium text-violet-300"
                      title={t("sources.duplicateHint")}
                    >
                      {t("sources.duplicate", { count: dupCount })}
                    </span>
                  )}
                </td>
                <td className="max-w-[18rem] py-2 pr-3">
                  <span className="block truncate" title={source.title ?? undefined}>
                    {source.title ?? (
                      <span className="text-slate-400">{t("common.unknown")}</span>
                    )}
                  </span>
                </td>
                <td className="py-2 pr-3 text-xs text-slate-400">
                  {source.engine_id
                    ? (engineNames[source.engine_id] ?? source.engine_id)
                    : t("common.unknown")}
                </td>
                <td className="py-2 text-xs text-slate-400">
                  {formatDateTime(source.fetched_at) ?? (
                    <span className="text-slate-400">{t("common.unknown")}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
