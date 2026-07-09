"""Enable-able ITIL process modules (incidents/requests/problems/changes)."""
from sqlalchemy import select

from app.db import SessionLocal
from app.metadata.models import MetaForm, MetaMenu, MetaTable, Role
from tests.helpers import _make_table, _ok, _setup


def test_modules_enable_after_setup(app, client):
    _setup(client)
    # an existing custom model stays; a pre-existing ci table gets cross-linked
    _make_table(client, app, "ci", "CI", "name")
    page = client.get("/designer/examples").get_data(as_text=True)
    assert "Incident management" in page and ">Enable<" in page

    _ok(client.post("/designer/modules/incidents/enable", follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        inc = s.scalar(select(MetaTable).where(MetaTable.phys_name == "incident"))
        assert inc is not None and inc.track_audit and inc.soft_delete
        assert any(f.phys_name == "ci_id" for f in inc.fields)     # wired to ci
        assert s.scalar(select(MetaMenu).where(
            MetaMenu.label == "ITSM", MetaMenu.kind == "group")) is not None

    # enabling twice is a friendly no-op
    r = client.post("/designer/modules/incidents/enable", follow_redirects=True)
    assert "already enabled" in r.get_data(as_text=True)

    # enabling problems later retro-wires incident.problem_id
    _ok(client.post("/designer/modules/problems/enable", follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        inc = s.scalar(select(MetaTable).where(MetaTable.phys_name == "incident"))
        assert any(f.phys_name == "problem_id" for f in inc.fields)
        assert s.scalar(select(MetaTable).where(
            MetaTable.phys_name == "known_error")) is not None

    # the module is genuinely usable: record entry, catalog card, SLA column
    with app.app_context():
        fid = SessionLocal().scalar(
            select(MetaForm).where(MetaForm.name == "incident_form")).id
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "Lab incident", "status": "new",
                          "priority": "P3 - moderate", "category": "network"},
                    follow_redirects=True))
    assert "Report an incident" in client.get("/u/catalog").get_data(as_text=True)
    lst = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert ">SLA<" in lst and "Lab incident" in lst


def test_modules_at_setup(app, client):
    """The setup wizard's checkboxes enable modules immediately."""
    _ok(client.post("/setup",
                    data={"username": "boss", "password": "secret1",
                          "confirm": "secret1", "modules": ["incidents", "changes"]},
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        phys = {t.phys_name for t in s.scalars(select(MetaTable))}
        assert {"incident", "change"} <= phys and "problem" not in phys
        inc = s.scalar(select(MetaTable).where(MetaTable.phys_name == "incident"))
        assert any(f.phys_name == "caused_by_change_id" for f in inc.fields)
        assert s.scalar(select(Role).where(Role.name == "change_manager")) is not None
    page = client.get("/designer/examples").get_data(as_text=True)
    assert page.count(">enabled<") == 2
