"""REST API: CRUD, tokens, OpenAPI and bulk endpoints. (Split from test_features.py.)"""

from sqlalchemy import func, select

from app.db import SessionLocal
from app.metadata.models import (
    AppUser,
    MetaTable,
    Notification,
)
from tests.helpers import (
    _add_field,
    _make_form,
    _make_table,
    _make_trigger,
    _mint,
    _new_amy,
    _ok,
    _setup,
)


def test_api_crud_and_auth(app, client):
    _setup(client)
    _make_table(client, app, "widget", "Widget", "name")
    with app.app_context():
        tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "widget")).id
    _add_field(client, tid, "qty", "integer")
    H = _mint(app, "boss")
    api = app.test_client()                       # fresh client: token-only (no session)

    assert api.get("/api/v1/widget").status_code == 401
    assert api.get("/api/v1/widget",
                   headers={"Authorization": "Bearer nope"}).status_code == 401
    _ok(api.get("/api/v1/widget", headers=H))

    r = api.post("/api/v1/widget", json={"name": "A", "qty": 5}, headers=H)
    assert r.status_code == 201, r.get_data(as_text=True)
    obj = r.get_json()
    pk = obj["id"]
    assert obj["name"] == "A" and obj["qty"] == 5
    assert r.headers.get("Location", "").endswith(f"/api/v1/widget/{pk}")

    assert api.get(f"/api/v1/widget/{pk}", headers=H).get_json()["name"] == "A"
    lst = api.get("/api/v1/widget", headers=H).get_json()
    assert lst["total"] == 1 and lst["data"][0]["id"] == pk

    assert api.patch(f"/api/v1/widget/{pk}", json={"qty": 9}, headers=H).get_json()["qty"] == 9

    assert api.post("/api/v1/widget", json={"name": "B", "nope": 1}, headers=H).status_code == 400
    assert api.post("/api/v1/widget", json={"name": "B", "qty": "abc"}, headers=H).status_code == 400

    assert api.delete(f"/api/v1/widget/{pk}", headers=H).status_code == 204
    assert api.get(f"/api/v1/widget/{pk}", headers=H).status_code == 404

def test_api_permissions_and_ownership(app, client):
    _setup(client)
    tid = _make_table(client, app, "lead", "Lead", "name")
    fid = _make_form(client, app, "lead_form", "Leads", tid)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    A = _mint(app, "amy", "amy")
    B = _mint(app, "boss", "boss")
    api = app.test_client()

    _ok(client.post("/designer/permissions", data={f"access_{fid}": "read"}, follow_redirects=True))
    _ok(api.get("/api/v1/lead", headers=A))                       # read allowed
    assert api.post("/api/v1/lead", json={"name": "x"}, headers=A).status_code == 403

    _ok(client.post("/designer/permissions", data={f"access_{fid}": "write"}, follow_redirects=True))
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(row_owned="y"), follow_redirects=True))
    assert api.post("/api/v1/lead", json={"name": "amy-row"}, headers=A).status_code == 201
    assert api.post("/api/v1/lead", json={"name": "boss-row"}, headers=B).status_code == 201

    amy_names = {r["name"] for r in api.get("/api/v1/lead", headers=A).get_json()["data"]}
    assert amy_names == {"amy-row"}                               # ownership scoping
    boss_names = {r["name"] for r in api.get("/api/v1/lead", headers=B).get_json()["data"]}
    assert {"amy-row", "boss-row"} <= boss_names                  # designer sees all

def test_api_token_lifecycle(app, client):
    _setup(client)
    _make_table(client, app, "widget", "Widget", "name")
    from app.metadata.models import ApiToken
    H = _mint(app, "boss")
    api = app.test_client()
    _ok(api.get("/api/v1/widget", headers=H))
    with app.app_context():
        tok_id = SessionLocal().scalar(select(ApiToken)).id

    _ok(client.post(f"/u/tokens/{tok_id}/revoke", follow_redirects=True))   # owner revokes via UI
    assert api.get("/api/v1/widget", headers=H).status_code == 401          # revoked → unauthorized

    # a different user cannot revoke someone else's token
    amy = _new_amy(app, client)
    H2 = _mint(app, "boss", "t2")
    with app.app_context():
        tok2 = SessionLocal().scalars(select(ApiToken).order_by(ApiToken.id.desc())).first().id
    _ok(amy.post(f"/u/tokens/{tok2}/revoke", follow_redirects=True))
    with app.app_context():
        assert SessionLocal().get(ApiToken, tok2).revoked is False
    _ok(api.get("/api/v1/widget", headers=H2))                              # still works

def test_api_openapi_and_docs(app, client):
    _setup(client)
    tid = _make_table(client, app, "widget", "Widget", "name")
    _add_field(client, tid, "qty", "integer")
    _add_field(client, tid, "status", "enum", enum_options="new\ndone")
    _ok(client.post(f"/designer/tables/{tid}/fields",                 # required (nullable omitted)
                    data=dict(phys_name="code", label="Code", data_type="string", length=40),
                    follow_redirects=True))
    H = _mint(app, "boss")
    api = app.test_client()

    spec = api.get("/api/v1/openapi.json", headers=H)
    assert spec.status_code == 200
    doc = spec.get_json()
    assert doc["openapi"].startswith("3.0")
    assert "/widget" in doc["paths"] and "/widget/bulk" in doc["paths"]
    assert {"post", "patch", "delete"} <= set(doc["paths"]["/widget/bulk"])
    props = doc["components"]["schemas"]["widget"]["properties"]
    assert props["qty"]["type"] == "integer"
    assert props["status"]["enum"] == ["new", "done"]
    assert props["id"]["readOnly"] is True                       # pk is read-only
    assert "code" in doc["components"]["schemas"]["widget"]["required"]   # non-nullable ⇒ required

    docs = api.get("/api/v1/docs", headers=H)
    assert docs.status_code == 200
    body = docs.get_data(as_text=True)
    assert "/api/v1/widget/bulk" in body and "widget" in body    # self-hosted reference renders

def test_api_bulk_operations(app, client):
    _setup(client)
    tid = _make_table(client, app, "widget", "Widget", "name")
    _add_field(client, tid, "qty", "integer")
    fid = _make_form(client, app, "widget_form", "Widgets", tid)
    with app.app_context():
        boss_id = SessionLocal().scalar(select(AppUser).where(AppUser.username == "boss")).id
    # a create trigger proves the bulk path goes through record_service (triggers fire per row)
    _make_trigger(app, "widget", name="On add", event="create", in_app=True,
                  notify_target="user", notify_user_id=boss_id, message="new {name}")
    H = _mint(app, "boss")
    api = app.test_client()

    # bulk create: 3 valid rows
    r = api.post("/api/v1/widget/bulk",
                 json={"records": [{"name": "A", "qty": 1}, {"name": "B", "qty": 2},
                                   {"name": "C", "qty": 3}]}, headers=H)
    assert r.status_code == 201 and len(r.get_json()["created"]) == 3
    with app.app_context():
        assert SessionLocal().scalar(select(func.count()).select_from(Notification)
                                     .where(Notification.channel == "in_app")) == 3   # fired per row

    # one bad row → 207, the others still created
    r2 = api.post("/api/v1/widget/bulk",
                  json={"records": [{"name": "D", "qty": 4}, {"name": "E", "qty": "bad"}]}, headers=H)
    assert r2.status_code == 207
    assert len(r2.get_json()["created"]) == 1 and r2.get_json()["errors"][0]["index"] == 1
    ids = r.get_json()["created"]

    # bulk update: a real id + a missing id
    up = api.patch("/api/v1/widget/bulk",
                   json=[{"id": ids[0], "qty": 99}, {"id": 999999, "qty": 1}], headers=H)
    assert up.status_code == 207 and up.get_json()["updated"] == [ids[0]]
    assert api.get(f"/api/v1/widget/{ids[0]}", headers=H).get_json()["qty"] == 99

    # bulk delete: a real id + a missing id
    dl = api.delete("/api/v1/widget/bulk", json={"ids": [ids[1], 999999]}, headers=H)
    assert dl.status_code == 207 and dl.get_json()["deleted"] == [ids[1]]
    assert api.get(f"/api/v1/widget/{ids[1]}", headers=H).status_code == 404

    # over the cap → 400
    big = api.post("/api/v1/widget/bulk",
                   json={"records": [{"name": str(i)} for i in range(1001)]}, headers=H)
    assert big.status_code == 400

    # read-only role can't bulk-write → 403
    _new_amy(app, client)
    Hamy = _mint(app, "amy", "amy")
    _ok(client.post("/designer/permissions", data={f"access_{fid}": "read"}, follow_redirects=True))
    assert api.post("/api/v1/widget/bulk", json={"records": [{"name": "X"}]},
                    headers=Hamy).status_code == 403
