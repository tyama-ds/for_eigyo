"use client";

import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { warningToText } from "@/lib/format";
import { Markdown } from "../Markdown";

export function ReportsTab({
  jobId,
  engineNames,
}: {
  jobId: string;
  engineNames: Record<string, string>;
}) {
  const { data, loading, error, reload } = useFetch(
    () => api.getResults(jobId),
    [jobId],
  );

  if (loading) return <p className="text-sm text-slate-500">{t("common.loading")}</p>;
  if (error) {
    return (
      <div role="alert" className="text-sm text-rose-700">
        <p>
          {t("reports.loadFailed")}: {error}
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
  if (!data || data.length === 0) {
    return <p className="text-sm text-slate-500">{t("common.empty")}</p>;
  }

  return (
    <div className="space-y-6">
      {data.map((result) => (
        <section
          key={result.run_id}
          aria-label={engineNames[result.engine_id] ?? result.engine_id}
          className="rounded border border-slate-200"
        >
          <h3 className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-sm font-semibold text-slate-900">
            {engineNames[result.engine_id] ?? result.engine_id}
          </h3>
          <div className="p-3">
            {result.summary && (
              <p className="mb-3 rounded bg-slate-50 p-2 text-sm text-slate-700">
                <span className="font-medium">{t("reports.summary")}: </span>
                {result.summary}
              </p>
            )}
            {result.report_markdown ? (
              <Markdown source={result.report_markdown} />
            ) : (
              <p className="text-sm text-slate-500">
                {t("reports.noReport")}
                {result.warnings.length > 0 && (
                  <span className="block text-xs text-amber-800">
                    {result.warnings.map(warningToText).join(" / ")}
                  </span>
                )}
              </p>
            )}
          </div>
        </section>
      ))}
    </div>
  );
}
