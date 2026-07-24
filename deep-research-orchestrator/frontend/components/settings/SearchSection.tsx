"use client";

import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";

function valueText(v: unknown): string {
  if (v === null || v === undefined) return t("common.notSet");
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export function SearchSection() {
  const { data, loading, error } = useFetch(() => api.getSearchSettings(), []);

  return (
    <section
      aria-label={t("settings.search.title")}
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <h2 className="mb-1 text-base font-semibold text-slate-900">
        {t("settings.search.title")}
      </h2>
      <p className="mb-3 text-xs text-slate-500">{t("settings.search.note")}</p>

      {error && (
        <p role="alert" className="text-sm text-rose-700">
          {t("settings.search.loadFailed")}: {error}
        </p>
      )}
      {loading && <p className="text-sm text-slate-500">{t("common.loading")}</p>}

      {data && (
        <dl className="grid grid-cols-1 gap-x-8 gap-y-1 text-sm sm:grid-cols-2">
          {Object.entries(data).map(([key, value]) => (
            <div key={key} className="flex gap-2">
              <dt className="shrink-0 font-mono text-xs text-slate-500">
                {key}:
              </dt>
              <dd className="break-all text-slate-800">{valueText(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
