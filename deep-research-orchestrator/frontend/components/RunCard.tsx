"use client";

import { useEffect, useState } from "react";
import type { RunLiveState } from "@/lib/sse-reducer";
import { isRunActive, RUN_LOG_LIMIT } from "@/lib/sse-reducer";
import {
  findWarningReason,
  formatDateTime,
  formatElapsed,
  formatInteger,
  formatUsd,
  parseIsoMs,
  warningToText,
} from "@/lib/format";
import { getEngineMeta, EngineAvatar } from "@/lib/engine-meta";
import { t } from "@/lib/i18n";
import { StatusBadge } from "./StatusBadge";
import { Icon } from "./Icon";

interface RunCardProps {
  run: RunLiveState;
  engineName: string;
  onCancel?: (runId: string) => void;
  cancelDisabled?: boolean;
}

/** 不明 (with optional reason tooltip) — never renders a fabricated 0. */
function UnknownValue({ reason }: { reason: string | null }) {
  return (
    <span className="text-slate-500" {...(reason ? { title: reason } : {})}>
      {reason ? t("common.unknownReason", { reason }) : t("common.unknown")}
    </span>
  );
}

function MetricRow({
  label,
  children,
  testId,
}: {
  label: string;
  children: React.ReactNode;
  testId: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-sm">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="text-right text-slate-200" data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

export function RunCard({
  run,
  engineName,
  onCancel,
  cancelDisabled,
}: RunCardProps) {
  const active = isRunActive(run.status);
  const meta = getEngineMeta(run.engineId);

  // Elapsed time ticks locally from started_at while the run is active.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!active || !run.startedAt) return;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active, run.startedAt]);

  let elapsedText: string | null = null;
  if (run.elapsedSeconds !== null && !active) {
    elapsedText = formatElapsed(run.elapsedSeconds);
  } else {
    const startMs = parseIsoMs(run.startedAt);
    if (startMs !== null) {
      const endMs = parseIsoMs(run.finishedAt) ?? (active ? nowMs : null);
      if (endMs !== null) elapsedText = formatElapsed((endMs - startMs) / 1000);
    }
  }

  const warnings = run.warnings ?? [];
  const tokenReason = findWarningReason(warnings, ["token", "トークン"]);
  const costReason = findWarningReason(warnings, ["cost", "コスト", "課金"]);

  return (
    <article
      aria-label={engineName}
      className="group relative flex flex-col overflow-hidden rounded-2xl bg-slate-900/80 shadow-xl shadow-black/20 ring-1 ring-white/10 backdrop-blur transition-all duration-200 hover:ring-white/20"
    >
      {/* Engine accent bar */}
      <span
        aria-hidden="true"
        className={`h-1 w-full bg-gradient-to-r ${meta.accent}`}
      />

      <div className="flex flex-1 flex-col p-4">
        <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2.5">
            <EngineAvatar engineId={run.engineId} />
            <div className="min-w-0">
              <h3 className="truncate text-sm font-semibold tracking-tight text-white">
                {engineName}
              </h3>
              <p className="truncate text-[11px] text-slate-500">
                {t(meta.taglineKey)}
              </p>
            </div>
          </div>
          <StatusBadge status={run.status} />
        </header>

        {/* Indeterminate progress shimmer while active — no fabricated % */}
        {active && (
          <div
            aria-hidden="true"
            className="relative mb-3 h-1 overflow-hidden rounded-full bg-white/10"
          >
            <span
              className={`absolute inset-y-0 w-1/3 animate-shimmer rounded-full bg-gradient-to-r ${meta.accent} opacity-90`}
            />
          </div>
        )}

        <dl className="space-y-1.5">
          <MetricRow label={t("run.stage")} testId="metric-stage">
            {run.stage ?? <UnknownValue reason={null} />}
          </MetricRow>
          <MetricRow label={t("run.elapsed")} testId="metric-elapsed">
            {elapsedText ?? <UnknownValue reason={null} />}
          </MetricRow>
          <MetricRow label={t("run.searches")} testId="metric-searches">
            {run.searchCount !== null ? (
              formatInteger(run.searchCount)
            ) : (
              <UnknownValue reason={null} />
            )}
          </MetricRow>
          <MetricRow label={t("run.sources")} testId="metric-sources">
            {run.sourceCount !== null ? (
              formatInteger(run.sourceCount)
            ) : (
              <UnknownValue reason={null} />
            )}
          </MetricRow>
          <MetricRow label={t("run.tokens")} testId="metric-tokens">
            {run.tokensTotal !== null ? (
              <>
                {formatInteger(run.tokensTotal)}
                {run.tokensInput !== null && run.tokensOutput !== null && (
                  <span className="ml-1 text-xs text-slate-500">
                    ({t("run.tokensIn")} {formatInteger(run.tokensInput)} /{" "}
                    {t("run.tokensOut")} {formatInteger(run.tokensOutput)})
                  </span>
                )}
              </>
            ) : (
              <UnknownValue reason={tokenReason} />
            )}
          </MetricRow>
          <MetricRow label={t("run.llmCost")} testId="metric-llm-cost">
            {run.llmCostUsd !== null ? (
              <>
                {formatUsd(run.llmCostUsd)}
                {run.llmCostIsEstimate !== null && (
                  <span className="ml-1 rounded-full bg-white/10 px-1.5 text-xs text-slate-300 ring-1 ring-inset ring-white/10">
                    {run.llmCostIsEstimate
                      ? t("common.estimateTag")
                      : t("common.measuredTag")}
                  </span>
                )}
              </>
            ) : (
              <UnknownValue reason={costReason} />
            )}
          </MetricRow>
          <MetricRow label={t("run.searchApiCost")} testId="metric-search-cost">
            {run.searchApiCostUsd === 0 ? (
              t("common.selfHostedZeroCost")
            ) : run.searchApiCostUsd !== null ? (
              formatUsd(run.searchApiCostUsd)
            ) : (
              <UnknownValue reason={costReason} />
            )}
          </MetricRow>
          <MetricRow label={t("run.infraCost")} testId="metric-infra-cost">
            {run.infraCost === "not_measured" ? (
              t("common.notMeasured")
            ) : typeof run.infraCost === "number" ? (
              formatUsd(run.infraCost)
            ) : typeof run.infraCost === "string" ? (
              run.infraCost
            ) : (
              <UnknownValue reason={null} />
            )}
          </MetricRow>
          {run.maxAttempts > 1 && (
            <MetricRow label={t("run.attempt")} testId="metric-attempt">
              {run.attempt} / {run.maxAttempts}
            </MetricRow>
          )}
        </dl>

        {run.error && (
          <p
            role="alert"
            className="mt-3 flex items-start gap-1.5 rounded-xl bg-rose-500/10 px-3 py-2 text-xs text-rose-300 ring-1 ring-inset ring-rose-400/30"
          >
            <Icon name="x" className="mt-0.5 h-3.5 w-3.5" />
            <span>
              {t("run.error")}: {run.error}
            </span>
          </p>
        )}

        {warnings.length > 0 && (
          <ul
            aria-label={t("common.warnings")}
            className="mt-3 space-y-1 rounded-xl bg-amber-500/10 px-3 py-2 ring-1 ring-inset ring-amber-400/30"
          >
            {warnings.map((w, i) => (
              <li
                key={i}
                className="flex items-start gap-1.5 text-xs text-amber-300"
              >
                <Icon name="warn" className="mt-0.5 h-3.5 w-3.5" />
                <span>{warningToText(w)}</span>
              </li>
            ))}
          </ul>
        )}

        <details className="mt-3">
          <summary className="cursor-pointer select-none text-xs text-slate-500 transition-colors duration-200 hover:text-slate-300">
            {t("run.eventLog", { count: RUN_LOG_LIMIT })}
          </summary>
          {run.log.length === 0 ? (
            <p className="mt-1.5 text-xs text-slate-600">{t("run.noEvents")}</p>
          ) : (
            <ol className="mt-1.5 max-h-48 space-y-0.5 overflow-y-auto rounded-xl bg-black/60 p-3 font-mono text-[11px] leading-relaxed ring-1 ring-inset ring-white/10">
              {run.log.map((ev) => (
                <li key={ev.seq} className="whitespace-nowrap">
                  <span className="text-slate-600">#{ev.seq}</span>{" "}
                  <span className="text-slate-500">
                    {formatDateTime(ev.created_at)}
                  </span>{" "}
                  <span className="font-semibold text-emerald-300/90">
                    {ev.type}
                  </span>{" "}
                  <span className="text-slate-500">
                    {JSON.stringify(ev.payload)}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </details>

        <div className="mt-auto pt-3">
          {run.cancelRequested ? (
            <p className="text-xs text-slate-500">{t("run.cancelRequested")}</p>
          ) : (
            active &&
            onCancel && (
              <button
                type="button"
                onClick={() => onCancel(run.runId)}
                disabled={cancelDisabled}
                className="rounded-xl px-3 py-1.5 text-xs text-rose-300 ring-1 ring-inset ring-rose-400/40 transition-all duration-200 hover:bg-rose-500/10 hover:text-rose-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 disabled:opacity-50"
              >
                {t("run.cancelRun")}
              </button>
            )
          )}
        </div>
      </div>
    </article>
  );
}
