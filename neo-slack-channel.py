#!/usr/bin/env python3
"""
MCP Channel server for Claude Code — Slack bridge via HTTP.

Each Claude Code session spawns one instance of this server as a subprocess.
It listens on a local HTTP port for messages from the Slack relay, and pushes
them into Claude Code as MCP channel notifications. It also exposes Slack tools
(reply, reaction, etc.) so Claude can interact with Slack directly.

Supports permission relay: when Claude Code needs tool approval, it forwards
the prompt to Slack so you can approve/deny from your phone.

Usage (spawned by Claude Code via .mcp.json, not run manually):
    python neo-slack-channel.py --port 9100 --channel-id C1234567890
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from http import HTTPStatus
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
    TextContent,
    Tool,
)

# ---------------------------------------------------------------------------
# Logging — stderr only, stdout is the MCP stdio transport
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("neo-slack-channel")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHANNEL_NOTIFICATION = "notifications/claude/channel"
PERMISSION_NOTIFICATION = "notifications/claude/channel/permission"
PERMISSION_REQUEST = "notifications/claude/channel/permission_request"

# Regex for permission verdicts: "yes abcde" or "no abcde"
# ID alphabet: lowercase a-z minus 'l' (5 chars)
PERMISSION_REPLY_RE = re.compile(r"^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$", re.IGNORECASE)

INSTRUCTIONS = (
    "CRITICAL: The user cannot see the terminal. The ONLY way they receive your "
    "response is if you call the slack_reply tool. You MUST call slack_reply for "
    "every single Slack message — no exceptions.\n\n"
    'Messages from Slack arrive as <channel source="neo-slack" channel_id="..." '
    'user="..." ts="...">message</channel>.\n'
    "When you receive a message:\n"
    "1. IMMEDIATELY add a :hourglass_flowing_sand: reaction using slack_add_reaction "
    "with the channel_id and ts from the message.\n"
    "2. Do whatever work the message asks for.\n"
    "3. Reply using the slack_reply tool with the channel_id from the message.\n"
    "4. After replying, add a :white_check_mark: reaction to the original message "
    "using slack_add_reaction.\n\n"
    "You can also create channels, canvases, lists, reactions, reminders, "
    "and upload files using the other slack_* tools."
)

# ---------------------------------------------------------------------------
# Slack client
# ---------------------------------------------------------------------------

_slack_client: WebClient | None = None


def get_slack_client() -> WebClient:
    global _slack_client
    if _slack_client is None:
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError("SLACK_BOT_TOKEN not set")
        _slack_client = WebClient(token=token)
    return _slack_client


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="slack_reply",
        description="Post a message to a Slack channel or thread.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "text": {"type": "string", "description": "Message text (Slack mrkdwn)"},
                "thread_ts": {
                    "type": "string",
                    "description": "Thread timestamp to reply in (optional)",
                },
            },
            "required": ["channel_id", "text"],
        },
    ),
    Tool(
        name="slack_create_channel",
        description="Create a new Slack channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Channel name (lowercase, no spaces)"},
                "topic": {"type": "string", "description": "Channel topic (optional)"},
                "purpose": {"type": "string", "description": "Channel purpose (optional)"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="slack_list_channels",
        description="List all Slack channels the bot can see.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="slack_create_canvas",
        description="Create a Slack canvas and post it to a channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "title": {"type": "string", "description": "Canvas title"},
                "content": {"type": "string", "description": "Canvas content (markdown)"},
            },
            "required": ["channel_id", "title", "content"],
        },
    ),
    Tool(
        name="slack_create_list",
        description="Create a Slack list (kanban board) in a channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "title": {"type": "string", "description": "List title"},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Column names",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional initial items",
                },
            },
            "required": ["channel_id", "title", "columns"],
        },
    ),
    Tool(
        name="slack_add_reaction",
        description="Add an emoji reaction to a Slack message.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "timestamp": {"type": "string", "description": "Message timestamp"},
                "emoji_name": {
                    "type": "string",
                    "description": "Emoji name without colons (e.g. 'thumbsup')",
                },
            },
            "required": ["channel_id", "timestamp", "emoji_name"],
        },
    ),
    Tool(
        name="slack_set_reminder",
        description="Create a Slack reminder.",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Reminder text"},
                "time": {"type": "string", "description": "When to remind (natural language or unix timestamp)"},
            },
            "required": ["text", "time"],
        },
    ),
    Tool(
        name="slack_upload_file",
        description="Upload a file to a Slack channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "file_path": {"type": "string", "description": "Local path to the file"},
                "title": {"type": "string", "description": "File title (optional)"},
            },
            "required": ["channel_id", "file_path"],
        },
    ),
    Tool(
        name="slack_get_thread",
        description="Read all messages in a Slack thread.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "thread_ts": {"type": "string", "description": "Thread parent timestamp"},
            },
            "required": ["channel_id", "thread_ts"],
        },
    ),
    Tool(
        name="slack_pin_message",
        description="Pin a message in a Slack channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID"},
                "timestamp": {"type": "string", "description": "Message timestamp to pin"},
            },
            "required": ["channel_id", "timestamp"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _err(op: str, e: SlackApiError) -> list[TextContent]:
    return [TextContent(type="text", text=f"Slack error ({op}): {e.response['error']}")]


def handle_slack_reply(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    kwargs: dict[str, Any] = {"channel": args["channel_id"], "text": args["text"]}
    if thread_ts := args.get("thread_ts"):
        kwargs["thread_ts"] = thread_ts
    try:
        resp = client.chat_postMessage(**kwargs)
        return [TextContent(type="text", text=f"sent (ts={resp['ts']})")]
    except SlackApiError as e:
        return _err("chat.postMessage", e)


def handle_slack_create_channel(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        resp = client.conversations_create(name=args["name"])
        cid = resp["channel"]["id"]
        if topic := args.get("topic"):
            client.conversations_setTopic(channel=cid, topic=topic)
        if purpose := args.get("purpose"):
            client.conversations_setPurpose(channel=cid, purpose=purpose)
        return [TextContent(type="text", text=f"Channel created: {cid}")]
    except SlackApiError as e:
        return _err("conversations.create", e)


def handle_slack_list_channels(_args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        resp = client.conversations_list(types="public_channel,private_channel", limit=200)
        lines = [f"#{ch['name']} ({ch['id']})" for ch in resp.get("channels", [])]
        return [TextContent(type="text", text="\n".join(lines) or "No channels found.")]
    except SlackApiError as e:
        return _err("conversations.list", e)


def handle_slack_create_canvas(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        canvas_resp = client.api_call(
            "canvases.create",
            json={
                "title": args["title"],
                "document_content": {"type": "markdown", "markdown": args["content"]},
            },
        )
        canvas_id = canvas_resp.get("canvas_id", "unknown")
        client.chat_postMessage(
            channel=args["channel_id"],
            text=f"Canvas: {args['title']}",
            metadata={"event_type": "canvas_shared", "event_payload": {"canvas_id": canvas_id}},
        )
        return [TextContent(type="text", text=f"Canvas created (id={canvas_id})")]
    except SlackApiError as e:
        return _err("canvases.create", e)


def handle_slack_create_list(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        columns_def = [{"name": col, "type": "text"} for col in args["columns"]]
        resp = client.api_call("lists.create", json={"title": args["title"], "columns": columns_def})
        list_id = resp.get("list_id", "unknown")
        for item_text in args.get("items", []):
            client.api_call("lists.items.create", json={"list_id": list_id, "item": {"title": item_text}})
        client.chat_postMessage(channel=args["channel_id"], text=f"List created: {args['title']} (id={list_id})")
        return [TextContent(type="text", text=f"List created (id={list_id})")]
    except SlackApiError as e:
        return _err("lists.create", e)


def handle_slack_add_reaction(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        client.reactions_add(channel=args["channel_id"], timestamp=args["timestamp"], name=args["emoji_name"])
        return [TextContent(type="text", text=f":{args['emoji_name']}: added")]
    except SlackApiError as e:
        return _err("reactions.add", e)


def handle_slack_set_reminder(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        resp = client.reminders_add(text=args["text"], time=args["time"])
        rid = resp.get("reminder", {}).get("id", "unknown")
        return [TextContent(type="text", text=f"Reminder set (id={rid})")]
    except SlackApiError as e:
        return _err("reminders.add", e)


def handle_slack_upload_file(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    from pathlib import Path

    if not Path(args["file_path"]).is_file():
        return [TextContent(type="text", text=f"File not found: {args['file_path']}")]
    try:
        kwargs: dict[str, Any] = {"channel": args["channel_id"], "file": args["file_path"]}
        if title := args.get("title"):
            kwargs["title"] = title
        client.files_upload_v2(**kwargs)
        return [TextContent(type="text", text=f"Uploaded: {args['file_path']}")]
    except SlackApiError as e:
        return _err("files.uploadV2", e)


def handle_slack_get_thread(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        resp = client.conversations_replies(channel=args["channel_id"], ts=args["thread_ts"])
        lines = [f"[{m.get('ts', '')}] {m.get('user', '?')}: {m.get('text', '')}" for m in resp.get("messages", [])]
        return [TextContent(type="text", text="\n".join(lines) or "No messages in thread.")]
    except SlackApiError as e:
        return _err("conversations.replies", e)


def handle_slack_pin_message(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        client.pins_add(channel=args["channel_id"], timestamp=args["timestamp"])
        return [TextContent(type="text", text=f"Pinned {args['timestamp']}")]
    except SlackApiError as e:
        return _err("pins.add", e)


TOOL_HANDLERS = {
    "slack_reply": handle_slack_reply,
    "slack_create_channel": handle_slack_create_channel,
    "slack_list_channels": handle_slack_list_channels,
    "slack_create_canvas": handle_slack_create_canvas,
    "slack_create_list": handle_slack_create_list,
    "slack_add_reaction": handle_slack_add_reaction,
    "slack_set_reminder": handle_slack_set_reminder,
    "slack_upload_file": handle_slack_upload_file,
    "slack_get_thread": handle_slack_get_thread,
    "slack_pin_message": handle_slack_pin_message,
}


# ---------------------------------------------------------------------------
# MCP session state — set once the server starts
# ---------------------------------------------------------------------------

_active_session: Any = None
_channel_id: str = ""


async def send_channel_notification(content: str, meta: dict[str, str]) -> None:
    """Push a channel notification into Claude Code."""
    if _active_session is None:
        logger.error("No active MCP session")
        return
    try:
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method=CHANNEL_NOTIFICATION,
            params={"content": content, "meta": meta},
        )
        await _active_session._write_stream.send(
            SessionMessage(message=JSONRPCMessage(notification))
        )
        logger.info("Channel notification sent: %s", content[:80])
    except Exception as e:
        logger.error("Failed to send notification: %s", e)


async def send_permission_verdict(request_id: str, behavior: str) -> None:
    """Send a permission relay verdict (allow/deny) back to Claude Code."""
    if _active_session is None:
        logger.error("No active MCP session")
        return
    try:
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method=PERMISSION_NOTIFICATION,
            params={"request_id": request_id, "behavior": behavior},
        )
        await _active_session._write_stream.send(
            SessionMessage(message=JSONRPCMessage(notification))
        )
        logger.info("Permission verdict sent: %s -> %s", request_id, behavior)
    except Exception as e:
        logger.error("Failed to send permission verdict: %s", e)


# ---------------------------------------------------------------------------
# HTTP server — receives messages from the relay
# ---------------------------------------------------------------------------


async def handle_http_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle an incoming HTTP request from the relay."""
    try:
        # Read the request line
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not request_line:
            writer.close()
            return

        parts = request_line.decode().strip().split()
        if len(parts) < 2:
            writer.close()
            return

        method, path = parts[0], parts[1]

        # Read headers
        content_length = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if line in (b"\r\n", b"\n", b""):
                break
            header = line.decode().strip().lower()
            if header.startswith("content-length:"):
                content_length = int(header.split(":", 1)[1].strip())

        # Read body
        body = b""
        if content_length > 0:
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

        # Health check
        if method == "GET" and path == "/health":
            response = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            return

        # Only accept POST /message
        if method != "POST" or path != "/message":
            response = "HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nnot found"
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            return

        # Parse the message JSON
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            response = "HTTP/1.1 400 Bad Request\r\nContent-Length: 10\r\n\r\nbad json\r\n"
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            return

        text = data.get("text", "")
        user = data.get("user_name", data.get("user_id", "unknown"))
        ts = data.get("ts", "")
        thread_ts = data.get("thread_ts", "")
        channel_id = data.get("channel_id", _channel_id)

        # Check if this is a permission verdict
        m = PERMISSION_REPLY_RE.match(text)
        if m:
            verdict = "allow" if m.group(1).lower().startswith("y") else "deny"
            await send_permission_verdict(m.group(2).lower(), verdict)
        else:
            meta = {
                "channel_id": channel_id,
                "user": user,
                "ts": ts,
            }
            if thread_ts:
                meta["thread_ts"] = thread_ts
            await send_channel_notification(text, meta)

        response = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        writer.write(response.encode())
        await writer.drain()

    except Exception as e:
        logger.error("HTTP handler error: %s", e)
        try:
            response = "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 5\r\n\r\nerror"
            writer.write(response.encode())
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_http_server(port: int) -> None:
    """Start the local HTTP server that receives messages from the relay."""
    server = await asyncio.start_server(handle_http_request, "127.0.0.1", port)
    addr = server.sockets[0].getsockname()
    logger.info("HTTP server listening on %s:%d", addr[0], addr[1])
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# Permission relay handler — Claude Code sends these when a tool needs approval
# ---------------------------------------------------------------------------


async def handle_permission_request(params: dict[str, Any]) -> None:
    """Forward a permission prompt to Slack so the user can approve/deny remotely."""
    request_id = params.get("request_id", "")
    tool_name = params.get("tool_name", "")
    description = params.get("description", "")
    input_preview = params.get("input_preview", "")

    client = get_slack_client()
    prompt_text = (
        f":warning: *Permission request* (`{request_id}`)\n"
        f"Tool: `{tool_name}`\n"
        f"Action: {description}\n"
    )
    if input_preview:
        prompt_text += f"```{input_preview[:500]}```\n"
    prompt_text += f'\nReply `yes {request_id}` or `no {request_id}`'

    try:
        client.chat_postMessage(channel=_channel_id, text=prompt_text)
        logger.info("Permission prompt sent to Slack: %s %s", tool_name, request_id)
    except SlackApiError as e:
        logger.error("Failed to send permission prompt: %s", e)


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------


def create_server() -> Server:
    server = Server(name="neo-slack", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        return await asyncio.get_event_loop().run_in_executor(None, handler, arguments or {})

    return server


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Neo Slack MCP channel server")
    parser.add_argument("--port", type=int, required=True, help="HTTP port to listen on")
    parser.add_argument("--channel-id", required=True, help="Slack channel ID")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    global _channel_id
    _channel_id = args.channel_id

    server = create_server()
    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={
            "claude/channel": {},
            "claude/channel/permission": {},
        },
    )

    from contextlib import AsyncExitStack
    from mcp.server.session import ServerSession
    import anyio

    async with stdio_server() as (read_stream, write_stream):
        async with AsyncExitStack() as stack:
            global _active_session
            lifespan_context = await stack.enter_async_context(server.lifespan(server))
            session = await stack.enter_async_context(
                ServerSession(read_stream, write_stream, init_options)
            )
            _active_session = session
            logger.info("MCP session established (channel=%s, port=%d)", _channel_id, args.port)

            # Run HTTP server and MCP message handler concurrently
            http_task = asyncio.create_task(start_http_server(args.port))

            try:
                async with anyio.create_task_group() as tg:
                    async for message in session.incoming_messages:
                        # Check for permission_request notifications from Claude Code
                        msg_obj = message.message
                        if hasattr(msg_obj, "root"):
                            msg_obj = msg_obj.root
                        if hasattr(msg_obj, "method") and msg_obj.method == PERMISSION_REQUEST:
                            params = getattr(msg_obj, "params", {})
                            if isinstance(params, dict):
                                tg.start_soon(handle_permission_request, params)
                            continue

                        # Dispatch normal MCP messages (tool calls, etc.)
                        tg.start_soon(
                            server._handle_message, message, session, lifespan_context, False
                        )
            finally:
                http_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
