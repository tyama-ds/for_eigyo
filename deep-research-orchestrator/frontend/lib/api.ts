import type {
  AllowlistEntry,
  ClaimView,
  CompareView,
  CreateJobRequest,
  EgressPreview,
  EngineView,
  JobView,
  LlmProfileIn,
  LlmProfileTestResult,
  LlmProfileView,
  NormalizedResultView,
  ProxyConfigIn,
  ProxyConfigView,
  ProxyTestIn,
  ProxyTestResult,
  RoleAssignments,
  SourceView,
  SynthesisView,
} from "./api-types";

export const API_BASE: string =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8800";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch (e) {
    throw new ApiError(0, e instanceof Error ? e.message : String(e));
  }
  if (!res.ok) {
    let detail: unknown = null;
    let message = `${res.status} ${res.statusText}`;
    try {
      detail = await res.json();
      const d = detail as { detail?: unknown };
      if (typeof d?.detail === "string") message = d.detail;
      else if (Array.isArray(d?.detail) && d.detail.length > 0) {
        const first = d.detail[0] as { msg?: string };
        if (typeof first?.msg === "string") message = first.msg;
      }
    } catch {
      // non-JSON error body
    }
    throw new ApiError(res.status, message, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  // Engines / egress
  listEngines: () => request<EngineView[]>("/api/engines"),
  egressPreview: (engines: string[]) =>
    request<EgressPreview>(
      `/api/egress-preview?engines=${encodeURIComponent(engines.join(","))}`,
    ),

  // Jobs
  createJob: (body: CreateJobRequest, idempotencyKey: string) =>
    request<JobView>("/api/jobs", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    }),
  listJobs: (limit = 20) => request<JobView[]>(`/api/jobs?limit=${limit}`),
  getJob: (jobId: string) =>
    request<JobView>(`/api/jobs/${encodeURIComponent(jobId)}`),
  cancelJob: (jobId: string) =>
    request<JobView>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    }),
  cancelRun: (jobId: string, runId: string) =>
    request<JobView>(
      `/api/jobs/${encodeURIComponent(jobId)}/runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" },
    ),

  // Results
  getResults: (jobId: string) =>
    request<NormalizedResultView[]>(
      `/api/jobs/${encodeURIComponent(jobId)}/results`,
    ),
  getSources: (jobId: string) =>
    request<SourceView[]>(`/api/jobs/${encodeURIComponent(jobId)}/sources`),
  getClaims: (jobId: string) =>
    request<ClaimView[]>(`/api/jobs/${encodeURIComponent(jobId)}/claims`),
  getCompare: (jobId: string) =>
    request<CompareView>(`/api/jobs/${encodeURIComponent(jobId)}/compare`),
  getSynthesis: (jobId: string) =>
    request<SynthesisView>(`/api/jobs/${encodeURIComponent(jobId)}/synthesis`),
  retrySynthesis: (jobId: string, profileId?: string | null) =>
    request<Record<string, string>>(
      `/api/jobs/${encodeURIComponent(jobId)}/synthesis/retry${
        profileId ? `?profile_id=${encodeURIComponent(profileId)}` : ""
      }`,
      { method: "POST" },
    ),

  // URLs (no fetch)
  eventsUrl: (jobId: string) =>
    `${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/events`,
  artifactUrl: (artifactId: string) =>
    `${API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}`,
  exportUrl: (jobId: string, format: "markdown" | "json") =>
    `${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/export?format=${format}`,

  // Settings — LLM profiles
  listProfiles: () => request<LlmProfileView[]>("/api/settings/llm-profiles"),
  createProfile: (body: LlmProfileIn) =>
    request<LlmProfileView>("/api/settings/llm-profiles", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateProfile: (profileId: string, body: LlmProfileIn) =>
    request<LlmProfileView>(
      `/api/settings/llm-profiles/${encodeURIComponent(profileId)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  deleteProfile: (profileId: string) =>
    request<void>(
      `/api/settings/llm-profiles/${encodeURIComponent(profileId)}`,
      { method: "DELETE" },
    ),
  testProfile: (profileId: string) =>
    request<LlmProfileTestResult>(
      `/api/settings/llm-profiles/${encodeURIComponent(profileId)}/test`,
      { method: "POST" },
    ),

  // Settings — roles
  getRoles: () => request<RoleAssignments>("/api/settings/roles"),
  putRoles: (assignments: RoleAssignments) =>
    request<RoleAssignments>("/api/settings/roles", {
      method: "PUT",
      body: JSON.stringify({ assignments }),
    }),

  // Settings — proxy
  getProxy: () => request<ProxyConfigView[]>("/api/settings/proxy"),
  putProxy: (body: ProxyConfigIn) =>
    request<ProxyConfigView>("/api/settings/proxy", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  testProxy: (body: ProxyTestIn) =>
    request<ProxyTestResult>("/api/settings/proxy/test", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Settings — search / allowlist
  getSearchSettings: () =>
    request<Record<string, unknown>>("/api/settings/search"),
  getAllowlist: () =>
    request<AllowlistEntry[]>("/api/settings/llm-endpoint-allowlist"),
  deleteAllowlistEntry: (entryId: string) =>
    request<void>(
      `/api/settings/llm-endpoint-allowlist/${encodeURIComponent(entryId)}`,
      { method: "DELETE" },
    ),
};

/** Download the export via blob so the filename is controlled client-side. */
export async function downloadExport(
  jobId: string,
  format: "markdown" | "json",
): Promise<void> {
  const res = await fetch(api.exportUrl(jobId, format));
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `dro-job-${jobId}.${format === "markdown" ? "md" : "json"}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
