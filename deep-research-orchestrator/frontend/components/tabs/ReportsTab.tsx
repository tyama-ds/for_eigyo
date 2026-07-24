"use client";

import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { warningToText } from "@/lib/format";
import { Markdown } from "../Markdown";
import { EngineAvatar } from "@/lib/engine-meta";

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
      <div role="alert" className="text-sm text-rose-300">
        <p>
          {t("reports.loadFailed")}: {error}
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
    return <p className="text-sm text-slate-500">{t("common.empty")}</p>;
  }

  return (
    <div className="space-y-6">
      {data.map((result) => (
        <section
          key={result.run_id}
          aria-label={engineNames[result.engine_id] ?? result.engine_id}
          className="overflow-hidden rounded-xl ring-1 ring-white/10"
        >
          <h3 className="flex items-center gap-2 border-b border-white/10 bg-white/5 px-3 py-2.5 text-sm font-semibold tracking-tight text-white">
            <EngineAvatar engineId={result.engine_id} size="h-6 w-6" />
            {engineNames[result.engine_id] ?? result.engine_id}
          </h3>
          <div className="p-3">
            {result.summary && (
              <p className="mb-3 rounded-lg bg-white/5 p-2 text-sm text-slate-300">
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
                  <span className="block text-xs text-amber-300">
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
