"use client";

import { useEffect, useRef, useState } from "react";
import type { SynthesisCitation } from "@/lib/api-types";
import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { Markdown } from "../Markdown";
import { Icon } from "../Icon";
import { StatusBadge } from "../StatusBadge";

function citationSid(c: SynthesisCitation, index: number): string {
  if (typeof c.sid === "string" && c.sid !== "") {
    return c.sid.startsWith("S") ? c.sid : `S${c.sid}`;
  }
  return `S${index + 1}`;
}

function CitationPanel({
  citation,
  onClose,
}: {
  citation: SynthesisCitation;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  return (
    <aside
      role="dialog"
      aria-label={t("synthesis.citationDetail")}
      className="rounded-lg border border-indigo-400/40 bg-indigo-500/10 p-3"
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
    >
      <div className="mb-2 flex items-center justify-between">
        <h4 className="text-sm font-semibold text-indigo-200">
          {t("synthesis.citationDetail")}
        </h4>
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          className="rounded-lg border border-indigo-400/40 px-2 py-0.5 text-xs text-indigo-300 hover:bg-indigo-500/20 focus:outline-none focus:ring-2 focus:ring-indigo-400"
        >
          {t("common.close")}
        </button>
      </div>
      <dl className="space-y-1 text-sm">
        <div className="flex gap-2">
          <dt className="shrink-0 text-xs text-slate-500">
            {t("synthesis.citationUrl")}:
          </dt>
          <dd>
            {citation.url ? (
              <a
                href={citation.url}
                target="_blank"
                rel="noopener noreferrer"
                className="break-all text-indigo-300 underline underline-offset-2"
              >
                {citation.url}
              </a>
            ) : (
              <span className="text-slate-400">{t("common.unknown")}</span>
            )}
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="shrink-0 text-xs text-slate-500">
            {t("synthesis.citationTitle")}:
          </dt>
          <dd>
            {citation.title ?? (
              <span className="text-slate-400">{t("common.unknown")}</span>
            )}
          </dd>
        </div>
        <div className="flex gap-2">
          <dt className="shrink-0 text-xs text-slate-500">
            {t("synthesis.citationFetchedAt")}:
          </dt>
          <dd>
            {formatDateTime(citation.fetched_at ?? null) ?? (
              <span className="text-slate-400">{t("common.unknown")}</span>
            )}
          </dd>
        </div>
        {citation.excerpt && (
          <div className="flex gap-2">
            <dt className="shrink-0 text-xs text-slate-500">
              {t("synthesis.citationExcerpt")}:
            </dt>
            <dd className="text-slate-300">{citation.excerpt}</dd>
          </div>
        )}
        <div className="flex gap-2">
          <dt className="shrink-0 text-xs text-slate-500">
            {t("synthesis.citationEngines")}:
          </dt>
          <dd>
            {Array.isArray(citation.engines) && citation.engines.length > 0 ? (
              citation.engines.join(", ")
            ) : (
              <span className="text-slate-400">{t("common.unknown")}</span>
            )}
          </dd>
        </div>
      </dl>
    </aside>
  );
}

export function SynthesisTab({
  jobId,
  liveSynthesisStatus,
}: {
  jobId: string;
  liveSynthesisStatus: string | null;
}) {
  const { data, loading, error, reload } = useFetch(
    () => api.getSynthesis(jobId),
    [jobId],
  );
  const [selectedSid, setSelectedSid] = useState<string | null>(null);
  const [retryProfile, setRetryProfile] = useState("");
  const [retryBusy, setRetryBusy] = useState(false);
  const [retryMessage, setRetryMessage] = useState<string | null>(null);
  const { data: profiles } = useFetch(() => api.listProfiles(), []);

  // Refresh when live SSE says synthesis status changed.
  const prevLive = useRef(liveSynthesisStatus);
  useEffect(() => {
    if (liveSynthesisStatus !== prevLive.current) {
      prevLive.current = liveSynthesisStatus;
      reload();
    }
  }, [liveSynthesisStatus, reload]);

  if (loading) return <p className="text-sm text-slate-500">{t("common.loading")}</p>;
  if (error) {
    return (
      <div role="alert" className="text-sm text-rose-300">
        <p>
          {t("synthesis.loadFailed")}: {error}
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
  if (!data) return null;

  const citations = data.citations ?? [];
  const selectedCitation =
    selectedSid !== null
      ? citations.find((c, i) => citationSid(c, i) === selectedSid)
      : undefined;

  const failed = data.status === "failed" || data.status === "unavailable";

  const handleRetry = async () => {
    setRetryBusy(true);
    setRetryMessage(null);
    try {
      await api.retrySynthesis(jobId, retryProfile === "" ? null : retryProfile);
      setRetryMessage(t("synthesis.retryRequested"));
    } catch (e) {
      setRetryMessage(
        `${t("synthesis.retryFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setRetryBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm text-slate-400">{t("synthesis.status")}:</span>
        <StatusBadge status={data.status} />
        <span className="text-xs text-slate-500">
          {t("synthesis.attempt")}: {data.attempt}
        </span>
      </div>

      {failed && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-rose-400/40 bg-rose-500/10 p-3 text-sm text-rose-200"
        >
          <Icon name="warn" className="mt-0.5 h-4 w-4" />
          <div>
            <p className="font-semibold">
              {data.status === "failed"
                ? t("synthesis.failed")
                : t("synthesis.unavailable")}
            </p>
            {data.error && (
              <p>{t("synthesis.errorReason", { reason: data.error })}</p>
            )}
          </div>
        </div>
      )}

      {/* Retry with profile selector */}
      <div className="flex flex-wrap items-end gap-2 rounded-lg border border-white/10 bg-white/5 p-3">
        <div>
          <label
            htmlFor="synth-profile"
            className="mb-1 block text-xs font-medium text-slate-400"
          >
            {t("synthesis.retryProfile")}
          </label>
          <select
            id="synth-profile"
            className="rounded-lg border border-white/15 bg-slate-950/60 px-2 py-1.5 text-sm"
            value={retryProfile}
            onChange={(e) => setRetryProfile(e.target.value)}
          >
            <option value="">{t("synthesis.retryProfileDefault")}</option>
            {(profiles ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.model})
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={handleRetry}
          disabled={retryBusy}
          className="rounded-lg bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500 shadow-lg shadow-indigo-500/25 px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 focus:outline-none focus:ring-2 focus:ring-indigo-400 disabled:opacity-50"
        >
          {t("synthesis.retry")}
        </button>
        {retryMessage && (
          <p role="status" className="text-xs text-slate-300">
            {retryMessage}
          </p>
        )}
      </div>

      {selectedCitation && (
        <CitationPanel
          citation={selectedCitation}
          onClose={() => setSelectedSid(null)}
        />
      )}

      {data.report_markdown && (
        <Markdown
          source={data.report_markdown}
          citations
          onCitationClick={(sid) => setSelectedSid(sid)}
        />
      )}

      {citations.length > 0 && (
        <section aria-label={t("synthesis.citations")}>
          <h3 className="mb-2 text-sm font-semibold text-white">
            {t("synthesis.citations")}
          </h3>
          <ul className="space-y-1 text-sm">
            {citations.map((c, i) => {
              const sid = citationSid(c, i);
              return (
                <li key={sid} className="flex items-start gap-2">
                  <button
                    type="button"
                    onClick={() => setSelectedSid(sid)}
                    aria-label={t("synthesis.openCitation", { sid })}
                    className="mt-0.5 inline-flex items-center rounded-full border border-indigo-400/40 bg-indigo-500/10 px-1.5 text-xs font-medium text-indigo-300 hover:bg-indigo-500/20 focus:outline-none focus:ring-2 focus:ring-indigo-400"
                  >
                    {sid}
                  </button>
                  <span className="min-w-0">
                    <span className="block truncate">
                      {c.title ?? c.url ?? t("common.unknown")}
                    </span>
                    {Array.isArray(c.engines) && c.engines.length > 0 && (
                      <span className="text-xs text-slate-500">
                        {c.engines.join(", ")}
                      </span>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
