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


def test_module_tenant_fields(app, client):
    """Module tables carry a Company field; scoping works; known errors stay global."""
    from sqlalchemy import text

    from app.db import get_engine
    from tests.test_portal import _mk_company, _mk_user
    _setup(client)
    # a pre-existing ci table (no company field yet) gets retro-fitted
    _make_table(client, app, "ci", "CI", "name")
    _ok(client.post("/designer/modules/incidents/enable", follow_redirects=True))
    _ok(client.post("/designer/modules/problems/enable", follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        tables = {t.phys_name: t for t in s.scalars(select(MetaTable))}
        for phys in ("incident", "problem", "ci"):
            assert any(f.data_type == "company" for f in tables[phys].fields), phys
        assert not any(f.data_type == "company"
                       for f in tables["known_error"].fields)   # global by design

    # per-tenant visibility: a scoped engineer sees only their company's incidents
    acme_id = _mk_company(client, app, "Acme")
    _mk_company(client, app, "Globex")
    with app.app_context():
        fid = SessionLocal().scalar(
            select(MetaForm).where(MetaForm.name == "incident_form")).id
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "Acme outage", "status": "new",
                          "priority": "P2 - high", "category": "network",
                          "company": str(acme_id)}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "Unscoped outage", "status": "new",
                          "priority": "P4 - low", "category": "other",
                          "company": ""}, follow_redirects=True))
    eng = _mk_user(client, app, "eng.acme", "user", acme_id)
    lst = eng.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert "Acme outage" in lst and "Unscoped outage" not in lst

    # a scoped engineer's own incident is auto-stamped with their company
    _ok(eng.post(f"/u/forms/{fid}/new",
                 data={"title": "Acme printer", "status": "new",
                       "priority": "P4 - low", "category": "hardware",
                       "company": ""}, follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text(
                "SELECT company FROM incident WHERE title='Acme printer'")
            ).scalar() == acme_id


def test_tenant_scoped_pickers(app, client):
    """CI pickers on catalog forms only offer the chooser's tenant's CIs."""
    from sqlalchemy import text

    from app.db import get_engine
    from tests.test_portal import _mk_company, _mk_user
    _setup(client)
    _make_table(client, app, "ci", "CI", "name")
    _ok(client.post("/designer/modules/incidents/enable", follow_redirects=True))

    acme_id = _mk_company(client, app, "Acme")
    glob_id = _mk_company(client, app, "Globex")
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO ci (name, company) VALUES "
                           "('acme-rtr', :a), ('glob-rtr', :g), ('shared-rtr', NULL)"),
                      {"a": acme_id, "g": glob_id})
        fid = SessionLocal().scalar(
            select(MetaForm).where(MetaForm.name == "incident_form")).id

    # scoped staff: form picker + list filter choices show only Acme CIs
    eng = _mk_user(client, app, "eng.acme", "user", acme_id)
    page = eng.get(f"/u/forms/{fid}/new").get_data(as_text=True)
    assert "acme-rtr" in page and "glob-rtr" not in page and "shared-rtr" not in page
    lst = eng.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert "glob-rtr" not in lst

    # scoped portal customer: same wall on the catalog form
    ann = _mk_user(client, app, "ann", "portal", acme_id)
    page = ann.get(f"/portal/new/{fid}").get_data(as_text=True)
    assert "acme-rtr" in page and "glob-rtr" not in page

    # designers keep the full picker
    page = client.get(f"/u/forms/{fid}/new").get_data(as_text=True)
    assert "acme-rtr" in page and "glob-rtr" in page and "shared-rtr" in page
