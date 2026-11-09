"""Channel pinning (FR-3)."""

from __future__ import annotations

from .audit import utcnow
from .config import Config
from .errors import ChannelMismatch
from .fsutil import atomic_write_json
from .youtube import YouTubeClient


def verify_pin(client: YouTubeClient, pinned_channel_id: str | None) -> dict:
    """Exactly one channel must return and its ID must equal the pin."""
    if not pinned_channel_id:
        raise ChannelMismatch("no channel is pinned; owner must run `studio-uploader pin`")
    items = client.channels_mine()
    if len(items) != 1:
        raise ChannelMismatch(f"expected exactly one channel for this credential, got {len(items)}")
    ch = items[0]
    if ch.get("id") != pinned_channel_id:
        raise ChannelMismatch("authorized channel does not match the pinned channel ID")
    return ch


def save_pin(cfg: Config, channel: dict, confirmed_by: str) -> None:
    atomic_write_json(cfg.pin_path, {
        "channel_id": channel["id"],
        "channel_title": channel.get("snippet", {}).get("title", ""),
        "custom_url": channel.get("snippet", {}).get("customUrl", ""),
        "confirmed_by": confirmed_by,
        "confirmed_at": utcnow(),
    }, mode=0o444)


def uploads_playlist_id(channel: dict) -> str | None:
    return (channel.get("contentDetails", {}).get("relatedPlaylists", {}) or {}).get("uploads")
