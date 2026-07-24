"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t, type MessageKey } from "@/lib/i18n";

const ROLE_KEYS: { role: string; labelKey: MessageKey }[] = [
  { role: "research", labelKey: "settings.roles.research" },
  { role: "summarization", labelKey: "settings.roles.summarization" },
  { role: "normalization", labelKey: "settings.roles.normalization" },
  { role: "synthesis", labelKey: "settings.roles.synthesis" },
];

export function RolesSection() {
  const roles = useFetch(() => api.getRoles(), []);
  const profiles = useFetch(() => api.listProfiles(), []);
  const [assignments, setAssignments] = useState<Record<string, string | null>>(
    {},
  );
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (roles.data) setAssignments(roles.data);
  }, [roles.data]);

  const handleSave = async () => {
    setBusy(true);
    setMessage(null);
    setErrorMsg(null);
    try {
      const saved = await api.putRoles(assignments);
      setAssignments(saved);
      setMessage(t("common.saved"));
    } catch (e) {
      setErrorMsg(
        `${t("settings.roles.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      aria-label={t("settings.roles.title")}
      className="rounded-2xl bg-slate-900/80 p-5 shadow-xl shadow-black/20 ring-1 ring-white/10 backdrop-blur"
    >
      <h2 className="mb-1 text-base font-semibold text-white">
        {t("settings.roles.title")}
      </h2>
      <p className="mb-3 text-xs text-slate-500">{t("settings.roles.help")}</p>

      {roles.error && (
        <p role="alert" className="text-sm text-rose-300">
          {t("settings.roles.loadFailed")}: {roles.error}
        </p>
      )}
      {roles.loading && (
        <p className="text-sm text-slate-500">{t("common.loading")}</p>
      )}

      {roles.data && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {ROLE_KEYS.map(({ role, labelKey }) => (
            <div key={role}>
              <label
                htmlFor={`role-${role}`}
                className="mb-1 block text-xs font-medium text-slate-400"
              >
                {t(labelKey)}
              </label>
              <select
                id={`role-${role}`}
                className="w-full rounded-lg border border-white/15 bg-slate-950/60 px-2 py-1.5 text-sm focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-400"
                value={assignments[role] ?? ""}
                onChange={(e) =>
                  setAssignments((prev) => ({
                    ...prev,
                    [role]: e.target.value === "" ? null : e.target.value,
                  }))
                }
              >
                <option value="">{t("settings.roles.unassigned")}</option>
                {(profiles.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.model})
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      )}

      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          onClick={handleSave}
          disabled={busy || !roles.data}
          className="rounded-lg bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500 shadow-lg shadow-indigo-500/25 px-3 py-1.5 text-sm font-medium text-white hover:brightness-110 focus:outline-none focus:ring-2 focus:ring-indigo-400 disabled:opacity-50"
        >
          {t("common.save")}
        </button>
        {message && (
          <p role="status" className="text-sm text-emerald-300">
            {message}
          </p>
        )}
        {errorMsg && (
          <p role="alert" className="text-sm text-rose-300">
            {errorMsg}
          </p>
        )}
      </div>
    </section>
  );
}
