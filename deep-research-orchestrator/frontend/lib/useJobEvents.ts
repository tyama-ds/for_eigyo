"use client";

import { useEffect, useReducer, useCallback } from "react";
import { api } from "./api";
import {
  initialJobState,
  jobReducer,
  KNOWN_EVENT_TYPES,
  type JobLiveState,
} from "./sse-reducer";
import type { JobEvent } from "./api-types";

/**
 * Live job state: GET snapshot + a single EventSource on
 * /api/jobs/{id}/events. The browser's EventSource sends Last-Event-ID
 * automatically on reconnect (the SSE `id:` field is the event seq), so the
 * server replays missed events; the reducer dedupes by seq.
 */
export function useJobEvents(jobId: string | null): {
  state: JobLiveState;
  refreshSnapshot: () => void;
  snapshotError: string | null;
} {
  const [state, dispatch] = useReducer(jobReducer, initialJobState);
  const [snapshotError, setSnapshotError] = useReducer(
    (_prev: string | null, next: string | null) => next,
    null,
  );

  const refreshSnapshot = useCallback(() => {
    if (!jobId) return;
    api
      .getJob(jobId)
      .then((job) => {
        setSnapshotError(null);
        dispatch({ type: "snapshot", job });
      })
      .catch((e) => setSnapshotError(e instanceof Error ? e.message : String(e)));
  }, [jobId]);

  useEffect(() => {
    dispatch({ type: "reset" });
    setSnapshotError(null);
    if (!jobId) return;

    // Initial snapshot.
    let cancelled = false;
    api
      .getJob(jobId)
      .then((job) => {
        if (!cancelled) dispatch({ type: "snapshot", job });
      })
      .catch((e) => {
        if (!cancelled)
          setSnapshotError(e instanceof Error ? e.message : String(e));
      });

    // Single EventSource with named-event listeners + onmessage fallback.
    dispatch({ type: "connection", status: "connecting" });
    const es = new EventSource(api.eventsUrl(jobId));

    const handle = (ev: MessageEvent) => {
      let data: JobEvent;
      try {
        data = JSON.parse(ev.data) as JobEvent;
      } catch {
        return;
      }
      dispatch({ type: "event", event: data });
      if (data.type === "stream_end") {
        es.close();
        dispatch({ type: "connection", status: "closed" });
      }
    };

    for (const type of KNOWN_EVENT_TYPES) {
      es.addEventListener(type, handle);
    }
    es.onmessage = handle; // fallback for unnamed / unknown event types
    es.onopen = () => dispatch({ type: "connection", status: "open" });
    es.onerror = () => {
      // EventSource reconnects automatically (sending Last-Event-ID).
      if (es.readyState !== EventSource.CLOSED) {
        dispatch({ type: "connection", status: "reconnecting" });
      } else {
        dispatch({ type: "connection", status: "closed" });
      }
    };

    return () => {
      cancelled = true;
      es.close();
    };
  }, [jobId]);

  return { state, refreshSnapshot, snapshotError };
}
