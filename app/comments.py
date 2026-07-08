"""Record conversations: staff ⇄ customer comments with an internal/public split.

A comment belongs to one data record (``table_phys`` + ``row_pk``). ``internal``
comments are staff work notes — the customer portal never shows them. Posting a
comment notifies the other participants of the thread (prior commenters plus the
record's creator) with an in-app notification; portal users are only notified of
public comments.
"""
from sqlalchemy import select

from .metadata.models import ROLE_PORTAL, AppUser, Comment, Notification

SNIPPET_LEN = 140


def list_for(session, table_phys, row_pk, *, include_internal):
    """The record's thread, ascending; each entry gets ``.username`` attached."""
    q = select(Comment).where(Comment.table_phys == table_phys,
                              Comment.row_pk == str(row_pk))
    if not include_internal:
        q = q.where(Comment.internal.is_(False))
    comments = session.scalars(q.order_by(Comment.id)).all()
    users = {u.id: u.username for u in session.scalars(
        select(AppUser).where(AppUser.id.in_({c.user_id for c in comments} - {None})))}
    for c in comments:
        c.username = users.get(c.user_id, "?")
    return comments


def _participants(session, table_phys, row_pk, row):
    """User ids with a stake in the thread: prior commenters, the record
    creator, and everyone watching the record."""
    from . import watch
    ids = set(session.scalars(
        select(Comment.user_id).where(Comment.table_phys == table_phys,
                                      Comment.row_pk == str(row_pk))
    ).all())
    if row and row.get("created_by"):
        ids.add(row["created_by"])
    ids |= watch.watchers(session, table_phys, row_pk)
    ids.discard(None)
    return ids


def add(session, table_phys, row_pk, user, body, *, internal=False,
        row=None, record_label=None):
    """Insert a comment and notify the other thread participants in-app.

    ``row`` (the physical record dict, if the caller has it) supplies
    ``created_by`` so the customer is included from the very first staff reply;
    ``record_label`` makes the notification subject readable.
    """
    body = (body or "").strip()
    if not body:
        raise ValueError("Comment is empty.")
    recipients = _participants(session, table_phys, row_pk, row)
    recipients.discard(user.id)

    comment = Comment(table_phys=table_phys, row_pk=str(row_pk),
                      user_id=user.id, body=body, internal=internal)
    session.add(comment)

    users = {u.id: u for u in session.scalars(
        select(AppUser).where(AppUser.id.in_(recipients)))} if recipients else {}
    subject = f"New comment on {record_label or table_phys}"
    snippet = body if len(body) <= SNIPPET_LEN else body[:SNIPPET_LEN - 1] + "…"
    try:
        int_pk = int(row_pk)
    except (TypeError, ValueError):
        int_pk = None
    for uid in recipients:
        u = users.get(uid)
        if u is None or not u.is_active:
            continue
        if u.role == ROLE_PORTAL and internal:
            continue                      # work notes never reach customers
        session.add(Notification(
            table_phys=table_phys, row_pk=int_pk, event="comment",
            channel="in_app", user_id=uid, subject=subject,
            body=f"{user.username}: {snippet}", status="unread"))
    session.commit()
    return comment
