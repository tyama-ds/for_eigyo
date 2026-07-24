"use client";

import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { t } from "@/lib/i18n";
import { FindingTable, findingText } from "./CompareShared";

export function CompareTab({ jobId }: { jobId: string }) {
  const { data, loading, error, reload } = useFetch(
    () => api.getCompare(jobId),
    [jobId],
  );

  if (loading) return <p className="text-sm text-slate-500">{t("common.loading")}</p>;
  if (error) {
    return (
      <div role="alert" className="text-sm text-rose-700">
        <p>
          {t("compare.loadFailed")}: {error}
        </p>
        <button
          type="button"
          onClick={reload}
          className="mt-2 rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
        >
          {t("common.reload")}
        </button>
      </div>
    );
  }
  if (!data) return <p className="text-sm text-slate-500">{t("compare.notReady")}</p>;

  const coverage = data.coverage;
  const openQuestions = Array.isArray(data.open_questions)
    ? data.open_questions
    : [];

  return (
    <div className="space-y-6">
      <section aria-label={t("compare.agreements")}>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">
          {t("compare.agreements")}
        </h3>
        <FindingTable
          items={data.agreements}
          emptyText={t("compare.emptySection")}
        />
      </section>

      <section aria-label={t("compare.partialFindings")}>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">
          {t("compare.partialFindings")}
        </h3>
        <FindingTable
          items={data.partial_findings}
          emptyText={t("compare.emptySection")}
        />
      </section>

      <section aria-label={t("compare.conflicts")}>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">
          {t("compare.conflicts")}
        </h3>
        <FindingTable
          items={data.conflicts}
          emptyText={t("compare.emptySection")}
        />
      </section>

      <section aria-label={t("compare.unsupportedClaims")}>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">
          {t("compare.unsupportedClaims")}
        </h3>
        <FindingTable
          items={data.unsupported_claims}
          emptyText={t("compare.emptySection")}
        />
      </section>

      <section aria-label={t("compare.coverage")}>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">
          {t("compare.coverage")}
        </h3>
        {!coverage ||
        (Array.isArray(coverage) && coverage.length === 0) ||
        (typeof coverage === "object" &&
          !Array.isArray(coverage) &&
          Object.keys(coverage).length === 0) ? (
          <p className="text-sm text-slate-500">{t("compare.emptySection")}</p>
        ) : Array.isArray(coverage) ? (
          <FindingTable items={coverage} emptyText={t("compare.emptySection")} />
        ) : (
          <dl className="space-y-1 text-sm">
            {Object.entries(coverage).map(([key, value]) => (
              <div key={key} className="flex gap-2">
                <dt className="font-medium text-slate-700">{key}:</dt>
                <dd className="text-slate-600">{findingText(value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <section aria-label={t("compare.openQuestions")}>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">
          {t("compare.openQuestions")}
        </h3>
        {openQuestions.length === 0 ? (
          <p className="text-sm text-slate-500">{t("compare.emptySection")}</p>
        ) : (
          <ul className="list-disc space-y-1 pl-6 text-sm">
            {openQuestions.map((q, i) => (
              <li key={i}>{findingText(q)}</li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
