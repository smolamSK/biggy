"""Unit tests for data import validation (no database needed)."""
import pytest

from app import data_io


def test_import_data_rejects_bad_version():
    with pytest.raises(data_io.DataError):
        data_io.import_data(None, None, {"version": 99, "tables": {}})
    with pytest.raises(data_io.DataError):
        data_io.import_data(None, None, {"tables": {}})   # missing version
    with pytest.raises(data_io.DataError):
        data_io.import_data(None, None, "not a dict")
