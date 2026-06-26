"""Pytest fixtures. Integration tests use a dedicated ``biggy_test`` database
and are skipped automatically if it is unreachable."""
import gc

import pytest
from sqlalchemy import text

from app import create_app
from app.db import build_url, get_engine, init_engine, make_engine, test_connection

TEST_DB = "biggy_test"
SRC2_DB = "biggy_test2"


@pytest.fixture(scope="session")
def app():
    application = create_app()
    application.config.update(
        DB_NAME=TEST_DB, DATABASE_URL=None, TESTING=True, WTF_CSRF_ENABLED=False
    )
    init_engine(application)  # rebind engine/session to the test database
    ok, msg = test_connection(build_url(application.config))
    if not ok:
        pytest.skip(f"Test database '{TEST_DB}' unavailable: {msg}")
    return application


@pytest.fixture
def clean_db(app):
    """Drop every table in the test DB and reset bootstrap state.

    SQLAlchemy ``Connection``/``Result`` objects form reference cycles, so reads
    left unclosed by a prior test are freed only on a GC pass — until then they
    hold shared metadata locks that would block ``DROP TABLE`` indefinitely.
    Force collection and discard the pool before dropping; cap the metadata-lock
    wait so any future regression fails fast instead of hanging.
    """
    eng = get_engine()
    gc.collect()
    eng.dispose()
    with eng.begin() as conn:
        conn.execute(text("SET SESSION lock_wait_timeout = 20"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for (tbl,) in conn.execute(text("SHOW TABLES")).all():
            conn.execute(text(f"DROP TABLE IF EXISTS `{tbl}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    app.config.pop("_BOOTSTRAPPED", None)
    yield eng


@pytest.fixture
def src2(app):
    """A second database (``biggy_test2``) for multi-data-source tests.

    Yields ``{"params": <DataSource fields>, "engine": <Engine>}``; skips if the
    second database is unreachable. Its tables are dropped before and after.
    """
    cfg = app.config
    params = dict(driver=cfg.get("DB_DRIVER", "mysql+pymysql"), host=cfg.get("DB_HOST"),
                  port=cfg.get("DB_PORT"), username=cfg.get("DB_USER"),
                  password=cfg.get("DB_PASSWORD"), database=SRC2_DB)
    url = build_url({"DATABASE_URL": None, "DB_DRIVER": params["driver"],
                     "DB_HOST": params["host"], "DB_PORT": params["port"],
                     "DB_USER": params["username"], "DB_PASSWORD": params["password"],
                     "DB_NAME": SRC2_DB})
    ok, msg = test_connection(url)
    if not ok:
        pytest.skip(f"Second test database '{SRC2_DB}' unavailable: {msg}")
    eng = make_engine(url)

    def _clean():
        gc.collect()
        with eng.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for (tbl,) in conn.execute(text("SHOW TABLES")).all():
                conn.execute(text(f"DROP TABLE IF EXISTS `{tbl}`"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    _clean()
    yield {"params": params, "engine": eng}
    _clean()
    eng.dispose()


@pytest.fixture
def sqlite_source(app):
    """A temp-file SQLite database exposed as DataSource params (no server needed).

    Proves the DDL layer is dialect-agnostic. Yields ``{"params": ..., "engine": ...}``
    and removes the file afterwards.
    """
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    params = dict(driver="sqlite", host=None, port=None, username=None,
                  password=None, database=path)
    eng = make_engine(f"sqlite:///{path}")
    yield {"params": params, "engine": eng}
    eng.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def client(app, clean_db):
    return app.test_client()


@pytest.fixture
def engine(app, clean_db):
    return clean_db
