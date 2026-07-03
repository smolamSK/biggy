"""Connections/feeds (out), webhooks (in) and pull sources. (Split from test_features.py.)"""
import hashlib
import hmac
import io
import json

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    Connection,
    MetaTable,
    Notification,
    PullSource,
    Webhook,
)
from tests.helpers import (
    _add_field,
    _fid,
    _loopback,
    _make_connection,
    _make_feed_orm,
    _make_form,
    _make_form_p,
    _make_pull_orm,
    _make_table,
    _make_webhook,
    _make_workflow,
    _ok,
    _raw_token,
    _setup,
    _status_field_id,
)


def test_connection_crud_and_ping(app, client):
    from app import connectors
    _setup(client)
    _make_table(client, app, "widget", "Widget", "name")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        with app.app_context():
            assert SessionLocal().get(Connection, cid).token == raw

        # Test button pings the peer and records the reachable tables
        r = client.post(f"/designer/connections/{cid}/test", follow_redirects=True)
        assert "widget" in r.get_data(as_text=True)

        # the /fields endpoint is reachable through the connector
        with app.app_context():
            fields = connectors.remote_fields(SessionLocal().get(Connection, cid), "widget")
        assert "name" in {f["name"] for f in fields}

        # editing with a blank token keeps the existing secret
        _ok(client.post(f"/designer/connections/{cid}",
                        data={"name": "peer2", "base_url": "http://self", "active": "y", "token": ""},
                        follow_redirects=True))
        with app.app_context():
            c = SessionLocal().get(Connection, cid)
            assert c.name == "peer2" and c.token == raw
    finally:
        connectors.set_transport(None)

def test_feed_event_push(app, client):
    from app import connectors
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _make_table(client, app, "ordr", "Order", "name")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")], event="create")
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "Big deal"},
                        follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                names = [r[0] for r in c.execute(text("SELECT name FROM ordr")).all()]
            note = SessionLocal().scalar(
                select(Notification).where(Notification.channel == "feed"))
        assert names == ["Big deal"]                   # pushed through the real /api/v1
        assert note is not None and note.status == "sent"
    finally:
        connectors.set_transport(None)

def test_feed_upsert_and_workflow(app, client):
    from app import connectors
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _add_field(client, deal_tid, "ref", "string", length="40")
    _add_field(client, deal_tid, "stage", "string", length="40")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)

    order_tid = _make_table(client, app, "ordr", "Order", "name")
    _add_field(client, order_tid, "ext_ref", "string", length="40")
    _add_field(client, order_tid, "status", "enum", enum_options="new\nfulfilled\ncancelled")
    sid = _status_field_id(app, "ordr")
    _make_workflow(client, app, sid, [{"from": "new", "to": "fulfilled", "roles": []}], "new")

    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, deal_tid, cid, "ordr",
                       [("title", "name"), ("ref", "ext_ref"), ("stage", "status")],
                       event="update", mode="upsert", match="ext_ref")
        _ok(client.post(f"/u/forms/{deal_fid}/new",
                        data={"title": "T1", "ref": "D1", "stage": "new"}, follow_redirects=True))
        with app.app_context():
            pk = get_engine().connect().execute(text("SELECT id FROM crm_deal LIMIT 1")).scalar()
        edit = f"/u/forms/{deal_fid}/{pk}/edit"

        def _order():
            with app.app_context():
                with get_engine().connect() as c:
                    return c.execute(text("SELECT name, status FROM ordr WHERE ext_ref='D1'")
                                     ).mappings().first()

        def _count():
            with app.app_context():
                with get_engine().connect() as c:
                    return c.execute(text("SELECT COUNT(*) FROM ordr")).scalar()

        # first update creates the order (upsert finds none) with status 'new'
        _ok(client.post(edit, data={"title": "T1", "ref": "D1", "stage": "new"},
                        follow_redirects=True))
        assert _count() == 1 and _order()["status"] == "new"

        # second update upserts the SAME order and drives new -> fulfilled
        _ok(client.post(edit, data={"title": "T1", "ref": "D1", "stage": "fulfilled"},
                        follow_redirects=True))
        assert _count() == 1                            # no duplicate
        assert _order()["status"] == "fulfilled"       # remote workflow transition applied

        # illegal fulfilled -> cancelled is rejected by the *remote* workflow
        _ok(client.post(edit, data={"title": "T1", "ref": "D1", "stage": "cancelled"},
                        follow_redirects=True))
        assert _order()["status"] == "fulfilled"       # unchanged
        with app.app_context():
            statuses = [n.status for n in SessionLocal().scalars(
                select(Notification).where(Notification.channel == "feed")
                .order_by(Notification.id))]
        assert "failed" in statuses
    finally:
        connectors.set_transport(None)

def test_feed_condition_gating(app, client):
    from app import connectors
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _add_field(client, deal_tid, "stage", "string", length="40")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    _make_table(client, app, "ordr", "Order", "name")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")], event="create",
                       cond_field_id=_fid(app, "crm_deal", "stage"), cond_op="eq",
                       cond_value="won")
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "A", "stage": "open"},
                        follow_redirects=True))
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "B", "stage": "won"},
                        follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                names = [r[0] for r in c.execute(text("SELECT name FROM ordr ORDER BY id")).all()]
        assert names == ["B"]                           # only the 'won' deal pushed
    finally:
        connectors.set_transport(None)

def test_feed_designer_ui(app, client):
    from app.metadata.models import Feed
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _make_table(client, app, "ordr", "Order", "name")
    cid = _make_connection(app, client, _raw_token(app))

    _ok(client.post("/designer/feeds",
                    data={"table_id": deal_tid, "connection_id": cid}, follow_redirects=True))
    with app.app_context():
        fid = SessionLocal().scalar(select(Feed)).id
    _ok(client.get(f"/designer/feeds/{fid}"))           # edit page renders
    _ok(client.post(f"/designer/feeds/{fid}", data={
        "name": "Deal to Order", "connection_id": cid, "target_table": "ordr",
        "mode": "create", "event": "create", "field_id": 0, "cond_field_id": 0,
        "cond_op": "", "active": "y", "allow_manual": "y", "skip_api_writes": "y",
        "map_source": ["title", "stage"], "map_target": ["name", ""]}, follow_redirects=True))
    with app.app_context():
        feed = SessionLocal().get(Feed, fid)
        assert feed.name == "Deal to Order" and feed.event == "create"
        assert json.loads(feed.field_map) == [{"target": "name", "source": "title"}]

def test_feed_manual_send(app, client):
    from app import connectors
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    _make_table(client, app, "ordr", "Order", "name")
    _make_form_p(client, app, "deal_view", "Deal", deal_tid, "view")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        # manual-only feed (no live event)
        _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")], event=None)
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "A"}, follow_redirects=True))
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "B"}, follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                assert c.execute(text("SELECT COUNT(*) FROM ordr")).scalar() == 0   # no live push
                ids = [r[0] for r in c.execute(text("SELECT id FROM crm_deal ORDER BY id")).all()]

        # bulk 'Send to tools' pushes both selected rows
        _ok(client.post(f"/u/forms/{deal_fid}/send",
                        data={"ids": [str(i) for i in ids]}, follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                names = sorted(r[0] for r in c.execute(text("SELECT name FROM ordr")).all())
        assert names == ["A", "B"]

        # the single-record view page offers the same action
        v = client.get(f"/u/view/{deal_tid}/{ids[0]}").get_data(as_text=True)
        assert "Send to tools" in v
    finally:
        connectors.set_transport(None)

def test_feed_scheduled(app, client):
    from app import connectors
    from app import feeds as feeds_mod
    from app.metadata.models import Feed
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    _make_table(client, app, "ordr", "Order", "name")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        feed_id = _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")],
                                 event=None, schedule_minutes=15)
        for t in ("A", "B"):
            _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": t}, follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                ids = [r[0] for r in c.execute(text("SELECT id FROM crm_deal ORDER BY id")).all()]

        def _names():
            with app.app_context():
                with get_engine().connect() as c:
                    return sorted(r[0] for r in c.execute(text("SELECT name FROM ordr")).all())

        # due (never run) -> pushes both rows; the watermark advances
        with app.app_context():
            assert feeds_mod.run_scheduled(SessionLocal(), get_engine()) == 2
            assert SessionLocal().get(Feed, feed_id).watermark == max(ids)
        assert _names() == ["A", "B"]

        # not due again so soon -> no-op (schedule gate)
        with app.app_context():
            assert feeds_mod.run_scheduled(SessionLocal(), get_engine()) == 0

        # 'Run now' bypasses the schedule and pushes only rows past the watermark
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "C"}, follow_redirects=True))
        _ok(client.post(f"/designer/feeds/{feed_id}/run", follow_redirects=True))
        assert _names() == ["A", "B", "C"]

        # the `flask sync` CLI runs without error (now an alias for run-jobs)
        assert "Ran jobs" in app.test_cli_runner().invoke(args=["sync"]).output
    finally:
        connectors.set_transport(None)

def test_feed_schema_roundtrip(app, client):
    from app.metadata.models import Feed
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _make_table(client, app, "ordr", "Order", "name")
    cid = _make_connection(app, client, "secret-token-123")
    _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")], event="create",
                   mode="upsert", match="name")

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    data = json.loads(exp.get_data())
    assert data["connections"] and "token" not in data["connections"][0]   # secret redacted
    assert data["feeds"] and data["feeds"][0]["target_table"] == "ordr"

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        conn = s.scalar(select(Connection))
        feed = s.scalar(select(Feed))
        assert conn is not None and conn.token is None          # re-entered after import
        assert feed is not None and feed.target_table == "ordr" and feed.mode == "upsert"
        assert json.loads(feed.field_map) == [{"source": "title", "target": "name"}]
        assert feed.connection_id == conn.id                    # re-wired to imported ids
        assert feed.source_table_id == s.scalar(
            select(MetaTable).where(MetaTable.phys_name == "crm_deal")).id

def test_feed_loop_guard(app, client):
    """A feed on the target table must not re-fire when the write arrived via the API."""
    from app import connectors
    _setup(client)
    a_tid = _make_table(client, app, "tool_a", "A", "name")
    b_tid = _make_table(client, app, "tool_b", "B", "name")
    a_fid = _make_form(client, app, "a_form", "A", a_tid)
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, a_tid, cid, "tool_b", [("name", "name")], event="create")
        # back-feed B -> A, guarded against API-originated writes (the default)
        _make_feed_orm(app, b_tid, cid, "tool_a", [("name", "name")], event="create",
                       skip_api_writes=True)
        _ok(client.post(f"/u/forms/{a_fid}/new", data={"name": "X"}, follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                a_count = c.execute(text("SELECT COUNT(*) FROM tool_a")).scalar()
                b_count = c.execute(text("SELECT COUNT(*) FROM tool_b")).scalar()
        assert a_count == 1 and b_count == 1            # A pushed to B; B did NOT loop back
    finally:
        connectors.set_transport(None)

def test_webhook_receive_upsert_and_dotted_path(app, client):
    _setup(client)
    tid = _make_table(client, app, "lead", "Lead", "name")
    _add_field(client, tid, "email", "email", length=120)
    _add_field(client, tid, "score", "integer")
    # email comes from a nested path; upsert keyed on email
    _, token = _make_webhook(client, app, tid,
                             [("name", "name"), ("customer.email", "email"), ("score", "score")],
                             mode="upsert", match_field="email")

    r = client.post(f"/hooks/{token}",
                    json={"name": "Ada", "customer": {"email": "ada@x.com"}, "score": 7})
    assert r.status_code == 201 and r.get_json()["action"] == "create"
    with app.app_context():
        with get_engine().connect() as c:
            row = c.execute(text("SELECT name, email, score FROM lead")).mappings().all()
        assert len(row) == 1
        assert row[0]["name"] == "Ada" and row[0]["email"] == "ada@x.com" and row[0]["score"] == 7
        # delivery logged on the inbound channel
        assert SessionLocal().scalar(select(func.count()).select_from(Notification).where(
            Notification.channel == "webhook_in", Notification.status == "received")) == 1

    # second POST with the same key updates the same row (no duplicate)
    r2 = client.post(f"/hooks/{token}",
                     json={"name": "Ada L", "customer": {"email": "ada@x.com"}, "score": 9})
    assert r2.status_code == 200 and r2.get_json()["action"] == "update"
    with app.app_context():
        with get_engine().connect() as c:
            rows = c.execute(text("SELECT name, score FROM lead")).mappings().all()
        assert len(rows) == 1 and rows[0]["name"] == "Ada L" and rows[0]["score"] == 9

def test_webhook_hmac_and_unknown_token(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "subject")
    secret = "sh4red-s3cret"
    _, token = _make_webhook(client, app, tid, [("subject", "subject")], secret=secret)

    body = json.dumps({"subject": "Help"}).encode("utf-8")
    def sign(b):
        return "sha256=" + hmac.new(secret.encode(), b, hashlib.sha256).hexdigest()

    # unsigned and badly-signed are rejected
    assert client.post(f"/hooks/{token}", data=body,
                       content_type="application/json").status_code == 401
    assert client.post(f"/hooks/{token}", data=body, content_type="application/json",
                       headers={"X-Biggy-Signature": "sha256=deadbeef"}).status_code == 401
    # a valid signature is accepted
    r = client.post(f"/hooks/{token}", data=body, content_type="application/json",
                    headers={"X-Biggy-Signature": sign(body)})
    assert r.status_code == 201
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT subject FROM ticket")).scalar() == "Help"

    # an unknown token is a 404, with nothing written
    assert client.post("/hooks/whk_does_not_exist", json={"subject": "X"}).status_code == 404
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM ticket")).scalar() == 1

def test_webhook_schema_roundtrip(app, client):
    _setup(client)
    tid = _make_table(client, app, "signup", "Signup", "email")
    _make_webhook(client, app, tid, [("user.email", "email")], mode="upsert",
                  match_field="email", secret="top-secret",
                  max_body_bytes=2048, rate_limit=30, rate_window=10)

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    data = json.loads(exp.get_data())
    assert data["webhooks"] and data["webhooks"][0]["mode"] == "upsert"
    wb = data["webhooks"][0]
    assert "token_hash" not in wb and "secret" not in wb        # secrets redacted
    assert json.loads(wb["field_map"]) == [{"source": "user.email", "target": "email"}]
    assert wb["max_body_bytes"] == 2048 and wb["rate_limit"] == 30 and wb["rate_window"] == 10

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        wh = s.scalar(select(Webhook))
        assert wh is not None and wh.mode == "upsert" and wh.match_field == "email"
        assert wh.token_hash and wh.secret is None              # fresh token, secret re-entered
        assert wh.max_body_bytes == 2048 and wh.rate_limit == 30 and wh.rate_window == 10
        assert wh.target_table_id == s.scalar(
            select(MetaTable).where(MetaTable.phys_name == "signup")).id

def test_webhook_payload_size_cap(app, client):
    _setup(client)
    tid = _make_table(client, app, "blurb", "Blurb", "body")
    # a per-webhook cap of 200 bytes (set via the designer form)
    _, token = _make_webhook(client, app, tid, [("body", "body")], max_body_bytes=200)

    big = client.post(f"/hooks/{token}", json={"body": "x" * 500})
    assert big.status_code == 413
    small = client.post(f"/hooks/{token}", json={"body": "ok"})
    assert small.status_code == 201
    with app.app_context():
        with get_engine().connect() as c:
            rows = c.execute(text("SELECT body FROM blurb")).scalars().all()
    assert rows == ["ok"]                                       # oversized one never written

def test_webhook_rate_limit(app, client):
    _setup(client)
    tid = _make_table(client, app, "ping", "Ping", "label")
    # allow 2 per window; the 3rd is throttled (set via the designer form)
    _, token = _make_webhook(client, app, tid, [("label", "label")],
                             rate_limit=2, rate_window=60)

    assert client.post(f"/hooks/{token}", json={"label": "a"}).status_code == 201
    assert client.post(f"/hooks/{token}", json={"label": "b"}).status_code == 201
    blocked = client.post(f"/hooks/{token}", json={"label": "c"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1            # tells the caller when to retry
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM ping")).scalar() == 2   # 'c' rejected

def test_pull_from_peer(app, client):
    from app import connectors, pull, scheduler
    _setup(client)
    src_tid = _make_table(client, app, "widget", "Widget", "name")
    _add_field(client, src_tid, "sku", "string", length="40")
    src_fid = _make_form(client, app, "widget_form", "Widgets", src_tid)
    dest_tid = _make_table(client, app, "mirror", "Mirror", "title")
    _add_field(client, dest_tid, "code", "string", length="40")
    for nm, sku in [("Alpha", "A1"), ("Beta", "B2")]:
        _ok(client.post(f"/u/forms/{src_fid}/new", data={"name": nm, "sku": sku},
                        follow_redirects=True))

    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        pid = _make_pull_orm(app, dest_tid, [("name", "title"), ("sku", "code")],
                             kind="peer", connection_id=cid, remote_table="widget",
                             mode="upsert", match_field="code", cursor_field="id",
                             schedule_minutes=15)

        def _mirror():
            with app.app_context():
                with get_engine().connect() as c:
                    return sorted((r[0], r[1]) for r in
                                  c.execute(text("SELECT title, code FROM mirror")))

        # the scheduler runs the due pull → both remote rows land locally
        with app.app_context():
            assert scheduler.run_due(SessionLocal(), get_engine())["pulls"] == 2
        assert _mirror() == [("Alpha", "A1"), ("Beta", "B2")]
        with app.app_context():
            assert SessionLocal().scalar(select(func.count()).select_from(Notification).where(
                Notification.channel == "pull_in", Notification.status == "received")) >= 1

        # a new remote row → only it is pulled next time (watermark over id)
        _ok(client.post(f"/u/forms/{src_fid}/new", data={"name": "Gamma", "sku": "G3"},
                        follow_redirects=True))
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 1
        assert _mirror() == [("Alpha", "A1"), ("Beta", "B2"), ("Gamma", "G3")]

        # reset the watermark → all rows re-pulled but upserted on code (no duplicates)
        with app.app_context():
            s = SessionLocal()
            s.get(PullSource, pid).watermark = None
            s.commit()
            pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid)
        assert len(_mirror()) == 3
    finally:
        connectors.set_transport(None)

def test_pull_from_rest(app, client):
    from app import connectors, pull
    _setup(client)
    tid = _make_table(client, app, "person", "Person", "name")
    _add_field(client, tid, "email", "email", length="120")

    payload = {"result": {"items": [
        {"id": 1, "name": "Ann", "user": {"email": "ann@x.com"}},
        {"id": 2, "name": "Bob", "user": {"email": "bob@x.com"}}]}}

    def transport(method, url, headers, body):
        assert "api.test" in url and headers.get("X-Key") == "sek"   # url + headers honoured
        return 200, json.dumps(payload)

    connectors.set_transport(transport)
    try:
        pid = _make_pull_orm(app, tid, [("name", "name"), ("user.email", "email")],
                             kind="rest", url="http://api.test/people",
                             headers=json.dumps({"X-Key": "sek"}), records_path="result.items",
                             mode="upsert", match_field="email", cursor_field="id")

        def _people():
            with app.app_context():
                with get_engine().connect() as c:
                    return sorted((r[0], r[1]) for r in
                                  c.execute(text("SELECT name, email FROM person")))

        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 2
        assert _people() == [("Ann", "ann@x.com"), ("Bob", "bob@x.com")]   # nested dotted path

        # same canned response → cursor watermark filters everything out (no re-import)
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 0
        assert len(_people()) == 2
    finally:
        connectors.set_transport(None)

def test_pull_schema_roundtrip(app, client):
    _setup(client)
    _make_table(client, app, "widget", "Widget", "name")
    dest_tid = _make_table(client, app, "mirror", "Mirror", "title")
    cid = _make_connection(app, client, "secret-token")
    _make_pull_orm(app, dest_tid, [("name", "title")], kind="peer", connection_id=cid,
                   remote_table="widget", mode="upsert", match_field="title",
                   cursor_field="id", schedule_minutes=20, headers=json.dumps({"X-Key": "s"}))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    ps = json.loads(exp.get_data())["pull_sources"][0]
    assert ps["remote_table"] == "widget" and ps["schedule_minutes"] == 20
    assert "headers" not in ps and "watermark" not in ps        # secret + runtime state redacted

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        src = s.scalar(select(PullSource))
        conn = s.scalar(select(Connection))
        assert src is not None and src.kind == "peer" and src.cursor_field == "id"
        assert src.headers is None and src.watermark is None    # re-entered / reset on import
        assert src.connection_id == conn.id                     # re-wired to the imported connection
        assert src.target_table_id == s.scalar(
            select(MetaTable).where(MetaTable.phys_name == "mirror")).id

def test_pull_rest_pagination_auth_templating(app, client):
    from app import connectors, pull
    _setup(client)
    tid = _make_table(client, app, "thing", "Thing", "title")
    seen = {"auth": None, "urls": []}
    pages = {1: [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
             2: [{"id": 3, "name": "C"}], 3: []}

    def transport(method, url, headers, body):
        from urllib.parse import parse_qs, urlsplit
        seen["auth"] = headers.get("Authorization")
        seen["urls"].append(url)
        page = int(parse_qs(urlsplit(url).query).get("p", ["1"])[0])
        return 200, json.dumps({"items": pages.get(page, [])})

    connectors.set_transport(transport)
    try:
        pid = _make_pull_orm(
            app, tid, [("name", "title")], kind="rest", url="http://api.test/things",
            records_path="items", mode="create", watermark="2026-01-01",
            config={"auth": {"type": "bearer"}, "request": {"params": {"since": "{watermark}"}},
                    "pagination": {"style": "page", "param": "p", "max_pages": 5}},
            auth_secret="sek")
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 3
        with app.app_context():
            with get_engine().connect() as c:
                assert sorted(r[0] for r in c.execute(text("SELECT title FROM thing"))) == \
                    ["A", "B", "C"]                              # collected across pages
        assert seen["auth"] == "Bearer sek"                      # auth preset built the header
        assert any("since=2026-01-01" in u for u in seen["urls"])   # {watermark} substituted
        assert any("p=2" in u for u in seen["urls"])             # actually paginated
    finally:
        connectors.set_transport(None)

def test_pull_rest_max_pages_cap(app, client):
    """A source whose pages never empty must stop at max_pages (no runaway)."""
    from app import connectors, pull
    _setup(client)
    tid = _make_table(client, app, "blip", "Blip", "title")
    calls = {"n": 0}

    def transport(method, url, headers, body):
        calls["n"] += 1
        return 200, json.dumps({"items": [{"id": calls["n"], "name": f"r{calls['n']}"}]})

    connectors.set_transport(transport)
    try:
        pid = _make_pull_orm(app, tid, [("name", "title")], kind="rest",
                             url="http://api.test/blip", records_path="items", mode="create",
                             config={"pagination": {"style": "page", "param": "p", "max_pages": 3}})
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 3
        assert calls["n"] == 3                                   # capped at max_pages, not infinite
    finally:
        connectors.set_transport(None)

def test_pull_rest_template_transform_filter(app, client):
    from app import connectors, pull
    _setup(client)
    tid = _make_table(client, app, "person", "Person", "fullname")
    _add_field(client, tid, "state", "string", length="20")
    body = {"items": [
        {"id": 1, "first": "Ada", "last": "L", "status": "A", "archived": False},
        {"id": 2, "first": "Bo", "last": "X", "status": "", "archived": False},
        {"id": 3, "first": "Zed", "last": "Q", "status": "A", "archived": True}]}

    def transport(method, url, headers, _body):
        return 200, json.dumps(body)

    connectors.set_transport(transport)
    try:
        pid = _make_pull_orm(
            app, tid, [("{first} {last}", "fullname"), ("status", "state")], kind="rest",
            url="http://api.test/p", records_path="items", mode="create",
            config={"filter": {"field": "archived", "op": "is_false"},
                    "transforms": {"state": {"map": {"A": "Active"}, "default": "pending"}}})
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 2
        with app.app_context():
            with get_engine().connect() as c:
                rows = sorted((r[0], r[1]) for r in
                              c.execute(text("SELECT fullname, state FROM person")))
        # id=3 archived → filtered; {first} {last} template joins names; map A→Active; ""→default
        assert rows == [("Ada L", "Active"), ("Bo X", "pending")]
    finally:
        connectors.set_transport(None)

def test_pull_advanced_config_roundtrip(app, client):
    _setup(client)
    dest = _make_table(client, app, "mirror", "Mirror", "title")
    _make_pull_orm(app, dest, [("name", "title")], kind="rest", url="http://api.test/x",
                   config={"auth": {"type": "bearer"}, "pagination": {"style": "page", "param": "p"}},
                   auth_secret="topsecret", headers=json.dumps({"X": "y"}))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    ps = json.loads(exp.get_data())["pull_sources"][0]
    assert json.loads(ps["config"])["pagination"]["style"] == "page"
    assert "auth_secret" not in ps and "headers" not in ps      # secrets redacted

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        src = SessionLocal().scalar(select(PullSource))
        assert json.loads(src.config)["auth"]["type"] == "bearer"   # config round-trips
        assert src.auth_secret is None and src.headers is None      # secrets re-entered on import
