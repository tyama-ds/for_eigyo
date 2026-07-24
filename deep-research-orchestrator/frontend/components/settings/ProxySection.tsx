"use client";

import { useEffect, useState } from "react";
import type { ProxyConfigIn, ProxyMode, ProxyTestResult } from "@/lib/api-types";
import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { Icon } from "../Icon";

const inputCls =
  "w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500";
const labelCls = "block text-xs font-medium text-slate-600 mb-1";

function HasIndicator({ has }: { has: boolean }) {
  return has ? (
    <span className="ml-2 inline-flex items-center gap-1 text-xs text-emerald-700">
      <Icon name="check" className="h-3 w-3" />
      {t("common.set")}
    </span>
  ) : (
    <span className="ml-2 text-xs text-slate-400">{t("common.notSet")}</span>
  );
}

function readBool(obj: unknown, keys: string[]): boolean | null {
  if (!obj || typeof obj !== "object") return null;
  const rec = obj as Record<string, unknown>;
  for (const key of keys) {
    if (typeof rec[key] === "boolean") return rec[key] as boolean;
  }
  return null;
}

export function ProxySection() {
  const proxy = useFetch(() => api.getProxy(), []);
  const globalConfig =
    proxy.data?.find((c) => c.scope === "global") ?? proxy.data?.[0] ?? null;

  const [mode, setMode] = useState<ProxyMode>("off");
  const [httpProxy, setHttpProxy] = useState("");
  const [httpsProxy, setHttpsProxy] = useState("");
  const [allProxy, setAllProxy] = useState("");
  const [noProxyText, setNoProxyText] = useState("");
  const [caBundlePath, setCaBundlePath] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const [testResult, setTestResult] = useState<ProxyTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  useEffect(() => {
    if (globalConfig) {
      const m = globalConfig.mode;
      setMode(m === "inherit" || m === "explicit" ? m : "off");
      setNoProxyText(globalConfig.no_proxy.join("\n"));
      setCaBundlePath(globalConfig.ca_bundle_path ?? "");
      // Proxy URLs are write-only: never prefilled.
      setHttpProxy("");
      setHttpsProxy("");
      setAllProxy("");
    }
  }, [globalConfig]);

  const handleSave = async () => {
    setBusy(true);
    setMessage(null);
    setErrorMsg(null);
    const body: ProxyConfigIn = {
      scope: "global",
      mode,
      // Write-only URLs: only send when the user typed a value.
      ...(httpProxy.trim() !== "" ? { http_proxy: httpProxy.trim() } : {}),
      ...(httpsProxy.trim() !== "" ? { https_proxy: httpsProxy.trim() } : {}),
      ...(allProxy.trim() !== "" ? { all_proxy: allProxy.trim() } : {}),
      no_proxy: noProxyText
        .split("\n")
        .map((s) => s.trim())
        .filter((s) => s !== ""),
      ca_bundle_path: caBundlePath.trim() === "" ? null : caBundlePath.trim(),
    };
    try {
      await api.putProxy(body);
      setMessage(t("common.saved"));
      setHttpProxy("");
      setHttpsProxy("");
      setAllProxy("");
      proxy.reload();
    } catch (e) {
      setErrorMsg(
        `${t("settings.proxy.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setBusy(false);
    }
  };

  const handleTest = async () => {
    setTestBusy(true);
    setTestResult(null);
    setTestError(null);
    try {
      const result = await api.testProxy({ scope: "global" });
      setTestResult(result);
    } catch (e) {
      setTestError(
        `${t("settings.proxy.testFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setTestBusy(false);
    }
  };

  const external = testResult?.external;
  const internal = testResult?.internal;
  const viaProxy =
    readBool(external, ["via_proxy"]) ?? readBool(testResult, ["via_proxy"]);
  const bypassed =
    readBool(internal, ["bypassed"]) ??
    readBool(testResult, ["internal_bypassed", "bypassed"]);

  return (
    <section
      aria-label={t("settings.proxy.title")}
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <h2 className="mb-1 text-base font-semibold text-slate-900">
        {t("settings.proxy.title")}
      </h2>
      <p className="mb-3 text-xs text-slate-500">
        {t("settings.proxy.scopeGlobal")}
      </p>

      {proxy.error && (
        <p role="alert" className="text-sm text-rose-700">
          {t("settings.proxy.loadFailed")}: {proxy.error}
        </p>
      )}
      {proxy.loading && (
        <p className="text-sm text-slate-500">{t("common.loading")}</p>
      )}

      {globalConfig && (
        <div className="space-y-3">
          <fieldset>
            <legend className={labelCls}>{t("settings.proxy.mode")}</legend>
            <div className="flex flex-wrap gap-4">
              {(["off", "inherit", "explicit"] as const).map((m) => (
                <label key={m} className="flex items-center gap-1.5 text-sm">
                  <input
                    type="radio"
                    name="proxy-mode"
                    value={m}
                    checked={mode === m}
                    onChange={() => setMode(m)}
                    className="h-4 w-4"
                  />
                  {t(`settings.proxy.mode.${m}`)}
                </label>
              ))}
            </div>
          </fieldset>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div>
              <label htmlFor="px-http" className={labelCls}>
                {t("settings.proxy.httpProxy")}
                <HasIndicator has={globalConfig.has_http_proxy} />
              </label>
              <input
                id="px-http"
                type="password"
                autoComplete="off"
                className={inputCls}
                value={httpProxy}
                onChange={(e) => setHttpProxy(e.target.value)}
                aria-describedby="px-writeonly-help"
              />
            </div>
            <div>
              <label htmlFor="px-https" className={labelCls}>
                {t("settings.proxy.httpsProxy")}
                <HasIndicator has={globalConfig.has_https_proxy} />
              </label>
              <input
                id="px-https"
                type="password"
                autoComplete="off"
                className={inputCls}
                value={httpsProxy}
                onChange={(e) => setHttpsProxy(e.target.value)}
                aria-describedby="px-writeonly-help"
              />
            </div>
            <div>
              <label htmlFor="px-all" className={labelCls}>
                {t("settings.proxy.allProxy")}
                <HasIndicator has={globalConfig.has_all_proxy} />
              </label>
              <input
                id="px-all"
                type="password"
                autoComplete="off"
                className={inputCls}
                value={allProxy}
                onChange={(e) => setAllProxy(e.target.value)}
                aria-describedby="px-writeonly-help"
              />
            </div>
          </div>
          <p id="px-writeonly-help" className="text-xs text-slate-500">
            {t("settings.proxy.writeOnlyHelp")}
          </p>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label htmlFor="px-noproxy" className={labelCls}>
                {t("settings.proxy.noProxy")}
              </label>
              <textarea
                id="px-noproxy"
                rows={4}
                className={inputCls}
                value={noProxyText}
                onChange={(e) => setNoProxyText(e.target.value)}
                aria-describedby="px-noproxy-help"
              />
              <p id="px-noproxy-help" className="mt-1 text-xs text-slate-500">
                {t("settings.proxy.noProxyHelp")}
              </p>
            </div>
            <div>
              <label htmlFor="px-ca" className={labelCls}>
                {t("settings.proxy.caBundlePath")}
              </label>
              <input
                id="px-ca"
                className={inputCls}
                value={caBundlePath}
                onChange={(e) => setCaBundlePath(e.target.value)}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={handleSave}
              disabled={busy}
              className="rounded bg-sky-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-800 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
            >
              {t("common.save")}
            </button>
            <button
              type="button"
              onClick={handleTest}
              disabled={testBusy}
              className="rounded border border-sky-300 px-3 py-1.5 text-sm text-sky-800 hover:bg-sky-50 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
            >
              {testBusy
                ? t("settings.proxy.testRunning")
                : t("settings.proxy.test")}
            </button>
            {message && (
              <p role="status" className="text-sm text-emerald-700">
                {message}
              </p>
            )}
            {errorMsg && (
              <p role="alert" className="text-sm text-rose-700">
                {errorMsg}
              </p>
            )}
            {testError && (
              <p role="alert" className="text-sm text-rose-700">
                {testError}
              </p>
            )}
          </div>

          {testResult && (
            <div className="rounded border border-slate-200 bg-slate-50 p-3 text-sm">
              <dl className="space-y-1">
                <div className="flex items-center gap-2">
                  <dt className="text-xs text-slate-500">
                    {t("settings.proxy.testExternal")}:
                  </dt>
                  <dd>
                    {viaProxy === true ? (
                      <span className="inline-flex items-center gap-1 text-emerald-700">
                        <Icon name="check" className="h-3.5 w-3.5" />
                        {t("settings.proxy.testViaProxy")}
                      </span>
                    ) : viaProxy === false ? (
                      <span className="inline-flex items-center gap-1 text-amber-800">
                        <Icon name="warn" className="h-3.5 w-3.5" />
                        {t("settings.proxy.testNotViaProxy")}
                      </span>
                    ) : (
                      <span className="text-slate-400">
                        {t("common.unknown")}
                      </span>
                    )}
                  </dd>
                </div>
                <div className="flex items-center gap-2">
                  <dt className="text-xs text-slate-500">
                    {t("settings.proxy.testInternal")}:
                  </dt>
                  <dd>
                    {bypassed === true ? (
                      <span className="inline-flex items-center gap-1 text-emerald-700">
                        <Icon name="check" className="h-3.5 w-3.5" />
                        {t("settings.proxy.testBypassed")}
                      </span>
                    ) : bypassed === false ? (
                      <span className="inline-flex items-center gap-1 text-amber-800">
                        <Icon name="warn" className="h-3.5 w-3.5" />
                        {t("settings.proxy.testNotBypassed")}
                      </span>
                    ) : (
                      <span className="text-slate-400">
                        {t("common.unknown")}
                      </span>
                    )}
                  </dd>
                </div>
              </dl>
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-slate-600">
                  {t("settings.proxy.testRawResult")}
                </summary>
                <pre className="mt-1 max-h-48 overflow-auto rounded bg-slate-900 p-2 text-xs text-slate-100">
                  {JSON.stringify(testResult, null, 2)}
                </pre>
              </details>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
