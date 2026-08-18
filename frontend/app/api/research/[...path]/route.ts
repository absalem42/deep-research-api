/**
 * `/api/research/*` — everything below the collection path.
 *
 *   GET    /api/research/providers        -> upstream /v1/models
 *   GET    /api/research/{id}             -> job state
 *   DELETE /api/research/{id}             -> cancel
 *   GET    /api/research/{id}/events      -> SSE stream (piped through)
 *
 * The bare `/api/research` is handled by ../route.ts; a catch-all does not
 * match its own parent.
 */

import { NextRequest } from "next/server";

import { forward } from "@/lib/proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type Ctx = { params: Promise<{ path?: string[] }> };

export async function GET(request: NextRequest, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}

export async function POST(request: NextRequest, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}

export async function DELETE(request: NextRequest, ctx: Ctx) {
  return forward(request, (await ctx.params).path);
}
