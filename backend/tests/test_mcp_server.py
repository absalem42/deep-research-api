"""Smoke tests for the MCP server.

These exist because of a near miss: Dependabot proposed `mcp>=2.0.0`, which
removes `Server.list_tools` and breaks this module outright. Nothing in the
suite imported `mcp_server`, so CI went green and the PR looked safe to merge.

Importing the module is most of the value here -- it is the check that would
have caught it.
"""

from __future__ import annotations

import json

from mcp_server.server import _options, _render, list_tools


class TestToolDefinitions:
    async def test_exposes_the_expected_tools(self):
        names = {tool.name for tool in await list_tools()}
        assert names == {
            "deep_research",
            "deep_research_start",
            "deep_research_status",
            "deep_research_providers",
        }

    async def test_every_tool_has_a_usable_description(self):
        for tool in await list_tools():
            assert tool.description and len(tool.description) > 40, tool.name

    async def test_schemas_are_valid_json_schema_objects(self):
        for tool in await list_tools():
            schema = tool.inputSchema
            assert schema["type"] == "object"
            json.dumps(schema)  # must be serialisable for the wire

    async def test_query_is_required_where_it_matters(self):
        by_name = {t.name: t for t in await list_tools()}
        for name in ("deep_research", "deep_research_start"):
            assert by_name[name].inputSchema["required"] == ["query"]


class TestOptions:
    def test_clarification_is_disabled(self):
        """An MCP caller is an agent; there is nobody to answer a question."""
        assert _options({"query": "x"})["allow_clarification"] is False

    def test_only_supplied_options_are_forwarded(self):
        assert _options({"query": "x", "provider": "anthropic"}) == {
            "provider": "anthropic",
            "allow_clarification": False,
        }

    def test_none_values_are_dropped(self):
        assert "model" not in _options({"query": "x", "model": None})


class TestRendering:
    def test_succeeded_job_includes_report_and_sources(self):
        out = _render(
            {
                "id": "job_1",
                "status": "succeeded",
                "provider": "anthropic",
                "model": "claude",
                "duration_seconds": 82.1,
                "result": {
                    "report_markdown": "# Findings",
                    "sources": [{"url": "https://a.com", "title": "A"}],
                    "usage": {"searches": 3},
                },
            }
        )
        assert "# Findings" in out
        assert "https://a.com" in out
        assert "82.1s" in out

    def test_failed_job_surfaces_retryability(self):
        out = _render(
            {"id": "j", "status": "failed", "error": {"message": "rate limited", "retryable": True}}
        )
        assert "retryable" in out
        assert "rate limited" in out

    def test_running_job_reports_status(self):
        assert "running" in _render({"id": "j", "status": "running"})

    def test_succeeded_but_empty_report_does_not_crash(self):
        out = _render({"id": "j", "status": "succeeded", "result": {"sources": []}})
        assert "empty report" in out


def test_mcp_sdk_major_is_pinned():
    """mcp 2.x removed Server.list_tools; langchain-mcp-adapters also pins <2.

    If this fails, the SDK moved under us -- port `mcp_server/server.py` to the
    new API before relaxing the pin in requirements.txt.
    """
    import importlib.metadata as metadata

    major = int(metadata.version("mcp").split(".")[0])
    assert major == 1, (
        f"mcp {metadata.version('mcp')} installed; mcp_server/server.py targets the 1.x API"
    )
