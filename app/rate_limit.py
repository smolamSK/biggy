"""Shared, DB-backed sliding-window rate limiting (multi-worker-safe).

State lives in :class:`~app.metadata.models.RateHit`, so limits hold across worker
processes. Two usage patterns:

- **Per-request limits** (inbound webhooks): :func:`hit_ok` counts *every* call —
  each allowed call records a hit.
- **Failure lockouts** (login / MFA codes): :func:`blocked` only *checks*; the
  caller invokes :func:`record` on a *failed* attempt — successful sign-ins never
  count toward a lockout.

Old hits are swept hourly by the scheduler (see ``scheduler.run_due``), so reads
filter by the window cutoff rather than deleting per call — 2 statements per hit.
"""
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .db import SessionLocal
from .metadata.models import RateHit


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _window_state(session, key, window, now):
    """(hits_in_window, oldest_hit_at) for ``key`` — one aggregate query."""
    count, oldest = session.execute(
        select(func.count(), func.min(RateHit.at)).where(
            RateHit.key == key, RateHit.at >= now - timedelta(seconds=window))
    ).one()
    return count or 0, oldest


def _retry_after(oldest, window, now):
    if oldest is None:
        return 1
    return max(1, math.ceil(window - (now - oldest).total_seconds()))


def hit_ok(key, limit, window):
    """Count this call against ``key``. Returns ``(ok, retry_after_seconds)``.

    ``limit <= 0`` disables. Soft under extreme concurrency (a small over-count is
    possible) — acceptable for a shared throttle.
    """
    if not limit or limit <= 0:
        return True, 0
    now = _now()
    session = SessionLocal()
    count, oldest = _window_state(session, key, window, now)
    if count >= limit:
        return False, _retry_after(oldest, window, now)
    session.add(RateHit(key=key, at=now))
    session.commit()
    return True, 0


def blocked(key, limit, window):
    """Check-only: is ``key`` over ``limit`` recorded failures in ``window``?"""
    if not limit or limit <= 0:
        return False, 0
    now = _now()
    count, oldest = _window_state(SessionLocal(), key, window, now)
    if count >= limit:
        return True, _retry_after(oldest, window, now)
    return False, 0


def record(key):
    """Record one failed attempt against ``key``."""
    session = SessionLocal()
    session.add(RateHit(key=key, at=_now()))
    session.commit()
