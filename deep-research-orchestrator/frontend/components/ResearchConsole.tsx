"use client";

import { useCallback, useMemo } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { JobView } from "@/lib/api-types";
import { api } from "@/lib/api";
import { useFetch } from "@/lib/useFetch";
import { JobForm } from "./JobForm";
import { JobList } from "./JobList";
import { JobLiveView } from "./JobLiveView";

/**
 * Research Console — the single main page. The selected job lives in the
 * `?job=` query param so past jobs can be reopened (SSE replay + snapshot
 * resume live updates automatically).
 */
export function ResearchConsole() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const jobId = searchParams.get("job");

  const engines = useFetch(() => api.listEngines(), []);
  const jobs = useFetch(() => api.listJobs(20), []);

  const engineNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const e of engines.data ?? []) map[e.engine_id] = e.display_name;
    return map;
  }, [engines.data]);

  const openJob = useCallback(
    (id: string) => {
      router.push(`/?job=${encodeURIComponent(id)}`);
    },
    [router],
  );

  const handleCreated = useCallback(
    (job: JobView) => {
      jobs.reload();
      openJob(job.id);
    },
    [jobs, openJob],
  );

  const handleBack = useCallback(() => {
    jobs.reload();
    router.push("/");
  }, [jobs, router]);

  if (jobId) {
    return (
      <JobLiveView jobId={jobId} engineNames={engineNames} onBack={handleBack} />
    );
  }

  return (
    <div className="space-y-4">
      <JobForm
        engines={engines.data}
        enginesError={engines.error}
        onCreated={handleCreated}
      />
      <JobList
        jobs={jobs.data}
        error={jobs.error}
        loading={jobs.loading}
        onOpen={openJob}
        onRefresh={jobs.reload}
      />
    </div>
  );
}
