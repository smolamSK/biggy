"""Company (tenant) tree and per-user data separation.

Companies chain via ``parent_id``; being allowed on a company implies access to
every company **below** it. A user with ``company_id`` set is *scoped*: on any
table that has a ``company``-type field they only see rows whose company sits in
their subtree (rows without a company are hidden from them). Users without a
company — and designers — are unscoped and see everything, as before.
"""
from sqlalchemy import select

from .metadata.models import AppUser, Company


def all_companies(session):
    return session.scalars(select(Company).order_by(Company.name)).all()


def subtree_ids(session, root_id):
    """The company id and every descendant's id (cycle-safe)."""
    children = {}
    for c in session.scalars(select(Company)):
        children.setdefault(c.parent_id, set()).add(c.id)
    ids, frontier = {root_id}, {root_id}
    while frontier:
        nxt = set()
        for cid in frontier:
            nxt |= children.get(cid, set())
        nxt -= ids
        ids |= nxt
        frontier = nxt
    return ids


def allowed_for_user(session, user_id):
    """``None`` = unscoped (sees everything); else the visible company-id set."""
    u = session.get(AppUser, user_id) if user_id else None
    if u is None or not u.company_id:
        return None
    return subtree_ids(session, u.company_id)


def get_or_create(session, name):
    """A company by exact name, created on demand (bulk user import)."""
    name = (name or "").strip()
    if not name:
        return None
    c = session.scalar(select(Company).where(Company.name == name))
    if c is None:
        c = Company(name=name)
        session.add(c)
        session.flush()
    return c
