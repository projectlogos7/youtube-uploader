"""Command-line interface.

Who runs what:
  implementer : scan, prepare, validate, run (dry run), status, doctor, review-pack
  owner       : authorize, pin, revoke
  reviewer    : approve, release, activate/deactivate, run --live, resolve
  anyone      : kill (engaging the kill switch is always allowed), followup
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import platform
import subprocess

from . import approval, auth, doctor, reviewpack
from . import manifest as mf
from .audit import AuditLog
from .config import Config, load_config
from .errors import UploaderError
from .keychain import MacKeychain
from .killswitch import KillSwitch
from .ledger import Ledger
from .notify import macos_notifier
from .pinning import save_pin
from .staging import candidates, check_ready
from .state import StateStore
from .transport import HttpTransport
from .uploader import Uploader
from .youtube import YouTubeClient

DEFAULT_CONFIG = "~/.config/studio-uploader/config.toml"


def _cfg(args) -> Config:
    path = os.path.expanduser(args.config or os.environ.get("STUDIO_UPLOADER_CONFIG", DEFAULT_CONFIG))
    return load_config(path)


def _client(cfg: Config, audit: AuditLog, *, live: bool) -> YouTubeClient:
    transport = HttpTransport()
    tokens = auth.RefreshTokenProvider(cfg.oauth, MacKeychain(cfg.keychain), transport)
    return YouTubeClient(transport, tokens, KillSwitch(cfg.killswitch_path), audit,
                         live=live, retry=cfg.retry)


def cmd_scan(cfg: Config, args) -> int:
    rows = []
    for p in candidates(cfg):
        r = check_ready(p, cfg)
        rows.append({"file": p.name, "ready": r.ready, "reasons": r.reasons})
    print(json.dumps(rows, indent=2))
    if args.notify:
        ready = [r["file"] for r in rows if r["ready"]]
        if ready:
            macos_notifier(cfg.notifications)("Studio uploader: files ready for review",
                                             f"{len(ready)} ready in staging: {', '.join(ready)[:200]}")
    print("Note: readiness never authorizes upload. A reviewer must approve a manifest.", file=sys.stderr)
    return 0


def cmd_prepare(cfg: Config, args) -> int:
    r = check_ready(Path(args.file), cfg)
    if not r.ready:
        print("NOT READY: " + "; ".join(r.reasons), file=sys.stderr)
        return 1
    out = approval.create_draft(cfg, r)
    print(f"Draft manifest written: {out}\nThe reviewer fills in every field, then runs `approve`.")
    return 0


def cmd_validate(cfg: Config, args) -> int:
    m = mf.load(Path(args.manifest))
    errs = mf.validate(m, draft=args.draft, pinned_channel_id=cfg.pinned_channel_id())
    for e in errs:
        print("- " + e)
    print("valid" if not errs else f"{len(errs)} problem(s)")
    return 0 if not errs else 1


def cmd_approve(cfg: Config, args) -> int:
    print("You are approving the exact file and metadata in this draft for a PRIVATE upload.")
    job = approval.approve(cfg, Path(args.draft), args.reviewer)
    print(f"Approved. approval_id={job.approval_id}\nFrozen copy: {job.video_path}\n"
          f"Before a live upload, release it: studio-uploader release {job.approval_id} --reviewer NAME")
    return 0


def cmd_release(cfg: Config, args) -> int:
    out = approval.release(cfg, args.approval_id, args.reviewer)
    print(f"Released for live upload (still Private): {out}")
    return 0


def cmd_activate(cfg: Config, args) -> int:
    rec = approval.activate(cfg, args.reviewer, args.note or "", pilot_ids=args.pilot,
                            max_uploads=args.max_uploads, expires_in_days=args.expires_in_days)
    scope = f"pilot: {', '.join(rec['allowed_approval_ids'])}" if rec["allowed_approval_ids"] else "any released video"
    print(f"Live mode activated ({rec['activation_id']}).\n"
          f"  scope   : {scope}\n  uploads : at most {rec['max_uploads']}\n  expires : {rec['expires_at']}\n"
          "Each video still needs `approve` and `release`. Uploads are Private; publishing stays manual.")
    return 0


def cmd_deactivate(cfg: Config, args) -> int:
    approval.deactivate(cfg)
    print("Live mode deactivated.")
    return 0


def cmd_kill(cfg: Config, args) -> int:
    KillSwitch(cfg.killswitch_path).engage(getpass.getuser(), args.reason or "")
    print(f"Kill switch ENGAGED ({cfg.killswitch_path}). No API call will be made until released.")
    return 0


def cmd_unkill(cfg: Config, args) -> int:
    KillSwitch(cfg.killswitch_path).release()
    print("Kill switch released.")
    return 0


def _token_age(cfg: Config):
    if platform.system() != "Darwin":
        return None
    kc = MacKeychain(cfg.keychain)
    return lambda: auth.token_age_days(kc)


def cmd_run(cfg: Config, args, *, followup_only: bool = False) -> int:
    audit = AuditLog(cfg.log_dir)
    live = getattr(args, "live", False)
    offline = getattr(args, "offline", False)
    client = None if offline else _client(cfg, audit, live=live)
    up = Uploader(cfg, client, audit, live=live, offline=offline,
                  notifier=macos_notifier(cfg.notifications),
                  token_age_days=_token_age(cfg) if live else None,
                  skip_if_idle=getattr(args, "if_active", False), followup_only=followup_only)
    res = up.run()
    print(json.dumps({"run_id": res.run_id, "mode": res.mode, "exit_code": res.exit_code,
                      "idle": res.idle, "hard_stop": res.hard_stop, "warnings": res.warnings,
                      "api_requests": res.api_requests, "upload_requests": res.upload_requests,
                      "jobs": res.jobs, "followups": res.followups}, indent=2))
    return res.exit_code


def cmd_followup(cfg: Config, args) -> int:
    return cmd_run(cfg, args, followup_only=True)


def _store(cfg: Config):
    return MacKeychain(cfg.keychain) if platform.system() == "Darwin" else None


def cmd_doctor(cfg: Config, args) -> int:
    checks = doctor.run_doctor(cfg, store=_store(cfg))
    print(json.dumps(doctor.as_dicts(checks), indent=2) if args.json else doctor.format_checks(checks))
    return doctor.summarize(checks)


def cmd_review_pack(cfg: Config, args) -> int:
    checks = doctor.run_doctor(cfg, store=_store(cfg))
    try:
        ver = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        ver = ""
    out = reviewpack.build(cfg, checks, out_dir=Path(args.out) if args.out else None,
                           test_log=Path(args.test_log) if args.test_log else None, code_version=ver)
    print(f"Review pack written: {out}\nOpen {out / 'SUMMARY.md'}")
    return 0


def cmd_status(cfg: Config, args) -> int:
    ks = KillSwitch(cfg.killswitch_path)
    act = cfg.activation_path.exists()
    states = [{"approval_id": s.approval_id, "status": s.status, "offset": s.offset,
               "total": s.total_bytes} for s in StateStore(cfg.state_dir).all()]
    jobs = []
    for aid in approval.list_jobs(cfg):
        last = Ledger(cfg.ledger_path).latest_for(aid)
        jobs.append({"approval_id": aid,
                     "released": (cfg.releases_dir / f"{aid}.json").exists(),
                     "ledger": last and {k: last.get(k) for k in ("status", "video_id", "ts")}})
    print(json.dumps({"kill_switch": ks.engaged(), "live_activated": act,
                      "pinned_channel_id": cfg.pinned_channel_id(), "in_flight": states,
                      "jobs": jobs}, indent=2))
    return 0


def cmd_resolve(cfg: Config, args) -> int:
    states = StateStore(cfg.state_dir)
    st = states.load(args.approval_id)
    job = approval.load_job(cfg, args.approval_id)
    led = Ledger(cfg.ledger_path)
    if args.video_id:
        led.append(approval_id=args.approval_id, file_sha256=job.manifest["file_sha256"],
                   channel_id=cfg.pinned_channel_id(), status="reconciled", video_id=args.video_id,
                   method="reviewer", resolved_by=args.reviewer)
    else:
        led.append(approval_id=args.approval_id, file_sha256=job.manifest["file_sha256"],
                   channel_id=cfg.pinned_channel_id(), status="resolved_not_uploaded",
                   resolved_by=args.reviewer)
    if st:
        states.delete(args.approval_id)
    print("Resolved and recorded in the ledger.")
    return 0


def cmd_authorize(cfg: Config, args) -> int:
    print("OWNER STEP. Run this with the channel owner present, signed in to the channel's Google account.\n"
          "Consent grants youtube.upload and youtube.readonly only. Consent approves no video.")
    secret = getpass.getpass("Desktop OAuth client secret (input hidden): ").strip()
    auth.run_consent(cfg.oauth, HttpTransport(), MacKeychain(cfg.keychain),
                     client_secret=secret, login_hint=cfg.expected_account_hint)
    print("Credential stored in the Keychain. Next: `studio-uploader pin` with the owner.")
    return 0


def cmd_pin(cfg: Config, args) -> int:
    audit = AuditLog(cfg.log_dir)
    client = _client(cfg, audit, live=False)
    items = client.channels_mine()
    if len(items) != 1:
        print(f"Expected exactly one channel, got {len(items)}. Not pinning.", file=sys.stderr)
        return 2
    ch = items[0]
    print(f"Channel title : {ch.get('snippet', {}).get('title')}\n"
          f"Custom URL    : {ch.get('snippet', {}).get('customUrl', '')}\n"
          f"Channel ID    : {ch['id']}")
    typed = input("OWNER: type the channel ID exactly to confirm this is the studio channel: ").strip()
    if typed != ch["id"]:
        print("Did not match. Not pinning.", file=sys.stderr)
        return 2
    who = input("Owner name for the record: ").strip()
    save_pin(cfg, ch, who)
    audit.info("channel.pinned", channel_id=ch["id"], confirmed_by=who)
    print(f"Pinned {ch['id']}.")
    return 0


def cmd_revoke(cfg: Config, args) -> int:
    kc = MacKeychain(cfg.keychain)
    ok = auth.revoke(cfg.oauth, kc, HttpTransport())
    print("Grant revoked at Google." if ok else "Revocation call did not succeed; revoke in the Google account too.")
    if args.delete_keychain_item:
        kc.delete()
        print("Keychain item deleted.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="studio-uploader", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help=f"config file (default {DEFAULT_CONFIG} or $STUDIO_UPLOADER_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="report readiness of files in the staging folder")
    s.add_argument("--notify", action="store_true", help="macOS notification if files are ready")
    s = sub.add_parser("prepare", help="create a draft manifest for a ready file")
    s.add_argument("file")
    s = sub.add_parser("validate", help="validate a manifest")
    s.add_argument("manifest")
    s.add_argument("--draft", action="store_true")
    s = sub.add_parser("approve", help="REVIEWER: approve a draft; freezes file and manifest")
    s.add_argument("draft")
    s.add_argument("--reviewer", required=True)
    s = sub.add_parser("release", help="REVIEWER: approve one approved video for live upload")
    s.add_argument("approval_id")
    s.add_argument("--reviewer", required=True)
    s = sub.add_parser("activate", help="REVIEWER: enable live mode (Level 3), bounded")
    s.add_argument("--reviewer", required=True)
    s.add_argument("--note")
    s.add_argument("--pilot", action="append", metavar="APPROVAL_ID",
                   help="restrict this activation to the named approved video(s) (M7 pilot)")
    s.add_argument("--max-uploads", type=int, default=1, help="uploads allowed under this activation (default 1)")
    s.add_argument("--expires-in-days", type=float, default=7.0, help="activation expiry (default 7, max 31)")
    sub.add_parser("deactivate", help="disable live mode")
    s = sub.add_parser("kill", help="engage the kill switch")
    s.add_argument("--reason")
    sub.add_parser("unkill", help="release the kill switch")
    s = sub.add_parser("run", help="one pass over approved jobs (dry run unless --live)")
    s.add_argument("--live", action="store_true", help="REVIEWER: send real uploads (Private only)")
    s.add_argument("--offline", action="store_true", help="dry run without any API call")
    s.add_argument("--if-active", action="store_true",
                   help="for scheduled runs: exit quietly, with no API call, when not activated or nothing to do")
    sub.add_parser("followup", help="re-check processing status of uploaded videos (read-only)")
    s = sub.add_parser("doctor", help="preflight checks (no API calls, no secrets shown)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("review-pack", help="collect evidence for the reviewer's sign-off")
    s.add_argument("--out")
    s.add_argument("--test-log", help="output of `pytest -q`")
    sub.add_parser("status", help="show kill switch, activation, pin, in-flight uploads, jobs")
    s = sub.add_parser("resolve", help="REVIEWER: resolve an uncertain outcome")
    s.add_argument("approval_id")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--video-id")
    g.add_argument("--not-uploaded", action="store_true")
    s.add_argument("--reviewer", required=True)
    sub.add_parser("authorize", help="OWNER: OAuth consent (PKCE, loopback)")
    sub.add_parser("pin", help="OWNER: confirm and pin the channel")
    s = sub.add_parser("revoke", help="revoke the OAuth grant")
    s.add_argument("--delete-keychain-item", action="store_true")
    return p


COMMANDS = {
    "scan": cmd_scan, "prepare": cmd_prepare, "validate": cmd_validate, "approve": cmd_approve,
    "release": cmd_release, "activate": cmd_activate, "deactivate": cmd_deactivate,
    "kill": cmd_kill, "unkill": cmd_unkill, "run": cmd_run, "status": cmd_status,
    "resolve": cmd_resolve, "followup": cmd_followup, "doctor": cmd_doctor,
    "review-pack": cmd_review_pack, "authorize": cmd_authorize, "pin": cmd_pin, "revoke": cmd_revoke,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "live", False) and getattr(args, "offline", False):
        print("--live and --offline are mutually exclusive", file=sys.stderr)
        return 2
    try:
        cfg = _cfg(args)
        return COMMANDS[args.cmd](cfg, args)
    except UploaderError as exc:
        print(f"STOP [{getattr(exc, 'code', 'error')}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
