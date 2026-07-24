/**
 * Hand-written TypeScript types for the DRO backend API.
 * Kept faithful to docs/openapi.json (OpenAPI 3.1).
 */

// ---------------------------------------------------------------------------
// Engines
// ---------------------------------------------------------------------------

export type EngineAvailability =
  | "available"
  | "experimental"
  | "unsupported"
  | "disabled"
  | "unhealthy";

export interface EngineView {
  engine_id: string;
  display_name: string;
  enabled: boolean;
  availability: EngineAvailability | string;
  unavailable_reason: string | null;
  max_concurrency: number;
  capabilities: Record<string, unknown> | null;
  healthy: boolean | null;
}

// ---------------------------------------------------------------------------
// Jobs / Runs
// ---------------------------------------------------------------------------

export interface CreateJobRequest {
  topic: string;
  objective?: string | null;
  instructions?: string | null;
  language?: string;
  engines: string[];
  max_time_seconds?: number | null;
  max_searches?: number | null;
  max_cost_usd?: number | null;
  auto_synthesize?: boolean;
  input_urls?: string[];
  documents?: Record<string, unknown>[];
  engine_options?: Record<string, Record<string, unknown>>;
  idempotency_key?: string | null;
}

export interface RunView {
  id: string;
  engine_id: string;
  status: string;
  stage: string | null;
  attempt: number;
  max_attempts: number;
  error: string | null;
  warnings: unknown[];
  metrics: Record<string, unknown>;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  elapsed_seconds: number | null;
}

export interface JobView {
  id: string;
  status: string;
  topic: string;
  objective: string | null;
  instructions: string | null;
  language: string;
  options: Record<string, unknown>;
  warnings: unknown[];
  error: string | null;
  cancel_requested: boolean;
  created_at: string;
  finished_at: string | null;
  runs: RunView[];
}

// ---------------------------------------------------------------------------
// Egress preview
// ---------------------------------------------------------------------------

export interface EgressDestination {
  kind?: string;
  name?: string;
  host?: string;
  purpose?: string;
  [key: string]: unknown;
}

export interface EgressPreview {
  destinations: EgressDestination[];
}

// ---------------------------------------------------------------------------
// Results / Sources / Claims / Compare / Synthesis
// ---------------------------------------------------------------------------

export interface NormalizedResultView {
  run_id: string;
  engine_id: string;
  summary: string | null;
  report_markdown: string | null;
  metrics: Record<string, unknown>;
  warnings: unknown[];
  raw_artifact_id: string | null;
}

export interface SourceView {
  id: string;
  run_id: string;
  engine_id?: string | null;
  url: string;
  canonical_url: string;
  title: string | null;
  fetched_at: string | null;
  excerpt: string | null;
}

export interface EvidenceView {
  id: string;
  source_id: string;
  url: string | null;
  excerpt: string | null;
  locator: string | null;
  stance: string;
  verification: string;
}

export interface ClaimView {
  id: string;
  run_id: string;
  engine_id: string | null;
  text: string;
  meta: Record<string, unknown>;
  evidence: EvidenceView[];
}

/**
 * /compare is `additionalProperties: true` in the schema; these are the
 * documented keys. Individual items are engine-produced and loosely shaped,
 * so components must read them defensively.
 */
export interface CompareEntry {
  engine_id?: string;
  run_id?: string;
  claim?: string;
  text?: string;
  value?: unknown;
  [key: string]: unknown;
}

export interface CompareFinding {
  id?: string;
  key?: string;
  topic?: string;
  text?: string;
  statement?: string;
  description?: string;
  engines?: unknown;
  entries?: CompareEntry[];
  /** 正式形状: エンジン別のclaim (engine_id / text / value / evidence_count) */
  claims?: Record<string, unknown>[];
  values?: Record<string, unknown> | unknown[];
  [key: string]: unknown;
}

export interface CompareView {
  agreements?: CompareFinding[];
  partial_findings?: CompareFinding[];
  conflicts?: CompareFinding[];
  unsupported_claims?: CompareFinding[];
  coverage?: Record<string, unknown> | CompareFinding[];
  open_questions?: unknown[];
  [key: string]: unknown;
}

export interface SynthesisCitation {
  sid?: string;
  url?: string;
  title?: string;
  excerpt?: string;
  engines?: string[];
  fetched_at?: string | null;
  [key: string]: unknown;
}

export interface SynthesisView {
  status: string;
  attempt: number;
  report_markdown: string | null;
  sections: Record<string, unknown>;
  citations: SynthesisCitation[];
  llm_profile_id: string | null;
  error: string | null;
  warnings: unknown[];
}

// ---------------------------------------------------------------------------
// Events (SSE)
// ---------------------------------------------------------------------------

export type JobEventType =
  | "run_status"
  | "engine_stage"
  | "engine_search"
  | "engine_source_found"
  | "engine_token_usage"
  | "engine_cost"
  | "job_status"
  | "compare_ready"
  | "synthesis_status"
  | "retry_scheduled"
  | "cancel_requested"
  | "stream_end"
  | string;

export interface JobEvent {
  seq: number;
  type: JobEventType;
  run_id: string | null;
  engine_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export type LlmProvider = "local" | "openai" | "anthropic";
export type LlmApi = "openai-compatible" | "anthropic";

export interface LlmProfileIn {
  name: string;
  provider: LlmProvider;
  api?: LlmApi;
  endpoint?: string | null;
  model: string;
  api_key?: string | null;
  timeout_seconds?: number;
  max_concurrency?: number;
  enabled?: boolean;
}

export interface LlmProfileView {
  id: string;
  name: string;
  provider: string;
  api: string;
  endpoint: string | null;
  model: string;
  has_api_key: boolean;
  api_key_masked: string | null;
  timeout_seconds: number;
  max_concurrency: number;
  enabled: boolean;
}

export interface LlmProfileTestResult {
  reachable?: boolean;
  authenticated?: boolean;
  model_available?: boolean;
  generation_ok?: boolean;
  error?: string | null;
  billing_note?: string | null;
  [key: string]: unknown;
}

export type RoleName =
  | "research"
  | "summarization"
  | "normalization"
  | "synthesis";

export type RoleAssignments = Record<string, string | null>;

export interface RoleAssignmentIn {
  assignments: Record<string, string | null>;
}

export type ProxyMode = "off" | "inherit" | "explicit";

export interface ProxyConfigIn {
  scope?: string;
  mode: ProxyMode;
  http_proxy?: string | null;
  https_proxy?: string | null;
  all_proxy?: string | null;
  no_proxy?: string[];
  ca_bundle_path?: string | null;
}

export interface ProxyConfigView {
  scope: string;
  mode: string;
  has_http_proxy: boolean;
  has_https_proxy: boolean;
  has_all_proxy: boolean;
  no_proxy: string[];
  ca_bundle_path: string | null;
}

export interface ProxyTestIn {
  scope?: string;
  external_url?: string;
  internal_url?: string | null;
}

export type ProxyTestResult = Record<string, unknown>;

export interface AllowlistEntry {
  id?: string;
  host?: string;
  endpoint?: string;
  provider?: string;
  created_at?: string;
  [key: string]: unknown;
}
