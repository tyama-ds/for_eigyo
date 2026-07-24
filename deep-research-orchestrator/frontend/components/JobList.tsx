"use client";

import type { JobView } from "@/lib/api-types";
import { formatDateTime } from "@/lib/format";
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
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-base font-semibold text-slate-900">
          {t("jobs.recentTitle")}
        </h2>
        <button
          type="button"
          onClick={onRefresh}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-sky-500"
        >
          {t("common.refresh")}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-rose-700">
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
              <tr className="border-b border-slate-200 text-xs text-slate-500">
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("jobs.topic")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("jobs.status")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("jobs.engines")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("jobs.createdAt")}
                </th>
                <th scope="col" className="py-1.5 font-medium">
                  <span className="sr-only">{t("jobs.open")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id} className="border-b border-slate-100">
                  <td className="max-w-[26rem] truncate py-2 pr-3" title={job.topic}>
                    {job.topic}
                  </td>
                  <td className="py-2 pr-3">
                    <StatusBadge status={job.status} />
                  </td>
                  <td className="py-2 pr-3 text-xs text-slate-600">
                    {job.runs.map((r) => r.engine_id).join(", ")}
                  </td>
                  <td className="py-2 pr-3 text-xs text-slate-600">
                    {formatDateTime(job.created_at) ?? t("common.unknown")}
                  </td>
                  <td className="py-2">
                    <button
                      type="button"
                      onClick={() => onOpen(job.id)}
                      className="rounded border border-sky-300 px-2 py-1 text-xs text-sky-800 hover:bg-sky-50 focus:outline-none focus:ring-2 focus:ring-sky-500"
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
