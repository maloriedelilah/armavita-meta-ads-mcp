# SPDX-License-Identifier: AGPL-3.0-only

"""Failed tool calls reach the client as isError (ADS-59).

Tools report a failure as JSON with a top-level `error` (Graph's error, or the tool's own
refusal), or as Graph's `{"success": false}`. Returned as ordinary content, that reads as a
successful call with an error inside, and clients and agents that branch on `isError` treat a
failed write as done. The server class raises ToolError for those replies instead; the text
stays the tool's JSON, unchanged, so a reader (and the hosted bridge's token-190 check) can
still parse it.
"""

import json
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError


def failure_text(result: Any) -> Optional[str]:
    """The tool's reply text if it reports a failure: a top-level `error`, or `success: false`."""
    content = result[0] if isinstance(result, tuple) and result else result
    if not isinstance(content, (list, tuple)):
        return None
    for block in content:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("success") is False):
            return text
    return None


class FailuresAreErrors(FastMCP):
    """FastMCP whose tool calls that report a failure come back as isError results."""

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        result = await super().call_tool(name, arguments)
        failure = failure_text(result)
        if failure is not None:
            raise ToolError(failure)
        return result
