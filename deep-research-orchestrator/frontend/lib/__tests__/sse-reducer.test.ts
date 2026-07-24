import { describe, expect, it } from "vitest";
import type { JobEvent, JobView } from "../api-types";
import {
  initialJobState,
  jobReducer,
  orderedRuns,
  type JobLiveState,
} from "../sse-reducer";

function makeSnapshot(): JobView {
  return {
    id: "job-1",
    status: "running",
    topic: "テスト",
    objective: null,
    instructions: null,
    language: "ja",
    options: {},
    warnings: [],
    error: null,
    cancel_requested: false,
    created_at: "2026-07-24T00:00:00Z",
    finished_at: null,
    runs: [
      {
        id: "run-a",
        engine_id: "engine-a",
        status: "queued",
        stage: null,
        attempt: 1,
        max_attempts: 2,
        error: null,
        warnings: [],
        metrics: {},
        cancel_requested: false,
        created_at: "2026-07-24T00:00:00Z",
        started_at: null,
        finished_at: null,
        elapsed_seconds: null,
      },
    ],
  };
}

function ev(
  seq: number,
  type: string,
  payload: Record<string, unknown> = {},
  runId: string | null = "run-a",
): JobEvent {
  return {
    seq,
    type,
    run_id: runId,
    engine_id: runId ? "engine-a" : null,
    payload,
    created_at: "2026-07-24T00:00:01Z",
  };
}

function apply(state: JobLiveState, events: JobEvent[]): JobLiveState {
  return events.reduce(
    (s, event) => jobReducer(s, { type: "event", event }),
    state,
  );
}

describe("jobReducer", () => {
  it("builds run state from a sequence of events", () => {
    let state = jobReducer(initialJobState, {
      type: "snapshot",
      job: makeSnapshot(),
    });
    state = apply(state, [
      ev(1, "run_status", { status: "starting" }),
      ev(2, "run_status", { status: "researching", stage: "search" }),
      ev(3, "engine_search", {}),
      ev(4, "engine_search", {}),
      ev(5, "engine_source_found", {}),
      ev(6, "engine_token_usage", { input_tokens: 100, output_tokens: 50 }),
      ev(7, "engine_cost", { llm_cost_usd: 0.12, llm_cost_is_estimate: true }),
      ev(8, "engine_stage", { stage: "normalize" }),
      ev(9, "run_status", { status: "succeeded" }),
      ev(10, "job_status", { status: "succeeded" }, null),
    ]);

    const run = state.runs["run-a"];
    expect(run.status).toBe("succeeded");
    expect(run.stage).toBe("normalize");
    expect(run.searchCount).toBe(2);
    expect(run.sourceCount).toBe(1);
    expect(run.tokensInput).toBe(100);
    expect(run.tokensOutput).toBe(50);
    expect(run.tokensTotal).toBe(150);
    expect(run.llmCostUsd).toBe(0.12);
    expect(run.llmCostIsEstimate).toBe(true);
    expect(state.jobStatus).toBe("succeeded");
  });

  it("deduplicates events by seq (replayed events are ignored)", () => {
    let state = jobReducer(initialJobState, {
      type: "snapshot",
      job: makeSnapshot(),
    });
    const search = ev(3, "engine_search", {});
    state = apply(state, [search, search, search]);
    expect(state.runs["run-a"].searchCount).toBe(1);

    // Replay of a whole batch must not double-count either.
    const batch = [ev(4, "engine_search", {}), ev(5, "engine_source_found", {})];
    state = apply(state, [...batch, ...batch]);
    expect(state.runs["run-a"].searchCount).toBe(2);
    expect(state.runs["run-a"].sourceCount).toBe(1);
  });

  it("ignores out-of-order status events (lower seq never overwrites)", () => {
    let state = jobReducer(initialJobState, {
      type: "snapshot",
      job: makeSnapshot(),
    });
    state = apply(state, [
      ev(10, "run_status", { status: "succeeded" }),
      ev(5, "run_status", { status: "researching" }),
      ev(6, "engine_stage", { stage: "search" }),
    ]);
    const run = state.runs["run-a"];
    expect(run.status).toBe("succeeded");
    expect(run.stage).toBeNull();
  });

  it("keeps unreported metrics null (never fabricates 0)", () => {
    let state = jobReducer(initialJobState, {
      type: "snapshot",
      job: makeSnapshot(),
    });
    state = apply(state, [ev(1, "run_status", { status: "researching" })]);
    const run = state.runs["run-a"];
    expect(run.tokensTotal).toBeNull();
    expect(run.llmCostUsd).toBeNull();
    expect(run.searchCount).toBeNull();
    expect(run.sourceCount).toBeNull();
    expect(run.infraCost).toBeNull();
  });

  it("creates a run stub for events about unknown runs", () => {
    let state = jobReducer(initialJobState, {
      type: "snapshot",
      job: makeSnapshot(),
    });
    state = apply(state, [
      {
        seq: 20,
        type: "run_status",
        run_id: "run-b",
        engine_id: "engine-b",
        payload: { status: "researching" },
        created_at: "2026-07-24T00:00:02Z",
      },
    ]);
    expect(orderedRuns(state).map((r) => r.runId)).toEqual(["run-a", "run-b"]);
    expect(state.runs["run-b"].engineId).toBe("engine-b");
    expect(state.runs["run-b"].status).toBe("researching");
  });

  it("tracks compare/synthesis/cancel/stream events", () => {
    let state = jobReducer(initialJobState, {
      type: "snapshot",
      job: makeSnapshot(),
    });
    state = apply(state, [
      ev(1, "compare_ready", {}, null),
      ev(2, "synthesis_status", { status: "failed", error: "profile missing" }, null),
      ev(3, "cancel_requested", {}, null),
      ev(4, "stream_end", {}, null),
    ]);
    expect(state.compareReady).toBe(true);
    expect(state.synthesisStatus).toBe("failed");
    expect(state.synthesisError).toBe("profile missing");
    expect(state.cancelRequested).toBe(true);
    expect(state.streamEnded).toBe(true);
  });

  it("caps the per-run event log at 50 entries", () => {
    let state = jobReducer(initialJobState, {
      type: "snapshot",
      job: makeSnapshot(),
    });
    const events = Array.from({ length: 60 }, (_, i) =>
      ev(i + 1, "engine_search", {}),
    );
    state = apply(state, events);
    const run = state.runs["run-a"];
    expect(run.log).toHaveLength(50);
    expect(run.log[0].seq).toBe(11);
    expect(run.log[49].seq).toBe(60);
    expect(run.searchCount).toBe(60);
  });
});
