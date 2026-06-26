"""Database engine and session management (no Flask-SQLAlchemy).

A single Engine is created from app config at startup. ORM metadata tables
(``app_*``) use the declarative ``Base`` defined in :mod:`app.metadata.models`.
Physical user tables are reflected on demand in :mod:`app.data_service`.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import scoped_session, sessionmaker

# Configured against the engine in init_engine().
SessionLocal = scoped_session(sessionmaker(autoflush=False, autocommit=False, future=True))

_engine = None
# Cache of data-source engines, keyed by connection URL. The *home* database
# (where app_* metadata lives) is the separate ``_engine`` above.
_engines = {}


def build_url(cfg):
    """Build a SQLAlchemy URL from a config object/dict."""
    get = cfg.get if isinstance(cfg, dict) else lambda k, d=None: getattr(cfg, k, d)
    if get("DATABASE_URL"):
        return get("DATABASE_URL")
    return URL.create(
        get("DB_DRIVER", "mysql+pymysql"),
        username=get("DB_USER"),
        password=get("DB_PASSWORD"),
        host=get("DB_HOST"),
        port=get("DB_PORT"),
        database=get("DB_NAME"),
    )


def make_engine(url):
    return create_engine(url, pool_pre_ping=True, future=True)


def init_engine(app):
    """Create the home engine from app config and bind the session factory."""
    global _engine
    url = build_url(app.config)
    _engine = make_engine(url)
    SessionLocal.configure(bind=_engine)
    _engines.clear()                       # data-source engines are config-relative
    return _engine


def get_engine():
    if _engine is None:
        raise RuntimeError("Engine not initialised; call init_engine() first.")
    return _engine


def test_connection(url):
    """Return (ok, message). Used by the setup wizard / connection test."""
    try:
        eng = make_engine(url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True, "Connection successful."
    except Exception as exc:  # noqa: BLE001 - surface any driver error to the UI
        return False, str(exc)


# --------------------------------------------------------------------------- #
# Data sources: tables may live in databases other than the home one.
# --------------------------------------------------------------------------- #
def _source_cfg(ds):
    return {"DATABASE_URL": None, "DB_DRIVER": ds.driver or "mysql+pymysql",
            "DB_HOST": ds.host, "DB_PORT": ds.port, "DB_USER": ds.username,
            "DB_PASSWORD": ds.password, "DB_NAME": ds.database}


def source_url(ds):
    """SQLAlchemy URL for a :class:`~app.metadata.models.DataSource`."""
    return build_url(_source_cfg(ds))


def engine_for(ds):
    """Engine for a DataSource (``None`` → the home engine), cached by URL."""
    if ds is None:
        return get_engine()
    key = str(source_url(ds))
    eng = _engines.get(key)
    if eng is None:
        eng = make_engine(source_url(ds))
        _engines[key] = eng
    return eng


def engine_for_table(meta_table):
    """Engine the given table's rows live in (home unless it has a ``source_id``)."""
    sid = getattr(meta_table, "source_id", None)
    if not sid:
        return get_engine()
    from .metadata.models import DataSource
    ds = SessionLocal().get(DataSource, sid)
    return engine_for(ds) if ds else get_engine()


def test_source(ds):
    return test_connection(source_url(ds))
