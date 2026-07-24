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

const secondaryBtn =
  "rounded-xl bg-white/5 px-3.5 py-2 text-sm text-slate-200 ring-1 ring-inset ring-white/15 transition-all duration-200 hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:opacity-50";

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
        <button type="button" onClick={onBack} className={secondaryBtn}>
          ← {t("job.backToList")}
        </button>
        {connectionKey && (
          <span
            role="status"
            className="inline-flex items-center gap-1.5 rounded-full bg-white/5 px-2.5 py-1 text-xs text-slate-400 ring-1 ring-inset ring-white/10"
          >
            <span
              aria-hidden="true"
              className={`h-1.5 w-1.5 rounded-full ${
                state.connection === "open"
                  ? "animate-pulse bg-emerald-400"
                  : state.connection === "closed"
                    ? "bg-slate-500"
                    : "animate-pulse bg-amber-400"
              }`}
            />
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
          className="rounded-xl bg-rose-500/10 px-4 py-2.5 text-sm text-rose-300 ring-1 ring-inset ring-rose-400/30"
        >
          {t("job.loadFailed")}: {snapshotError}
        </p>
      )}
      {actionError && (
        <p
          role="alert"
          className="rounded-xl bg-rose-500/10 px-4 py-2.5 text-sm text-rose-300 ring-1 ring-inset ring-rose-400/30"
        >
          {actionError}
        </p>
      )}

      {/* Job status banner — partial is never shown as full success */}
      <section
        aria-label={t("jobs.status")}
        className={`relative overflow-hidden rounded-2xl p-5 shadow-xl shadow-black/20 backdrop-blur ${
          isPartial
            ? "bg-amber-500/[0.08] ring-1 ring-inset ring-amber-400/40"
            : "bg-slate-900/80 ring-1 ring-white/10"
        }`}
      >
        <span
          aria-hidden="true"
          className={`absolute inset-x-0 top-0 h-1 bg-gradient-to-r ${
            isPartial
              ? "from-amber-400 to-orange-500"
              : "from-indigo-500 via-violet-500 to-fuchsia-500"
          }`}
        />
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {jobStatus && <StatusBadge status={jobStatus} />}
              {isPartial && (
                <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-amber-300">
                  <Icon name="warn" className="h-4 w-4" />
                  {t("job.statusBanner.partial")}
                </span>
              )}
              {state.cancelRequested && active && (
                <span className="text-sm text-slate-400">
                  {t("job.statusBanner.cancelRequested")}
                </span>
              )}
            </div>
            <h2
              className="mt-1.5 truncate text-lg font-semibold tracking-tight text-white"
              title={state.job?.topic}
            >
              {state.job?.topic ?? t("common.loading")}
            </h2>
            {state.jobError && (
              <p className="mt-1 text-sm text-rose-300">
                {t("job.error")}: {state.jobError}
              </p>
            )}
            {state.jobWarnings.length > 0 && (
              <ul className="mt-1.5 space-y-0.5">
                {state.jobWarnings.map((w, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-1.5 text-xs text-amber-300"
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
                className="rounded-xl px-3.5 py-2 text-sm text-rose-300 ring-1 ring-inset ring-rose-400/40 transition-all duration-200 hover:bg-rose-500/10 hover:text-rose-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400"
              >
                {t("job.cancelJob")}
              </button>
            )}
            <button
              type="button"
              onClick={() => handleExport("markdown")}
              disabled={exportBusy}
              className={secondaryBtn}
            >
              {t("job.exportMarkdown")}
            </button>
            <button
              type="button"
              onClick={() => handleExport("json")}
              disabled={exportBusy}
              className={secondaryBtn}
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
