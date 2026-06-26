"""Unit tests for identifier validation (no database required)."""
import pytest

from app.identifiers import IdentifierError, junction_name, validate_identifier


@pytest.mark.parametrize("name,expected", [
    ("Customer", "customer"),
    ("  Order ", "order"),
    ("first_name", "first_name"),
    ("a1_b2", "a1_b2"),
])
def test_valid_identifiers_are_normalized(name, expected):
    assert validate_identifier(name, kind="Table") == expected


@pytest.mark.parametrize("name", [
    "", "1col", "col-name", "col name", "col;drop", "schöne", "x" * 61,
])
def test_invalid_identifiers_rejected(name):
    with pytest.raises(IdentifierError):
        validate_identifier(name, kind="Column")


@pytest.mark.parametrize("name", ["app_meta", "j_link"])
def test_reserved_prefixes_blocked(name):
    with pytest.raises(IdentifierError):
        validate_identifier(name)
    # ...unless explicitly allowed
    assert validate_identifier(name, allow_reserved=True) == name


def test_junction_name_is_order_independent():
    assert junction_name("order", "tag") == junction_name("tag", "order") == "j_order_tag"
