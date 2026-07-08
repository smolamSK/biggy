"""Designer productivity & customization tools: form generation, CSV wizard,
duplication, branding, menu icons, enum colors, list defaults."""
import io
import json

from sqlalchemy import select, text

from app import schema_io
from app.db import SessionLocal, get_engine
from app.identifiers import sanitize_identifier
from app.importer import infer_schema
from app.metadata.models import MetaField, MetaForm, MetaMenu, MetaTable
from tests.helpers import (
    _add_field,
    _make_form,
    _make_form_p,
    _make_table,
    _ok,
    _setup,
)


# --------------------------------------------------------------------------- #
# Pure unit tests (no DB)
# --------------------------------------------------------------------------- #
def test_sanitize_identifier():
    assert sanitize_identifier("First Name") == "first_name"
    assert sanitize_identifier("Prix (EUR)") == "prix_eur"
    assert sanitize_identifier("2nd col") == "c_2nd_col"
    assert sanitize_identifier("id") == "x_id"           # reserved column dodged
    assert sanitize_identifier("app_x") == "x_app_x"     # reserved prefix dodged
    assert sanitize_identifier("") == "col"
    assert sanitize_identifier("---") == "col"
    assert len(sanitize_identifier("x" * 200)) <= 60


def test_infer_schema():
    csv_text = ("Name,Qty,Price,Active,Since,Name\n"
                "Ann,1,9.50,yes,2024-01-05,a\n"
                "Bob,2,12,no,2024-02-06,\n")
    cols, samples, n = infer_schema(csv_text)
    assert n == 2 and len(samples) == 2
    by = {c["name"]: c for c in cols}
    assert by["name"]["data_type"] == "string"
    assert by["qty"]["data_type"] == "integer"
    assert by["price"]["data_type"] == "decimal"         # 9.50 + 12 mix
    assert by["active"]["data_type"] == "boolean"
    assert by["since"]["data_type"] == "date"
    assert "name_2" in by                                # duplicate header de-duped


# --------------------------------------------------------------------------- #
# Integration (biggy_test)
# --------------------------------------------------------------------------- #
def test_generate_form_and_add_all(app, client):
    _setup(client)
    tid = _make_table(client, app, "asset", "Asset", "name")
    _add_field(client, tid, "qty", "integer")
    other = _make_table(client, app, "tagx", "TagX", "name")
    _ok(client.post("/designer/relations/new-mn",
                    data=dict(name="Tags", from_table_id=tid, to_table_id=other),
                    follow_redirects=True))

    r = client.post(f"/designer/tables/{tid}/generate-form",
                    data={"with_view": "y", "with_menu": "y"}, follow_redirects=True)
    _ok(r)
    with app.app_context():
        s = SessionLocal()
        mf = s.scalar(select(MetaForm).where(MetaForm.name == "asset_form"))
        vf = s.scalar(select(MetaForm).where(MetaForm.name == "asset_view"))
        assert mf is not None and vf is not None and vf.purpose == "view"
        assert len(mf.items) == 3                        # name + qty + m:n relation
        assert {i.kind for i in mf.items} == {"field", "relation"}
        assert s.scalar(select(MetaMenu).where(MetaMenu.target_form_id == mf.id)) is not None
        mf_id = mf.id

    # the generated form really works end-to-end
    _ok(client.post(f"/u/forms/{mf_id}/new", data={"name": "Laptop", "qty": "3"},
                    follow_redirects=True))
    assert "Laptop" in client.get(f"/u/forms/{mf_id}").get_data(as_text=True)

    # add-all fills an empty form and is idempotent
    _ok(client.post("/designer/forms/new",
                    data=dict(name="asset2", title="Asset 2", table_id=tid),
                    follow_redirects=True))
    with app.app_context():
        fid2 = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "asset2")).id
    _ok(client.post(f"/designer/forms/{fid2}/add-all", follow_redirects=True))
    _ok(client.post(f"/designer/forms/{fid2}/add-all", follow_redirects=True))
    with app.app_context():
        assert len(SessionLocal().get(MetaForm, fid2).items) == 3


def test_table_from_csv(app, client):
    _setup(client)
    csv_text = ("Item Name,Qty,Price,Active,Since\n"
                "Widget,1,9.50,yes,2024-01-05\n"
                "Gadget,2,12.00,no,2024-02-06\n")

    # step 1: upload → review shows sanitized names + sniffed types
    r = client.post("/designer/tables/from-csv",
                    data={"file": (io.BytesIO(csv_text.encode()), "inv.csv"),
                          "label": "Inventory"},
                    content_type="multipart/form-data")
    html = r.get_data(as_text=True)
    assert 'value="item_name"' in html
    assert "decimal" in html and "boolean" in html

    # step 2: create — drop 'since', keep the sniffed types
    data = {"step": "create", "csv_text": csv_text,
            "phys_name": "inventory", "label": "Inventory"}
    for i, (name, typ) in enumerate([("item_name", "string"), ("qty", "integer"),
                                     ("price", "decimal"), ("active", "boolean"),
                                     ("since", "date")]):
        if name != "since":
            data[f"include_{i}"] = "y"
        data[f"name_{i}"] = name
        data[f"label_{i}"] = name.title()
        data[f"type_{i}"] = typ
    _ok(client.post("/designer/tables/from-csv", data=data, follow_redirects=True))

    with app.app_context():
        s = SessionLocal()
        mt = s.scalar(select(MetaTable).where(MetaTable.phys_name == "inventory"))
        assert {f.phys_name for f in mt.fields} == {"item_name", "qty", "price", "active"}
        assert mt.display_field_id is not None           # first text column
        with get_engine().connect() as c:
            rows = c.execute(text(
                "SELECT item_name, qty, active FROM inventory ORDER BY id")).all()
        assert [(r[0], r[1], bool(r[2])) for r in rows] == \
            [("Widget", 1, True), ("Gadget", 2, False)]


def test_duplicate_table_and_form(app, client):
    _setup(client)
    tid = _make_table(client, app, "device", "Device", "name")
    _add_field(client, tid, "cost", "decimal")
    fid = _make_form(client, app, "device_form", "Devices", tid)
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "Router", "cost": "9"},
                    follow_redirects=True))

    _ok(client.post(f"/designer/tables/{tid}/duplicate",
                    data={"phys_name": "device2"}, follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        mt2 = s.scalar(select(MetaTable).where(MetaTable.phys_name == "device2"))
        assert mt2 is not None and mt2.label == "Device (copy)"
        assert {f.phys_name for f in mt2.fields} == {"name", "cost"}
        with get_engine().connect() as c:                # structure only, no data
            assert c.execute(text("SELECT COUNT(*) FROM device2")).scalar() == 0

    _ok(client.post(f"/designer/forms/{fid}/duplicate", follow_redirects=True))
    with app.app_context():
        f2 = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "device_form_copy"))
        assert f2 is not None and len(f2.items) == 2 and f2.table_id == tid


def test_icon_names_have_paths():
    """Every ICON_NAMES entry must exist in the _icons.html path registry."""
    import pathlib

    from app.helpers import ICON_NAMES
    src = (pathlib.Path(__file__).parent.parent
           / "app" / "templates" / "_icons.html").read_text()
    for name in ICON_NAMES:
        assert f"'{name}':" in src, f"icon '{name}' has no path in _icons.html"


def test_branding_settings(app, client):
    _setup(client)
    assert "Biggy" in client.get("/u/").get_data(as_text=True)

    _ok(client.post("/designer/settings",
                    data={"app_name": "AssetHub", "accent": "#0e7490",
                          "default_theme": "ocean"}, follow_redirects=True))
    base = client.get("/u/").get_data(as_text=True)
    assert "AssetHub" in base
    assert "--brand: #0e7490" in base                    # accent style emitted
    assert "t = 'ocean'" in base                         # default theme pre-paint
    assert "AssetHub" in app.test_client().get("/auth/login").get_data(as_text=True)

    # invalid accent is rejected; "use default" clears everything
    client.post("/designer/settings", data={"accent": "gibberish"},
                follow_redirects=True)
    assert "gibberish" not in client.get("/u/").get_data(as_text=True)
    _ok(client.post("/designer/settings",
                    data={"app_name": "", "accent": "#0e7490", "accent_default": "y",
                          "default_theme": ""}, follow_redirects=True))
    base = client.get("/u/").get_data(as_text=True)
    assert "Biggy" in base and "--brand: #" not in base  # back to Config fallback


def test_menu_icons(app, client):
    _setup(client)
    tid = _make_table(client, app, "book", "Book", "title")
    fid = _make_form(client, app, "book_form", "Books", tid)
    _ok(client.post("/designer/menus/new",
                    data={"label": "Books", "kind": "form", "parent_id": 0,
                          "target_form_id": fid, "target_table_id": 0,
                          "position": 0, "icon": "calendar"}, follow_redirects=True))
    page = client.get("/u/").get_data(as_text=True)
    assert "M8 2v4" in page                              # calendar path in the sidebar

    # an unknown icon name must never break rendering
    with app.app_context():
        s = SessionLocal()
        m = s.scalar(select(MetaMenu).where(MetaMenu.label == "Books"))
        m.icon = "no-such-icon"
        s.commit()
    _ok(client.get("/u/"))


def test_enum_chip_colors(app, client):
    _setup(client)
    tid = _make_table(client, app, "task", "Task", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\ndone")
    fid = _make_form(client, app, "task_form", "Tasks", tid)
    _make_form_p(client, app, "task_view", "Task", tid, "view")
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "T1", "status": "new"},
                    follow_redirects=True))
    with app.app_context():
        field_id = SessionLocal().scalar(
            select(MetaField).where(MetaField.phys_name == "status")).id

    # the field editor offers a color per saved option
    page = client.get(f"/designer/tables/{tid}/fields/{field_id}/edit").get_data(as_text=True)
    assert 'name="colorhue_0"' in page and 'name="colorhue_1"' in page

    _ok(client.post(f"/designer/tables/{tid}/fields/{field_id}/edit",
                    data={"phys_name": "status", "label": "Status", "data_type": "enum",
                          "enum_options": "new\ndone", "nullable": "y",
                          "colorval_0": "new", "colorhue_0": "red",
                          "colorval_1": "done", "colorhue_1": "auto"},
                    follow_redirects=True))
    with app.app_context():
        f = SessionLocal().get(MetaField, field_id)
        assert json.loads(f.enum_colors) == {"new": "red"}   # auto not stored

    lst = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert 'chip c-red">new</span>' in lst                   # chosen hue in the list
    assert "data-colors=" in lst                             # inline edit re-render map
    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert 'chip c-red">new</span>' in view                  # and on the record page

    with app.app_context():                                  # survives a round-trip
        s = SessionLocal()
        data = schema_io.export_schema(s)
        status = next(fd for fd in data["fields"] if fd["phys_name"] == "status")
        assert json.loads(status["enum_colors"]) == {"new": "red"}
        schema_io.import_schema(s, get_engine(), data, replace=True)
        f = s.scalar(select(MetaField).where(MetaField.phys_name == "status"))
        assert json.loads(f.enum_colors) == {"new": "red"}


def test_default_list_view(app, client):
    _setup(client)
    tid = _make_table(client, app, "city", "City", "name")
    fid = _make_form(client, app, "city_form", "Cities", tid)
    for n in ("Berlin", "Amsterdam", "Cologne"):
        _ok(client.post(f"/u/forms/{fid}/new", data={"name": n}, follow_redirects=True))

    _ok(client.post(f"/designer/forms/{fid}/defaults",
                    data={"default_sort": "name", "default_order": "desc",
                          "default_per_page": "50"}, follow_redirects=True))
    lst = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert 'aria-sort="descending"' in lst                   # default applied
    assert lst.index("Cologne") < lst.index("Berlin") < lst.index("Amsterdam")

    # explicit query args always win
    lst = client.get(f"/u/forms/{fid}", query_string={"sort": "name", "order": "asc"}) \
        .get_data(as_text=True)
    assert lst.index("Amsterdam") < lst.index("Berlin") < lst.index("Cologne")

    # a stale default (renamed/removed column) is ignored, not an error
    with app.app_context():
        s = SessionLocal()
        s.get(MetaForm, fid).default_sort = "gone_column"
        s.commit()
    _ok(client.get(f"/u/forms/{fid}"))
