/**
 * `/api/research` — the bare collection path.
 *
 * This exists as its own file on purpose. A catch-all segment (`[...path]`) does
 * not match its own parent, and Next 15's optional catch-all (`[[...path]]`)
 * returned 404 here for route handlers. Being explicit is more robust than
 * depending on that behaviour.
 *
 *   POST /api/research  -> start a job
 *   GET  /api/research  -> list jobs
 */

import { NextRequest } from "next/server";

import { forward } from "@/lib/proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request: NextRequest) {
  return forward(request, undefined);
}

export async function GET(request: NextRequest) {
  return forward(request, undefined);
}
