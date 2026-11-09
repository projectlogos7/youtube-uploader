from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio_uploader import approval
from studio_uploader import manifest as mf
from studio_uploader.errors import HashMismatch, ManifestInvalid

from conftest import good_manifest_fields, stage_file
from fake_youtube import CHANNEL_ID, OTHER_CHANNEL_ID
from studio_uploader.staging import Readiness

ROOT = Path(__file__).resolve().parents[1]


def full(**over):
    m = dict(good_manifest_fields(), file_sha256="a" * 64, channel_id=CHANNEL_ID,
             approved_by="Studio Lead", approved_at="2026-11-09T10:00:00+00:00",
             approval_id="20261109-abcdef123456", manifest_version=1)
    m.update(over)
    return m


def test_valid():
    assert mf.validate(full(), pinned_channel_id=CHANNEL_ID) == []


@pytest.mark.parametrize("field", mf.REQUIRED_FIELDS)
def test_each_required_field(field):
    m = full()
    del m[field]
    assert any(field in e for e in mf.validate(m))


@pytest.mark.parametrize("over,needle", [
    ({"title": "x" * 101}, "title exceeds"),
    ({"title": ""}, "non-empty"),
    ({"title": "a <b>"}, "'<'"),
    ({"description": "é" * 2501}, "description exceeds"),
    ({"tags": ["a" * 300, "b" * 250]}, "tags exceed"),
    ({"tags": ["a,b"]}, "tags must not"),
    ({"categoryId": 27}, "categoryId"),
    ({"defaultLanguage": "english"}, "defaultLanguage"),
    ({"madeForKids": None}, "madeForKids"),
    ({"containsSyntheticMedia": "no"}, "containsSyntheticMedia"),
    ({"license": "public-domain"}, "license"),
    ({"rights_note": " "}, "rights_note"),
    ({"channel_id": OTHER_CHANNEL_ID}, "pinned"),
    ({"file_sha256": "xyz"}, "file_sha256"),
    ({"surprise": 1}, "unknown field"),
])
def test_rules(over, needle):
    errs = mf.validate(full(**over), pinned_channel_id=CHANNEL_ID)
    assert any(needle in e for e in errs), errs


def test_tags_length_counts_quotes_and_commas():
    assert mf.tags_length(["ab", "c d"]) == 2 + (3 + 2) + 1


def test_draft_template_does_not_validate_until_filled():
    d = mf.draft_template("a" * 64, CHANNEL_ID, "x.mov")
    errs = mf.validate(d, draft=True)
    assert any("madeForKids" in e for e in errs)
    assert any("containsSyntheticMedia" in e for e in errs)


def test_schema_file_matches_code():
    schema = json.loads((ROOT / "schemas" / "manifest.schema.json").read_text())
    assert set(schema["required"]) == set(mf.REQUIRED_FIELDS)
    assert set(schema["properties"]) == set(mf.REQUIRED_FIELDS) | set(mf.OPTIONAL_FIELDS)
    assert schema["additionalProperties"] is False


def test_example_manifest_is_valid():
    ex = json.loads((ROOT / "templates" / "manifest.example.json").read_text())
    assert mf.validate(ex) == []


def test_approve_rejects_changed_staged_file(cfg):
    p = stage_file(cfg)
    draft = approval.create_draft(cfg, Readiness(p, True, []))
    d = json.loads(draft.read_text())
    d.update(good_manifest_fields())
    draft.write_text(json.dumps(d))
    with open(p, "ab") as fh:
        fh.write(b"!")
    with pytest.raises(HashMismatch):
        approval.approve(cfg, draft, "Studio Lead")
    assert list(cfg.frozen_dir.iterdir()) == []


def test_approve_requires_reviewer(cfg):
    p = stage_file(cfg)
    draft = approval.create_draft(cfg, Readiness(p, True, []))
    with pytest.raises(ManifestInvalid):
        approval.approve(cfg, draft, "  ")


def test_create_draft_refuses_unready(cfg):
    p = stage_file(cfg)
    with pytest.raises(ManifestInvalid):
        approval.create_draft(cfg, Readiness(p, False, ["file is open by another process"]))


def test_release_bound_to_manifest(cfg):
    from conftest import make_job
    job = make_job(cfg, release=True)
    rec = approval.check_released(cfg, job)
    assert rec["released_by"] == "Studio Lead"
