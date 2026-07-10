"""Email-to-ticket: poll IMAP mailboxes and turn replies into ticket updates.

The subject is scanned for a ticket number two ways: **autonumber prefixes**
(``INC-0007`` — derived automatically from every autonumber field's prefix, so
renaming a prefix in Designer reconfigures parsing) and a per-mailbox **alias
map** (``I-7`` → incident). The sender's address is matched to an account
(:class:`AppUser.email`) and the record must be visible to that account —
staff via the normal company/ownership scoping, portal senders via the same
company-member rule the portal uses. Matched mail becomes a **public comment**
(+ attachments onto the record's first file field); unknown or unauthorized
senders land as an **internal guest note** so nothing is lost and nothing
unauthenticated reaches the portal. Numberless mail from a known sender may
create a new ticket via the mailbox's catalog form.

Fetching goes through the swappable :data:`FETCHER` (the house transport
pattern), so tests feed synthetic messages without an IMAP server.
"""
import email
import email.policy
import imaplib
import io
import json
import logging
import re
from email.utils import parseaddr

from flask import current_app
from sqlalchemy import func, select
from werkzeug.datastructures import FileStorage

from . import comments, data_service, file_store, importer, record_service
from .db import engine_for_table
from .forms.builder import display_field_name
from .metadata.models import (
    ROLE_PORTAL,
    AppUser,
    Attachment,
    MetaField,
    MetaForm,
    MetaTable,
)

_logger = logging.getLogger(__name__)

DEFAULT_ALIASES = {"I": "incident", "R": "request", "C": "change", "P": "problem"}
_DEFAULT_TOKENS = ("now", "today", "current_user", "me")


# --------------------------------------------------------------------------- #
# Fetching (swappable for tests)
# --------------------------------------------------------------------------- #
def _imap_fetch(mailbox):
    """Fetch unseen messages as raw bytes and mark them seen."""
    cls = imaplib.IMAP4_SSL if mailbox.use_ssl else imaplib.IMAP4
    conn = cls(mailbox.host, mailbox.port or (993 if mailbox.use_ssl else 143))
    try:
        conn.login(mailbox.username or "", mailbox.password or "")
        conn.select(mailbox.folder or "INBOX")
        _typ, data = conn.search(None, "UNSEEN")
        out = []
        for uid in (data[0].split() if data and data[0] else []):
            _typ, msg_data = conn.fetch(uid, "(RFC822)")
            if msg_data and msg_data[0] and isinstance(msg_data[0], tuple):
                out.append(msg_data[0][1])
            conn.store(uid, "+FLAGS", "\\Seen")
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


FETCHER = _imap_fetch


def set_fetcher(fn):
    """Swap message fetching (tests); None restores IMAP."""
    global FETCHER
    FETCHER = fn or _imap_fetch


# --------------------------------------------------------------------------- #
# Subject → ticket
# --------------------------------------------------------------------------- #
def _number_patterns(session):
    """[(regex, table, autonumber field)] from every autonumber prefix."""
    out = []
    for f in session.scalars(select(MetaField).where(
            MetaField.data_type == "autonumber")):
        prefix = (f.default_value or "").strip()
        mt = session.get(MetaTable, f.table_id)
        if prefix and mt is not None:
            out.append((re.compile(re.escape(prefix) + r"\d+", re.I), mt, f))
    return out


def _alias_map(session, mailbox):
    """{LETTER: (table, autonumber field)} from the mailbox's alias JSON."""
    try:
        aliases = json.loads(mailbox.aliases or "{}")
    except (TypeError, ValueError):
        aliases = {}
    out = {}
    for letter, phys in aliases.items():
        mt = session.scalar(select(MetaTable).where(MetaTable.phys_name == phys))
        f = next((fd for fd in mt.fields if fd.data_type == "autonumber"),
                 None) if mt else None
        if mt is not None and f is not None and letter.strip():
            out[letter.strip().upper()] = (mt, f)
    return out


def _find_by_number(session, mt, field, value):
    try:
        return data_service.find_id_by(engine_for_table(mt), mt.phys_name,
                                       field.phys_name, value, normalize=True)
    except Exception:  # noqa: BLE001 - missing column / ambiguous match
        return None


def find_ticket(session, mailbox, subject):
    """The (table, pk) a subject refers to, or (None, None)."""
    subject = subject or ""
    for pat, mt, f in _number_patterns(session):
        m = pat.search(subject)
        if m:
            pk = _find_by_number(session, mt, f, m.group(0))
            if pk is not None:
                return mt, pk
    for letter, (mt, f) in _alias_map(session, mailbox).items():
        m = re.search(rf"\b{re.escape(letter)}-(\d+)\b", subject, re.I)
        if m:
            prefix = (f.default_value or "").strip()
            for cand in (f"{prefix}{int(m.group(1)):04d}",
                         f"{prefix}{m.group(1)}"):
                pk = _find_by_number(session, mt, f, cand)
                if pk is not None:
                    return mt, pk
    return None, None


# --------------------------------------------------------------------------- #
# Message parsing
# --------------------------------------------------------------------------- #
def _sender(msg):
    return (parseaddr(msg.get("From") or "")[1] or "").strip().lower()


def _is_auto(msg, own_sender):
    """Auto-replies / bulk mail / our own notifications — never processed."""
    if (msg.get("Auto-Submitted") or "no").strip().lower() != "no":
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "junk", "auto_reply"):
        return True
    if msg.get("X-Auto-Response-Suppress"):
        return True
    return bool(own_sender) and _sender(msg) == own_sender.strip().lower()


def _body_text(msg):
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        return ""
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001 - undecodable payloads
        return ""


_QUOTE_RE = re.compile(r"^On .{0,300}wrote:\s*$")


def clean_body(text):
    """Keep the reply itself: stop at quoted text or a signature marker."""
    lines = []
    for line in (text or "").splitlines():
        if line.startswith(">") or _QUOTE_RE.match(line):
            break
        if line.rstrip() == "--" and lines:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_re(subject):
    return re.sub(r"^\s*(?:(re|fw|fwd|aw)\s*(?:\[\d+\])?\s*:\s*)+",
                  "", subject or "", flags=re.I).strip()


# --------------------------------------------------------------------------- #
# Sender identity + access
# --------------------------------------------------------------------------- #
def _user_for(session, addr):
    if not addr:
        return None
    return session.scalar(select(AppUser).where(
        func.lower(AppUser.email) == addr, AppUser.is_active_flag.is_(True)))


def _can_access(session, user, mt, pk):
    """May this account see the record? (Same walls as the UI.)"""
    engine = engine_for_table(mt)
    if user.role == ROLE_PORTAL:
        row = record_service.get_record(engine, mt, pk, user_id=user.id,
                                        is_designer=True)
        if not row:
            return False
        from . import companies
        members = {user.id}
        if user.company_id:
            allowed = companies.subtree_ids(session, user.company_id)
            members |= set(session.scalars(select(AppUser.id).where(
                AppUser.company_id.in_(allowed), AppUser.role == ROLE_PORTAL)))
        return row.get("created_by") in members
    return record_service.get_record(engine, mt, pk, user_id=user.id,
                                     is_designer=user.is_designer) is not None


# --------------------------------------------------------------------------- #
# Effects
# --------------------------------------------------------------------------- #
def _save_attachments(session, mt, pk, msg, uploaded_by):
    """Email attachments → the record's first file/image field (best effort)."""
    field = next((f for f in mt.fields if f.data_type in ("file", "image")), None)
    try:
        int_pk = int(pk)
    except (TypeError, ValueError):
        return 0
    if field is None:
        return 0
    n = 0
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b""
        if not data:
            continue
        fs = FileStorage(stream=io.BytesIO(data),
                         filename=part.get_filename() or "attachment.bin",
                         content_type=part.get_content_type())
        try:
            meta = file_store.save(fs, field)
        except file_store.UploadError:
            continue                      # e.g. non-image on an image field
        session.add(Attachment(field_id=field.id, row_pk=int_pk,
                               uploaded_by=uploaded_by, **meta))
        n += 1
    if n:
        session.commit()
    return n


def _create_ticket(session, mailbox, user, subject, body):
    """Numberless mail from a known sender → a new record via the catalog form."""
    mf = session.get(MetaForm, mailbox.create_form_id) \
        if mailbox.create_form_id else None
    if mf is None or user is None:
        return None, None
    mt = mf.table
    values = {}
    disp = display_field_name(session, mt)
    if disp and disp != mt.pk_col:
        length = next((f.length for f in mt.fields if f.phys_name == disp), None)
        values[disp] = (subject or "Email").strip()[:length or 200]
    long_f = next((f for f in mt.fields
                   if f.data_type in ("markdown", "text") and f.phys_name != disp),
                  None)
    if long_f is not None and body:
        values[long_f.phys_name] = body
    # literal field defaults (the form UI applies these; tokens are handled
    # by record_service itself)
    for f in mt.fields:
        dv = (f.default_value or "").strip()
        if (not dv or f.phys_name in values or dv.lower() in _DEFAULT_TOKENS
                or f.data_type in ("autonumber", "formula", "relation",
                                   "file", "image")):
            continue
        try:
            values[f.phys_name] = importer.coerce_value(f, dv)
        except ValueError:
            pass
    pk = record_service.create(session, engine_for_table(mt), mt, values, user.id)
    return mt, pk


# --------------------------------------------------------------------------- #
# The processor
# --------------------------------------------------------------------------- #
def process_mailbox(session, mailbox):
    """Fetch and apply one mailbox's unseen mail. Returns handled count."""
    if current_app.config.get("TESTING") and FETCHER is _imap_fetch:
        return 0                          # tests must inject a fetcher
    own = current_app.config.get("MAIL_DEFAULT_SENDER", "")
    handled = 0
    for raw in FETCHER(mailbox):
        try:
            handled += _handle(session, mailbox, raw, own)
        except Exception as exc:  # noqa: BLE001 - one bad mail must not stop the rest
            _logger.warning("mailbox '%s': message failed: %s", mailbox.name, exc)
            session.rollback()
    return handled


def _handle(session, mailbox, raw, own_sender):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if _is_auto(msg, own_sender):
        return 0
    subject = _strip_re(msg.get("Subject") or "")
    body = clean_body(_body_text(msg))
    addr = _sender(msg)
    user = _user_for(session, addr)

    mt, pk = find_ticket(session, mailbox, subject)
    if mt is None:
        if user is not None and mailbox.create_form_id:
            nmt, npk = _create_ticket(session, mailbox, user, subject, body)
            if npk is not None:
                _save_attachments(session, nmt, npk, msg, user.id)
                return 1
        return 0                          # numberless + unknown sender: dropped

    row = data_service.get_row(engine_for_table(mt), mt.phys_name, pk)
    label = None
    if row:
        v = row.get(display_field_name(session, mt))
        label = f"{mt.label}: {v}" if v not in (None, "") else f"{mt.label} #{pk}"

    if user is not None and _can_access(session, user, mt, pk):
        comments.add(session, mt.phys_name, pk, user, body or "(empty email)",
                     internal=False, row=row, record_label=label)
        _save_attachments(session, mt, pk, msg, user.id)
    else:
        guest = (f"[via email from {addr or 'unknown sender'}] {subject}\n\n"
                 f"{body or '(empty email)'}")
        comments.add(session, mt.phys_name, pk, None, guest,
                     internal=True, row=row, record_label=label)
        _save_attachments(session, mt, pk, msg, None)
    return 1
