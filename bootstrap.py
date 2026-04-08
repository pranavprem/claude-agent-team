#!/usr/bin/env python3
"""Bootstrap script for Neo — creates and joins Slack channels from config.yaml.

Reads config.yaml and ensures every configured session has a corresponding
Slack channel. Channels are created if they don't exist, joined, and given
a topic indicating they're managed by Neo.

Usage:
    python bootstrap.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Logging — all output goes to stderr
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("bootstrap")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
ENV_PATH = Path(__file__).resolve().parent / ".env"


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


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


def load_slack_client() -> WebClient:
    """Load .env and return an authenticated Slack WebClient."""
    load_dotenv(ENV_PATH)

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token.startswith("xoxb-"):
        logger.error(
            "SLACK_BOT_TOKEN is missing or invalid — "
            "must start with 'xoxb-'. Check your .env file."
        )
        sys.exit(1)

    return WebClient(token=bot_token)


# ---------------------------------------------------------------------------
# Channel setup
# ---------------------------------------------------------------------------


def ensure_channel(client: WebClient, name: str, project_path: str, existing: bool) -> str | None:
    """Create (if needed) and join a Slack channel. Returns channel ID or None on failure."""
    channel_id = None

    if not existing:
        try:
            resp = client.conversations_create(name=name)
            channel_id = resp["channel"]["id"]
            logger.info("Created channel #%s (%s)", name, channel_id)
        except SlackApiError as e:
            if e.response.get("error") == "name_taken":
                logger.info("Channel #%s already exists — will join it", name)
            else:
                logger.error("Failed to create #%s: %s", name, e.response["error"])
                return None
    else:
        logger.info("Channel #%s marked as existing — skipping creation", name)

    # If we don't have channel_id yet (existing channel), look it up by name
    if not channel_id:
        channel_id = find_channel_by_name(client, name)
        if not channel_id:
            logger.error("Could not find channel #%s in Slack", name)
            return None

    # Join the channel
    try:
        client.conversations_join(channel=channel_id)
        logger.info("Joined channel #%s (%s)", name, channel_id)
    except SlackApiError as e:
        if e.response.get("error") == "already_in_channel":
            logger.info("Already in channel #%s (%s)", name, channel_id)
        else:
            logger.error("Failed to join #%s: %s", name, e.response["error"])
            return None

    # Invite the workspace owner so channels appear in their sidebar
    try:
        owner_id = _get_workspace_owner(client)
        if owner_id:
            client.conversations_invite(channel=channel_id, users=[owner_id])
            logger.info("Invited workspace owner to #%s", name)
    except SlackApiError as e:
        if e.response.get("error") != "already_in_channel":
            logger.warning("Could not invite owner to #%s: %s", name, e.response["error"])

    # Set the topic so it's clear Neo manages this channel
    topic = f"Managed by Neo — {project_path}"
    try:
        client.conversations_setTopic(channel=channel_id, topic=topic)
        logger.info("Set topic for #%s: %s", name, topic)
    except SlackApiError as e:
        logger.warning("Could not set topic for #%s: %s", name, e.response["error"])

    return channel_id


_workspace_owner_id: str | None = None


def _get_workspace_owner(client: WebClient) -> str | None:
    """Find the workspace owner's user ID (cached after first call)."""
    global _workspace_owner_id
    if _workspace_owner_id is not None:
        return _workspace_owner_id
    try:
        resp = client.users_list()
        for user in resp["members"]:
            if user.get("is_owner") and not user.get("is_bot") and not user.get("deleted"):
                _workspace_owner_id = user["id"]
                return _workspace_owner_id
    except SlackApiError:
        pass
    return None


def find_channel_by_name(client: WebClient, name: str) -> str | None:
    """Look up a channel ID by name, paginating through all channels."""
    cursor = None
    while True:
        kwargs: dict = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor

        try:
            resp = client.conversations_list(**kwargs)
        except SlackApiError as e:
            logger.error("conversations.list failed: %s", e.response["error"])
            return None

        for channel in resp.get("channels", []):
            if channel.get("name") == name:
                return channel["id"]

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    config = load_config()
    client = load_slack_client()
    sessions = config.get("sessions", {})

    logger.info("Bootstrapping %d session(s)...", len(sessions))

    project_channels: list[str] = []

    for session_name, session_cfg in sessions.items():
        channel_name = session_cfg.get("channel")
        project_path = session_cfg.get("path", "")
        is_existing = session_cfg.get("existing", False)

        if not channel_name:
            logger.warning("Session '%s' has no channel — skipping", session_name)
            continue

        logger.info("--- Setting up session: %s (channel: #%s) ---", session_name, channel_name)
        channel_id = ensure_channel(client, channel_name, project_path, is_existing)

        if channel_id and session_cfg.get("type") == "project":
            project_channels.append(channel_name)

    # Sidebar section note — Slack's sidebar sections are client-side on
    # Free/Pro plans. Programmatic assignment requires Enterprise Grid's
    # admin.conversations.setConversationPrefs API.
    if project_channels:
        logger.info(
            "NOTE: To organize channels, manually drag these into your "
            "'%s' sidebar section in Slack: %s",
            config.get("slack_section", "Projects"),
            ", ".join(f"#{c}" for c in project_channels),
        )

    logger.info("Bootstrap complete.")


if __name__ == "__main__":
    main()
