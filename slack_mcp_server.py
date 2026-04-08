#!/usr/bin/env python3
"""
MCP Channel server for Claude Code — bridges Slack messages via file-based inbox.

One instance per Claude Code session. Communicates via stdio (JSON-RPC).
Watches /tmp/neo/inbox/<channel_name>/ for new JSON message files and pushes
them to Claude Code as MCP channel notifications. Exposes Slack tools that
Claude Code calls to interact with Slack.

Usage:
    python slack_mcp_server.py --channel oracle --channel-id C1234567890
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from watchfiles import awatch, Change

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
# Logging — must go to stderr because stdout is the MCP stdio transport
# ---------------------------------------------------------------------------
_log_file = Path("/tmp/neo/mcp-server.log")
_log_file.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(_log_file, mode="a"),
    ],
)
logger = logging.getLogger("neo-slack")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INBOX_BASE_DIR = Path("/tmp/neo/inbox")
CHANNEL_NOTIFICATION_METHOD = "notifications/claude/channel"
INSTRUCTIONS = (
    'Messages from Slack arrive as <channel source="slack" channel_id="..." '
    'user="..." ts="...">message</channel>.\n'
    "When you receive a message, IMMEDIATELY add a :hourglass_flowing_sand: reaction "
    "using slack_add_reaction with the channel_id and ts from the message to show you're working on it.\n"
    "Reply using the slack_reply tool with the channel_id from the message.\n"
    "After replying, add a :white_check_mark: reaction to the original message "
    "using slack_add_reaction to show you're done.\n"
    "You can also create channels, canvases, lists, reactions, reminders, "
    "and upload files using the other slack_* tools."
)

# ---------------------------------------------------------------------------
# Slack client — initialized lazily so import-time doesn't fail without token
# ---------------------------------------------------------------------------
_slack_client: WebClient | None = None


def get_slack_client() -> WebClient:
    """Return the singleton Slack WebClient, creating it on first call."""
    global _slack_client
    if _slack_client is None:
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "SLACK_BOT_TOKEN environment variable is not set. "
                "Set it to your Slack bot's OAuth token before starting this server."
            )
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
                "text": {"type": "string", "description": "Message text"},
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
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="slack_create_canvas",
        description="Create a Slack canvas and post it to a channel.",
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "Slack channel ID to post in"},
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
                    "description": "Column names for the list",
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
                "timestamp": {"type": "string", "description": "Message timestamp to react to"},
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
                "time": {
                    "type": "string",
                    "description": "When to remind (natural language or unix timestamp)",
                },
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
# Tool dispatch — each handler returns a list of TextContent
# ---------------------------------------------------------------------------


def _slack_error_text(operation: str, err: SlackApiError) -> str:
    """Format a Slack API error into a human-readable message."""
    return f"Slack API error during {operation}: {err.response['error']} — {err.response.get('detail', '')}"


def _handle_slack_reply(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    kwargs: dict[str, Any] = {
        "channel": args["channel_id"],
        "text": args["text"],
    }
    if thread_ts := args.get("thread_ts"):
        kwargs["thread_ts"] = thread_ts
    try:
        resp = client.chat_postMessage(**kwargs)
        return [TextContent(type="text", text=f"Message sent (ts={resp['ts']})")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("chat.postMessage", e))]


def _handle_slack_create_channel(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        resp = client.conversations_create(name=args["name"])
        channel_id = resp["channel"]["id"]
        if topic := args.get("topic"):
            client.conversations_setTopic(channel=channel_id, topic=topic)
        if purpose := args.get("purpose"):
            client.conversations_setPurpose(channel=channel_id, purpose=purpose)
        return [TextContent(type="text", text=f"Channel created: {channel_id}")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("conversations.create", e))]


def _handle_slack_list_channels(_args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        resp = client.conversations_list(types="public_channel,private_channel", limit=200)
        channels = resp.get("channels", [])
        lines = [f"#{ch['name']} ({ch['id']})" for ch in channels]
        return [TextContent(type="text", text="\n".join(lines) if lines else "No channels found.")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("conversations.list", e))]


def _handle_slack_create_canvas(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        canvas_resp = client.api_call(
            "canvases.create",
            json={
                "title": args["title"],
                "document_content": {
                    "type": "markdown",
                    "markdown": args["content"],
                },
            },
        )
        canvas_id = canvas_resp.get("canvas_id", "unknown")
        # Share the canvas in the channel
        client.chat_postMessage(
            channel=args["channel_id"],
            text=f"Canvas: {args['title']}",
            metadata={
                "event_type": "canvas_shared",
                "event_payload": {"canvas_id": canvas_id},
            },
        )
        return [TextContent(type="text", text=f"Canvas created (id={canvas_id}) and posted to channel.")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("canvases.create", e))]


def _handle_slack_create_list(args: dict[str, Any]) -> list[TextContent]:
    """Create a Slack list via the API. This uses the lists endpoints which
    may require specific scopes and Slack plan features."""
    client = get_slack_client()
    try:
        # Build the list definition with columns
        columns_def = [{"name": col, "type": "text"} for col in args["columns"]]
        resp = client.api_call(
            "lists.create",
            json={
                "title": args["title"],
                "columns": columns_def,
            },
        )
        list_id = resp.get("list_id", "unknown")
        # Add initial items if provided
        for item_text in args.get("items", []):
            client.api_call(
                "lists.items.create",
                json={
                    "list_id": list_id,
                    "item": {"title": item_text},
                },
            )
        # Post a reference to the list in the channel
        client.chat_postMessage(
            channel=args["channel_id"],
            text=f"List created: {args['title']} (id={list_id})",
        )
        return [TextContent(type="text", text=f"List created (id={list_id}) with {len(args.get('items', []))} items.")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("lists.create", e))]


def _handle_slack_add_reaction(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        client.reactions_add(
            channel=args["channel_id"],
            timestamp=args["timestamp"],
            name=args["emoji_name"],
        )
        return [TextContent(type="text", text=f"Reaction :{args['emoji_name']}: added.")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("reactions.add", e))]


def _handle_slack_set_reminder(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        resp = client.reminders_add(text=args["text"], time=args["time"])
        reminder_id = resp.get("reminder", {}).get("id", "unknown")
        return [TextContent(type="text", text=f"Reminder set (id={reminder_id}).")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("reminders.add", e))]


def _handle_slack_upload_file(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    file_path = args["file_path"]
    if not Path(file_path).is_file():
        return [TextContent(type="text", text=f"File not found: {file_path}")]
    try:
        kwargs: dict[str, Any] = {
            "channel": args["channel_id"],
            "file": file_path,
        }
        if title := args.get("title"):
            kwargs["title"] = title
        client.files_upload_v2(**kwargs)
        return [TextContent(type="text", text=f"File uploaded: {file_path}")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("files.uploadV2", e))]


def _handle_slack_get_thread(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        resp = client.conversations_replies(
            channel=args["channel_id"],
            ts=args["thread_ts"],
        )
        messages = resp.get("messages", [])
        lines = []
        for msg in messages:
            user = msg.get("user", "unknown")
            text = msg.get("text", "")
            ts = msg.get("ts", "")
            lines.append(f"[{ts}] {user}: {text}")
        return [TextContent(type="text", text="\n".join(lines) if lines else "No messages in thread.")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("conversations.replies", e))]


def _handle_slack_pin_message(args: dict[str, Any]) -> list[TextContent]:
    client = get_slack_client()
    try:
        client.pins_add(channel=args["channel_id"], timestamp=args["timestamp"])
        return [TextContent(type="text", text=f"Message {args['timestamp']} pinned.")]
    except SlackApiError as e:
        return [TextContent(type="text", text=_slack_error_text("pins.add", e))]


# Maps tool name -> handler function
TOOL_HANDLERS: dict[str, Any] = {
    "slack_reply": _handle_slack_reply,
    "slack_create_channel": _handle_slack_create_channel,
    "slack_list_channels": _handle_slack_list_channels,
    "slack_create_canvas": _handle_slack_create_canvas,
    "slack_create_list": _handle_slack_create_list,
    "slack_add_reaction": _handle_slack_add_reaction,
    "slack_set_reminder": _handle_slack_set_reminder,
    "slack_upload_file": _handle_slack_upload_file,
    "slack_get_thread": _handle_slack_get_thread,
    "slack_pin_message": _handle_slack_pin_message,
}


# ---------------------------------------------------------------------------
# File watcher — watches inbox dir for new .json message files
# ---------------------------------------------------------------------------


# Global reference to the active MCP session — set once server.run() starts
_active_session: Any = None


async def send_channel_notification(
    write_stream: Any,
    content: str,
    meta: dict[str, Any],
) -> None:
    """Send a notifications/claude/channel JSON-RPC notification via the active session."""
    logger.info("Sending channel notification: %s", content[:80])

    if _active_session is None:
        logger.error("No active MCP session — cannot send notification")
        return

    try:
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method=CHANNEL_NOTIFICATION_METHOD,
            params={"content": content, "meta": meta},
        )
        session_message = SessionMessage(message=JSONRPCMessage(notification))
        await _active_session._write_stream.send(session_message)
        logger.info("Notification sent via session write stream")
    except Exception as e:
        logger.error("Failed to send notification: %s", e)


def parse_message_file(file_path: Path) -> dict[str, Any] | None:
    """Read, parse, and delete a JSON message file. Returns parsed dict or None on error."""
    try:
        raw = file_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read/parse %s: %s", file_path, e)
        return None
    finally:
        # Always attempt cleanup regardless of parse success
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass
    return data


async def watch_inbox(
    inbox_dir: Path,
    channel_id: str,
    write_stream: anyio.streams.memory.MemoryObjectSendStream | None = None,
) -> None:
    """Watch the inbox directory for new .json files and push channel notifications."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Watching inbox: %s", inbox_dir)

    # Process any files already present at startup
    for existing_file in sorted(inbox_dir.glob("*.json")):
        await _process_message_file(existing_file, channel_id, write_stream)

    try:
        async for changes in awatch(inbox_dir):
            for change_type, path_str in changes:
                path = Path(path_str)
                if change_type == Change.added and path.suffix == ".json":
                    await _process_message_file(path, channel_id, write_stream)
    except Exception as e:
        logger.error("File watcher crashed: %s", e)
        raise


async def _process_message_file(
    file_path: Path,
    channel_id: str,
    write_stream: anyio.streams.memory.MemoryObjectSendStream | None = None,
) -> None:
    """Parse a single message file and emit a channel notification."""
    data = parse_message_file(file_path)
    if data is None:
        return

    # Extract message content and metadata from the file
    content = data.get("text", "")
    meta = {
        "channel_id": data.get("channel_id", channel_id),
        "user": data.get("user", "unknown"),
        "ts": data.get("ts", ""),
        "thread_ts": data.get("thread_ts", ""),
    }
    logger.info("Incoming message from %s: %s", meta["user"], content[:80])
    await send_channel_notification(write_stream, content, meta)


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------


def create_server() -> Server:
    """Create and configure the MCP server with tool handlers."""
    server = Server(
        name="neo-slack",
        instructions=INSTRUCTIONS,
    )

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        # Run synchronous Slack SDK calls in a thread to avoid blocking the event loop
        return await asyncio.get_event_loop().run_in_executor(
            None, handler, arguments or {}
        )

    return server


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MCP Channel server for Slack — bridges messages to Claude Code"
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel name (matches inbox directory name under /tmp/neo/inbox/)",
    )
    parser.add_argument(
        "--channel-id",
        required=True,
        help="Slack channel ID (e.g. C1234567890)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    inbox_dir = INBOX_BASE_DIR / args.channel
    channel_id = args.channel_id

    server = create_server()
    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={"claude/channel": {}},
    )

    from contextlib import AsyncExitStack
    from mcp.server.session import ServerSession

    async with stdio_server() as (read_stream, write_stream):
        async with AsyncExitStack() as stack:
            global _active_session
            lifespan_context = await stack.enter_async_context(server.lifespan(server))
            session = await stack.enter_async_context(
                ServerSession(
                    read_stream,
                    write_stream,
                    init_options,
                )
            )
            _active_session = session
            logger.info("MCP session established, starting file watcher")

            async with anyio.create_task_group() as tg:
                tg.start_soon(watch_inbox, inbox_dir, channel_id)

                async for message in session.incoming_messages:
                    logger.debug("Received message: %s", message)
                    tg.start_soon(server._handle_message, message, session, lifespan_context, False)
            # When server.run() returns (client disconnected), cancel the watcher
            tg.cancel_scope.cancel()


if __name__ == "__main__":
    asyncio.run(main())
