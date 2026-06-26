"""Integration tests for schema_service DDL + data_service CRUD (live test DB)."""
from types import SimpleNamespace

from sqlalchemy import inspect, text

from app import data_service as ds
from app.metadata import schema_service as ss


def field(**kw):
    base = dict(phys_name="f", data_type="string", length=None, precision=None,
                scale=None, nullable=True, is_unique=False, enum_options=None,
                on_delete=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_create_table_and_crud(engine):
    ss.create_physical_table(engine, "person", [])
    ss.add_scalar_column(engine, "person", field(phys_name="name", data_type="string", length=80))
    ss.add_scalar_column(engine, "person", field(phys_name="age", data_type="integer"))
    assert ss.table_exists(engine, "person")

    pk = ds.insert_row(engine, "person", {"name": "Alice", "age": 30, "id": 999})
    assert ds.get_row(engine, "person", pk)["name"] == "Alice"

    ds.insert_row(engine, "person", {"name": "Bob", "age": 41})
    rows, total = ds.list_rows(engine, "person",
                               filters=[{"col": "name", "op": "contains", "value": "Ali"}])
    assert total == 1 and rows[0]["name"] == "Alice"

    ds.update_row(engine, "person", pk, {"age": 31})
    assert ds.get_row(engine, "person", pk)["age"] == 31

    ds.delete_row(engine, "person", pk)
    assert ds.get_row(engine, "person", pk) is None


def test_relations_and_links(engine):
    ss.create_physical_table(engine, "post", [])
    ss.create_physical_table(engine, "topic", [])
    ss.add_scalar_column(engine, "topic", field(phys_name="name", data_type="string", length=40))

    # many-to-one: post.author references topic (any table with id works for the test)
    ss.add_relation_column(engine, "post",
                           field(phys_name="topic_id", nullable=True, on_delete="SET NULL"),
                           "topic")
    fks = ds.reflect_table(engine, "post").foreign_keys
    assert any(fk.column.table.name == "topic" for fk in fks)

    # many-to-many junction
    ss.create_junction_table(engine, "j_post_topic", "post", "post_id", "topic", "topic_id")
    p = ds.insert_row(engine, "post", {})
    t1 = ds.insert_row(engine, "topic", {"name": "a"})
    t2 = ds.insert_row(engine, "topic", {"name": "b"})

    ds.set_links(engine, "j_post_topic", "post_id", p, "topic_id", [t1, t2])
    assert set(ds.get_links(engine, "j_post_topic", "post_id", p, "topic_id")) == {t1, t2}

    ds.set_links(engine, "j_post_topic", "post_id", p, "topic_id", [t1])
    assert ds.get_links(engine, "j_post_topic", "post_id", p, "topic_id") == [t1]


def test_drop_column(engine):
    ss.create_physical_table(engine, "widget", [])
    ss.add_scalar_column(engine, "widget", field(phys_name="color", data_type="string", length=20))
    assert "color" in ds.column_names(ds.reflect_table(engine, "widget"))
    ss.drop_column(engine, "widget", "color")
    assert "color" not in ds.column_names(ds.reflect_table(engine, "widget"))


def test_load_options_composite(engine):
    ss.create_physical_table(engine, "company", [])
    ss.add_scalar_column(engine, "company", field(phys_name="name", data_type="string", length=40))
    ss.add_scalar_column(engine, "company", field(phys_name="email", data_type="string", length=60))
    cid = ds.insert_row(engine, "company", {"name": "Acme", "email": "a@acme.test"})

    assert ds.load_options(engine, "company", "name") == [(cid, "Acme")]          # single (str)
    assert ds.load_options(engine, "company", ["name", "email"]) == [(cid, "Acme — a@acme.test")]

    bid = ds.insert_row(engine, "company", {})  # all label fields empty
    assert dict(ds.load_options(engine, "company", ["name", "email"]))[bid] == f"#{bid}"


def test_modify_column_rename_and_redefine(engine):
    ss.create_physical_table(engine, "person", [])
    ss.add_scalar_column(engine, "person", field(phys_name="fname", data_type="string", length=20))
    pk = ds.insert_row(engine, "person", {"fname": "Alice"})

    newf = field(phys_name="full_name", data_type="string", length=80, nullable=True)
    ss.modify_column(engine, "person", "fname", newf)

    cols = ds.column_names(ds.reflect_table(engine, "person"))
    assert "full_name" in cols and "fname" not in cols
    assert ds.get_row(engine, "person", pk)["full_name"] == "Alice"   # data preserved


def test_modify_column_adds_unique(engine):
    ss.create_physical_table(engine, "code_tbl", [])
    ss.add_scalar_column(engine, "code_tbl", field(phys_name="code", data_type="string", length=10))
    ss.modify_column(engine, "code_tbl", "code",
                     field(phys_name="code", data_type="string", length=10, is_unique=True))
    ds.insert_row(engine, "code_tbl", {"code": "x"})
    try:
        ds.insert_row(engine, "code_tbl", {"code": "x"})
        raise AssertionError("expected a unique-constraint violation")
    except Exception as exc:  # noqa: BLE001
        assert "Duplicate" in str(exc) or "unique" in str(exc).lower()


def test_load_options_with_extra(engine):
    ss.create_physical_table(engine, "city", [])
    ss.add_scalar_column(engine, "city", field(phys_name="name", data_type="string", length=40))
    ss.add_scalar_column(engine, "city", field(phys_name="region", data_type="string", length=20))
    cid = ds.insert_row(engine, "city", {"name": "Paris", "region": "EU"})
    assert ds.load_options_with(engine, "city", ["name"], "region") == [(cid, "Paris", "EU")]


def test_filter_operators(engine):
    ss.create_physical_table(engine, "item", [])
    ss.add_scalar_column(engine, "item", field(phys_name="name", data_type="string", length=40))
    ss.add_scalar_column(engine, "item", field(phys_name="qty", data_type="integer"))
    for n, qty in [("Apple", 5), ("Apricot", 10), ("Banana", 3), ("cherry", None)]:
        ds.insert_row(engine, "item", {"name": n, "qty": qty})

    def names(filters):
        rows, _ = ds.list_rows(engine, "item", filters=filters, per_page=100)
        return sorted(r["name"] for r in rows)

    def T(op, val):
        return [{"col": "name", "op": op, "value": val, "is_text": True}]

    def N(op, val):
        return [{"col": "qty", "op": op, "value": val}]

    assert names(T("contains", "ap")) == ["Apple", "Apricot"]
    assert names(T("not_contains", "ap")) == ["Banana", "cherry"]
    assert names(T("starts_with", "Ap")) == ["Apple", "Apricot"]
    assert names(T("ends_with", "rry")) == ["cherry"]
    assert names(T("eq", "Banana")) == ["Banana"]
    assert names(T("ne", "Banana")) == ["Apple", "Apricot", "cherry"]
    assert names(N("gt", "4")) == ["Apple", "Apricot"]
    assert names(N("lt", "5")) == ["Banana"]
    assert names(N("gte", "5")) == ["Apple", "Apricot"]
    assert names(N("empty", "")) == ["cherry"]
    assert names(N("not_empty", "")) == ["Apple", "Apricot", "Banana"]
    # multiple conditions AND together
    assert names([{"col": "name", "op": "starts_with", "value": "Ap", "is_text": True},
                  {"col": "qty", "op": "gte", "value": "10"}]) == ["Apricot"]


def test_ensure_meta_schema_idempotent(engine):
    from app.metadata.models import Base

    Base.metadata.create_all(engine)
    added = {"to_display_field_ids", "from_display_field_ids"}
    assert added <= {c["name"] for c in inspect(engine).get_columns("app_meta_relation")}

    # simulate an older database missing the new columns, then migrate twice
    q = engine.dialect.identifier_preparer.quote
    with engine.begin() as conn:
        for col in added:
            conn.execute(text(f"ALTER TABLE {q('app_meta_relation')} DROP COLUMN {q(col)}"))
    ss.ensure_meta_schema(engine)
    ss.ensure_meta_schema(engine)
    assert added <= {c["name"] for c in inspect(engine).get_columns("app_meta_relation")}
