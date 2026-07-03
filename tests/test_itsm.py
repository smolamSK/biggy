"""Impact map, SLA engine, approval workflows and the CMDB example. (Split from test_features.py.)"""
import io
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalStep,
    AppUser,
    MetaField,
    MetaTable,
    Notification,
    SlaClock,
    SlaPolicy,
    Workflow,
)
from tests.helpers import (
    _add_field,
    _col,
    _fid,
    _login_client,
    _make_form,
    _make_form_p,
    _make_table,
    _ok,
    _pk_of,
    _setup,
    _topo_graph,
)


def test_topology_impact_map(app, client):
    _setup(client)
    site_tid = _make_table(client, app, "site", "Site", "name")
    rack_tid = _make_table(client, app, "rack", "Rack", "code")
    mach_tid = _make_table(client, app, "machine", "Machine", "name")
    nic_tid = _make_table(client, app, "nic", "Nic", "name")
    hidden_tid = _make_table(client, app, "hidden_ci", "Hidden", "name")
    # view forms make a table reachable in the map; hidden_ci deliberately gets none
    for name, tid in [("site", site_tid), ("rack", rack_tid),
                      ("machine", mach_tid), ("nic", nic_tid)]:
        _make_form_p(client, app, f"{name}_view", name.title(), tid, "view")
    # rack→site, machine→rack, nic→machine, hidden_ci→rack (hidden has NO view form)
    for nm, frm, to, col in [("rack_site", rack_tid, site_tid, "site_id"),
                             ("machine_rack", mach_tid, rack_tid, "rack_id"),
                             ("nic_machine", nic_tid, mach_tid, "machine_id"),
                             ("hidden_rack", hidden_tid, rack_tid, "rack_id")]:
        _ok(client.post("/designer/relations/new-m1",
                        data=dict(name=nm, from_table_id=frm, to_table_id=to, field_name=col,
                                  on_delete="SET NULL", nullable="y"), follow_redirects=True))
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO site (id, name) VALUES (1,'DC1')"))
            c.execute(text("INSERT INTO rack (id, code, site_id) VALUES (1,'R1',1)"))
            c.execute(text("INSERT INTO machine (id, name, rack_id) VALUES (1,'M1',1)"))
            c.execute(text("INSERT INTO nic (id, name, machine_id) VALUES (1,'N1',1)"))
            c.execute(text("INSERT INTO hidden_ci (id, name, rack_id) VALUES (1,'SECRETX',1)"))

    # both directions, depth 2 from the rack: upstream site + downstream machine→nic
    g = _topo_graph(client, rack_tid, 1, direction="both", depth=2)
    labels = {n["label"] for n in g["nodes"]}
    assert {"R1", "M1", "N1", "DC1"} <= labels      # root + downstream chain + upstream parent
    assert "SECRETX" not in labels                  # child table without a view form is excluded
    assert g["truncated"] is False
    # an edge points from the machine to the rack it depends on
    assert any(e["directed"] and e["kind"] == "m1" for e in g["edges"])

    # direction filter: upstream only, depth 1 → just the parent site, no children
    g_up = _topo_graph(client, rack_tid, 1, direction="upstream", depth=1)
    up = {n["label"] for n in g_up["nodes"]}
    assert "DC1" in up and "M1" not in up

    # depth clamps to TOPOLOGY_MAX_DEPTH (no error on an over-large request)
    _ok(client.get(f"/u/topology/{rack_tid}/1?depth=99"))

    # node cap flips the truncated flag
    app.config["TOPOLOGY_MAX_NODES"] = 2
    try:
        g_cap = _topo_graph(client, rack_tid, 1, direction="both", depth=2)
        assert g_cap["truncated"] is True and len(g_cap["nodes"]) == 2
    finally:
        app.config["TOPOLOGY_MAX_NODES"] = 150

    # the view page links to the impact map
    assert f"/u/topology/{rack_tid}/1" in client.get(f"/u/view/{rack_tid}/1").get_data(as_text=True)

def test_sla_engine(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="open\nwaiting\nresolved")
    _add_field(client, tid, "sla_state", "string", length=20)
    _add_field(client, tid, "due", "datetime")
    fid = _make_form(client, app, "ticket_form", "Tickets", tid)
    status_fid = _fid(app, "ticket", "status")
    state_fid = _fid(app, "ticket", "sla_state")
    due_fid = _fid(app, "ticket", "due")

    with app.app_context():
        s = SessionLocal()
        s.add(SlaPolicy(table_id=tid, name="Resolve", active=True, target_minutes=60,
                        status_field_id=status_fid, start_on_create=True,
                        pause_states="waiting", stop_states="resolved",
                        state_field_id=state_fid, due_field_id=due_fid,
                        breach_email_to="ops@example.com", breach_message="breached {title}"))
        # a second, condition-gated policy that should never start a clock here
        s.add(SlaPolicy(table_id=tid, name="VIP", active=True, target_minutes=10,
                        status_field_id=status_fid, start_on_create=True,
                        cond_field_id=status_fid, cond_op="eq", cond_value="zzz"))
        s.commit()
        pol = s.scalar(select(SlaPolicy).where(SlaPolicy.name == "Resolve"))
        vip = s.scalar(select(SlaPolicy).where(SlaPolicy.name == "VIP"))
        pol_id, vip_id = pol.id, vip.id

    # create a ticket → a running clock + write-back to sla_state/due; gated policy is skipped
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "T1", "status": "open"}, follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        clk = s.scalar(select(SlaClock).where(SlaClock.policy_id == pol_id))
        assert clk and clk.state == "running" and clk.due_at is not None
        assert s.scalar(select(SlaClock).where(SlaClock.policy_id == vip_id)) is None  # gated out
    assert _col("ticket", 1, "sla_state") == "on_track"
    assert _col("ticket", 1, "due") is not None

    # move to a paused state → clock freezes, write-back flips to 'paused'
    _ok(client.post(f"/u/forms/{fid}/1/edit",
                    data={"title": "T1", "status": "waiting"}, follow_redirects=True))
    with app.app_context():
        clk = SessionLocal().scalar(select(SlaClock).where(SlaClock.policy_id == pol_id))
        assert clk.state == "paused" and clk.remaining_seconds is not None
    assert _col("ticket", 1, "sla_state") == "paused"

    # resume → running again with a fresh deadline
    _ok(client.post(f"/u/forms/{fid}/1/edit",
                    data={"title": "T1", "status": "open"}, follow_redirects=True))
    with app.app_context():
        clk = SessionLocal().scalar(select(SlaClock).where(SlaClock.policy_id == pol_id))
        assert clk.state == "running" and clk.due_at is not None
    assert _col("ticket", 1, "sla_state") == "on_track"

    # resolve before the deadline → met
    _ok(client.post(f"/u/forms/{fid}/1/edit",
                    data={"title": "T1", "status": "resolved"}, follow_redirects=True))
    with app.app_context():
        clk = SessionLocal().scalar(select(SlaClock).where(SlaClock.policy_id == pol_id))
        assert clk.state == "met"
    assert _col("ticket", 1, "sla_state") == "met"

    # second ticket, force its deadline into the past, then sweep → breached + escalation
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "T2", "status": "open"}, follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        clk2 = s.scalar(select(SlaClock).where(SlaClock.policy_id == pol_id,
                                               SlaClock.row_pk == "2"))
        clk2.due_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        s.commit()
        from app import scheduler
        summary = scheduler.run_due(SessionLocal(), get_engine())
        assert summary["sla"] >= 1
        s = SessionLocal()
        clk2 = s.scalar(select(SlaClock).where(SlaClock.policy_id == pol_id,
                                               SlaClock.row_pk == "2"))
        assert clk2.state == "breached" and clk2.breach_notified
        n = s.scalar(select(Notification).where(Notification.event == "sla_breach",
                                                Notification.channel == "email"))
        assert n is not None                                  # breach escalation recorded
    assert _col("ticket", 2, "sla_state") == "breached"

    # the SLA panel renders on the record view
    _make_form_p(client, app, "ticket_view", "Ticket", tid, "view")
    vh = client.get(f"/u/view/{tid}/2").get_data(as_text=True)
    assert "SLA" in vh and "breached" in vh

    # designer pages render
    _ok(client.get("/designer/sla-policies"))
    _ok(client.get(f"/designer/sla-policies/{pol_id}"))

    # schema export/import round-trips the policy with remapped field ids
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        p = s.scalar(select(SlaPolicy).where(SlaPolicy.name == "Resolve"))
        assert p is not None
        f = s.get(MetaField, p.status_field_id)
        assert f is not None and f.phys_name == "status"      # field ref remapped, not dangling

def test_approval_workflow(app, client):
    _setup(client)
    tid = _make_table(client, app, "change_req", "Change", "title")
    _add_field(client, tid, "status", "enum", enum_options="draft\nsubmitted\napproved\nrejected")
    fid = _make_form(client, app, "change_form", "Changes", tid)
    _make_form_p(client, app, "change_view", "Change", tid, "view")
    status_fid = _fid(app, "change_req", "status")

    with app.app_context():
        s = SessionLocal()
        s.add(Workflow(table_id=tid, field_id=status_fid, initial_state="draft",
                       transitions=json.dumps([
                           {"from": "draft", "to": "submitted", "roles": []},
                           {"from": "submitted", "to": "approved", "roles": []},
                           {"from": "submitted", "to": "rejected", "roles": []}])))
        s.commit()
        wf_id = s.scalar(select(Workflow.id).where(Workflow.field_id == status_fid))

    for r in ("manager", "director"):
        _ok(client.post("/designer/roles", data={"name": r, "label": r.title()},
                        follow_redirects=True))
    for u, r in (("mgr", "manager"), ("dir", "director"), ("bob", "user")):
        _ok(client.post("/auth/users/new",
                        data=dict(username=u, password="pw123456", role=r, is_active="y"),
                        follow_redirects=True))
    mgr, dir_, bob = (_login_client(app, "mgr"), _login_client(app, "dir"),
                      _login_client(app, "bob"))

    # two sequential approval steps on submitted -> approved (via the designer route)
    for pos, name, role in ((1, "Manager", "manager"), (2, "Director", "director")):
        _ok(client.post(f"/designer/approvals/{wf_id}/steps",
                        data={"from_state": "submitted", "to_state": "approved",
                              "position": str(pos), "name": name, "approver_role": role},
                        follow_redirects=True))
    _ok(client.get("/designer/approvals"))
    _ok(client.get(f"/designer/approvals/{wf_id}"))

    # bob creates + submits (draft->submitted is a direct transition)
    _ok(bob.post(f"/u/forms/{fid}/new", data={"title": "C1", "status": "draft"},
                 follow_redirects=True))
    pk = _pk_of(app, "change_req", "C1")
    _ok(bob.post(f"/u/forms/{fid}/{pk}/edit", data={"title": "C1", "status": "submitted"},
                 follow_redirects=True))
    assert _col("change_req", pk, "status") == "submitted"

    # bob requests submitted -> approved : HELD, a pending request is created
    _ok(bob.post(f"/u/forms/{fid}/{pk}/edit", data={"title": "C1", "status": "approved"},
                 follow_redirects=True))
    assert _col("change_req", pk, "status") == "submitted"        # not moved
    with app.app_context():
        s = SessionLocal()
        req = s.scalar(select(ApprovalRequest).where(ApprovalRequest.state == "pending"))
        assert req and (req.from_state, req.to_state, req.current_position) == ("submitted", "approved", 1)
        req_id = req.id
        bob_u = s.scalar(select(AppUser).where(AppUser.username == "bob"))
        mgr_u = s.scalar(select(AppUser).where(AppUser.username == "mgr"))
        from app import approvals
        assert not approvals.can_act(s, req, bob_u)              # requester can't self-approve
        assert approvals.can_act(s, req, mgr_u)

    # the record view shows the Approvals panel (with a working Approve button for the approver)
    vh = mgr.get(f"/u/view/{tid}/{pk}").get_data(as_text=True)
    assert "Approvals" in vh and "submitted" in vh and "approved" in vh
    assert "step 1 of 2" in vh and 'value="approve"' in vh
    vb = bob.get(f"/u/view/{tid}/{pk}").get_data(as_text=True)
    assert "Approvals" in vb and 'value="approve"' not in vb    # requester sees status, no buttons

    # manager sees it in the inbox and approves -> advances to position 2 (still held)
    assert "approved" in mgr.get("/u/approvals").get_data(as_text=True)
    _ok(mgr.post(f"/u/approvals/{req_id}/act", data={"decision": "approve", "comment": "ok"},
                 follow_redirects=True))
    assert _col("change_req", pk, "status") == "submitted"
    with app.app_context():
        req = SessionLocal().get(ApprovalRequest, req_id)
        assert req.state == "pending" and req.current_position == 2

    # director approves -> the transition is applied for real
    _ok(dir_.post(f"/u/approvals/{req_id}/act", data={"decision": "approve", "comment": "go"},
                  follow_redirects=True))
    assert _col("change_req", pk, "status") == "approved"
    with app.app_context():
        s = SessionLocal()
        assert s.get(ApprovalRequest, req_id).state == "approved"
        n = s.scalar(select(func.count()).select_from(ApprovalAction)
                     .where(ApprovalAction.request_id == req_id))
        assert n == 2                                            # the sign-off trail

    # reject path: a second record, manager rejects -> record stays put
    _ok(bob.post(f"/u/forms/{fid}/new", data={"title": "C2", "status": "draft"},
                 follow_redirects=True))
    pk2 = _pk_of(app, "change_req", "C2")
    _ok(bob.post(f"/u/forms/{fid}/{pk2}/edit", data={"title": "C2", "status": "submitted"},
                 follow_redirects=True))
    _ok(bob.post(f"/u/forms/{fid}/{pk2}/edit", data={"title": "C2", "status": "approved"},
                 follow_redirects=True))
    with app.app_context():
        req2_id = SessionLocal().scalar(select(ApprovalRequest.id).where(
            ApprovalRequest.row_pk == str(pk2), ApprovalRequest.state == "pending"))
    _ok(mgr.post(f"/u/approvals/{req2_id}/act", data={"decision": "reject", "comment": "no"},
                 follow_redirects=True))
    assert _col("change_req", pk2, "status") == "submitted"      # not moved
    with app.app_context():
        assert SessionLocal().get(ApprovalRequest, req2_id).state == "rejected"

    # schema export/import round-trips the steps with a remapped workflow id
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    assert len(json.loads(exp.get_data())["approval_steps"]) == 2
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        new_wf = s.scalar(select(Workflow))
        assert s.scalar(select(func.count()).select_from(ApprovalStep)
                        .where(ApprovalStep.workflow_id == new_wf.id)) == 2

def test_netcmdb_example_sla_and_approvals(app, client):
    """The big CMDB example carries its SLA + approval config in the schema JSON."""
    from app import schema_io
    from app.examples import build_netcmdb
    from app.metadata.models import Role
    _setup(client)
    schema, _data = build_netcmdb()
    with app.app_context():
        s = SessionLocal()
        schema_io.import_schema(s, get_engine(), schema, replace=True)   # schema only (fast)

        incident = s.scalar(select(MetaTable).where(MetaTable.phys_name == "incident"))
        pol = s.scalar(select(SlaPolicy).where(SlaPolicy.table_id == incident.id))
        assert pol and pol.target_minutes == 240 and pol.stop_states == "resolved,closed"
        assert s.get(MetaField, pol.status_field_id).phys_name == "status"   # field refs remapped
        assert s.get(MetaField, pol.state_field_id).phys_name == "sla_state"
        assert s.get(MetaField, pol.due_field_id).phys_name == "sla_due"
        assert pol.breach_notify_target == "owner"

        cr = s.scalar(select(MetaTable).where(MetaTable.phys_name == "change_request"))
        wf = s.scalar(select(Workflow).where(Workflow.table_id == cr.id))
        steps = s.scalars(select(ApprovalStep).where(
            ApprovalStep.workflow_id == wf.id, ApprovalStep.from_state == "submitted",
            ApprovalStep.to_state == "approved").order_by(ApprovalStep.position)).all()
        assert [(x.position, x.approver_role) for x in steps] == [(1, "change_manager"), (2, "noc")]
        assert {"change_manager", "noc"} <= {r.name for r in s.scalars(select(Role))}

def test_pending_approvals_candidates(app, client):
    """The badge/inbox query narrows to the user's own pending approvals (N+1 fix)."""
    from app import approvals
    _setup(client)
    tid = _make_table(client, app, "cr2", "CR2", "title")
    _add_field(client, tid, "status", "enum", enum_options="draft\nsubmitted\napproved")
    fid = _make_form(client, app, "cr2_form", "CR2s", tid)
    status_fid = _fid(app, "cr2", "status")
    _ok(client.post("/designer/roles", data={"name": "approver1", "label": "A1"},
                    follow_redirects=True))
    for u, r in (("appr", "approver1"), ("other", "user")):
        _ok(client.post("/auth/users/new",
                        data=dict(username=u, password="pw123456", role=r, is_active="y"),
                        follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        s.add(Workflow(table_id=tid, field_id=status_fid, initial_state="draft",
                       transitions=json.dumps([{"from": "draft", "to": "submitted", "roles": []},
                                               {"from": "submitted", "to": "approved", "roles": []}])))
        s.commit()
        wf_id = s.scalar(select(Workflow.id).where(Workflow.field_id == status_fid))
        s.add(ApprovalStep(workflow_id=wf_id, from_state="submitted", to_state="approved",
                           position=1, approver_role="approver1"))
        s.commit()

    _login_client(app, "appr")   # eligible approver (session not needed further)
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "X", "status": "draft"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/1/edit", data={"title": "X", "status": "submitted"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/1/edit", data={"title": "X", "status": "approved"},
                    follow_redirects=True))                      # held → pending request
    with app.app_context():
        s = SessionLocal()
        appr_u = s.scalar(select(AppUser).where(AppUser.username == "appr"))
        other_u = s.scalar(select(AppUser).where(AppUser.username == "other"))
        assert len(approvals.pending_for_user(s, appr_u)) == 1   # eligible: sees it
        assert approvals.pending_count_for_user(s, appr_u) == 1  # the badge count matches
        assert approvals.pending_for_user(s, other_u) == []      # unrelated role: filtered in SQL
        assert len(approvals._candidate_requests(s, other_u)) == 0   # …before can_act even runs
