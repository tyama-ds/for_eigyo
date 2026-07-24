"use client";

import { useState } from "react";
import type { AllowlistEntry } from "@/lib/api-types";
import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { ConfirmDialog } from "../ConfirmDialog";

function entryLabel(entry: AllowlistEntry): string {
  for (const key of ["host", "endpoint", "url", "name"]) {
    const v = entry[key];
    if (typeof v === "string" && v !== "") return v;
  }
  try {
    return JSON.stringify(entry);
  } catch {
    return String(entry);
  }
}

export function AllowlistSection() {
  const { data, loading, error, reload } = useFetch(
    () => api.getAllowlist(),
    [],
  );
  const [deleteTarget, setDeleteTarget] = useState<AllowlistEntry | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const handleDelete = async (entry: AllowlistEntry) => {
    setDeleteTarget(null);
    setErrorMsg(null);
    const id = typeof entry.id === "string" ? entry.id : null;
    if (!id) return;
    try {
      await api.deleteAllowlistEntry(id);
      reload();
    } catch (e) {
      setErrorMsg(
        `${t("settings.allowlist.deleteFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  };

  return (
    <section
      aria-label={t("settings.allowlist.title")}
      className="rounded-2xl bg-slate-900/80 p-5 shadow-xl shadow-black/20 ring-1 ring-white/10 backdrop-blur"
    >
      <h2 className="mb-3 text-base font-semibold text-white">
        {t("settings.allowlist.title")}
      </h2>

      {errorMsg && (
        <p role="alert" className="mb-2 text-sm text-rose-300">
          {errorMsg}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-rose-300">
          {t("settings.allowlist.loadFailed")}: {error}
        </p>
      )}
      {loading && <p className="text-sm text-slate-500">{t("common.loading")}</p>}
      {data && data.length === 0 && (
        <p className="text-sm text-slate-500">{t("settings.allowlist.empty")}</p>
      )}

      {data && data.length > 0 && (
        <ul className="divide-y divide-white/5">
          {data.map((entry, i) => (
            <li
              key={typeof entry.id === "string" ? entry.id : i}
              className="flex items-center justify-between gap-3 py-2"
            >
              <div className="min-w-0">
                <p className="truncate font-mono text-sm text-slate-200">
                  {entryLabel(entry)}
                </p>
                {typeof entry.provider === "string" && (
                  <p className="text-xs text-slate-500">{entry.provider}</p>
                )}
              </div>
              {typeof entry.id === "string" && (
                <button
                  type="button"
                  onClick={() => setDeleteTarget(entry)}
                  className="shrink-0 rounded-lg border border-rose-400/40 px-2 py-1 text-xs text-rose-300 hover:bg-rose-500/10 focus:outline-none focus:ring-2 focus:ring-rose-400"
                >
                  {t("common.delete")}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title={t("common.delete")}
        message={t("settings.allowlist.deleteConfirm")}
        confirmLabel={t("common.delete")}
        onConfirm={() => {
          if (deleteTarget) handleDelete(deleteTarget);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </section>
  );
}
