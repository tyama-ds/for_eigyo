"use client";

import type { JobView } from "@/lib/api-types";
import { formatDateTime } from "@/lib/format";
import { EngineAvatar } from "@/lib/engine-meta";
import { t } from "@/lib/i18n";
import { StatusBadge } from "./StatusBadge";

interface JobListProps {
  jobs: JobView[] | null;
  error: string | null;
  loading: boolean;
  onOpen: (jobId: string) => void;
  onRefresh: () => void;
}

export function JobList({ jobs, error, loading, onOpen, onRefresh }: JobListProps) {
  return (
    <section
      aria-label={t("jobs.recentTitle")}
      className="rounded-2xl bg-slate-900/80 p-5 shadow-xl shadow-black/20 ring-1 ring-white/10 backdrop-blur"
    >
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-base font-semibold tracking-tight text-white">
          {t("jobs.recentTitle")}
        </h2>
        <button
          type="button"
          onClick={onRefresh}
          className="rounded-xl bg-white/5 px-3 py-1.5 text-xs text-slate-200 ring-1 ring-inset ring-white/15 transition-all duration-200 hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
        >
          {t("common.refresh")}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-rose-300">
          {t("jobs.loadFailed")}: {error}
        </p>
      )}
      {loading && <p className="text-sm text-slate-500">{t("common.loading")}</p>}
      {jobs && jobs.length === 0 && !loading && (
        <p className="text-sm text-slate-500">{t("jobs.empty")}</p>
      )}
      {jobs && jobs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-white/10 text-xs text-slate-500">
                <th scope="col" className="py-2 pr-3 font-medium">
                  {t("jobs.topic")}
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  {t("jobs.status")}
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  {t("jobs.engines")}
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  {t("jobs.createdAt")}
                </th>
                <th scope="col" className="py-2 font-medium">
                  <span className="sr-only">{t("jobs.open")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr
                  key={job.id}
                  className="border-b border-white/5 transition-colors duration-200 hover:bg-white/[0.03]"
                >
                  <td
                    className="max-w-[26rem] truncate py-2.5 pr-3 text-slate-100"
                    title={job.topic}
                  >
                    {job.topic}
                  </td>
                  <td className="py-2.5 pr-3">
                    <StatusBadge status={job.status} />
                  </td>
                  <td className="py-2.5 pr-3">
                    <span className="flex items-center -space-x-1.5">
                      {job.runs.slice(0, 6).map((r) => (
                        <span key={r.id} title={r.engine_id}>
                          <EngineAvatar
                            engineId={r.engine_id}
                            size="h-6 w-6"
                          />
                        </span>
                      ))}
                      {job.runs.length > 6 && (
                        <span className="pl-3 text-xs text-slate-500">
                          +{job.runs.length - 6}
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="py-2.5 pr-3 text-xs text-slate-400">
                    {formatDateTime(job.created_at) ?? t("common.unknown")}
                  </td>
                  <td className="py-2.5">
                    <button
                      type="button"
                      onClick={() => onOpen(job.id)}
                      className="rounded-xl bg-indigo-500/10 px-3 py-1.5 text-xs text-indigo-300 ring-1 ring-inset ring-indigo-400/30 transition-all duration-200 hover:bg-indigo-500/20 hover:text-indigo-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
                    >
                      {t("jobs.open")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
