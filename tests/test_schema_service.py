"""Unit tests for type mapping and DDL generation (no database required)."""
import json
from types import SimpleNamespace

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.metadata import schema_service as ss


def field(**kw):
    base = dict(phys_name="f", data_type="string", length=None, precision=None,
                scale=None, nullable=True, is_unique=False, enum_options=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_type_mapping():
    assert isinstance(ss.sa_type_for_field(field(data_type="string", length=120)), String)
    assert isinstance(ss.sa_type_for_field(field(data_type="text")), Text)
    assert isinstance(ss.sa_type_for_field(field(data_type="integer")), Integer)
    assert isinstance(ss.sa_type_for_field(field(data_type="boolean")), Boolean)
    assert isinstance(ss.sa_type_for_field(field(data_type="date")), Date)
    assert isinstance(ss.sa_type_for_field(field(data_type="datetime")), DateTime)
    dec = ss.sa_type_for_field(field(data_type="decimal", precision=10, scale=2))
    assert isinstance(dec, Numeric) and dec.precision == 10 and dec.scale == 2
    strtype = ss.sa_type_for_field(field(data_type="string", length=64))
    assert strtype.length == 64


def test_enum_type_uses_options():
    t = ss.sa_type_for_field(field(data_type="enum", enum_options=json.dumps(["a", "b"])))
    assert set(t.enums) == {"a", "b"}


def test_build_data_table_ddl():
    fields = [
        field(phys_name="name", data_type="string", length=120, nullable=False),
        field(phys_name="active", data_type="boolean"),
        field(phys_name="customer_id", data_type=ss.RELATION_TYPE),  # FK skipped here
    ]
    table = ss.build_data_table("customer", fields)
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
    assert "CREATE TABLE customer" in ddl
    assert "id INTEGER NOT NULL AUTO_INCREMENT" in ddl
    assert "name VARCHAR(120) NOT NULL" in ddl
    assert "active BOOL" in ddl
    assert "PRIMARY KEY (id)" in ddl
    # relation column is added later via ALTER, not at create time
    assert "customer_id" not in ddl
