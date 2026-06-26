"""Unit tests for the read-only SQL console validator + CSV (no database)."""
import pytest

from app import sql_console


@pytest.mark.parametrize("q", [
    "SELECT 1",
    "  select * from t",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "-- a comment\nSELECT 2",
    "/* block */ SELECT 3",
    "SELECT 1;",
])
def test_validate_accepts_select(q):
    clean, err = sql_console.validate_select(q)
    assert err is None and clean


@pytest.mark.parametrize("q", [
    "UPDATE t SET a=1",
    "DELETE FROM t",
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t",
    "ALTER TABLE t ADD x INT",
    "SELECT 1; DROP TABLE t",
    "SELECT * INTO OUTFILE '/tmp/x' FROM t",
    "",
])
def test_validate_rejects_non_select(q):
    clean, err = sql_console.validate_select(q)
    assert clean is None and err


def test_to_csv():
    out = sql_console.to_csv(["a", "b"], [[1, None], ["x", "y"]])
    lines = out.splitlines()
    assert lines[0] == "a,b"
    assert lines[1] == "1,"        # None -> empty cell
    assert lines[2] == "x,y"
