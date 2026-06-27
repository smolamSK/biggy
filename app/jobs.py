"""Cross-worker job claiming for the scheduler.

Scheduled work (triggers / reports / feeds / pulls) is gated by ``last_run_at`` +
``schedule_minutes``. Reading that in Python and committing later is racy: two
worker processes can both decide a job is due and run it twice. :func:`claim_due`
replaces that with a single **atomic** conditional UPDATE — advancing
``last_run_at`` only if the job is still due — and reports whether *this* caller
won the claim (``rowcount == 1``). One UPDATE statement is atomic in both MariaDB
and SQLite, so exactly one worker runs each due job, with no advisory lock and no
new dependency. Takes the model class as an argument to avoid importing app models
here (keeps scheduler/pull/feeds free of an import cycle).
"""
from datetime import timedelta

from sqlalchemy import or_, update


def claim_due(session, model, obj_id, schedule_minutes, now):
    """Atomically advance ``model.last_run_at`` for ``obj_id`` iff it is due.

    Returns ``True`` only for the caller whose UPDATE actually moved the row — so
    concurrent workers see exactly one ``True``. A never-run job (``last_run_at`` is
    NULL) counts as due. ``schedule_minutes`` ≤ 0 / falsy is never due.
    """
    if not schedule_minutes or schedule_minutes <= 0:
        return False
    cutoff = now - timedelta(minutes=schedule_minutes)
    res = session.execute(
        update(model)
        .where(model.id == obj_id,
               or_(model.last_run_at.is_(None), model.last_run_at <= cutoff))
        .values(last_run_at=now))
    session.commit()
    return res.rowcount == 1
