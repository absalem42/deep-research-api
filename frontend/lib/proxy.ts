/**
 * Server-side forwarding to the Deep Research API.
 *
 * Lives here so both route handlers -- the bare `/api/research` and the
 * `/api/research/*` catch-all -- share one implementation.
 *
 * The service key is attached here and never leaves the server. Streaming
 * responses are piped through untouched so SSE keeps working.
 *
 * Required env (server-side only -- no NEXT_PUBLIC_ prefix, which would ship
 * the value to the browser):
 *   DEEP_RESEARCH_URL      https://research.example.com
 *   DEEP_RESEARCH_API_KEY  drk_...
 */

import { NextRequest } from "next/server";

const BASE = (process.env.DEEP_RESEARCH_URL ?? "http://localhost:8080").replace(/\/$/, "");
const KEY = process.env.DEEP_RESEARCH_API_KEY ?? "";

/** Map a client-facing path onto the upstream API. */
export function upstreamPath(segments: string[] | undefined, search: string): string {
  const parts = segments ?? [];
  // Friendlier alias so the browser does not need to know about /v1/models.
  if (parts.length === 1 && parts[0] === "providers") {
    return `/v1/models${search}`;
  }
  return `/v1/research${parts.length ? `/${parts.join("/")}` : ""}${search}`;
}

export async function forward(
  request: NextRequest,
  segments: string[] | undefined,
): Promise<Response> {
  if (!KEY) {
    return Response.json(
      { detail: "DEEP_RESEARCH_API_KEY is not set on the server." },
      { status: 500 },
    );
  }

  const { search } = new URL(request.url);
  const target = `${BASE}${upstreamPath(segments, search)}`;
  const isStream = target.endsWith("/events");

  const headers: Record<string, string> = {
    Authorization: `Bearer ${KEY}`,
    "Content-Type": "application/json",
  };
  if (isStream) headers.Accept = "text/event-stream";

  const body =
    request.method === "GET" || request.method === "DELETE"
      ? undefined
      : await request.text();

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      cache: "no-store", // never cache a research result or an event stream
    });
  } catch (error) {
    // A connection error here almost always means the backend is not running.
    return Response.json(
      {
        detail: `Could not reach the research API at ${BASE}: ${(error as Error).message}`,
      },
      { status: 502 },
    );
  }

  if (isStream && upstream.body) {
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  }

  return new Response(await upstream.text(), {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
