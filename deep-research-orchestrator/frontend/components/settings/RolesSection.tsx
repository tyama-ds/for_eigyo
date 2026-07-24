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
      className="rounded-lg border border-slate-200 bg-white p-4"
    >
      <h2 className="mb-1 text-base font-semibold text-slate-900">
        {t("settings.roles.title")}
      </h2>
      <p className="mb-3 text-xs text-slate-500">{t("settings.roles.help")}</p>

      {roles.error && (
        <p role="alert" className="text-sm text-rose-700">
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
                className="mb-1 block text-xs font-medium text-slate-600"
              >
                {t(labelKey)}
              </label>
              <select
                id={`role-${role}`}
                className="w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
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
          className="rounded bg-sky-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-800 focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
        >
          {t("common.save")}
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
      </div>
    </section>
  );
}
