#!/usr/bin/env python3
"""Neo — Slack-controlled Claude Code agent system.

Single process that connects to Slack via Socket Mode, routes messages
to Claude Code sessions (one per project), and posts responses back.

Usage:
    python neo.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("neo")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
ENV_PATH = SCRIPT_DIR / ".env"

load_dotenv(ENV_PATH)

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")

if not BOT_TOKEN.startswith("xoxb-") or not APP_TOKEN.startswith("xapp-"):
    logger.error("Missing or invalid SLACK_BOT_TOKEN / SLACK_APP_TOKEN in .env")
    sys.exit(1)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Session Manager — tracks claude -p session IDs per project
# ---------------------------------------------------------------------------

SESSION_FILE = SCRIPT_DIR / ".sessions.json"


class SessionManager:
    """Manages Claude Code session IDs and per-project locks."""

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._load()

    def _load(self):
        if SESSION_FILE.exists():
            try:
                self.sessions = json.loads(SESSION_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                self.sessions = {}

    def _save(self):
        SESSION_FILE.write_text(json.dumps(self.sessions, indent=2))

    def get_lock(self, project: str) -> asyncio.Lock:
        if project not in self.locks:
            self.locks[project] = asyncio.Lock()
        return self.locks[project]

    async def send_message(self, project: str, project_path: str, text: str) -> str:
        """Send a message to a Claude Code session and return the response."""
        lock = self.get_lock(project)
        async with lock:
            return await self._run_claude(project, project_path, text)

    async def send_message_streaming(
        self,
        project: str,
        project_path: str,
        text: str,
        client: AsyncWebClient,
        channel_id: str,
        thread_ts: str,
    ) -> None:
        """Send a message to Claude and stream the response to Slack."""
        lock = self.get_lock(project)
        async with lock:
            await self._run_claude_streaming(
                project, project_path, text, client, channel_id, thread_ts
            )

    async def _run_claude_streaming(
        self,
        project: str,
        project_path: str,
        text: str,
        client: AsyncWebClient,
        channel_id: str,
        thread_ts: str,
    ) -> None:
        cmd = [
            "claude",
            "-p", text,
            "--output-format", "stream-json",
            "--model", "opus",
            "--permission-mode", "bypassPermissions",
            "--verbose",
            "--include-partial-messages",
        ]

        if project in self.sessions:
            cmd.extend(["--resume", self.sessions[project]])

        logger.info("Running claude for %s: %s", project, text[:80])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=project_path,
            )
        except Exception as e:
            logger.error("Failed to start claude for %s: %s", project, e)
            await client.chat_postMessage(
                channel=channel_id, text=f"Error starting Claude: {e}", thread_ts=thread_ts
            )
            return

        # Track process so it can be cancelled via ❌ reaction
        running_procs[thread_ts] = proc

        # Post initial message that we'll update with streamed content
        resp = await client.chat_postMessage(
            channel=channel_id, text="_Thinking..._", thread_ts=thread_ts
        )
        msg_ts = resp["ts"]

        accumulated = ""
        last_update = 0.0
        update_interval = 1.0  # Update Slack every second for visible streaming
        session_id = None

        async def _read_stream():
            nonlocal accumulated, session_id, last_update
            async for line in proc.stdout:
                decoded = line.decode().strip()
                if not decoded:
                    continue
                try:
                    event = json.loads(decoded)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                # Capture session ID
                if etype == "system" and "session_id" in event:
                    session_id = event["session_id"]

                # Capture streaming text deltas
                if etype == "stream_event":
                    inner = event.get("event", {})
                    inner_type = inner.get("type", "")
                    if inner_type == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            accumulated += delta.get("text", "")

                # Capture final result (overrides accumulated with clean version)
                if etype == "result":
                    if "session_id" in event:
                        session_id = event["session_id"]
                    result_text = event.get("result", "")
                    if result_text:
                        accumulated = result_text

                # Periodically update the Slack message
                now = asyncio.get_event_loop().time()
                if accumulated and (now - last_update) > update_interval:
                    try:
                        display = accumulated[:3900]
                        await client.chat_update(
                            channel=channel_id, ts=msg_ts, text=display
                        )
                        last_update = now
                    except Exception:
                        pass

        await _read_stream()
        await proc.wait()

        # Clean up process tracking
        running_procs.pop(thread_ts, None)

        # Save session for continuity
        if session_id:
            self.sessions[project] = session_id
            self._save()

        # Final update with complete response
        if accumulated:
            chunks = split_slack_message(accumulated)
            try:
                await client.chat_update(
                    channel=channel_id, ts=msg_ts, text=chunks[0]
                )
            except Exception:
                pass
            # Post additional chunks as new messages if response is very long
            for chunk in chunks[1:]:
                await client.chat_postMessage(
                    channel=channel_id, text=chunk, thread_ts=thread_ts
                )
        else:
            await client.chat_update(
                channel=channel_id, ts=msg_ts, text="No response from Claude."
            )

        if proc.returncode != 0:
            stderr_text = (await proc.stderr.read()).decode().strip()
            logger.error("Claude exited %d for %s: %s", proc.returncode, project, stderr_text[:200])

    def reset_session(self, project: str):
        """Clear a project's session so next message starts fresh."""
        self.sessions.pop(project, None)
        self._save()


# ---------------------------------------------------------------------------
# Slack App
# ---------------------------------------------------------------------------

FILES_DIR = Path("/tmp/neo/files")

app = AsyncApp(token=BOT_TOKEN)
sessions = SessionManager()

# Track running Claude processes: message_ts -> subprocess
running_procs: dict[str, asyncio.subprocess.Process] = {}

# Resolved at startup: channel_id -> (project_name, project_path)
channel_routing: dict[str, tuple[str, str]] = {}
bot_user_id: str = ""


def split_slack_message(text: str, max_len: int = 3900) -> list[str]:
    """Split a long message into chunks that fit Slack's limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


@app.event("message")
async def handle_message(event: dict, client: AsyncWebClient):
    await _process(event, client)


@app.event("app_mention")
async def handle_mention(event: dict, client: AsyncWebClient):
    await _process(event, client)


@app.event("reaction_added")
async def handle_reaction(event: dict, client: AsyncWebClient):
    """Cancel a running Claude process when ❌ is added to the original message."""
    if event.get("reaction") != "x":
        return
    msg_ts = event.get("item", {}).get("ts", "")
    proc = running_procs.get(msg_ts)
    if proc and proc.returncode is None:
        proc.kill()
        running_procs.pop(msg_ts, None)
        channel_id = event.get("item", {}).get("channel", "")
        logger.info("Cancelled Claude process for message %s", msg_ts)
        await client.chat_postMessage(
            channel=channel_id, text="_Cancelled._", thread_ts=msg_ts
        )


async def _download_files(
    event: dict, client: AsyncWebClient, project_name: str
) -> list[str]:
    """Download files attached to a Slack message. Returns list of local paths."""
    files = event.get("files", [])
    if not files:
        return []

    project_dir = FILES_DIR / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    for f in files:
        url = f.get("url_private")
        name = f.get("name", "unknown")
        if not url:
            continue

        local_path = project_dir / name
        try:
            # Slack files need the bot token as a Bearer header
            import aiohttp
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {BOT_TOKEN}"}
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        local_path.write_bytes(await resp.read())
                        downloaded.append(str(local_path))
                        logger.info("Downloaded file: %s -> %s", name, local_path)
                    else:
                        logger.warning("Failed to download %s: HTTP %d", name, resp.status)
        except Exception as e:
            logger.error("Error downloading %s: %s", name, e)

    return downloaded


async def _process(event: dict, client: AsyncWebClient):
    """Handle an incoming Slack message."""
    # Ignore bots and our own messages
    if event.get("bot_id") or not event.get("user"):
        return
    if event.get("user") == bot_user_id:
        return
    # Ignore message subtypes except file_share and thread_broadcast
    subtype = event.get("subtype")
    if subtype and subtype not in {"file_share", "thread_broadcast"}:
        return

    channel_id = event.get("channel", "")
    route = channel_routing.get(channel_id)
    if not route:
        return

    project_name, project_path = route
    text = event.get("text", "").strip()
    ts = event.get("ts", "")

    # Strip bot mention from text
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

    # Download any attached files
    file_paths = await _download_files(event, client, project_name)

    # Build the prompt — include file references
    if file_paths and text:
        file_refs = "\n".join(f"- {p}" for p in file_paths)
        text = f"{text}\n\nThe user shared these files (use the Read tool to view them):\n{file_refs}"
    elif file_paths and not text:
        file_refs = "\n".join(f"- {p}" for p in file_paths)
        text = f"The user shared these files. Please analyze them:\n{file_refs}"
    elif not text:
        return

    logger.info("Message from %s in #%s: %s", event.get("user"), project_name, text[:80])

    # Ack with eyes emoji
    try:
        await client.reactions_add(channel=channel_id, timestamp=ts, name="eyes")
    except Exception:
        pass

    # Add hourglass to show we're working
    try:
        await client.reactions_add(channel=channel_id, timestamp=ts, name="hourglass_flowing_sand")
    except Exception:
        pass

    # Run Claude with streaming output to Slack
    await sessions.send_message_streaming(
        project_name, project_path, text, client, channel_id, ts
    )

    # Remove hourglass, add checkmark
    try:
        await client.reactions_remove(channel=channel_id, timestamp=ts, name="hourglass_flowing_sand")
    except Exception:
        pass
    try:
        await client.reactions_add(channel=channel_id, timestamp=ts, name="white_check_mark")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def resolve_channels(client: AsyncWebClient, config: dict) -> dict[str, tuple[str, str]]:
    """Map Slack channel IDs to (project_name, project_path) from config."""
    # Collect configured channel names
    name_to_session: dict[str, tuple[str, str]] = {}
    for session_name, session_cfg in config.get("sessions", {}).items():
        ch = session_cfg.get("channel")
        if ch:
            name_to_session[ch] = (session_name, session_cfg["path"])

    # Paginate through Slack channels
    routing: dict[str, tuple[str, str]] = {}
    cursor = None
    while True:
        resp = await client.conversations_list(
            types="public_channel,private_channel", limit=200, cursor=cursor
        )
        for ch in resp.get("channels", []):
            name = ch.get("name", "")
            if name in name_to_session:
                routing[ch["id"]] = name_to_session[name]
                logger.info("Routed #%s (%s) -> %s", name, ch["id"], name_to_session[name][0])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # Warn about missing channels
    resolved = {v[0] for v in routing.values()}
    for name, (sname, _) in name_to_session.items():
        if sname not in resolved:
            logger.warning("Channel #%s not found in Slack for session '%s'", name, sname)

    return routing


async def main():
    global channel_routing, bot_user_id

    config = load_config()
    client = AsyncWebClient(token=BOT_TOKEN)

    # Resolve bot identity
    auth = await client.auth_test()
    bot_user_id = auth.get("user_id", "")
    logger.info("Bot identity: %s (user_id=%s)", config.get("bot_name", "Neo"), bot_user_id)

    # Resolve channel routing
    channel_routing = await resolve_channels(client, config)
    logger.info("Routing %d channel(s)", len(channel_routing))

    if not channel_routing:
        logger.error("No channels resolved — check config.yaml and bot channel membership")
        sys.exit(1)

    # Start Socket Mode
    handler = AsyncSocketModeHandler(app, APP_TOKEN)
    logger.info("Starting Neo...")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
