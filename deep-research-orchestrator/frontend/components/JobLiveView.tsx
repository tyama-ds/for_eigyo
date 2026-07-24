"use client";

import { useEffect, useRef, useState } from "react";
import { useJobEvents } from "@/lib/useJobEvents";
import { isJobActive, orderedRuns } from "@/lib/sse-reducer";
import { downloadExport } from "@/lib/api";
import { api } from "@/lib/api";
import { t } from "@/lib/i18n";
import { warningToText } from "@/lib/format";
import { StatusBadge } from "./StatusBadge";
import { RunCard } from "./RunCard";
import { Icon } from "./Icon";
import { Tabs, TabPanel } from "./Tabs";
import { OverviewTab } from "./tabs/OverviewTab";
import { CompareTab } from "./tabs/CompareTab";
import { ReportsTab } from "./tabs/ReportsTab";
import { SourcesTab } from "./tabs/SourcesTab";
import { ConflictsTab } from "./tabs/ConflictsTab";
import { SynthesisTab } from "./tabs/SynthesisTab";
import { RawTab } from "./tabs/RawTab";

interface JobLiveViewProps {
  jobId: string;
  engineNames: Record<string, string>;
  onBack: () => void;
}

const CONNECTION_LABELS = {
  idle: null,
  connecting: "job.connection.connecting",
  open: "job.connection.open",
  reconnecting: "job.connection.reconnecting",
  closed: "job.connection.closed",
} as const;

export function JobLiveView({ jobId, engineNames, onBack }: JobLiveViewProps) {
  const { state, refreshSnapshot, snapshotError } = useJobEvents(jobId);
  const [activeTab, setActiveTab] = useState("overview");
  const [actionError, setActionError] = useState<string | null>(null);
  const [exportBusy, setExportBusy] = useState(false);

  const jobStatus = state.jobStatus;
  const active = isJobActive(jobStatus);

  // When the job reaches a terminal state, refresh the snapshot once to get
  // final metrics/warnings.
  const wasActive = useRef(false);
  useEffect(() => {
    if (wasActive.current && !active && jobStatus) {
      refreshSnapshot();
    }
    wasActive.current = active;
  }, [active, jobStatus, refreshSnapshot]);

  const runs = orderedRuns(state);

  const handleCancelJob = async () => {
    setActionError(null);
    try {
      await api.cancelJob(jobId);
      refreshSnapshot();
    } catch (e) {
      setActionError(
        `${t("job.cancelFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  };

  const handleCancelRun = async (runId: string) => {
    setActionError(null);
    try {
      await api.cancelRun(jobId, runId);
      refreshSnapshot();
    } catch (e) {
      setActionError(
        `${t("job.cancelFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  };

  const handleExport = async (format: "markdown" | "json") => {
    setActionError(null);
    setExportBusy(true);
    try {
      await downloadExport(jobId, format);
    } catch (e) {
      setActionError(
        `${t("job.exportFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setExportBusy(false);
    }
  };

  const connectionKey = CONNECTION_LABELS[state.connection];
  const isPartial = jobStatus === "partial";

  const tabs = [
    { id: "overview", label: t("tabs.overview") },
    { id: "compare", label: t("tabs.compare") },
    { id: "reports", label: t("tabs.reports") },
    { id: "sources", label: t("tabs.sources") },
    { id: "conflicts", label: t("tabs.conflicts") },
    { id: "synthesis", label: t("tabs.synthesis") },
    { id: "raw", label: t("tabs.raw") },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onBack}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-sky-500"
        >
          ← {t("job.backToList")}
        </button>
        {connectionKey && (
          <span
            role="status"
            className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-0.5 text-xs text-slate-600"
          >
            <Icon
              name={state.connection === "open" ? "check" : "spinner"}
              className="h-3 w-3"
              spin={
                state.connection === "connecting" ||
                state.connection === "reconnecting"
              }
            />
            {t(connectionKey)}
          </span>
        )}
      </div>

      {snapshotError && (
        <p
          role="alert"
          className="rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800"
        >
          {t("job.loadFailed")}: {snapshotError}
        </p>
      )}
      {actionError && (
        <p
          role="alert"
          className="rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800"
        >
          {actionError}
        </p>
      )}

      {/* Job status banner — partial is never shown as full success */}
      <section
        aria-label={t("jobs.status")}
        className={`rounded-lg border p-3 ${
          isPartial
            ? "border-amber-300 bg-amber-50"
            : "border-slate-200 bg-white"
        }`}
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {jobStatus && <StatusBadge status={jobStatus} />}
              {isPartial && (
                <span className="inline-flex items-center gap-1 text-sm font-semibold text-amber-900">
                  <Icon name="warn" className="h-4 w-4" />
                  {t("job.statusBanner.partial")}
                </span>
              )}
              {state.cancelRequested && active && (
                <span className="text-sm text-slate-600">
                  {t("job.statusBanner.cancelRequested")}
                </span>
              )}
            </div>
            <h2
              className="mt-1 truncate text-base font-semibold text-slate-900"
              title={state.job?.topic}
            >
              {state.job?.topic ?? t("common.loading")}
            </h2>
            {state.jobError && (
              <p className="mt-1 text-sm text-rose-700">
                {t("job.error")}: {state.jobError}
              </p>
            )}
            {state.jobWarnings.length > 0 && (
              <ul className="mt-1 space-y-0.5">
                {state.jobWarnings.map((w, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-1 text-xs text-amber-900"
                  >
                    <Icon name="warn" className="mt-0.5 h-3 w-3" />
                    {warningToText(w)}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            {active && !state.cancelRequested && (
              <button
                type="button"
                onClick={handleCancelJob}
                className="rounded border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:bg-rose-50 focus:outline-none focus:ring-2 focus:ring-rose-400"
              >
                {t("job.cancelJob")}
              </button>
            )}
            <button
              type="button"
              onClick={() => handleExport("markdown")}
              disabled={exportBusy}
              className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
            >
              {t("job.exportMarkdown")}
            </button>
            <button
              type="button"
              onClick={() => handleExport("json")}
              disabled={exportBusy}
              className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
            >
              {t("job.exportJson")}
            </button>
          </div>
        </div>
      </section>

      {/* Per-engine run cards */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {runs.map((run) => (
          <RunCard
            key={run.runId}
            run={run}
            engineName={engineNames[run.engineId] ?? run.engineId}
            onCancel={handleCancelRun}
          />
        ))}
      </div>

      {/* Results tabs */}
      <div>
        <Tabs
          tabs={tabs}
          active={activeTab}
          onChange={setActiveTab}
          ariaLabel={t("tabs.ariaLabel")}
        />
        <TabPanel id="overview" active={activeTab}>
          <OverviewTab state={state} engineNames={engineNames} />
        </TabPanel>
        <TabPanel id="compare" active={activeTab}>
          <CompareTab jobId={jobId} />
        </TabPanel>
        <TabPanel id="reports" active={activeTab}>
          <ReportsTab jobId={jobId} engineNames={engineNames} />
        </TabPanel>
        <TabPanel id="sources" active={activeTab}>
          <SourcesTab jobId={jobId} engineNames={engineNames} />
        </TabPanel>
        <TabPanel id="conflicts" active={activeTab}>
          <ConflictsTab jobId={jobId} />
        </TabPanel>
        <TabPanel id="synthesis" active={activeTab}>
          <SynthesisTab
            jobId={jobId}
            liveSynthesisStatus={state.synthesisStatus}
          />
        </TabPanel>
        <TabPanel id="raw" active={activeTab}>
          <RawTab jobId={jobId} engineNames={engineNames} />
        </TabPanel>
      </div>
    </div>
  );
}
