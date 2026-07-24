"use client";

import { useEffect, useMemo, useState } from "react";
import type {
  CreateJobRequest,
  EgressPreview,
  EngineView,
  JobView,
} from "@/lib/api-types";
import { api } from "@/lib/api";
import { getEngineMeta, EngineAvatar } from "@/lib/engine-meta";
import { t } from "@/lib/i18n";
import { Icon } from "./Icon";

interface JobFormProps {
  engines: EngineView[] | null;
  enginesError: string | null;
  onCreated: (job: JobView) => void;
}

function engineSelectable(e: EngineView): boolean {
  if (!e.enabled) return false;
  if (e.healthy === false) return false;
  return e.availability === "available" || e.availability === "experimental";
}

function availabilityLabel(availability: string): string {
  switch (availability) {
    case "available":
      return t("engine.availability.available");
    case "experimental":
      return t("engine.availability.experimental");
    case "unsupported":
      return t("engine.availability.unsupported");
    case "disabled":
      return t("engine.availability.disabled");
    case "unhealthy":
      return t("engine.availability.unhealthy");
    default:
      return availability;
  }
}

const inputCls =
  "w-full rounded-xl bg-slate-950/60 px-3 py-2 text-sm text-slate-100 ring-1 ring-inset ring-white/10 placeholder:text-slate-600 transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-indigo-400";
const labelCls = "block text-xs font-medium text-slate-400 mb-1.5";

export function JobForm({ engines, enginesError, onCreated }: JobFormProps) {
  const [topic, setTopic] = useState("");
  const [objective, setObjective] = useState("");
  const [instructions, setInstructions] = useState("");
  const [language, setLanguage] = useState("ja");
  const [inputUrlsText, setInputUrlsText] = useState("");
  const [selectedEngines, setSelectedEngines] = useState<string[]>([]);
  const [maxTime, setMaxTime] = useState("");
  const [maxSearches, setMaxSearches] = useState("");
  const [maxCost, setMaxCost] = useState("");
  const [autoSynthesize, setAutoSynthesize] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Egress preview — must be shown BEFORE starting a job, refreshed on
  // engine selection change.
  const [egress, setEgress] = useState<EgressPreview | null>(null);
  const [egressError, setEgressError] = useState<string | null>(null);
  const engineKey = selectedEngines.join(",");
  useEffect(() => {
    if (selectedEngines.length === 0) {
      setEgress(null);
      setEgressError(null);
      return;
    }
    let cancelled = false;
    api
      .egressPreview(selectedEngines)
      .then((p) => {
        if (!cancelled) {
          setEgress(p);
          setEgressError(null);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setEgress(null);
          setEgressError(t("egress.loadFailed"));
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engineKey]);

  const inputUrls = useMemo(
    () =>
      inputUrlsText
        .split("\n")
        .map((s) => s.trim())
        .filter((s) => s !== ""),
    [inputUrlsText],
  );

  const toggleEngine = (engineId: string) => {
    setSelectedEngines((prev) =>
      prev.includes(engineId)
        ? prev.filter((id) => id !== engineId)
        : [...prev, engineId],
    );
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    if (topic.trim() === "") {
      setFormError(t("form.topicRequired"));
      return;
    }
    if (selectedEngines.length === 0) {
      setFormError(t("form.enginesRequired"));
      return;
    }
    if (inputUrls.length > 20) {
      setFormError(t("form.tooManyUrls"));
      return;
    }
    // New idempotency key per form submission.
    const idempotencyKey = crypto.randomUUID();
    const body: CreateJobRequest = {
      topic: topic.trim(),
      objective: objective.trim() === "" ? null : objective.trim(),
      instructions: instructions.trim() === "" ? null : instructions.trim(),
      language,
      engines: selectedEngines,
      max_time_seconds: maxTime === "" ? null : Number(maxTime),
      max_searches: maxSearches === "" ? null : Number(maxSearches),
      max_cost_usd: maxCost === "" ? null : Number(maxCost),
      auto_synthesize: autoSynthesize,
      input_urls: inputUrls,
      engine_options: {},
      idempotency_key: idempotencyKey,
    };
    setSubmitting(true);
    try {
      const job = await api.createJob(body, idempotencyKey);
      onCreated(job);
    } catch (err) {
      setFormError(
        t("common.errorPrefix", {
          message: err instanceof Error ? err.message : String(err),
        }),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-2xl bg-slate-900/80 p-5 shadow-xl shadow-black/20 ring-1 ring-white/10 backdrop-blur"
      aria-label={t("form.title")}
    >
      <h2 className="mb-4 text-base font-semibold tracking-tight text-white">
        {t("form.title")}
      </h2>

      {formError && (
        <div
          role="alert"
          className="mb-4 flex items-center gap-2 rounded-xl bg-rose-500/10 px-3 py-2.5 text-sm text-rose-300 ring-1 ring-inset ring-rose-400/30"
        >
          <Icon name="warn" />
          <span>{formError}</span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="space-y-4">
          <div>
            <label htmlFor="jf-topic" className={labelCls}>
              {t("form.topic")}{" "}
              <span className="text-fuchsia-400">*{t("common.required")}</span>
            </label>
            <textarea
              id="jf-topic"
              required
              rows={2}
              maxLength={4000}
              className={inputCls}
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="jf-objective" className={labelCls}>
              {t("form.objective")}
              {t("form.optionalSuffix")}
            </label>
            <textarea
              id="jf-objective"
              rows={2}
              maxLength={4000}
              className={inputCls}
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="jf-instructions" className={labelCls}>
              {t("form.instructions")}
              {t("form.optionalSuffix")}
            </label>
            <textarea
              id="jf-instructions"
              rows={2}
              maxLength={8000}
              className={inputCls}
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="jf-language" className={labelCls}>
                {t("form.language")}
              </label>
              <select
                id="jf-language"
                className={inputCls}
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              >
                <option value="ja">{t("form.language.ja")}</option>
                <option value="en">{t("form.language.en")}</option>
              </select>
            </div>
            <div>
              <label htmlFor="jf-maxtime" className={labelCls}>
                {t("form.maxTimeSeconds")}
                {t("form.optionalSuffix")}
              </label>
              <input
                id="jf-maxtime"
                type="number"
                min={10}
                max={21600}
                className={inputCls}
                value={maxTime}
                onChange={(e) => setMaxTime(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="jf-maxsearches" className={labelCls}>
                {t("form.maxSearches")}
                {t("form.optionalSuffix")}
              </label>
              <input
                id="jf-maxsearches"
                type="number"
                min={1}
                max={200}
                className={inputCls}
                value={maxSearches}
                onChange={(e) => setMaxSearches(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="jf-maxcost" className={labelCls}>
                {t("form.maxCostUsd")}
                {t("form.optionalSuffix")}
              </label>
              <input
                id="jf-maxcost"
                type="number"
                min={0}
                step="0.01"
                className={inputCls}
                value={maxCost}
                onChange={(e) => setMaxCost(e.target.value)}
              />
            </div>
          </div>
          <div>
            <label htmlFor="jf-urls" className={labelCls}>
              {t("form.inputUrls")}
              {t("form.optionalSuffix")}
            </label>
            <textarea
              id="jf-urls"
              rows={3}
              className={inputCls}
              value={inputUrlsText}
              onChange={(e) => setInputUrlsText(e.target.value)}
              aria-describedby="jf-urls-help"
            />
            <p id="jf-urls-help" className="mt-1.5 text-xs text-slate-500">
              {t("form.inputUrlsHelp")}
            </p>
          </div>
          <div className="flex items-center gap-2.5">
            <input
              id="jf-autosynth"
              type="checkbox"
              checked={autoSynthesize}
              onChange={(e) => setAutoSynthesize(e.target.checked)}
              className="h-4 w-4 rounded border-white/20 bg-slate-950/60 text-indigo-500 focus:ring-indigo-400 focus:ring-offset-slate-900"
              aria-describedby="jf-autosynth-help"
            />
            <label htmlFor="jf-autosynth" className="text-sm text-slate-200">
              {t("form.autoSynthesize")}
            </label>
          </div>
          <p id="jf-autosynth-help" className="text-xs text-slate-500">
            {t("form.autoSynthesizeHelp")}
          </p>
        </div>

        <div className="space-y-5">
          <fieldset>
            <legend className={labelCls}>{t("form.engines")}</legend>
            {enginesError && (
              <p role="alert" className="text-sm text-rose-300">
                {t("common.errorPrefix", { message: enginesError })}
              </p>
            )}
            {engines && engines.length === 0 && (
              <p className="text-sm text-slate-500">{t("engine.noEngines")}</p>
            )}
            {!engines && !enginesError && (
              <p className="text-sm text-slate-500">{t("common.loading")}</p>
            )}
            {/* Engine picker: icon + name + tagline card tiles */}
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {(engines ?? []).map((engine) => {
                const selectable = engineSelectable(engine);
                const selected = selectedEngines.includes(engine.engine_id);
                const meta = getEngineMeta(engine.engine_id);
                const reason =
                  engine.unavailable_reason ??
                  (engine.healthy === false ? t("engine.unhealthy") : null);
                return (
                  <label
                    key={engine.engine_id}
                    htmlFor={`jf-engine-${engine.engine_id}`}
                    className={`group relative flex items-start gap-3 rounded-2xl p-3 ring-1 transition-all duration-200 ${
                      selected
                        ? "bg-white/10 ring-2 ring-indigo-400/70 shadow-lg shadow-indigo-500/10"
                        : "bg-white/5 ring-white/10"
                    } ${
                      selectable
                        ? "cursor-pointer hover:bg-white/10 hover:ring-white/25"
                        : "cursor-not-allowed opacity-45"
                    }`}
                  >
                    {/* タイル全面を覆う透明checkbox — 実クリック対象を入力自体にする
                        (sr-onlyだとlabelがpointer eventsを奪い、実ブラウザ自動操作で
                        checkboxを直接クリックできない) */}
                    <input
                      id={`jf-engine-${engine.engine_id}`}
                      type="checkbox"
                      className={`peer absolute inset-0 z-10 h-full w-full appearance-none rounded-2xl opacity-0 ${
                        selectable ? "cursor-pointer" : "cursor-not-allowed"
                      }`}
                      disabled={!selectable}
                      checked={selected}
                      onChange={() => toggleEngine(engine.engine_id)}
                    />
                    {/* Keyboard focus ring for the invisible checkbox */}
                    <span
                      aria-hidden="true"
                      className="pointer-events-none absolute inset-0 rounded-2xl ring-indigo-300 peer-focus-visible:ring-2"
                    />
                    <EngineAvatar engineId={engine.engine_id} size="h-10 w-10" />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5">
                        <span className="truncate text-sm font-semibold tracking-tight text-white">
                          {engine.display_name}
                        </span>
                        {selected && (
                          <Icon
                            name="check"
                            className="h-3.5 w-3.5 shrink-0 text-indigo-300"
                          />
                        )}
                      </span>
                      <span className="block truncate text-[11px] text-slate-400">
                        {t(meta.taglineKey)}
                      </span>
                      <span className="mt-1 inline-flex items-center rounded-full bg-white/5 px-1.5 py-px text-[10px] text-slate-400 ring-1 ring-inset ring-white/10">
                        {availabilityLabel(engine.availability)}
                      </span>
                      {!selectable && reason && (
                        <span className="mt-1 block text-[11px] text-amber-300/90">
                          {reason}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>

          {/* Egress preview panel */}
          <section
            aria-label={t("egress.title")}
            className="rounded-2xl bg-amber-500/[0.07] p-4 ring-1 ring-inset ring-amber-400/25"
          >
            <h3 className="mb-2.5 flex items-center gap-1.5 text-sm font-semibold tracking-tight text-amber-300">
              <Icon name="link" className="h-4 w-4" />
              {t("egress.title")}
            </h3>
            {selectedEngines.length === 0 ? (
              <p className="text-xs text-amber-200/70">
                {t("egress.selectEngines")}
              </p>
            ) : egressError ? (
              <p role="alert" className="text-xs text-rose-300">
                {egressError}
              </p>
            ) : !egress ? (
              <p className="text-xs text-amber-200/70">{t("common.loading")}</p>
            ) : egress.destinations.length === 0 ? (
              <p className="text-xs text-amber-200/70">{t("egress.empty")}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-amber-400/20 text-amber-300/80">
                      <th scope="col" className="py-1.5 pr-2 font-medium">
                        {t("egress.kind")}
                      </th>
                      <th scope="col" className="py-1.5 pr-2 font-medium">
                        {t("egress.name")}
                      </th>
                      <th scope="col" className="py-1.5 pr-2 font-medium">
                        {t("egress.host")}
                      </th>
                      <th scope="col" className="py-1.5 font-medium">
                        {t("egress.purpose")}
                      </th>
                    </tr>
                  </thead>
                  <tbody className="text-slate-300">
                    {egress.destinations.map((d, i) => (
                      <tr key={i} className="border-b border-amber-400/10">
                        <td className="py-1.5 pr-2">{String(d.kind ?? "")}</td>
                        <td className="py-1.5 pr-2">{String(d.name ?? "")}</td>
                        <td className="py-1.5 pr-2 font-mono text-amber-100/90">
                          {String(d.host ?? "")}
                        </td>
                        <td className="py-1.5">{String(d.purpose ?? "")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      </div>

      <div className="mt-5 border-t border-white/10 pt-4">
        <button
          type="submit"
          disabled={submitting}
          className="rounded-xl bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/25 transition-all duration-200 hover:brightness-110 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900 disabled:opacity-50"
        >
          {submitting ? t("form.submitting") : t("form.submit")}
        </button>
      </div>
    </form>
  );
}
