/**
 * Browser-side API layer for the Deep Research service.
 *
 * The important difference from the reference frontend: **no LLM API keys live
 * here.** That version kept provider keys in `localStorage` under `dra_api_keys`
 * and posted them to the backend on every request, which means any XSS on the
 * page exfiltrates the user's OpenAI/Anthropic billing credentials.
 *
 * Here the browser holds, at most, a service key scoped to this API -- and in
 * the recommended setup it holds nothing at all, because calls go through a
 * Next.js route handler that attaches the key server-side. See README.
 */

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Source {
  url: string;
  title?: string | null;
}

export interface ResearchResult {
  report_markdown: string;
  research_brief?: string | null;
  sources: Source[];
  stage_timings: Array<{ stage: string; seconds: number }>;
  usage: { tool_calls: number; searches: number };
}

export interface Job {
  id: string;
  status: JobStatus;
  query: string;
  provider: string;
  model: string;
  duration_seconds?: number | null;
  result?: ResearchResult | null;
  error?: { code: string; message: string; retryable: boolean } | null;
}

export interface ResearchEvent {
  type: string;
  job_id: string;
  stage?: string | null;
  content?: string | null;
  sequence: number;
  timestamp: string;
  data: Record<string, unknown>;
}

/**
 * Defaults to same-origin `/api/research`, i.e. the Next.js proxy route.
 * Point it straight at the service only if you accept a key in the browser.
 */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/research";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = await response.text();
    try {
      detail = (JSON.parse(detail) as { detail?: string }).detail ?? detail;
    } catch {
      /* raw text is fine */
    }
    throw new Error(detail.slice(0, 300) || `Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

export interface StartArgs {
  query: string;
  provider?: string;
  model?: string;
  maxConcurrentResearchUnits?: number;
}

export async function startResearch(args: StartArgs): Promise<string> {
  const options: Record<string, unknown> = {};
  if (args.provider) options.provider = args.provider;
  if (args.model) options.model = args.model;
  if (args.maxConcurrentResearchUnits)
    options.max_concurrent_research_units = args.maxConcurrentResearchUnits;

  const accepted = await call<{ id: string }>("", {
    method: "POST",
    body: JSON.stringify({ query: args.query, options }),
  });
  return accepted.id;
}

export async function getJob(jobId: string): Promise<Job> {
  return call<Job>(`/${jobId}`);
}

export async function cancelJob(jobId: string): Promise<Job> {
  return call<Job>(`/${jobId}`, { method: "DELETE" });
}

export async function listProviders(): Promise<
  Array<{ id: string; label: string; configured: boolean; default_model: string }>
> {
  return call("/providers");
}

/**
 * Subscribe to a run's events.
 *
 * Uses `fetch` + a stream reader rather than `EventSource`, because
 * `EventSource` cannot send an Authorization header. Returns an unsubscribe fn.
 */
export function subscribeToJob(
  jobId: string,
  handlers: {
    onEvent?: (event: ResearchEvent) => void;
    onDone?: () => void;
    onError?: (error: Error) => void;
  },
): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const response = await fetch(`${API_BASE}/${jobId}/events`, {
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        throw new Error(`Event stream failed (${response.status})`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Frames are separated by a blank line. A trailing partial frame must
        // stay buffered until the rest of it arrives.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          for (const line of frame.split("\n")) {
            if (!line.startsWith("data: ")) continue;
            try {
              handlers.onEvent?.(JSON.parse(line.slice(6)) as ResearchEvent);
            } catch {
              /* skip malformed frame rather than kill the stream */
            }
          }
        }
      }
      handlers.onDone?.();
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        handlers.onError?.(error as Error);
      }
    }
  })();

  return () => controller.abort();
}
