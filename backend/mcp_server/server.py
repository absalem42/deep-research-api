"""MCP server exposing deep research as agent tools.

Runs over stdio, so Claude Code / Claude Desktop / Cursor / any MCP client can
call it. It is a *client of the HTTP API*, not a second copy of the engine --
one deployment, one set of credentials, one place where limits are enforced.

    claude mcp add deep-research -- python -m mcp_server.server

Env:
    DEEP_RESEARCH_URL      default http://localhost:8080
    DEEP_RESEARCH_API_KEY  caller key for that service
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

BASE_URL = os.getenv("DEEP_RESEARCH_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("DEEP_RESEARCH_API_KEY", "")
# A deep research run is minutes, not seconds. Agents calling this need to know
# the wait is expected rather than a hang.
POLL_INTERVAL = float(os.getenv("DEEP_RESEARCH_POLL_INTERVAL", "3"))
MAX_WAIT = float(os.getenv("DEEP_RESEARCH_MAX_WAIT", "900"))

server = Server("deep-research")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="deep_research",
            description=(
                "Run a multi-agent deep research task and return a cited markdown "
                "report. A supervisor decomposes the question, parallel researchers "
                "search the web, and the findings are compressed into one report. "
                "Takes 30-120 seconds -- use it for questions that genuinely need "
                "multi-source synthesis, not for simple lookups."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The research question. Be specific.",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Optional provider override (anthropic, openai, moonshot, openrouter).",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model id within the provider.",
                    },
                    "max_concurrent_research_units": {
                        "type": "integer",
                        "description": "Parallel researchers, 1-10. More is faster but costs more.",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="deep_research_start",
            description=(
                "Start a research job and return its id immediately without waiting. "
                "Use when you want to kick off research and collect it later; pair "
                "with deep_research_status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "provider": {"type": "string"},
                    "model": {"type": "string"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="deep_research_status",
            description="Check a research job started with deep_research_start.",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        Tool(
            name="deep_research_providers",
            description="List which model providers this deployment has credentials for.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _options(args: dict[str, Any]) -> dict[str, Any]:
    options = {
        k: args[k]
        for k in ("provider", "model", "max_concurrent_research_units")
        if args.get(k) is not None
    }
    # An MCP caller is an agent with nobody to answer a clarifying question.
    options["allow_clarification"] = False
    return options


def _render(job: dict[str, Any]) -> str:
    status = job.get("status")
    if status == "succeeded":
        result = job.get("result") or {}
        report = result.get("report_markdown") or "(empty report)"
        sources = result.get("sources") or []
        parts = [report]
        if sources:
            parts.append("\n\n## Sources\n")
            parts.extend(
                f"{i}. {s.get('title') or s['url']} — {s['url']}"
                for i, s in enumerate(sources, 1)
            )
        usage = result.get("usage") or {}
        parts.append(
            f"\n\n---\n*{job.get('provider')}/{job.get('model')} · "
            f"{job.get('duration_seconds')}s · {usage.get('searches', 0)} searches "
            f"· {len(sources)} sources*"
        )
        return "".join(parts)

    if status == "failed":
        error = job.get("error") or {}
        retry = " (retryable)" if error.get("retryable") else ""
        return f"Research failed{retry}: {error.get('message', 'unknown error')}"
    return f"Job {job.get('id')} is {status}."


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
            if name == "deep_research_providers":
                response = await client.get(f"{BASE_URL}/v1/models")
                response.raise_for_status()
                lines = [
                    f"- {p['id']} ({p['label']}) — {'ready' if p['configured'] else 'no credential'}"
                    f" — default {p['default_model']}"
                    for p in response.json()
                ]
                return [TextContent(type="text", text="\n".join(lines))]

            if name == "deep_research_status":
                response = await client.get(
                    f"{BASE_URL}/v1/research/{arguments['job_id']}"
                )
                response.raise_for_status()
                return [TextContent(type="text", text=_render(response.json()))]

            if name in ("deep_research", "deep_research_start"):
                response = await client.post(
                    f"{BASE_URL}/v1/research",
                    json={
                        "query": arguments["query"],
                        "options": _options(arguments),
                    },
                )
                response.raise_for_status()
                job_id = response.json()["id"]

                if name == "deep_research_start":
                    return [
                        TextContent(
                            type="text",
                            text=(
                                f"Started job {job_id}. Check it with "
                                f"deep_research_status."
                            ),
                        )
                    ]

                waited = 0.0
                while waited < MAX_WAIT:
                    await asyncio.sleep(POLL_INTERVAL)
                    waited += POLL_INTERVAL
                    poll = await client.get(f"{BASE_URL}/v1/research/{job_id}")
                    poll.raise_for_status()
                    job = poll.json()
                    if job["status"] in ("succeeded", "failed", "cancelled"):
                        return [TextContent(type="text", text=_render(job))]

                return [
                    TextContent(
                        type="text",
                        text=(
                            f"Still running after {int(MAX_WAIT)}s. Job id {job_id} — "
                            f"check with deep_research_status."
                        ),
                    )
                ]

            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:  # noqa: BLE001
            detail = exc.response.text[:400]
        return [
            TextContent(
                type="text",
                text=f"Deep research API returned {exc.response.status_code}: {detail}",
            )
        ]
    except httpx.HTTPError as exc:
        return [
            TextContent(
                type="text",
                text=(
                    f"Could not reach the deep research service at {BASE_URL}: {exc}. "
                    f"Is it running, and is DEEP_RESEARCH_URL correct?"
                ),
            )
        ]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
