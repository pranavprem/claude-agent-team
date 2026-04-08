#!/usr/bin/env python3
"""
Slim Slack relay — single Socket Mode connection, routes messages to per-project
channel servers via HTTP POST.

Replaces the old filesystem-based relay. Each project's MCP channel server
listens on a local port. This relay matches incoming Slack messages by channel
ID and POSTs them to the right port.

Usage:
    python slack-relay.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

import yaml
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("slack-relay")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
ENV_PATH = SCRIPT_DIR / ".env"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_tokens() -> tuple[str, str]:
    load_dotenv(ENV_PATH)
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not bot_token.startswith("xoxb-"):
        logger.error("SLACK_BOT_TOKEN missing or invalid")
        sys.exit(1)
    if not app_token.startswith("xapp-"):
        logger.error("SLACK_APP_TOKEN missing or invalid")
        sys.exit(1)
    return bot_token, app_token


# ---------------------------------------------------------------------------
# Channel ID -> port mapping
# ---------------------------------------------------------------------------


def build_route_table(client: WebClient, config: dict) -> dict[str, dict]:
    """Build a mapping of Slack channel ID -> {name, port} from config.

    Resolves channel names to IDs via the Slack API.
    """
    sessions = config.get("sessions", {})

    # Collect channel names and ports from config
    name_to_info: dict[str, dict] = {}
    for sess in sessions.values():
        channel_name = sess.get("channel")
        port = sess.get("port")
        if channel_name and port:
            name_to_info[channel_name] = {"name": channel_name, "port": port}

    # Resolve channel names to Slack IDs
    channel_id_to_info: dict[str, dict] = {}
    cursor = None
    target_names = set(name_to_info.keys())

    while True:
        resp = client.conversations_list(
            types="public_channel,private_channel", limit=200, cursor=cursor
        )
        for ch in resp.get("channels", []):
            if ch["name"] in target_names:
                channel_id_to_info[ch["id"]] = name_to_info[ch["name"]]

        if target_names <= {info["name"] for info in channel_id_to_info.values()}:
            break
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # Warn about unresolved channels
    resolved = {info["name"] for info in channel_id_to_info.values()}
    for name in target_names - resolved:
        logger.warning("Channel #%s not found in Slack — ensure bot is invited", name)

    return channel_id_to_info


# ---------------------------------------------------------------------------
# User name cache
# ---------------------------------------------------------------------------


class UserNameCache:
    def __init__(self, app: App) -> None:
        self._app = app
        self._cache: dict[str, str] = {}

    def get(self, user_id: str) -> str:
        if user_id in self._cache:
            return self._cache[user_id]
        try:
            resp = self._app.client.users_info(user=user_id)
            if resp["ok"]:
                user = resp["user"]
                name = (
                    user.get("profile", {}).get("display_name")
                    or user.get("real_name")
                    or user_id
                )
                self._cache[user_id] = name
                return name
        except Exception:
            logger.warning("Failed to resolve user %s", user_id)
        self._cache[user_id] = user_id
        return user_id


# ---------------------------------------------------------------------------
# HTTP POST to channel server
# ---------------------------------------------------------------------------


def post_to_channel_server(port: int, payload: dict) -> None:
    """POST a JSON payload to a per-project channel server."""
    url = f"http://127.0.0.1:{port}/message"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as e:
        logger.error("Failed to POST to port %d: %s", port, e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    config = load_config()
    bot_token, app_token = load_tokens()

    app = App(token=bot_token)

    route_table = build_route_table(app.client, config)
    if not route_table:
        logger.error("No channels resolved — exiting")
        sys.exit(1)

    logger.info(
        "Routing %d channel(s): %s",
        len(route_table),
        ", ".join(f"#{info['name']}->:{info['port']}" for info in route_table.values()),
    )

    user_cache = UserNameCache(app)

    # Resolve bot's own user ID to ignore self-messages
    auth_resp = app.client.auth_test()
    bot_user_id = auth_resp.get("user_id", "")
    logger.info("Bot user_id: %s", bot_user_id)

    def _process_event(event: dict) -> None:
        subtype = event.get("subtype")
        if subtype and subtype not in {"file_share", "thread_broadcast"}:
            return
        user_id = event.get("user", "")
        if event.get("bot_id") or not user_id or user_id == bot_user_id:
            return

        channel_id = event.get("channel", "")
        route = route_table.get(channel_id)
        if not route:
            return

        user_name = user_cache.get(user_id)

        payload = {
            "channel_id": channel_id,
            "channel_name": route["name"],
            "user_id": user_id,
            "user_name": user_name,
            "text": event.get("text", ""),
            "ts": event.get("ts", ""),
            "thread_ts": event.get("thread_ts"),
        }

        logger.info("Routing %s in #%s -> :%d", user_name, route["name"], route["port"])
        post_to_channel_server(route["port"], payload)

    @app.event("message")
    def handle_message(event: dict, say) -> None:  # noqa: ARG001
        _process_event(event)

    @app.event("app_mention")
    def handle_app_mention(event: dict, say) -> None:  # noqa: ARG001
        _process_event(event)

    handler = SocketModeHandler(app, app_token)
    logger.info("Starting Socket Mode connection...")
    handler.start()


if __name__ == "__main__":
    main()
