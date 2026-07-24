"use client";

import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { EngineAvatar } from "@/lib/engine-meta";

export function RawTab({
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
          {t("raw.loadFailed")}: {error}
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
    <div className="space-y-4">
      <section aria-label={t("raw.artifacts")}>
        <h3 className="mb-2 text-sm font-semibold text-white">
          {t("raw.artifacts")}
        </h3>
        <ul className="space-y-1 text-sm">
          {data.map((result) => (
            <li key={result.run_id} className="flex flex-wrap items-center gap-2">
              <EngineAvatar engineId={result.engine_id} size="h-6 w-6" />
              <span className="font-medium text-slate-100">
                {engineNames[result.engine_id] ?? result.engine_id}
              </span>
              <span className="text-xs text-slate-500">
                ({t("raw.run")}: {result.run_id})
              </span>
              {result.raw_artifact_id ? (
                <a
                  href={api.artifactUrl(result.raw_artifact_id)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="rounded-lg border border-indigo-400/40 px-2 py-0.5 text-xs text-indigo-300 hover:bg-indigo-500/10"
                >
                  {t("raw.artifactDownload")}
                </a>
              ) : (
                <span className="text-xs text-slate-400">
                  {t("raw.noArtifact")}
                </span>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section aria-label={t("raw.normalizedJson")}>
        <h3 className="mb-2 text-sm font-semibold text-white">
          {t("raw.normalizedJson")}
        </h3>
        <pre className="max-h-[32rem] overflow-auto rounded-lg bg-black/60 p-3 text-xs text-emerald-200/90 ring-1 ring-inset ring-white/10">
          {JSON.stringify(data, null, 2)}
        </pre>
      </section>
    </div>
  );
}
