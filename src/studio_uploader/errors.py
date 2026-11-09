"""Error types.

``HardStop`` subclasses end the whole run and alert the reviewer (RS-7).
``JobRefused`` subclasses refuse a single job; the run continues with others.
"""


class UploaderError(Exception):
    """Base class for all uploader errors."""


# ---- Hard stops: the run ends, the reviewer is alerted -------------------

class HardStop(UploaderError):
    code = "hard_stop"


class KillSwitchEngaged(HardStop):
    code = "kill_switch"


class AuthError(HardStop):
    """invalid_grant, revoked or expired consent, 401 after refresh."""
    code = "auth"


class QuotaExceeded(HardStop):
    code = "quota"


class ChannelMismatch(HardStop):
    code = "channel_mismatch"


class PermanentApiError(HardStop):
    """A 4xx that retrying will not fix."""
    code = "api_permanent"


class RetriesExhausted(HardStop):
    code = "retries_exhausted"


class UncertainOutcome(HardStop):
    """An earlier upload may or may not have created a video; reviewer must resolve."""
    code = "uncertain_outcome"


class LiveModeNotActivated(HardStop):
    code = "not_activated"


class DryRunViolation(HardStop):
    """A write request was attempted while not in live mode. Programming error."""
    code = "dry_run_violation"


class EndpointNotAllowed(HardStop):
    code = "endpoint_not_allowed"


class PrivacyViolation(HardStop):
    """A request would not be private. Never sent."""
    code = "privacy_violation"


class VerificationFailed(HardStop):
    code = "verification_failed"


class LockHeld(HardStop):
    code = "lock_held"


# ---- Per-job refusals -----------------------------------------------------

class JobRefused(UploaderError):
    code = "job_refused"


class ManifestInvalid(JobRefused):
    code = "manifest_invalid"


class ApprovalVoid(JobRefused):
    code = "approval_void"


class HashMismatch(JobRefused):
    code = "hash_mismatch"


class NotReleased(JobRefused):
    code = "not_released"


# ---- Internal -------------------------------------------------------------

class TransportError(UploaderError):
    """Network-level failure (connection reset, timeout). Retryable."""
