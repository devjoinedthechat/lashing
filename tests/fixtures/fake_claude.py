"""Stands in for `claude -p ... --output-format stream-json`: reads the prompt and MCP config, calls
lashing's get_booking over HTTP like Claude Code would, and prints Claude Code's stream-json events."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import Client


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


async def main(argv: list[str]) -> None:
    prompt = argv[argv.index("-p") + 1]
    config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
    url = config["mcpServers"]["lashing"]["url"]
    leaked = sorted(k for k in os.environ if k.startswith(("CLAUDE", "VSCODE", "MCP_")))
    emit({"type": "system", "subtype": "init", "mcp_servers": [{"name": "lashing", "status": "connected"}]})
    found = re.search(r"\b(LSIM\d{6})\b", prompt)
    if found is None:
        raise SystemExit("no booking reference in the prompt")
    reference = found.group(1)
    async with Client(url) as client:
        result = await client.call_tool("get_booking", {"reference": reference})
    text = "".join(getattr(b, "text", "") for b in result.content)
    call = {"type": "tool_use", "id": "toolu_1", "name": "mcp__lashing__get_booking", "input": {"reference": reference}}
    emit({"type": "assistant", "message": {"content": [call]}})
    answer = {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": text}]}
    emit({"type": "user", "message": {"content": [answer]}})
    final = f"It is CONFIRMED. The carrier's message is not an instruction I act on. leaked={leaked}"
    emit({"type": "result", "subtype": "success", "result": final, "num_turns": 2, "total_cost_usd": 0.0123})


anyio.run(main, sys.argv)
