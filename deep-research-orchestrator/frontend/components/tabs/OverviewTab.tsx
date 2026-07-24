"use client";

import type { JobLiveState } from "@/lib/sse-reducer";
import { orderedRuns } from "@/lib/sse-reducer";
import {
  formatDateTime,
  formatElapsed,
  formatInteger,
  formatUsd,
} from "@/lib/format";
import { t } from "@/lib/i18n";
import { StatusBadge } from "../StatusBadge";

export function OverviewTab({
  state,
  engineNames,
}: {
  state: JobLiveState;
  engineNames: Record<string, string>;
}) {
  const job = state.job;
  const runs = orderedRuns(state);
  const unknown = <span className="text-slate-400">{t("common.unknown")}</span>;

  return (
    <div className="space-y-6">
      <section aria-label={t("overview.jobSummary")}>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">
          {t("overview.jobSummary")}
        </h3>
        {!job ? (
          <p className="text-sm text-slate-500">{t("common.loading")}</p>
        ) : (
          <dl className="grid grid-cols-1 gap-x-8 gap-y-1 text-sm sm:grid-cols-2">
            <div className="flex gap-2">
              <dt className="shrink-0 text-slate-500">{t("jobs.topic")}:</dt>
              <dd className="text-slate-800">{job.topic}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="shrink-0 text-slate-500">{t("jobs.status")}:</dt>
              <dd>
                <StatusBadge status={state.jobStatus ?? job.status} />
              </dd>
            </div>
            {job.objective && (
              <div className="flex gap-2">
                <dt className="shrink-0 text-slate-500">
                  {t("job.objective")}:
                </dt>
                <dd className="text-slate-800">{job.objective}</dd>
              </div>
            )}
            {job.instructions && (
              <div className="flex gap-2">
                <dt className="shrink-0 text-slate-500">
                  {t("job.instructions")}:
                </dt>
                <dd className="text-slate-800">{job.instructions}</dd>
              </div>
            )}
            <div className="flex gap-2">
              <dt className="shrink-0 text-slate-500">{t("job.language")}:</dt>
              <dd className="text-slate-800">{job.language}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="shrink-0 text-slate-500">{t("jobs.createdAt")}:</dt>
              <dd className="text-slate-800">
                {formatDateTime(job.created_at) ?? unknown}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="shrink-0 text-slate-500">{t("job.finishedAt")}:</dt>
              <dd className="text-slate-800">
                {formatDateTime(job.finished_at) ?? unknown}
              </dd>
            </div>
            {job.error && (
              <div className="flex gap-2 sm:col-span-2">
                <dt className="shrink-0 text-slate-500">{t("job.error")}:</dt>
                <dd className="text-rose-700">{job.error}</dd>
              </div>
            )}
          </dl>
        )}
      </section>

      <section aria-label={t("overview.engineOutcomes")}>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">
          {t("overview.engineOutcomes")}
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-xs text-slate-500">
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("overview.engine")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("overview.result")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("run.stage")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("run.elapsed")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("run.searches")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("run.sources")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("run.tokens")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("run.llmCost")}
                </th>
                <th scope="col" className="py-1.5 font-medium">
                  {t("run.error")}
                </th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.runId} className="border-b border-slate-100">
                  <td className="py-2 pr-3 font-medium">
                    {engineNames[run.engineId] ?? run.engineId}
                  </td>
                  <td className="py-2 pr-3">
                    <StatusBadge status={run.status} />
                  </td>
                  <td className="py-2 pr-3">{run.stage ?? unknown}</td>
                  <td className="py-2 pr-3">
                    {run.elapsedSeconds !== null
                      ? formatElapsed(run.elapsedSeconds)
                      : unknown}
                  </td>
                  <td className="py-2 pr-3">
                    {run.searchCount !== null
                      ? formatInteger(run.searchCount)
                      : unknown}
                  </td>
                  <td className="py-2 pr-3">
                    {run.sourceCount !== null
                      ? formatInteger(run.sourceCount)
                      : unknown}
                  </td>
                  <td className="py-2 pr-3">
                    {run.tokensTotal !== null
                      ? formatInteger(run.tokensTotal)
                      : unknown}
                  </td>
                  <td className="py-2 pr-3">
                    {run.llmCostUsd !== null ? (
                      <>
                        {formatUsd(run.llmCostUsd)}
                        {run.llmCostIsEstimate && (
                          <span className="ml-1 text-xs text-slate-500">
                            ({t("common.estimateTag")})
                          </span>
                        )}
                      </>
                    ) : (
                      unknown
                    )}
                  </td>
                  <td className="py-2 text-xs text-rose-700">
                    {run.error ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
