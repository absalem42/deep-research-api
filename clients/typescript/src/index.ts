/**
 * TypeScript client for the Deep Research API.
 *
 *   import { DeepResearchClient } from "@yourorg/deep-research";
 *
 *   const client = new DeepResearchClient({
 *     baseUrl: "https://research.example.com",
 *     apiKey: process.env.DEEP_RESEARCH_API_KEY!,
 *   });
 *
 *   const job = await client.research("What is the state of the EU AI Act?");
 *   console.log(job.result?.report_markdown);
 *
 * Zero runtime dependencies: fetch, crypto and TextDecoder are all built in on
 * Node 18+ and in the browser (minus webhook verification, which is server-side
 * by nature).
 */

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Source {
  url: string;
  title?: string | null;
  snippet?: string | null;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  tool_calls: number;
  searches: number;
}

export interface ResearchResult {
  report_markdown: string;
  research_brief?: string | null;
  sources: Source[];
  stage_timings: Array<{ stage: string; seconds: number }>;
  usage: Usage;
}

export interface Job {
  id: string;
  status: JobStatus;
  query: string;
  provider: string;
  model: string;
  created_at: string;
  updated_at: string;
  duration_seconds?: number | null;
  result?: ResearchResult | null;
  error?: { code: string; message: string; retryable: boolean } | null;
  metadata: Record<string, unknown>;
}

export interface ResearchOptions {
  provider?: string;
  model?: string;
  searchApi?: string;
  maxConcurrentResearchUnits?: number;
  timeoutSeconds?: number;
}

export interface StartOptions extends ResearchOptions {
  callbackUrl?: string;
  metadata?: Record<string, unknown>;
  idempotencyKey?: string;
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

export class DeepResearchError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "DeepResearchError";
  }
}

export class ResearchFailedError extends DeepResearchError {
  constructor(message: string, readonly code: string, readonly retryable: boolean) {
    super(message);
    this.name = "ResearchFailedError";
  }
}

export interface ClientConfig {
  baseUrl: string;
  apiKey: string;
  fetch?: typeof fetch;
}

const TERMINAL: JobStatus[] = ["succeeded", "failed", "cancelled"];

export class DeepResearchClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchImpl: typeof fetch;

  constructor(config: ClientConfig) {
    this.baseUrl = config.baseUrl.replace(/\/$/, "");
    this.apiKey = config.apiKey;
    this.fetchImpl = config.fetch ?? globalThis.fetch;
    if (!this.fetchImpl) {
      throw new DeepResearchError("No fetch implementation available (Node 18+ required).");
    }
  }

  private headers(): Record<string, string> {
    return {
      Authorization: `Bearer ${this.apiKey}`,
      "Content-Type": "application/json",
    };
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method,
      headers: this.headers(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    if (!response.ok) {
      let detail = await response.text();
      try {
        detail = (JSON.parse(detail) as { detail?: string }).detail ?? detail;
      } catch {
        /* keep the raw text */
      }
      throw new DeepResearchError(
        `${response.status} from ${path}: ${detail.slice(0, 400)}`,
        response.status,
      );
    }
    return (await response.json()) as T;
  }

  async providers(): Promise<Array<{ id: string; label: string; configured: boolean }>> {
    return this.request("GET", "/v1/models");
  }

  /** Submit a job; resolves with the job id as soon as it is accepted. */
  async start(query: string, options: StartOptions = {}): Promise<string> {
    const payload: Record<string, unknown> = {
      query,
      options: dropUndefined({
        provider: options.provider,
        model: options.model,
        search_api: options.searchApi,
        max_concurrent_research_units: options.maxConcurrentResearchUnits,
        timeout_seconds: options.timeoutSeconds,
      }),
    };
    if (options.callbackUrl) payload.callback_url = options.callbackUrl;
    if (options.metadata) payload.metadata = options.metadata;
    if (options.idempotencyKey) payload.idempotency_key = options.idempotencyKey;

    const accepted = await this.request<{ id: string }>("POST", "/v1/research", payload);
    return accepted.id;
  }

  async get(jobId: string): Promise<Job> {
    return this.request("GET", `/v1/research/${jobId}`);
  }

  async cancel(jobId: string): Promise<Job> {
    return this.request("DELETE", `/v1/research/${jobId}`);
  }

  async wait(
    jobId: string,
    opts: { pollIntervalMs?: number; maxWaitMs?: number; onUpdate?: (job: Job) => void } = {},
  ): Promise<Job> {
    const pollIntervalMs = opts.pollIntervalMs ?? 3000;
    const deadline = Date.now() + (opts.maxWaitMs ?? 900_000);

    while (Date.now() < deadline) {
      const job = await this.get(jobId);
      opts.onUpdate?.(job);
      if (TERMINAL.includes(job.status)) {
        if (job.status === "failed" && job.error) {
          throw new ResearchFailedError(job.error.message, job.error.code, job.error.retryable);
        }
        return job;
      }
      await sleep(pollIntervalMs);
    }
    throw new DeepResearchError(`Job ${jobId} did not finish in time.`);
  }

  /** Submit and wait. The common case. */
  async research(query: string, options: StartOptions & { maxWaitMs?: number } = {}): Promise<Job> {
    const jobId = await this.start(query, options);
    return this.wait(jobId, { maxWaitMs: options.maxWaitMs });
  }

  /** Async-iterate the SSE stream: `for await (const e of client.stream(id))`. */
  async *stream(jobId: string): AsyncGenerator<ResearchEvent> {
    const response = await this.fetchImpl(`${this.baseUrl}/v1/research/${jobId}/events`, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });
    if (!response.ok || !response.body) {
      throw new DeepResearchError(`${response.status} opening event stream`, response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line; anything short of one is a
        // partial frame and must stay in the buffer.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          for (const line of frame.split("\n")) {
            if (line.startsWith("data: ")) {
              yield JSON.parse(line.slice(6)) as ResearchEvent;
            }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
}

function dropUndefined<T extends Record<string, unknown>>(obj: T): Partial<T> {
  return Object.fromEntries(Object.entries(obj).filter(([, v]) => v !== undefined)) as Partial<T>;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Verify a webhook before trusting it. Node only (needs `crypto`).
 *
 * Pass the RAW request body -- if your framework parsed and re-serialised the
 * JSON, the bytes differ and the signature will not match.
 */
export async function verifyWebhook(
  secret: string,
  rawBody: string,
  signatureHeader: string,
  toleranceSeconds = 300,
): Promise<boolean> {
  const parts = Object.fromEntries(
    signatureHeader.split(",").map((p) => {
      const i = p.indexOf("=");
      return [p.slice(0, i), p.slice(i + 1)];
    }),
  );
  const timestamp = Number(parts.t);
  if (!Number.isFinite(timestamp)) return false;
  if (Math.abs(Date.now() / 1000 - timestamp) > toleranceSeconds) return false;

  const { createHmac, timingSafeEqual } = await import("node:crypto");
  const expected = createHmac("sha256", secret).update(`${timestamp}.${rawBody}`).digest("hex");
  const a = Buffer.from(`t=${timestamp},v1=${expected}`);
  const b = Buffer.from(signatureHeader);
  return a.length === b.length && timingSafeEqual(a, b);
}
