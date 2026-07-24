"use client";

import { useState } from "react";
import type {
  LlmProfileIn,
  LlmProfileTestResult,
  LlmProfileView,
} from "@/lib/api-types";
import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { Icon } from "../Icon";
import { ConfirmDialog } from "../ConfirmDialog";

const inputCls =
  "w-full rounded-lg border border-white/15 bg-slate-950/60 px-2 py-1.5 text-sm focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-400";
const labelCls = "block text-xs font-medium text-slate-400 mb-1";

interface ProfileFormState {
  name: string;
  provider: "local" | "openai" | "anthropic";
  api: "openai-compatible" | "anthropic";
  endpoint: string;
  model: string;
  apiKey: string;
  timeoutSeconds: string;
  maxConcurrency: string;
  enabled: boolean;
}

const emptyForm: ProfileFormState = {
  name: "",
  provider: "local",
  api: "openai-compatible",
  endpoint: "",
  model: "",
  apiKey: "",
  timeoutSeconds: "120",
  maxConcurrency: "2",
  enabled: true,
};

function formFromProfile(p: LlmProfileView): ProfileFormState {
  return {
    name: p.name,
    provider: (["local", "openai", "anthropic"].includes(p.provider)
      ? p.provider
      : "local") as ProfileFormState["provider"],
    api: (p.api === "anthropic" ? "anthropic" : "openai-compatible") as
      | "openai-compatible"
      | "anthropic",
    endpoint: p.endpoint ?? "",
    model: p.model,
    apiKey: "", // write-only: never echo the stored key back
    timeoutSeconds: String(p.timeout_seconds),
    maxConcurrency: String(p.max_concurrency),
    enabled: p.enabled,
  };
}

function providerLabel(provider: string): string {
  switch (provider) {
    case "local":
      return t("settings.profiles.provider.local");
    case "openai":
      return t("settings.profiles.provider.openai");
    case "anthropic":
      return t("settings.profiles.provider.anthropic");
    default:
      return provider;
  }
}

function CheckResult({ value }: { value: boolean | undefined | null }) {
  if (value === true) {
    return (
      <span className="inline-flex items-center gap-1 text-emerald-300">
        <Icon name="check" className="h-3.5 w-3.5" />
        {t("settings.profiles.testOk")}
      </span>
    );
  }
  if (value === false) {
    return (
      <span className="inline-flex items-center gap-1 text-rose-300">
        <Icon name="x" className="h-3.5 w-3.5" />
        {t("settings.profiles.testNg")}
      </span>
    );
  }
  return (
    <span className="text-slate-400">{t("settings.profiles.testNotRun")}</span>
  );
}

export function ProfilesSection() {
  const { data: profiles, loading, error, reload } = useFetch(
    () => api.listProfiles(),
    [],
  );
  const [editingId, setEditingId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<ProfileFormState>(emptyForm);
  const [busy, setBusy] = useState(false);
  const [sectionError, setSectionError] = useState<string | null>(null);
  const [testConfirm, setTestConfirm] = useState<LlmProfileView | null>(null);
  const [testResults, setTestResults] = useState<
    Record<string, LlmProfileTestResult | "running">
  >({});
  const [deleteConfirm, setDeleteConfirm] = useState<LlmProfileView | null>(
    null,
  );

  const startCreate = () => {
    setCreating(true);
    setEditingId(null);
    setForm(emptyForm);
    setSectionError(null);
  };

  const startEdit = (p: LlmProfileView) => {
    setEditingId(p.id);
    setCreating(false);
    setForm(formFromProfile(p));
    setSectionError(null);
  };

  const closeForm = () => {
    setEditingId(null);
    setCreating(false);
    setForm(emptyForm);
  };

  const editingProfile =
    editingId !== null ? profiles?.find((p) => p.id === editingId) : undefined;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setSectionError(null);
    const body: LlmProfileIn = {
      name: form.name.trim(),
      provider: form.provider,
      api: form.api,
      endpoint: form.endpoint.trim() === "" ? null : form.endpoint.trim(),
      model: form.model.trim(),
      // Write-only: only send a key when the user typed one.
      ...(form.apiKey !== "" ? { api_key: form.apiKey } : {}),
      timeout_seconds: Number(form.timeoutSeconds) || 120,
      max_concurrency: Number(form.maxConcurrency) || 2,
      enabled: form.enabled,
    };
    try {
      if (editingId) await api.updateProfile(editingId, body);
      else await api.createProfile(body);
      closeForm();
      reload();
    } catch (err) {
      setSectionError(
        `${t("settings.profiles.saveFailed")}: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setBusy(false);
    }
  };

  const runTest = async (profile: LlmProfileView) => {
    setTestResults((prev) => ({ ...prev, [profile.id]: "running" }));
    try {
      const result = await api.testProfile(profile.id);
      setTestResults((prev) => ({ ...prev, [profile.id]: result }));
    } catch (e) {
      setTestResults((prev) => ({
        ...prev,
        [profile.id]: {
          error: `${t("settings.profiles.testFailed")}: ${e instanceof Error ? e.message : String(e)}`,
        },
      }));
    }
  };

  const handleDelete = async (profile: LlmProfileView) => {
    setDeleteConfirm(null);
    setSectionError(null);
    try {
      await api.deleteProfile(profile.id);
      if (editingId === profile.id) closeForm();
      reload();
    } catch (e) {
      setSectionError(
        t("common.errorPrefix", {
          message: e instanceof Error ? e.message : String(e),
        }),
      );
    }
  };

  const isPaidProvider = (provider: string) =>
    provider === "openai" || provider === "anthropic";

  return (
    <section
      aria-label={t("settings.profiles.title")}
      className="rounded-2xl bg-slate-900/80 p-5 shadow-xl shadow-black/20 ring-1 ring-white/10 backdrop-blur"
    >
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-base font-semibold text-white">
          {t("settings.profiles.title")}
        </h2>
        <button
          type="button"
          onClick={startCreate}
          className="rounded-lg bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500 shadow-lg shadow-indigo-500/25 px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 focus:outline-none focus:ring-2 focus:ring-indigo-400"
        >
          {t("common.create")}
        </button>
      </div>

      {sectionError && (
        <p role="alert" className="mb-3 text-sm text-rose-300">
          {sectionError}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-rose-300">
          {t("settings.profiles.loadFailed")}: {error}
        </p>
      )}
      {loading && <p className="text-sm text-slate-500">{t("common.loading")}</p>}
      {profiles && profiles.length === 0 && !creating && (
        <p className="text-sm text-slate-500">{t("settings.profiles.empty")}</p>
      )}

      {profiles && profiles.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-white/10 text-xs text-slate-500">
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("settings.profiles.name")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("settings.profiles.provider")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("settings.profiles.model")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("settings.profiles.apiKey")}
                </th>
                <th scope="col" className="py-1.5 pr-3 font-medium">
                  {t("settings.profiles.enabled")}
                </th>
                <th scope="col" className="py-1.5 font-medium">
                  <span className="sr-only">{t("common.edit")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {profiles.map((p) => {
                const result = testResults[p.id];
                return (
                  <tr key={p.id} className="border-b border-white/5 align-top">
                    <td className="py-2 pr-3 font-medium">{p.name}</td>
                    <td className="py-2 pr-3">
                      {providerLabel(p.provider)}
                      <span className="block text-xs text-slate-500">
                        {p.api}
                      </span>
                    </td>
                    <td className="py-2 pr-3">
                      {p.model}
                      {p.endpoint && (
                        <span
                          className="block max-w-[16rem] truncate text-xs text-slate-500"
                          title={p.endpoint}
                        >
                          {p.endpoint}
                        </span>
                      )}
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">
                      {p.has_api_key
                        ? (p.api_key_masked ?? "••••••••")
                        : t("settings.profiles.apiKeyNotSet")}
                    </td>
                    <td className="py-2 pr-3">
                      {p.enabled ? (
                        <span className="inline-flex items-center gap-1 text-emerald-300">
                          <Icon name="check" className="h-3.5 w-3.5" />
                          {t("common.enabled")}
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-slate-500">
                          <Icon name="ban" className="h-3.5 w-3.5" />
                          {t("common.disabled")}
                        </span>
                      )}
                    </td>
                    <td className="py-2">
                      <div className="flex flex-wrap gap-1.5">
                        <button
                          type="button"
                          onClick={() => startEdit(p)}
                          className="rounded-lg border border-white/15 px-2 py-1 text-xs text-slate-300 hover:bg-white/5 focus:outline-none focus:ring-2 focus:ring-indigo-400"
                        >
                          {t("common.edit")}
                        </button>
                        <button
                          type="button"
                          onClick={() => setTestConfirm(p)}
                          disabled={result === "running"}
                          className="rounded-lg border border-indigo-400/40 px-2 py-1 text-xs text-indigo-300 hover:bg-indigo-500/10 focus:outline-none focus:ring-2 focus:ring-indigo-400 disabled:opacity-50"
                        >
                          {result === "running"
                            ? t("settings.profiles.testRunning")
                            : t("settings.profiles.test")}
                        </button>
                        <button
                          type="button"
                          onClick={() => setDeleteConfirm(p)}
                          className="rounded-lg border border-rose-400/40 px-2 py-1 text-xs text-rose-300 hover:bg-rose-500/10 focus:outline-none focus:ring-2 focus:ring-rose-400"
                        >
                          {t("common.delete")}
                        </button>
                      </div>
                      {result && result !== "running" && (
                        <dl className="mt-2 space-y-0.5 rounded-lg border border-white/10 bg-white/5 p-2 text-xs">
                          <div className="flex justify-between gap-2">
                            <dt>{t("settings.profiles.testReachable")}</dt>
                            <dd>
                              <CheckResult value={result.reachable} />
                            </dd>
                          </div>
                          <div className="flex justify-between gap-2">
                            <dt>{t("settings.profiles.testAuthenticated")}</dt>
                            <dd>
                              <CheckResult value={result.authenticated} />
                            </dd>
                          </div>
                          <div className="flex justify-between gap-2">
                            <dt>{t("settings.profiles.testModelAvailable")}</dt>
                            <dd>
                              <CheckResult value={result.model_available} />
                            </dd>
                          </div>
                          <div className="flex justify-between gap-2">
                            <dt>{t("settings.profiles.testGenerationOk")}</dt>
                            <dd>
                              <CheckResult value={result.generation_ok} />
                            </dd>
                          </div>
                          {typeof result.error === "string" && result.error && (
                            <p role="alert" className="text-rose-300">
                              {t("common.error")}: {result.error}
                            </p>
                          )}
                          {typeof result.billing_note === "string" &&
                            result.billing_note && (
                              <p className="text-amber-300">
                                {t("settings.profiles.testBillingNote")}:{" "}
                                {result.billing_note}
                              </p>
                            )}
                        </dl>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {(creating || editingId) && (
        <form
          onSubmit={handleSubmit}
          className="mt-4 rounded-lg border border-white/10 bg-white/5 p-3"
          aria-label={
            editingId
              ? t("settings.profiles.editTitle")
              : t("settings.profiles.createTitle")
          }
        >
          <h3 className="mb-3 text-sm font-semibold text-white">
            {editingId
              ? t("settings.profiles.editTitle")
              : t("settings.profiles.createTitle")}
          </h3>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <label htmlFor="pf-name" className={labelCls}>
                {t("settings.profiles.name")}
              </label>
              <input
                id="pf-name"
                required
                maxLength={100}
                className={inputCls}
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="pf-provider" className={labelCls}>
                {t("settings.profiles.provider")}
              </label>
              <select
                id="pf-provider"
                className={inputCls}
                value={form.provider}
                onChange={(e) =>
                  setForm({
                    ...form,
                    provider: e.target.value as ProfileFormState["provider"],
                  })
                }
              >
                <option value="local">
                  {t("settings.profiles.provider.local")}
                </option>
                <option value="openai">
                  {t("settings.profiles.provider.openai")}
                </option>
                <option value="anthropic">
                  {t("settings.profiles.provider.anthropic")}
                </option>
              </select>
            </div>
            <div>
              <label htmlFor="pf-api" className={labelCls}>
                {t("settings.profiles.api")}
              </label>
              <select
                id="pf-api"
                className={inputCls}
                value={form.api}
                onChange={(e) =>
                  setForm({
                    ...form,
                    api: e.target.value as ProfileFormState["api"],
                  })
                }
              >
                <option value="openai-compatible">openai-compatible</option>
                <option value="anthropic">anthropic</option>
              </select>
            </div>
            <div>
              <label htmlFor="pf-endpoint" className={labelCls}>
                {t("settings.profiles.endpoint")}
              </label>
              <input
                id="pf-endpoint"
                className={inputCls}
                value={form.endpoint}
                onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="pf-model" className={labelCls}>
                {t("settings.profiles.model")}
              </label>
              <input
                id="pf-model"
                required
                maxLength={200}
                className={inputCls}
                value={form.model}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="pf-apikey" className={labelCls}>
                {t("settings.profiles.apiKey")}
              </label>
              <input
                id="pf-apikey"
                type="password"
                autoComplete="new-password"
                className={inputCls}
                value={form.apiKey}
                placeholder={
                  editingProfile?.has_api_key
                    ? (editingProfile.api_key_masked ?? "••••••••")
                    : ""
                }
                onChange={(e) => setForm({ ...form, apiKey: e.target.value })}
                aria-describedby="pf-apikey-help"
              />
              <p id="pf-apikey-help" className="mt-1 text-xs text-slate-500">
                {t("settings.profiles.apiKeyHelp")}
              </p>
            </div>
            <div>
              <label htmlFor="pf-timeout" className={labelCls}>
                {t("settings.profiles.timeout")}
              </label>
              <input
                id="pf-timeout"
                type="number"
                min={5}
                max={3600}
                className={inputCls}
                value={form.timeoutSeconds}
                onChange={(e) =>
                  setForm({ ...form, timeoutSeconds: e.target.value })
                }
              />
            </div>
            <div>
              <label htmlFor="pf-conc" className={labelCls}>
                {t("settings.profiles.maxConcurrency")}
              </label>
              <input
                id="pf-conc"
                type="number"
                min={1}
                max={32}
                className={inputCls}
                value={form.maxConcurrency}
                onChange={(e) =>
                  setForm({ ...form, maxConcurrency: e.target.value })
                }
              />
            </div>
            <div className="flex items-center gap-2 pt-5">
              <input
                id="pf-enabled"
                type="checkbox"
                className="h-4 w-4 rounded-lg border-white/15"
                checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
              />
              <label htmlFor="pf-enabled" className="text-sm text-slate-200">
                {t("settings.profiles.enabled")}
              </label>
            </div>
          </div>
          <div className="mt-3 flex gap-2">
            <button
              type="submit"
              disabled={busy}
              className="rounded-lg bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500 shadow-lg shadow-indigo-500/25 px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 focus:outline-none focus:ring-2 focus:ring-indigo-400 disabled:opacity-50"
            >
              {t("common.save")}
            </button>
            <button
              type="button"
              onClick={closeForm}
              className="rounded-lg border border-white/15 px-3 py-1.5 text-sm text-slate-300 hover:bg-white/10 focus:outline-none focus:ring-2 focus:ring-indigo-400"
            >
              {t("common.cancel")}
            </button>
          </div>
        </form>
      )}

      {/* Billing warning BEFORE the test runs (paid providers). */}
      <ConfirmDialog
        open={testConfirm !== null}
        title={t("settings.profiles.testTitle")}
        message={
          testConfirm && isPaidProvider(testConfirm.provider)
            ? t("settings.profiles.testBillingWarning", {
                provider: providerLabel(testConfirm.provider),
              })
            : t("settings.profiles.testLocalNote")
        }
        onConfirm={() => {
          if (testConfirm) runTest(testConfirm);
          setTestConfirm(null);
        }}
        onCancel={() => setTestConfirm(null)}
      />

      <ConfirmDialog
        open={deleteConfirm !== null}
        title={t("common.delete")}
        message={
          deleteConfirm
            ? t("settings.profiles.deleteConfirm", { name: deleteConfirm.name })
            : ""
        }
        confirmLabel={t("common.delete")}
        onConfirm={() => {
          if (deleteConfirm) handleDelete(deleteConfirm);
        }}
        onCancel={() => setDeleteConfirm(null)}
      />
    </section>
  );
}
