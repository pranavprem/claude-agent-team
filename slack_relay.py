"""Slack relay — listens to all configured channels via Socket Mode and
writes each human message as a JSON file to the per-channel inbox directory.

Each MCP server instance watches its own channel directory for new files.

Usage:
    python slack_relay.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------------------------------------------------------------------
# Logging — all output goes to stderr so stdout stays clean for piping
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("slack_relay")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_config() -> dict:
    """Load and validate config.yaml."""
    if not CONFIG_PATH.exists():
        logger.error("Config file not found: %s", CONFIG_PATH)
        sys.exit(1)

    with open(CONFIG_PATH) as fh:
        config = yaml.safe_load(fh)

    if not config or "sessions" not in config:
        logger.error("Invalid config — missing 'sessions' key in %s", CONFIG_PATH)
        sys.exit(1)

    return config


def load_tokens() -> tuple[str, str]:
    """Load Slack tokens from .env. Exits on missing values."""
    load_dotenv(ENV_PATH)

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")

    if not bot_token.startswith("xoxb-"):
        logger.error(
            "SLACK_BOT_TOKEN is missing or invalid — "
            "must start with 'xoxb-'. Check your .env file."
        )
        sys.exit(1)

    if not app_token.startswith("xapp-"):
        logger.error(
            "SLACK_APP_TOKEN is missing or invalid — "
            "must start with 'xapp-'. Check your .env file."
        )
        sys.exit(1)

    return bot_token, app_token


# ---------------------------------------------------------------------------
# Channel resolution — map Slack channel IDs to config channel names
# ---------------------------------------------------------------------------


def build_channel_map(app: App, config: dict) -> dict[str, str]:
    """Build a mapping of Slack channel ID -> config channel name.

    Fetches all conversations the bot is a member of and matches them
    against the channel names declared in config.yaml sessions.
    """
    # Collect the set of channel names we care about from config
    configured_names: dict[str, str] = {}
    for session in config["sessions"].values():
        channel_name = session.get("channel")
        if channel_name:
            configured_names[channel_name] = channel_name

    # Paginate through all channels the bot has joined
    channel_id_to_name: dict[str, str] = {}
    cursor = None

    while True:
        kwargs: dict = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor

        response = app.client.conversations_list(**kwargs)

        if not response["ok"]:
            logger.error(
                "conversations.list failed: %s", response.get("error", "unknown")
            )
            break

        for channel in response.get("channels", []):
            name = channel.get("name", "")
            cid = channel.get("id", "")
            if name in configured_names:
                channel_id_to_name[cid] = name
                logger.info("Mapped channel: #%s -> %s", name, cid)

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # Warn about any configured channels we couldn't find
    resolved_names = set(channel_id_to_name.values())
    for name in configured_names:
        if name not in resolved_names:
            logger.warning(
                "Channel #%s is in config but was not found in Slack — "
                "make sure the bot is invited to the channel.",
                name,
            )

    return channel_id_to_name


# ---------------------------------------------------------------------------
# User name cache — avoids repeated API calls for the same user
# ---------------------------------------------------------------------------


class UserNameCache:
    """Lazy cache for Slack user ID -> display name resolution."""

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
                # Prefer display_name, fall back to real_name, then user ID
                name = (
                    user.get("profile", {}).get("display_name")
                    or user.get("real_name")
                    or user_id
                )
                self._cache[user_id] = name
                return name
        except Exception:
            logger.warning("Failed to resolve user name for %s", user_id)

        self._cache[user_id] = user_id
        return user_id


# ---------------------------------------------------------------------------
# Inbox directory setup
# ---------------------------------------------------------------------------


def ensure_inbox_dirs(inbox_dir: str, config: dict) -> None:
    """Create inbox directories for every configured channel."""
    for session in config["sessions"].values():
        channel_name = session.get("channel")
        if channel_name:
            path = Path(inbox_dir) / channel_name
            path.mkdir(parents=True, exist_ok=True)
            logger.info("Inbox ready: %s", path)


# ---------------------------------------------------------------------------
# Message file writer
# ---------------------------------------------------------------------------


def extract_files(event: dict) -> list[dict]:
    """Extract file attachment metadata from a Slack event."""
    files = []
    for f in event.get("files", []):
        files.append(
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "mimetype": f.get("mimetype"),
                "url": f.get("url_private"),
                "size": f.get("size"),
            }
        )
    return files


def write_message(inbox_dir: str, channel_name: str, event: dict, user_name: str) -> Path:
    """Serialize a Slack message event to a JSON file in the channel inbox."""
    ts = event.get("ts", "")
    safe_ts = ts.replace(".", "_")
    channel_dir = Path(inbox_dir) / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    file_path = channel_dir / f"{safe_ts}.json"

    payload = {
        "channel_id": event.get("channel", ""),
        "channel_name": channel_name,
        "user_id": event.get("user", ""),
        "user_name": user_name,
        "text": event.get("text", ""),
        "ts": ts,
        "thread_ts": event.get("thread_ts"),
        "files": extract_files(event),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return file_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    config = load_config()
    bot_token, app_token = load_tokens()

    inbox_dir = config.get("inbox_dir", "/tmp/neo/inbox")
    bot_name = config.get("bot_name", "Neo")

    ensure_inbox_dirs(inbox_dir, config)

    app = App(token=bot_token)

    # Resolve channel IDs on startup
    channel_map = build_channel_map(app, config)
    if not channel_map:
        logger.error(
            "No configured channels found in Slack. "
            "Ensure the bot is invited to at least one configured channel."
        )
        sys.exit(1)

    logger.info(
        "Monitoring %d channel(s): %s",
        len(channel_map),
        ", ".join(f"#{n}" for n in sorted(channel_map.values())),
    )

    user_cache = UserNameCache(app)

    # Resolve the bot's own user ID so we can ignore our own messages
    auth_resp = app.client.auth_test()
    bot_user_id = auth_resp.get("user_id", "")
    logger.info("Bot identity: %s (user_id=%s)", bot_name, bot_user_id)

    @app.event("message")
    def handle_message(event: dict, say) -> None:  # noqa: ARG001 — say is required by bolt
        """Route incoming messages to the appropriate channel inbox."""
        subtype = event.get("subtype")

        # Ignore non-human message subtypes (bot_message, channel_join, etc.)
        # Allow 'file_share' and 'thread_broadcast' which carry user content
        PASSTHROUGH_SUBTYPES = {"file_share", "thread_broadcast"}
        if subtype and subtype not in PASSTHROUGH_SUBTYPES:
            return

        user_id = event.get("user", "")

        # Ignore messages from bots — bot_id is set on bot messages even
        # without the bot_message subtype (e.g., unfurls, workflow messages)
        if event.get("bot_id") or not user_id:
            return

        # Ignore our own messages as a safety net
        if user_id == bot_user_id:
            return

        channel_id = event.get("channel", "")
        channel_name = channel_map.get(channel_id)

        # Silently ignore messages from channels not in our config
        if not channel_name:
            return

        user_name = user_cache.get(user_id)
        file_path = write_message(inbox_dir, channel_name, event, user_name)

        logger.info(
            "Message from %s in #%s -> %s",
            user_name,
            channel_name,
            file_path,
        )

    # Socket Mode connects via WebSocket — no public HTTP endpoint needed.
    # The handler manages reconnections automatically.
    handler = SocketModeHandler(app, app_token)
    logger.info("Starting Socket Mode connection...")
    handler.start()


if __name__ == "__main__":
    main()
