import { Suspense } from "react";
import { ResearchConsole } from "@/components/ResearchConsole";
import { t } from "@/lib/i18n";

export default function Page() {
  return (
    <Suspense
      fallback={<p className="text-sm text-slate-500">{t("common.loading")}</p>}
    >
      <ResearchConsole />
    </Suspense>
  );
}
