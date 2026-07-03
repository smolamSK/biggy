"""Reports, charts and dashboards. (Split from test_features.py.)"""
import io
import json

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    AppUser,
    MetaTable,
    ReportDef,
)
from tests.helpers import (
    _add_field,
    _add_widget,
    _csv_rows,
    _make_form,
    _make_shared_dashboard,
    _make_table,
    _new_amy,
    _ok,
    _setup,
    _ticket_table,
)


def test_report_aggregate_and_csv(app, client):
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "name")
    _add_field(client, tid, "tier", "enum", enum_options="gold\nsilver")
    _add_field(client, tid, "amount", "integer")
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO sale (name, tier, amount) VALUES "
                           "('a','gold',100),('b','gold',50),('c','silver',30)"))

    q = "group=tier&metric=count&metric=sum:amount"
    html = client.get(f"/u/report/{tid}?{q}")
    _ok(html)
    assert "gold" in html.get_data(as_text=True)

    csvr = client.get(f"/u/report/{tid}?{q}&export=csv")
    _ok(csvr)
    assert csvr.mimetype == "text/csv"
    rows = _csv_rows(csvr.get_data(as_text=True))
    assert rows["gold"][1:] == ["2", "150"]
    assert rows["silver"][1:] == ["1", "30"]
    assert rows["Total"][1:] == ["3", "180"]          # grand totals incl. across groups

def test_report_group_by_relation(app, client):
    _setup(client)
    comp_tid = _make_table(client, app, "company", "Company", "name")
    sale_tid = _make_table(client, app, "sale", "Sale", "code")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="company_id", from_table_id=sale_tid, to_table_id=comp_tid,
                              field_name="company_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO company (id, name) VALUES (1,'Acme')"))
            c.execute(text("INSERT INTO sale (code, company_id) VALUES ('s1',1),('s2',1)"))
    body = client.get(f"/u/report/{sale_tid}?group=company_id&metric=count").get_data(as_text=True)
    assert "Acme" in body                              # grouped by label, not raw id

def test_report_scoping_user_vs_designer(app, client):
    _setup(client)
    tid = _make_table(client, app, "lead", "Lead", "name")
    _add_field(client, tid, "amount", "integer")
    fid = _make_form(client, app, "lead_form", "Leads", tid)
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(row_owned="y"),
                    follow_redirects=True))
    amy = _new_amy(app, client)
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "boss", "amount": "100"},
                    follow_redirects=True))
    _ok(amy.post(f"/u/forms/{fid}/new", data={"name": "amy", "amount": "5"},
                 follow_redirects=True))

    q = "metric=count&metric=sum:amount&export=csv"
    amy_row = _csv_rows(amy.get(f"/u/report/{tid}?{q}").get_data(as_text=True))
    boss_row = _csv_rows(client.get(f"/u/report/{tid}?{q}").get_data(as_text=True))
    # no grouping -> a single totals row (keyed by the count value in _csv_rows)
    assert ["1", "5"] in list(amy_row.values())        # amy sees only her own row
    assert ["2", "105"] in list(boss_row.values())     # designer sees all rows
    # the Designer-mode report also sees everything
    drows = _csv_rows(client.get(
        f"/designer/report?table_id={tid}&{q}").get_data(as_text=True))
    assert any(r[0] == "2" for r in drows.values())

def test_saved_reports(app, client):
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "name")
    _add_field(client, tid, "tier", "enum", enum_options="gold\nsilver")
    _make_form(client, app, "sale_form", "Sales", tid)       # so a plain user can read it

    _ok(client.post(f"/u/reports/{tid}",
                    data={"name": "By tier", "query": "group=tier&metric=count",
                          "next": f"/u/report/{tid}"}, follow_redirects=True))
    assert "By tier" in client.get(f"/u/report/{tid}").get_data(as_text=True)
    with app.app_context():
        rid = SessionLocal().scalar(select(ReportDef)).id

    amy = _new_amy(app, client)
    assert "By tier" not in amy.get(f"/u/report/{tid}").get_data(as_text=True)   # per-user

    _ok(client.post(f"/u/reports/{rid}/delete", follow_redirects=True))
    with app.app_context():
        assert SessionLocal().scalar(select(ReportDef)) is None

def test_chart_data_shape():
    from app import reporting
    grouped = {"grouped": True, "group_label": "Tier", "titles": ["Count", "Sum of Amount"],
               "rows": [["gold", 2, 150], ["silver", 1, 30]], "totals": [3, 180]}
    cd = reporting.chart_data(grouped)
    assert cd["labels"] == ["gold", "silver"]
    assert cd["series"][0]["name"] == "Count" and cd["series"][0]["values"] == [2.0, 1.0]
    assert cd["series"][1]["values"] == [150.0, 30.0]

    ungrouped = {"grouped": False, "group_label": None, "titles": ["Count", "Sum"],
                 "rows": [[5, 99]], "totals": None}
    cu = reporting.chart_data(ungrouped)
    assert cu["labels"] == ["Count", "Sum"] and cu["series"][0]["values"] == [5.0, 99.0]

def test_report_chart_render(app, client):
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "name")
    _add_field(client, tid, "tier", "enum", enum_options="gold\nsilver")
    _add_field(client, tid, "amount", "integer")
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO sale (name, tier, amount) VALUES "
                           "('a','gold',100),('b','silver',30)"))

    html = client.get(f"/u/report/{tid}?group=tier&metric=count&chart=bar").get_data(as_text=True)
    assert 'class="js-chart"' in html and 'data-type="bar"' in html and "charts.js" in html
    assert "gold" in html                                   # chart JSON labels
    assert 'class="js-chart"' not in \
        client.get(f"/u/report/{tid}?group=tier&metric=count&chart=table").get_data(as_text=True)
    d = client.get(f"/designer/report?table_id={tid}&group=tier&metric=count&chart=pie")
    assert 'data-type="pie"' in d.get_data(as_text=True)

def test_dashboard_pin_flow(app, client):
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "name")
    _add_field(client, tid, "tier", "enum", enum_options="gold\nsilver")
    _make_form(client, app, "sale_form", "Sales", tid)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO sale (name, tier) VALUES ('a','gold'),('b','silver')"))
    _ok(client.post(f"/u/reports/{tid}",
                    data={"name": "By tier", "query": "group=tier&metric=count&chart=bar",
                          "next": f"/u/report/{tid}"}, follow_redirects=True))
    with app.app_context():
        rid = SessionLocal().scalar(select(ReportDef)).id

    assert "By tier" not in client.get("/u/").get_data(as_text=True)   # not pinned yet
    _ok(client.post(f"/u/reports/{rid}/pin", follow_redirects=True))
    home = client.get("/u/").get_data(as_text=True)
    assert "By tier" in home and 'class="js-chart"' in home and 'data-type="bar"' in home

    amy = _new_amy(app, client)
    assert "By tier" not in amy.get("/u/").get_data(as_text=True)      # per-user

    _ok(client.post(f"/u/reports/{rid}/pin", follow_redirects=True))   # unpin
    assert "By tier" not in client.get("/u/").get_data(as_text=True)

def test_shared_dashboard_widgets(app, client):
    from app import dashboards
    from app.metadata.models import Dashboard
    _setup(client)
    tid, _fid = _ticket_table(client, app)
    did = _make_shared_dashboard(client, app)
    _add_widget(client, did, kind="chart", title="By status", table_id=tid,
                query="group=status&metric=count", chart_type="bar", width="2")
    _add_widget(client, did, kind="number", title="Total", table_id=tid, query="metric=count")
    _add_widget(client, did, kind="list", title="Recent", table_id=tid, limit="2")
    _add_widget(client, did, kind="text", title="Note", content="# Heading\n\nHello world")

    with app.app_context():
        s = SessionLocal()
        boss = s.scalar(select(AppUser).where(AppUser.username == "boss"))
        tiles = {t["kind"]: t for t in dashboards.render(s, boss, s.get(Dashboard, did))}
        assert tiles["chart"]["chart_data"]["series"][0]["values"]      # grouped counts
        assert tiles["number"]["value"] == 3                            # total rows
        assert len(tiles["list"]["rows"]) == 2                          # row limit honoured
        assert "Hello world" in str(tiles["text"]["html"])              # markdown rendered

    assert client.get(f"/u/dashboards/{did}").status_code == 200        # page renders

    # a dashboard-kind menu links to it and shows in the nav
    _ok(client.post("/designer/menus/new",
                    data={"label": "Ops board", "kind": "dashboard", "parent_id": 0,
                          "target_dashboard_id": did, "position": 0}, follow_redirects=True))
    assert f"/u/dashboards/{did}" in client.get("/u/").get_data(as_text=True)

def test_dashboard_gating_and_personal(app, client):
    from app import dashboards
    from app.metadata.models import Dashboard, DashboardWidget
    _setup(client)
    tid, _fid = _ticket_table(client, app)
    sec_tid = _make_table(client, app, "secret", "Secret", "code")
    sec_fid = _make_form(client, app, "secret_form", "Secrets", sec_tid)
    amy = _new_amy(app, client)

    # a shared dashboard mixing a readable (ticket) + a denied (secret) widget
    did = _make_shared_dashboard(client, app, "Mixed")
    _add_widget(client, did, kind="number", title="Tickets", table_id=tid, query="metric=count")
    _add_widget(client, did, kind="number", title="Secrets", table_id=sec_tid, query="metric=count")
    _ok(client.post("/designer/permissions", data={f"access_{sec_fid}": "none"},
                    follow_redirects=True))                       # deny the 'user' role on secret

    with app.app_context():
        s = SessionLocal()
        amy_user = s.scalar(select(AppUser).where(AppUser.username == "amy"))
        dash = s.get(Dashboard, did)
        assert dashboards.visible(s, amy_user, dash)              # the ticket widget is readable
        amy_kinds = dashboards.render(s, amy_user, dash)
        assert len(amy_kinds) == 1 and amy_kinds[0]["w"].table_id == tid   # secret widget hidden
    assert amy.get(f"/u/dashboards/{did}").status_code == 200

    # a dashboard with ONLY the denied widget is invisible to amy (404), visible to the designer
    only = _make_shared_dashboard(client, app, "Locked")
    _add_widget(client, only, kind="number", table_id=sec_tid, query="metric=count")
    assert amy.get(f"/u/dashboards/{only}").status_code == 404
    assert client.get(f"/u/dashboards/{only}").status_code == 200

    # personal dashboards: amy builds one; the designer can't see it
    amy.post("/u/dashboards/new", data={"name": "Mine"}, follow_redirects=True)
    with app.app_context():
        pid = SessionLocal().scalar(
            select(Dashboard).where(Dashboard.name == "Mine")).id
    _ok(amy.post(f"/u/dashboards/{pid}/widgets",
                 data={"kind": "number", "title": "My tickets", "table_id": tid,
                       "query": "metric=count"}, follow_redirects=True))
    assert amy.get(f"/u/dashboards/{pid}").status_code == 200
    assert client.get(f"/u/dashboards/{pid}").status_code == 404   # not the designer's
    with app.app_context():
        assert SessionLocal().scalar(select(func.count()).select_from(DashboardWidget)
                                     .where(DashboardWidget.dashboard_id == pid)) == 1

def test_dashboard_schema_roundtrip(app, client):
    from app.metadata.models import Dashboard, DashboardWidget, MetaMenu
    _setup(client)
    tid, _fid = _ticket_table(client, app)
    did = _make_shared_dashboard(client, app, "Board")
    _add_widget(client, did, kind="chart", title="By status", table_id=tid,
                query="group=status&metric=count", chart_type="pie", width="2")
    _ok(client.post("/designer/menus/new",
                    data={"label": "Board", "kind": "dashboard", "parent_id": 0,
                          "target_dashboard_id": did, "position": 0}, follow_redirects=True))
    # a personal dashboard must NOT be exported
    amy = _new_amy(app, client)
    amy.post("/u/dashboards/new", data={"name": "Personal"}, follow_redirects=True)

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    data = json.loads(exp.get_data())
    assert [d["name"] for d in data["dashboards"]] == ["Board"]      # shared only, personal excluded
    assert data["dashboard_widgets"][0]["chart_type"] == "pie"

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        dash = s.scalar(select(Dashboard).where(Dashboard.owner_user_id.is_(None)))
        widget = s.scalar(select(DashboardWidget))
        menu = s.scalar(select(MetaMenu).where(MetaMenu.kind == "dashboard"))
        new_tid = s.scalar(select(MetaTable).where(MetaTable.phys_name == "ticket")).id
        assert dash.name == "Board" and widget.chart_type == "pie"
        assert widget.dashboard_id == dash.id and widget.table_id == new_tid   # remapped
        assert menu.target_dashboard_id == dash.id                  # menu target remapped
