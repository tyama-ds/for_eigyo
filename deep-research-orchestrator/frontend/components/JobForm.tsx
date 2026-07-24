"use client";

import { useEffect, useMemo, useState } from "react";
import type {
  CreateJobRequest,
  EgressPreview,
  EngineView,
  JobView,
} from "@/lib/api-types";
import { api } from "@/lib/api";
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
  "w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500";
const labelCls = "block text-xs font-medium text-slate-600 mb-1";

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
      className="rounded-lg border border-slate-200 bg-white p-4"
      aria-label={t("form.title")}
    >
      <h2 className="mb-3 text-base font-semibold text-slate-900">
        {t("form.title")}
      </h2>

      {formError && (
        <div
          role="alert"
          className="mb-3 flex items-center gap-2 rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800"
        >
          <Icon name="warn" />
          <span>{formError}</span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="space-y-3">
          <div>
            <label htmlFor="jf-topic" className={labelCls}>
              {t("form.topic")}{" "}
              <span className="text-rose-600">*{t("common.required")}</span>
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
          <div className="grid grid-cols-2 gap-3">
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
            <p id="jf-urls-help" className="mt-1 text-xs text-slate-500">
              {t("form.inputUrlsHelp")}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              id="jf-autosynth"
              type="checkbox"
              checked={autoSynthesize}
              onChange={(e) => setAutoSynthesize(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300"
              aria-describedby="jf-autosynth-help"
            />
            <label htmlFor="jf-autosynth" className="text-sm text-slate-800">
              {t("form.autoSynthesize")}
            </label>
          </div>
          <p id="jf-autosynth-help" className="text-xs text-slate-500">
            {t("form.autoSynthesizeHelp")}
          </p>
        </div>

        <div className="space-y-4">
          <fieldset>
            <legend className={labelCls}>{t("form.engines")}</legend>
            {enginesError && (
              <p role="alert" className="text-sm text-rose-700">
                {t("common.errorPrefix", { message: enginesError })}
              </p>
            )}
            {engines && engines.length === 0 && (
              <p className="text-sm text-slate-500">{t("engine.noEngines")}</p>
            )}
            {!engines && !enginesError && (
              <p className="text-sm text-slate-500">{t("common.loading")}</p>
            )}
            <ul className="space-y-1">
              {(engines ?? []).map((engine) => {
                const selectable = engineSelectable(engine);
                const reason =
                  engine.unavailable_reason ??
                  (engine.healthy === false ? t("engine.unhealthy") : null);
                return (
                  <li
                    key={engine.engine_id}
                    className={`flex items-start gap-2 rounded border px-2 py-1.5 ${
                      selectable
                        ? "border-slate-200 bg-white"
                        : "border-slate-200 bg-slate-50"
                    }`}
                  >
                    <input
                      id={`jf-engine-${engine.engine_id}`}
                      type="checkbox"
                      className="mt-0.5 h-4 w-4 rounded border-slate-300"
                      disabled={!selectable}
                      checked={selectedEngines.includes(engine.engine_id)}
                      onChange={() => toggleEngine(engine.engine_id)}
                    />
                    <label
                      htmlFor={`jf-engine-${engine.engine_id}`}
                      className={`flex-1 text-sm ${
                        selectable ? "text-slate-800" : "text-slate-400"
                      }`}
                    >
                      <span className="font-medium">{engine.display_name}</span>{" "}
                      <span className="text-xs text-slate-500">
                        ({availabilityLabel(engine.availability)})
                      </span>
                      {!selectable && reason && (
                        <span className="block text-xs text-slate-500">
                          {reason}
                        </span>
                      )}
                    </label>
                  </li>
                );
              })}
            </ul>
          </fieldset>

          {/* Egress preview panel */}
          <section
            aria-label={t("egress.title")}
            className="rounded border border-amber-200 bg-amber-50 p-3"
          >
            <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-amber-900">
              <Icon name="link" className="h-4 w-4" />
              {t("egress.title")}
            </h3>
            {selectedEngines.length === 0 ? (
              <p className="text-xs text-amber-800">
                {t("egress.selectEngines")}
              </p>
            ) : egressError ? (
              <p role="alert" className="text-xs text-rose-700">
                {egressError}
              </p>
            ) : !egress ? (
              <p className="text-xs text-amber-800">{t("common.loading")}</p>
            ) : egress.destinations.length === 0 ? (
              <p className="text-xs text-amber-800">{t("egress.empty")}</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-amber-200 text-amber-900">
                      <th scope="col" className="py-1 pr-2 font-medium">
                        {t("egress.kind")}
                      </th>
                      <th scope="col" className="py-1 pr-2 font-medium">
                        {t("egress.name")}
                      </th>
                      <th scope="col" className="py-1 pr-2 font-medium">
                        {t("egress.host")}
                      </th>
                      <th scope="col" className="py-1 font-medium">
                        {t("egress.purpose")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {egress.destinations.map((d, i) => (
                      <tr key={i} className="border-b border-amber-100">
                        <td className="py-1 pr-2">{String(d.kind ?? "")}</td>
                        <td className="py-1 pr-2">{String(d.name ?? "")}</td>
                        <td className="py-1 pr-2 font-mono">
                          {String(d.host ?? "")}
                        </td>
                        <td className="py-1">{String(d.purpose ?? "")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      </div>

      <div className="mt-4 border-t border-slate-100 pt-3">
        <button
          type="submit"
          disabled={submitting}
          className="rounded bg-sky-700 px-4 py-2 text-sm font-medium text-white hover:bg-sky-800 focus:outline-none focus:ring-2 focus:ring-sky-500 focus:ring-offset-1 disabled:opacity-50"
        >
          {submitting ? t("form.submitting") : t("form.submit")}
        </button>
      </div>
    </form>
  );
}
