"""`lashing demo` as a real subprocess over stdio, the way Claude Desktop or Claude Code runs it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio


async def test_the_demo_server_answers_over_stdio(tmp_path: Path) -> None:
    command = StdioServerParameters(
        command=sys.executable,
        args=["-m", "lashing.cli", "demo", "--state-dir", str(tmp_path), "--start", "2026-09-21T08:00:00Z"],
    )
    async with Client(command) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("find_sailings", {"origin": "CNSHA", "destination": "NLRTM"})
    assert "apply_plan" in tools
    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["sailings"][0]["departs"]["port"] == "CNSHA"
