#!/usr/bin/env python3
"""Resolve Slack channel names to IDs using the Slack API.

Reads session config from config.yaml, looks up each channel by name
in the configured Slack section, and outputs a JSON mapping:
    {"oracle": "C123ABC", "tor": "C456DEF", ...}

Usage:
    python resolve_channels.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
ENV_PATH = SCRIPT_DIR / ".env"


def load_config() -> dict:
    """Load and return the YAML config."""
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve_channel_ids(client: WebClient, channel_names: list[str]) -> dict[str, str]:
    """Map channel names to Slack channel IDs.

    Uses conversations.list to fetch all channels, then matches by name.
    Paginates to handle workspaces with many channels.
    """
    name_to_id: dict[str, str] = {}
    target_names = set(channel_names)
    cursor = None

    while True:
        response = client.conversations_list(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
        )

        for channel in response["channels"]:
            if channel["name"] in target_names:
                name_to_id[channel["name"]] = channel["id"]

        # Stop early if we found everything
        if target_names <= set(name_to_id.keys()):
            break

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return name_to_id


def main() -> None:
    load_dotenv(ENV_PATH)

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("Error: SLACK_BOT_TOKEN not set in environment or .env", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    sessions = config.get("sessions", {})
    channel_names = [s["channel"] for s in sessions.values()]

    client = WebClient(token=token)

    try:
        channel_map = resolve_channel_ids(client, channel_names)
    except SlackApiError as e:
        print(f"Slack API error: {e.response['error']}", file=sys.stderr)
        sys.exit(1)

    # Warn about any channels we couldn't resolve
    missing = set(channel_names) - set(channel_map.keys())
    if missing:
        print(f"Warning: could not resolve channels: {', '.join(sorted(missing))}", file=sys.stderr)

    # Output clean JSON to stdout
    json.dump(channel_map, sys.stdout, indent=2)
    print()  # trailing newline


if __name__ == "__main__":
    main()
