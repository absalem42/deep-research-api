"use client";

/**
 * React hook driving one research run: submit, live progress, final report.
 *
 *   const { run, status, stage, events, job, error, cancel } = useResearch();
 *   <button onClick={() => run("What is MCP?")}>Research</button>
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  cancelJob,
  getJob,
  startResearch,
  subscribeToJob,
  type Job,
  type ResearchEvent,
  type StartArgs,
} from "../lib/deepResearch";

export interface UseResearchState {
  jobId: string | null;
  job: Job | null;
  status: "idle" | "queued" | "running" | "succeeded" | "failed" | "cancelled";
  stage: string | null;
  events: ResearchEvent[];
  error: string | null;
  isRunning: boolean;
}

export function useResearch() {
  const [state, setState] = useState<UseResearchState>({
    jobId: null,
    job: null,
    status: "idle",
    stage: null,
    events: [],
    error: null,
    isRunning: false,
  });

  const unsubscribeRef = useRef<(() => void) | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      // Tearing down the component must also tear down the stream, or the
      // reader keeps the connection open for the life of the tab.
      mountedRef.current = false;
      unsubscribeRef.current?.();
    };
  }, []);

  const run = useCallback(async (queryOrArgs: string | StartArgs) => {
    const args: StartArgs =
      typeof queryOrArgs === "string" ? { query: queryOrArgs } : queryOrArgs;

    unsubscribeRef.current?.();
    setState({
      jobId: null,
      job: null,
      status: "queued",
      stage: null,
      events: [],
      error: null,
      isRunning: true,
    });

    try {
      const jobId = await startResearch(args);
      if (!mountedRef.current) return;
      setState((s) => ({ ...s, jobId, status: "running" }));

      const finish = async () => {
        try {
          const job = await getJob(jobId);
          if (!mountedRef.current) return;
          setState((s) => ({
            ...s,
            job,
            status: job.status,
            isRunning: false,
            error: job.error?.message ?? null,
          }));
        } catch (e) {
          if (mountedRef.current) {
            setState((s) => ({ ...s, isRunning: false, error: (e as Error).message }));
          }
        }
      };

      unsubscribeRef.current = subscribeToJob(jobId, {
        onEvent: (event) => {
          if (!mountedRef.current) return;
          setState((s) => ({
            ...s,
            stage: event.stage ?? s.stage,
            // heartbeats carry no information for the UI
            events: event.type === "heartbeat" ? s.events : [...s.events, event],
          }));
        },
        onDone: finish,
        onError: (e) => {
          if (!mountedRef.current) return;
          setState((s) => ({ ...s, isRunning: false, error: e.message }));
        },
      });
    } catch (e) {
      if (mountedRef.current) {
        setState((s) => ({
          ...s,
          status: "failed",
          isRunning: false,
          error: (e as Error).message,
        }));
      }
    }
  }, []);

  const cancel = useCallback(async () => {
    if (!state.jobId) return;
    try {
      await cancelJob(state.jobId);
      unsubscribeRef.current?.();
      setState((s) => ({ ...s, status: "cancelled", isRunning: false }));
    } catch (e) {
      setState((s) => ({ ...s, error: (e as Error).message }));
    }
  }, [state.jobId]);

  return { ...state, run, cancel };
}
