"""Built-in example demos: structure, one-click load, and the schema-rule fix."""
import io
import json

import pytest
from sqlalchemy import func, select, text

from app import examples
from app.db import SessionLocal, get_engine
from app.metadata.models import MetaField, MetaForm, MetaTable


def _ok(resp):
    assert resp.status_code < 400, resp.get_data(as_text=True)[:300]


@pytest.mark.parametrize("key", list(examples.EXAMPLES))
def test_example_builds_canonically(key):
    schema, data = examples.EXAMPLES[key]["build"]()
    assert schema["version"] == 1 and data["version"] == 1
    assert schema["tables"] and schema["relations"] and schema["forms"]
    assert any(f["data_type"] == "enum" for f in schema["fields"])           # has an enum
    assert all(r.get("from_field_id") for r in schema["relations"] if r["kind"] == "m1")


@pytest.mark.parametrize("key", list(examples.EXAMPLES))
def test_example_loads_and_is_usable(app, client, key):
    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    resp = client.post(f"/designer/examples/{key}/load", follow_redirects=True)
    _ok(resp)
    assert "Loaded the" in resp.get_data(as_text=True)

    schema, data = examples.EXAMPLES[key]["build"]()
    with app.app_context():
        s = SessionLocal()
        with get_engine().connect() as conn:
            physical = {t for (t,) in conn.execute(text("SHOW TABLES")).all()}
            first_table = next(iter(data["tables"]))
            rows = conn.execute(text(f"SELECT COUNT(*) FROM `{first_table}`")).scalar()
        for t in schema["tables"]:
            assert t["phys_name"] in physical
        assert s.scalar(select(func.count()).select_from(MetaTable)) == len(schema["tables"])
        assert s.scalar(select(func.count()).select_from(MetaForm)) == len(schema["forms"])
        assert rows == len(data["tables"][first_table])
        form_id = s.scalar(select(MetaForm).order_by(MetaForm.id)).id

    _ok(client.get(f"/u/forms/{form_id}"))   # imported demo renders in User mode


def test_netcmdb_is_large_with_workflows(app, client):
    schema, data = examples.EXAMPLES["netcmdb"]["build"]()
    assert len(schema["tables"]) >= 10
    assert len(schema["workflows"]) >= 4
    data_tables = {k: v for k, v in data["tables"].items() if not k.startswith("j_")}
    assert data_tables and all(len(rows) == 50 for rows in data_tables.values())

    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    _ok(client.post("/designer/examples/netcmdb/load", follow_redirects=True))
    with app.app_context():
        from app.metadata.models import Workflow
        assert SessionLocal().scalar(
            select(func.count()).select_from(Workflow)) == len(schema["workflows"])

    # the seeded router workflow enforces transitions (router #1 seeds to 'provisioning')
    from app.api.tokens import mint
    from app.metadata.models import AppUser
    with app.app_context():
        bid = SessionLocal().scalar(select(AppUser).where(AppUser.username == "boss")).id
        _t, raw = mint(SessionLocal(), bid, "t")
    api = app.test_client()
    H = {"Authorization": f"Bearer {raw}"}
    assert api.patch("/api/v1/router/1", json={"status": "decommissioned"},
                     headers=H).status_code == 409          # provisioning → decommissioned: blocked
    assert api.patch("/api/v1/router/1", json={"status": "active"},
                     headers=H).status_code == 200          # provisioning → active: allowed


def test_schema_export_preserves_validation_rules(app, client):
    """Regression: schema export/import must keep field validation rules."""
    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="thing", label="Thing"),
                    follow_redirects=True))
    with app.app_context():
        tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "thing")).id
    _ok(client.post(f"/designer/tables/{tid}/fields",
                    data=dict(phys_name="code", label="Code", data_type="string", length=20,
                              nullable="y", max_length="10", pattern="^[A-Z]+$"),
                    follow_redirects=True))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    payload = exp.get_data()
    fld = next(f for f in json.loads(payload)["fields"] if f["phys_name"] == "code")
    assert fld["max_length"] == 10 and fld["pattern"] == "^[A-Z]+$"

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(payload), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        f = SessionLocal().scalar(select(MetaField).where(MetaField.phys_name == "code"))
        assert f.max_length == 10 and f.pattern == "^[A-Z]+$"
