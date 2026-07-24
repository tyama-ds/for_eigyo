/**
 * Reducer for live job state driven by the job snapshot (GET /api/jobs/{id})
 * plus the SSE event stream (GET /api/jobs/{id}/events).
 *
 * Rules:
 * - Events are deduplicated by `seq` (the server replays on reconnect).
 * - Status-like fields are guarded against out-of-order delivery: an event
 *   with a lower seq than the last applied status event for the same run
 *   never overwrites the newer status.
 * - Metrics that were never reported stay `null` (rendered as 不明), never 0.
 */

import type { JobEvent, JobView, RunView } from "./api-types";
import { isFiniteNumber, readMetricNumber, readMetricValue } from "./format";

export const RUN_LOG_LIMIT = 50;

export const KNOWN_EVENT_TYPES = [
  "run_status",
  "engine_stage",
  "engine_search",
  "engine_source_found",
  "engine_token_usage",
  "engine_cost",
  "job_status",
  "compare_ready",
  "synthesis_status",
  "retry_scheduled",
  "cancel_requested",
  "stream_end",
] as const;

export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

export interface RunLiveState {
  runId: string;
  engineId: string;
  status: string;
  stage: string | null;
  attempt: number;
  maxAttempts: number;
  error: string | null;
  warnings: unknown[];
  cancelRequested: boolean;
  createdAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  elapsedSeconds: number | null;
  searchCount: number | null;
  sourceCount: number | null;
  tokensInput: number | null;
  tokensOutput: number | null;
  tokensTotal: number | null;
  llmCostUsd: number | null;
  // null = 実測/推定の別が不明 (タグを出さない)
  llmCostIsEstimate: boolean | null;
  searchApiCostUsd: number | null;
  /** number, or the string "not_measured", or null (=unknown) */
  infraCost: number | string | null;
  log: JobEvent[];
  /** highest seq of a status-bearing event applied to this run */
  lastStatusSeq: number;
}

export interface JobLiveState {
  job: JobView | null;
  jobStatus: string | null;
  jobError: string | null;
  jobWarnings: unknown[];
  cancelRequested: boolean;
  runs: Record<string, RunLiveState>;
  runOrder: string[];
  seenSeqs: Record<number, true>;
  lastSeq: number;
  lastJobStatusSeq: number;
  compareReady: boolean;
  synthesisStatus: string | null;
  synthesisError: string | null;
  streamEnded: boolean;
  connection: ConnectionStatus;
}

export type JobAction =
  | { type: "reset" }
  | { type: "snapshot"; job: JobView }
  | { type: "event"; event: JobEvent }
  | { type: "connection"; status: ConnectionStatus };

export const initialJobState: JobLiveState = {
  job: null,
  jobStatus: null,
  jobError: null,
  jobWarnings: [],
  cancelRequested: false,
  runs: {},
  runOrder: [],
  seenSeqs: {},
  lastSeq: 0,
  lastJobStatusSeq: 0,
  compareReady: false,
  synthesisStatus: null,
  synthesisError: null,
  streamEnded: false,
  connection: "idle",
};

const ACTIVE_RUN_STATUSES = new Set([
  "queued",
  "starting",
  "running",
  "researching",
  "normalizing",
  "pending",
]);

export function isRunActive(status: string): boolean {
  return ACTIVE_RUN_STATUSES.has(status);
}

export function isJobActive(status: string | null): boolean {
  if (!status) return false;
  return ["queued", "starting", "running", "pending"].includes(status);
}

function emptyRun(runId: string, engineId: string): RunLiveState {
  return {
    runId,
    engineId,
    status: "queued",
    stage: null,
    attempt: 1,
    maxAttempts: 1,
    error: null,
    warnings: [],
    cancelRequested: false,
    createdAt: null,
    startedAt: null,
    finishedAt: null,
    elapsedSeconds: null,
    searchCount: null,
    sourceCount: null,
    tokensInput: null,
    tokensOutput: null,
    tokensTotal: null,
    llmCostUsd: null,
    llmCostIsEstimate: null,
    searchApiCostUsd: null,
    infraCost: null,
    log: [],
    lastStatusSeq: 0,
  };
}

/** Build/merge run state from a RunView snapshot, keeping live-only fields. */
function runFromSnapshot(view: RunView, prev?: RunLiveState): RunLiveState {
  const m = view.metrics ?? {};
  const tokensInput = readMetricNumber(m, [
    "input_tokens",
    "tokens_input",
    "prompt_tokens",
  ]);
  const tokensOutput = readMetricNumber(m, [
    "output_tokens",
    "tokens_output",
    "completion_tokens",
  ]);
  let tokensTotal = readMetricNumber(m, ["total_tokens", "tokens", "tokens_total"]);
  if (tokensTotal === null && tokensInput !== null && tokensOutput !== null) {
    tokensTotal = tokensInput + tokensOutput;
  }
  const infraRaw = readMetricValue(m, ["infra_cost", "infra_cost_usd", "infra"]);
  const infraCost = isFiniteNumber(infraRaw)
    ? infraRaw
    : typeof infraRaw === "string"
      ? infraRaw
      : (prev?.infraCost ?? null);

  const estimateRaw = readMetricValue(m, ["llm_cost_is_estimate"]);

  return {
    ...(prev ?? emptyRun(view.id, view.engine_id)),
    runId: view.id,
    engineId: view.engine_id,
    status: view.status,
    stage: view.stage,
    attempt: view.attempt,
    maxAttempts: view.max_attempts,
    error: view.error,
    warnings: view.warnings ?? [],
    cancelRequested: view.cancel_requested || (prev?.cancelRequested ?? false),
    createdAt: view.created_at,
    startedAt: view.started_at,
    finishedAt: view.finished_at,
    elapsedSeconds: view.elapsed_seconds,
    searchCount:
      readMetricNumber(m, ["searches", "search_count", "num_searches"]) ??
      prev?.searchCount ??
      null,
    sourceCount:
      readMetricNumber(m, ["sources", "source_count", "num_sources"]) ??
      prev?.sourceCount ??
      null,
    tokensInput: tokensInput ?? prev?.tokensInput ?? null,
    tokensOutput: tokensOutput ?? prev?.tokensOutput ?? null,
    tokensTotal: tokensTotal ?? prev?.tokensTotal ?? null,
    llmCostUsd:
      readMetricNumber(m, ["llm_cost_usd", "llm_cost"]) ??
      prev?.llmCostUsd ??
      null,
    llmCostIsEstimate:
      typeof estimateRaw === "boolean"
        ? estimateRaw
        : (prev?.llmCostIsEstimate ?? null),
    searchApiCostUsd:
      readMetricNumber(m, ["search_api_cost_usd", "search_cost_usd"]) ??
      prev?.searchApiCostUsd ??
      null,
    infraCost,
    log: prev?.log ?? [],
    lastStatusSeq: prev?.lastStatusSeq ?? 0,
  };
}

function num(v: unknown): number | null {
  return isFiniteNumber(v) ? v : null;
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function applyEventToRun(
  run: RunLiveState,
  event: JobEvent,
): RunLiveState {
  const p = event.payload ?? {};
  const next: RunLiveState = { ...run };

  // Every run-scoped event goes into the collapsible log (capped).
  next.log = [...run.log, event].slice(-RUN_LOG_LIMIT);

  switch (event.type) {
    case "run_status": {
      // Out-of-order guard for status-bearing events.
      if (event.seq < run.lastStatusSeq) break;
      next.lastStatusSeq = event.seq;
      const status = str(p.status);
      if (status) next.status = status;
      if (str(p.stage) !== null) next.stage = str(p.stage);
      if (str(p.error) !== null) next.error = str(p.error);
      if (str(p.started_at) !== null) next.startedAt = str(p.started_at);
      // payloadにstarted_atが無い場合、researching遷移イベントの時刻を開始時刻とする
      if (
        next.startedAt === null &&
        status === "researching" &&
        typeof event.created_at === "string"
      ) {
        next.startedAt = event.created_at;
      }
      if (str(p.finished_at) !== null) next.finishedAt = str(p.finished_at);
      if (num(p.elapsed_seconds) !== null)
        next.elapsedSeconds = num(p.elapsed_seconds);
      if (num(p.attempt) !== null) next.attempt = num(p.attempt) as number;
      break;
    }
    case "engine_stage": {
      if (event.seq < run.lastStatusSeq) break;
      next.lastStatusSeq = event.seq;
      const stage = str(p.stage) ?? str(p.name);
      if (stage) next.stage = stage;
      break;
    }
    case "engine_search": {
      const count = num(p.count) ?? num(p.search_count) ?? num(p.searches);
      next.searchCount = count !== null ? count : (run.searchCount ?? 0) + 1;
      break;
    }
    case "engine_source_found": {
      const count = num(p.count) ?? num(p.source_count) ?? num(p.sources);
      next.sourceCount = count !== null ? count : (run.sourceCount ?? 0) + 1;
      break;
    }
    case "engine_token_usage": {
      const input = num(p.input_tokens) ?? num(p.prompt_tokens);
      const output = num(p.output_tokens) ?? num(p.completion_tokens);
      const total = num(p.total_tokens) ?? num(p.tokens);
      if (input !== null) next.tokensInput = input;
      if (output !== null) next.tokensOutput = output;
      if (total !== null) next.tokensTotal = total;
      else if (input !== null || output !== null) {
        const i = next.tokensInput;
        const o = next.tokensOutput;
        if (i !== null && o !== null) next.tokensTotal = i + o;
      }
      break;
    }
    case "engine_cost": {
      const llm = num(p.llm_cost_usd) ?? num(p.llm_cost);
      if (llm !== null) next.llmCostUsd = llm;
      const estFlag =
        typeof p.llm_cost_is_estimate === "boolean"
          ? p.llm_cost_is_estimate
          : typeof p.estimate === "boolean"
            ? p.estimate
            : null;
      if (estFlag !== null) next.llmCostIsEstimate = estFlag;
      const search = num(p.search_api_cost_usd) ?? num(p.search_cost_usd);
      if (search !== null) next.searchApiCostUsd = search;
      const infra = p.infra_cost ?? p.infra_cost_usd;
      if (isFiniteNumber(infra) || typeof infra === "string") {
        next.infraCost = infra;
      }
      break;
    }
    case "retry_scheduled": {
      if (num(p.attempt) !== null) next.attempt = num(p.attempt) as number;
      break;
    }
    case "cancel_requested": {
      next.cancelRequested = true;
      break;
    }
    default:
      break;
  }
  return next;
}

export function jobReducer(
  state: JobLiveState,
  action: JobAction,
): JobLiveState {
  switch (action.type) {
    case "reset":
      return initialJobState;

    case "connection":
      return { ...state, connection: action.status };

    case "snapshot": {
      const job = action.job;
      const runs: Record<string, RunLiveState> = { ...state.runs };
      const runOrder: string[] = [...state.runOrder];
      for (const view of job.runs) {
        runs[view.id] = runFromSnapshot(view, state.runs[view.id]);
        if (!runOrder.includes(view.id)) runOrder.push(view.id);
      }
      return {
        ...state,
        job,
        jobStatus: job.status,
        jobError: job.error,
        jobWarnings: job.warnings ?? [],
        cancelRequested: job.cancel_requested || state.cancelRequested,
        runs,
        runOrder,
      };
    }

    case "event": {
      const event = action.event;
      if (!isFiniteNumber(event.seq)) return state;
      // Dedupe by seq: the server replays events on reconnect.
      if (state.seenSeqs[event.seq]) return state;

      const next: JobLiveState = {
        ...state,
        seenSeqs: { ...state.seenSeqs, [event.seq]: true },
        lastSeq: Math.max(state.lastSeq, event.seq),
      };
      const p = event.payload ?? {};

      if (event.run_id) {
        const existing =
          state.runs[event.run_id] ??
          emptyRun(event.run_id, event.engine_id ?? "");
        const updated = applyEventToRun(existing, event);
        next.runs = { ...state.runs, [event.run_id]: updated };
        if (!state.runOrder.includes(event.run_id)) {
          next.runOrder = [...state.runOrder, event.run_id];
        }
      }

      switch (event.type) {
        case "job_status": {
          if (event.seq >= state.lastJobStatusSeq) {
            next.lastJobStatusSeq = event.seq;
            const status = str(p.status);
            if (status) next.jobStatus = status;
            if (str(p.error) !== null) next.jobError = str(p.error);
          }
          break;
        }
        case "compare_ready":
          next.compareReady = true;
          break;
        case "synthesis_status": {
          const status = str(p.status);
          if (status) next.synthesisStatus = status;
          next.synthesisError = str(p.error);
          break;
        }
        case "cancel_requested": {
          if (!event.run_id) next.cancelRequested = true;
          break;
        }
        case "stream_end":
          next.streamEnded = true;
          next.connection = "closed";
          break;
        default:
          break;
      }
      return next;
    }

    default:
      return state;
  }
}

/** Ordered run states for rendering. */
export function orderedRuns(state: JobLiveState): RunLiveState[] {
  return state.runOrder
    .map((id) => state.runs[id])
    .filter((r): r is RunLiveState => Boolean(r));
}
