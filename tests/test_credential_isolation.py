"""Credential isolation (FR-2). This can only be demonstrated on the studio Mac,
with the restricted uploader user and its Keychain set up. It runs there via
scripts/isolation_check.sh, which records the evidence for the reviewer.

On any other machine this test is skipped, and reported as skipped, not passed.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or not os.environ.get("STUDIO_UPLOADER_ISOLATION_TEST"),
    reason="requires the studio Mac with the restricted uploader user (set STUDIO_UPLOADER_ISOLATION_TEST=1)",
)


def test_other_identities_cannot_read_keychain_item():
    r = subprocess.run(["bash", str(ROOT / "scripts" / "isolation_check.sh")], capture_output=True, text=True)
    print(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr
