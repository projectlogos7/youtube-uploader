"""Approval manifest schema and validation (FR-5).

The manifest is the only source of video metadata. It cannot express a
privacy setting: privacy is fixed to Private in code (FR-6). Any attempt to
include privacy, scheduling or notification fields is a validation error.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1

APPROVAL_FIELDS = ("approved_by", "approved_at", "approval_id")

REQUIRED_FIELDS = (
    "file_sha256", "channel_id", "title", "description", "tags", "categoryId",
    "defaultLanguage", "madeForKids", "license", "rights_note",
    "containsSyntheticMedia",
) + APPROVAL_FIELDS

OPTIONAL_FIELDS = ("manifest_version", "source_filename", "notes")

# Fields that would let a manifest influence visibility or notification. They
# are rejected explicitly so the error message is unambiguous.
FORBIDDEN_FIELDS = (
    "privacyStatus", "privacy", "publishAt", "publish_at", "visibility",
    "notifySubscribers", "unlisted", "public", "status",
)

LICENSES = ("youtube", "creativeCommon")

TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHANNEL_ID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_BCP47 = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")
_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def tags_length(tags: list[str]) -> int:
    """YouTube counts quotes around tags that contain spaces, plus separators."""
    total = 0
    for t in tags:
        total += len(t) + (2 if " " in t else 0)
    total += max(len(tags) - 1, 0)
    return total


def validate(m: Any, *, draft: bool = False, pinned_channel_id: str | None = None) -> list[str]:
    """Return a list of problems. Empty list means valid.

    ``draft=True`` skips the approval fields (they are stamped on approval).
    """
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["manifest must be a JSON object"]

    for f in FORBIDDEN_FIELDS:
        if f in m:
            errs.append(f"field {f!r} is not allowed: uploads are always Private, "
                        "never scheduled, and never notify subscribers")

    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    for f in sorted(set(m) - allowed - set(FORBIDDEN_FIELDS)):
        errs.append(f"unknown field {f!r}")

    required = [f for f in REQUIRED_FIELDS if not (draft and f in APPROVAL_FIELDS)]
    for f in required:
        if f not in m:
            errs.append(f"missing required field {f!r}")

    def present(f: str) -> bool:
        return f in m

    if present("manifest_version") and m["manifest_version"] != MANIFEST_VERSION:
        errs.append(f"manifest_version must be {MANIFEST_VERSION}")

    if present("file_sha256") and not (isinstance(m["file_sha256"], str) and _SHA256.match(m["file_sha256"])):
        errs.append("file_sha256 must be 64 lowercase hex characters")

    if present("channel_id"):
        cid = m["channel_id"]
        if not (isinstance(cid, str) and _CHANNEL_ID.match(cid)):
            errs.append("channel_id must look like a YouTube channel ID (UC + 22 chars)")
        elif pinned_channel_id is not None and cid != pinned_channel_id:
            errs.append("channel_id does not match the pinned channel")

    if present("title"):
        t = m["title"]
        if not isinstance(t, str) or not t.strip():
            errs.append("title must be a non-empty string")
        else:
            if len(t) > TITLE_MAX_CHARS:
                errs.append(f"title exceeds {TITLE_MAX_CHARS} characters ({len(t)})")
            if "<" in t or ">" in t:
                errs.append("title must not contain '<' or '>'")

    if present("description"):
        d = m["description"]
        if not isinstance(d, str):
            errs.append("description must be a string")
        else:
            if len(d.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
                errs.append(f"description exceeds {DESCRIPTION_MAX_BYTES} bytes")
            if "<" in d or ">" in d:
                errs.append("description must not contain '<' or '>'")

    if present("tags"):
        tags = m["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            errs.append("tags must be a list of non-empty strings")
        else:
            if any("<" in t or ">" in t or "," in t for t in tags):
                errs.append("tags must not contain '<', '>' or ','")
            if tags_length(tags) > TAGS_MAX_CHARS:
                errs.append(f"tags exceed {TAGS_MAX_CHARS} characters in total")

    if present("categoryId") and not (isinstance(m["categoryId"], str) and m["categoryId"].isdigit()):
        errs.append("categoryId must be a numeric string, e.g. \"27\"")

    if present("defaultLanguage") and not (isinstance(m["defaultLanguage"], str)
                                           and _BCP47.match(m["defaultLanguage"])):
        errs.append("defaultLanguage must be a BCP-47 language tag, e.g. \"en\" or \"ur-PK\"")

    for f in ("madeForKids", "containsSyntheticMedia"):
        if present(f) and not isinstance(m[f], bool):
            errs.append(f"{f} must be explicitly true or false")

    if present("license") and m["license"] not in LICENSES:
        errs.append(f"license must be one of {LICENSES}")

    if present("rights_note") and not (isinstance(m["rights_note"], str) and m["rights_note"].strip()):
        errs.append("rights_note must describe how rights were cleared")

    if not draft:
        if present("approved_by") and not (isinstance(m["approved_by"], str) and m["approved_by"].strip()):
            errs.append("approved_by must name the reviewer")
        if present("approved_at") and not (isinstance(m["approved_at"], str) and _ISO_TS.match(m["approved_at"])):
            errs.append("approved_at must be an ISO-8601 timestamp")
        if present("approval_id") and not (isinstance(m["approval_id"], str) and _APPROVAL_ID.match(m["approval_id"])):
            errs.append("approval_id has an invalid format")

    return errs


def load(path: Path) -> dict:
    with open(path) as fh:
        return json.load(fh)


def draft_template(file_sha256: str, channel_id: str, source_filename: str) -> dict:
    """A draft for the reviewer to fill in. Booleans are deliberately null so
    the reviewer must choose; a draft with nulls does not validate."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "file_sha256": file_sha256,
        "channel_id": channel_id,
        "source_filename": source_filename,
        "title": "",
        "description": "",
        "tags": [],
        "categoryId": "",
        "defaultLanguage": "",
        "madeForKids": None,
        "license": "youtube",
        "rights_note": "",
        "containsSyntheticMedia": None,
    }
