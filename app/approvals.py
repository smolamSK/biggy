"""Approval workflows: multi-step sign-off held on a workflow transition.

A :class:`~app.metadata.models.Workflow` transition ``from_state → to_state``
**requires approval** iff it has one or more :class:`ApprovalStep` rows. When a user
requests such a transition through any write path, the move is *held*: the record
stays in ``from_state`` (the field is diverted out of the write), an
:class:`ApprovalRequest` is created, and eligible approvers Approve/Reject — each
recorded as an :class:`ApprovalAction` (the sign-off trail). ``position`` groups steps:
same position = parallel (all must approve), different = sequential. When the last
position approves, the transition is applied for real via
:func:`record_service.update` (so triggers / SLA / feeds fire); any rejection cancels
the move. Wired in at the *route* write paths, never in ``record_service._fire``, so
applying the approved move can't recurse.
"""
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select

from . import record_service, workflow
from .db import engine_for_table
from .metadata.models import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalStep,
    AppUser,
    MetaField,
    MetaTable,
    Notification,
    Workflow,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _int_or_none(pk):
    try:
        return int(pk)
    except (TypeError, ValueError):
        return None


def _table(session, table_phys):
    return session.scalar(select(MetaTable).where(MetaTable.phys_name == table_phys))


# --------------------------------------------------------------------------- #
# Step config
# --------------------------------------------------------------------------- #
def steps_for(session, workflow_id, frm, to):
    return session.scalars(select(ApprovalStep).where(
        ApprovalStep.workflow_id == workflow_id, ApprovalStep.from_state == frm,
        ApprovalStep.to_state == to).order_by(ApprovalStep.position, ApprovalStep.id)).all()


def requires_approval(session, workflow_id, frm, to):
    return session.scalar(select(ApprovalStep.id).where(
        ApprovalStep.workflow_id == workflow_id, ApprovalStep.from_state == frm,
        ApprovalStep.to_state == to).limit(1)) is not None


def extra_choices(session, wf, current):
    """to-states of approval-required transitions from ``current`` (any role)."""
    return [t["to"] for t in workflow.transitions(wf)
            if t["from"] == current and requires_approval(session, wf.id, current, t["to"])]


def _eligible(step, user):
    if getattr(user, "is_designer", False):
        return True
    if step.approver_user_id and step.approver_user_id == getattr(user, "id", None):
        return True
    return bool(step.approver_role) and getattr(user, "role", None) == step.approver_role


def _approver_user_ids(session, steps):
    ids, roles = set(), set()
    for s in steps:
        if s.approver_user_id:
            ids.add(s.approver_user_id)
        if s.approver_role:
            roles.add(s.approver_role)
    if roles:
        for u in session.scalars(select(AppUser).where(AppUser.role.in_(roles))):
            ids.add(u.id)
    return ids


# --------------------------------------------------------------------------- #
# Write-path interception
# --------------------------------------------------------------------------- #
def plan_diversions(session, mt, old, values):
    """Pop approval-required transition fields out of ``values`` (pure, no commits).

    Returns ``[{"wf","field","frm","to"}]``; ``values`` is mutated in place so the
    caller writes only the remaining (direct) changes.
    """
    out = []
    wfs = workflow.for_table(session, mt.id)
    if not wfs:
        return out
    field_by_id = {f.id: f for f in mt.fields}
    for fid, wf in wfs.items():
        field = field_by_id.get(fid) or session.get(MetaField, fid)
        if not field or field.phys_name not in values:
            continue
        frm = (old or {}).get(field.phys_name)
        to = values[field.phys_name]
        if to == frm or to in (None, ""):
            continue
        if workflow._match(wf, frm, to) and requires_approval(session, wf.id, frm, to):
            out.append({"wf": wf, "field": field, "frm": frm, "to": to})
            del values[field.phys_name]
    return out


def request_transition(session, engine, mt, wf, pk, frm, to, user):
    """Create (or reuse) a pending request for ``frm→to`` and notify the first approvers."""
    existing = session.scalar(select(ApprovalRequest).where(
        ApprovalRequest.workflow_id == wf.id, ApprovalRequest.table_phys == mt.phys_name,
        ApprovalRequest.row_pk == str(pk), ApprovalRequest.from_state == frm,
        ApprovalRequest.to_state == to, ApprovalRequest.state == "pending"))
    if existing:
        return existing
    steps = steps_for(session, wf.id, frm, to)
    first_pos = steps[0].position if steps else 1
    req = ApprovalRequest(workflow_id=wf.id, table_phys=mt.phys_name, row_pk=str(pk),
                          from_state=frm, to_state=to, state="pending",
                          current_position=first_pos, requested_by=getattr(user, "id", None))
    session.add(req)
    session.flush()
    _notify_position(session, mt, req, first_pos)
    session.commit()
    return req


# --------------------------------------------------------------------------- #
# Acting on a request
# --------------------------------------------------------------------------- #
def _open_steps(session, request):
    """Steps at the current position not yet satisfied by an approve action."""
    steps = [s for s in steps_for(session, request.workflow_id, request.from_state,
                                  request.to_state) if s.position == request.current_position]
    done = {a.step_id for a in session.scalars(select(ApprovalAction).where(
        ApprovalAction.request_id == request.id, ApprovalAction.decision == "approve"))}
    return [s for s in steps if s.id not in done]


def can_act(session, request, user):
    return request.state == "pending" and any(
        _eligible(s, user) for s in _open_steps(session, request))


def act(session, request, user, decision, comment):
    """Record one approve/reject; advance / finish / reject the request."""
    if request.state != "pending":
        raise ValueError("This approval has already been decided.")
    mine = [s for s in _open_steps(session, request) if _eligible(s, user)]
    if not mine:
        raise ValueError("You are not an eligible approver for the current step.")
    now = _now()
    session.add(ApprovalAction(request_id=request.id, step_id=mine[0].id,
                               position=request.current_position,
                               user_id=getattr(user, "id", None),
                               decision=("reject" if decision == "reject" else "approve"),
                               comment=(comment or None), at=now))
    session.flush()

    if decision == "reject":
        request.state = "rejected"
        request.decided_at = now
        session.commit()
        _notify_requester(session, request, "rejected")
        session.commit()
        return request

    if _open_steps(session, request):           # parallel siblings still pending
        session.commit()
        return request

    positions = sorted({s.position for s in steps_for(
        session, request.workflow_id, request.from_state, request.to_state)})
    nxt = next((p for p in positions if p > request.current_position), None)
    if nxt is None:                              # fully approved → apply for real
        request.state = "approved"
        request.decided_at = now
        session.commit()
        _apply(session, request)
        _notify_requester(session, request, "approved")
        session.commit()
    else:                                        # advance to the next step group
        request.current_position = nxt
        session.commit()
        mt = _table(session, request.table_phys)
        if mt:
            _notify_position(session, mt, request, nxt)
        session.commit()
    return request


def _apply(session, request):
    wf = session.get(Workflow, request.workflow_id)
    if not wf:
        return
    mt = session.get(MetaTable, wf.table_id)
    field = session.get(MetaField, wf.field_id)
    if not mt or not field:
        return
    record_service.update(session, engine_for_table(mt), mt, request.row_pk,
                          {field.phys_name: request.to_state}, request.requested_by)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def _notify_position(session, mt, request, position):
    steps = [s for s in steps_for(session, request.workflow_id, request.from_state,
                                  request.to_state) if s.position == position]
    msg = (f"Approval needed: {mt.label} #{request.row_pk} — "
           f"{request.from_state} → {request.to_state}")
    for uid in _approver_user_ids(session, steps):
        session.add(Notification(table_phys=mt.phys_name, row_pk=_int_or_none(request.row_pk),
                                 event="approval", channel="in_app", user_id=uid,
                                 status="unread", body=msg))


def _notify_requester(session, request, what):
    if not request.requested_by:
        return
    mt = _table(session, request.table_phys)
    label = mt.label if mt else request.table_phys
    session.add(Notification(
        table_phys=request.table_phys, row_pk=_int_or_none(request.row_pk), event="approval",
        channel="in_app", user_id=request.requested_by, status="unread",
        body=f"Your request {request.from_state} → {request.to_state} on "
             f"{label} #{request.row_pk} was {what}."))


# --------------------------------------------------------------------------- #
# Queries for the inbox / badge / record panel
# --------------------------------------------------------------------------- #
def _candidate_requests(session, user):
    """Pending requests with a step at the current position this user *might* act on.

    One SQL query (join to the step table; role/user filter for non-designers), so
    the per-request badge/inbox cost is O(this user's pending), not O(all pending).
    The exact `can_act` check still runs on the few candidates (it also excludes
    steps already satisfied).
    """
    q = (select(ApprovalRequest)
         .join(ApprovalStep, and_(
             ApprovalStep.workflow_id == ApprovalRequest.workflow_id,
             ApprovalStep.from_state == ApprovalRequest.from_state,
             ApprovalStep.to_state == ApprovalRequest.to_state,
             ApprovalStep.position == ApprovalRequest.current_position))
         .where(ApprovalRequest.state == "pending"))
    if not getattr(user, "is_designer", False):
        q = q.where(or_(ApprovalStep.approver_user_id == getattr(user, "id", None),
                        ApprovalStep.approver_role == getattr(user, "role", None)))
    return session.scalars(q.distinct().order_by(ApprovalRequest.id.desc())).all()


def pending_for_user(session, user):
    return [req for req in _candidate_requests(session, user)
            if can_act(session, req, user)]


def pending_count_for_user(session, user):
    return len(pending_for_user(session, user))


def requests_for_record(session, table_phys, pk):
    reqs = session.scalars(select(ApprovalRequest).where(
        ApprovalRequest.table_phys == table_phys, ApprovalRequest.row_pk == str(pk))
        .order_by(ApprovalRequest.id.desc())).all()
    names = {u.id: u.username for u in session.scalars(select(AppUser))}
    out = []
    for req in reqs:
        steps = steps_for(session, req.workflow_id, req.from_state, req.to_state)
        actions = session.scalars(select(ApprovalAction).where(
            ApprovalAction.request_id == req.id).order_by(ApprovalAction.id)).all()
        positions = sorted({s.position for s in steps})
        out.append({
            "req": req,
            "n_steps": len(steps),
            "positions": positions,
            "n_positions": len(positions),
            "actions": [{"user": names.get(a.user_id, "—"), "decision": a.decision,
                         "comment": a.comment, "at": a.at, "position": a.position}
                        for a in actions],
        })
    return out
