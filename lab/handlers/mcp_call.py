"""Call one tool of an operator-signed MCP server (#256).

The payload names the server, the tool and its arguments:

    {"server": "notes", "tool": "search", "arguments": {"query": "backup"}}

The handler makes one call, mcp.call, and holds no other tool. It is granted
only the servers in the operator-signed MCP config, and no network. The
broker refuses an unsigned server, a tool off the signed allowlist or a tool
that changed since it was signed before a person is asked. Every call then
parks the task until the operator signs an approval bound to the signed
entry and the exact arguments.

What the server returns, and the tool's own description, come back only as
fixed-schema evidence. They are returned as data for a person to read,
never acted on here.

Policy tier: approve, the tier of mcp.call. Registered by ``register_all``
only when an MCP servers file is configured and every entry in it verifies.
The owner granted the tool to this handler on 2026-10-01.
"""

from __future__ import annotations

from typing import Any

from lab.broker import PermanentFailure, ToolSession
from lab.policy import Tier
from lab.queue import Task
from lab.untrusted import validate_evidence

KIND = "mcp.call"
REF = "lab.handlers.mcp_call:call_tool"
TOOLS = frozenset({"mcp.call"})
TIER = Tier.APPROVE

_FIELDS = frozenset({"server", "tool", "arguments"})
_USAGE = ("payload must be {'server': '<name>', 'tool': '<name>', "
          "'arguments': {...}}")


def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The broker parameters for a well-formed payload, or a refusal."""
    server = payload.get("server")
    tool = payload.get("tool")
    arguments = payload.get("arguments", {})
    if (set(payload) - _FIELDS or not isinstance(server, str) or not server
            or not isinstance(tool, str) or not tool or not isinstance(arguments, dict)):
        raise PermanentFailure(_USAGE)
    # ``name`` is the broker's word for the server's tool (lab.broker.TOOL_SCHEMAS).
    return {"server": server, "name": tool, "arguments": arguments}


async def call_tool(task: Task, tools: ToolSession) -> dict[str, Any]:
    params = check_payload(task.payload)
    result = tools.submit("mcp.call", **params)
    if "evidence" not in result.detail:
        # Refused, timed out or cancelled. The call is journaled, so a retry
        # would only replay this outcome.
        raise PermanentFailure(f"mcp.call failed: {result.error}")
    return {"server": params["server"], "tool": params["name"],
            "is_error": bool(result.detail.get("is_error")),
            "evidence": validate_evidence(result.detail["evidence"]).as_payload(),
            "description": validate_evidence(result.detail.get("description")).as_payload(),
            "untrusted": True}
