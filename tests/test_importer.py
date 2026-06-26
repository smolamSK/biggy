"""Unit tests for CSV value coercion and template generation (no database)."""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import importer


def f(data_type, **kw):
    base = dict(phys_name="c", data_type=data_type, nullable=True, default_value=None,
                enum_options=None, related_table_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_coerce_scalars():
    assert importer.coerce_value(f("string"), "hi") == "hi"
    assert importer.coerce_value(f("string"), "") is None
    assert importer.coerce_value(f("string"), None) is None
    assert importer.coerce_value(f("integer"), "5") == 5
    assert importer.coerce_value(f("decimal"), "1.50") == Decimal("1.50")
    assert importer.coerce_value(f("boolean"), "Yes") is True
    assert importer.coerce_value(f("boolean"), "0") is False
    assert importer.coerce_value(f("date"), "2024-01-02") == date(2024, 1, 2)
    assert importer.coerce_value(f("datetime"), "2024-01-02 03:04") == datetime(2024, 1, 2, 3, 4)


@pytest.mark.parametrize("dt,val", [
    ("integer", "x"), ("decimal", "abc"), ("boolean", "maybe"), ("date", "nope"),
])
def test_coerce_bad_values_raise(dt, val):
    with pytest.raises(ValueError):
        importer.coerce_value(f(dt), val)


def test_coerce_enum():
    fld = f("enum", enum_options='["open","closed"]')
    assert importer.coerce_value(fld, "open") == "open"
    with pytest.raises(ValueError):
        importer.coerce_value(fld, "nope")


def test_coerce_relation_id_fallback():
    # with no resolver, a relation cell is treated as a raw id
    assert importer.coerce_value(f("relation"), "7", None) == 7


def test_coerce_enforces_validation_rules():
    assert importer.coerce_value(f("string", max_length=3), "abc") == "abc"
    with pytest.raises(ValueError):
        importer.coerce_value(f("string", max_length=3), "abcd")
    with pytest.raises(ValueError):
        importer.coerce_value(f("string", min_length=2), "a")
    with pytest.raises(ValueError):
        importer.coerce_value(f("string", pattern=r"^\d+$"), "abc")
    assert importer.coerce_value(f("integer", max_value="120"), "100") == 100
    with pytest.raises(ValueError):
        importer.coerce_value(f("integer", max_value="120"), "200")
    with pytest.raises(ValueError):
        importer.coerce_value(f("integer", min_value="0"), "-5")


def test_template_csv_header():
    mt = SimpleNamespace(fields=[f("string", phys_name="name"), f("integer", phys_name="age")])
    assert importer.template_csv(mt).strip() == "name,age"
