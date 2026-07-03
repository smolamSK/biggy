"""Reconciliation: merge two duplicate records into one (CMDB data quality).

The automated half of reconciliation is composite/normalized upsert keys
(``data_service.find_id_by_key``); this is the human half. ``merge`` folds a
*duplicate* record into a *survivor*:

1. every incoming M:1 foreign key pointing at the duplicate is repointed to the
   survivor (``record_service._incoming_m1`` + ``data_service.repoint_fk``);
2. the duplicate's M:N links are unioned onto the survivor
   (``data_service.set_links``) and removed from the duplicate;
3. the survivor's **empty** fields are filled from the duplicate — applied through
   :func:`record_service.update`, so the merge is audit-logged and triggers/SLA fire;
4. the duplicate is deleted via :func:`record_service.remove` (Trash when the table
   soft-deletes; also audit-logged).

``preview`` reports what a merge would do without changing anything.
"""
import logging

from . import data_service, record_service
from .metadata.field_types import FILE_TYPES

_log = logging.getLogger(__name__)


def _fillable_cols(meta_table):
    return [f.phys_name for f in meta_table.fields if f.data_type not in FILE_TYPES]


def _fills(meta_table, survivor, duplicate):
    """Columns the merge would copy from the duplicate into the survivor."""
    return {c: duplicate.get(c) for c in _fillable_cols(meta_table)
            if survivor.get(c) in (None, "") and duplicate.get(c) not in (None, "")}


def preview(session, engine, meta_table, survivor_pk, dup_pk):
    """What a merge would do, or None when either record is missing."""
    survivor = data_service.get_row(engine, meta_table.phys_name, survivor_pk)
    duplicate = data_service.get_row(engine, meta_table.phys_name, dup_pk)
    if not survivor or not duplicate:
        return None
    children = []
    for child, fk, _rel in record_service._incoming_m1(session, meta_table):
        n = data_service.count_rows(engine, child.phys_name, fk, dup_pk)
        if n:
            children.append({"label": child.label, "count": n})
    links = []
    for rel, jname, this_col, other_col, other in record_service._mn_links(session, meta_table):
        n = len(data_service.get_links(engine, jname, this_col, dup_pk, other_col))
        if n:
            links.append({"label": rel.name or other.label, "count": n})
    return {"survivor": survivor, "duplicate": duplicate,
            "fills": _fills(meta_table, survivor, duplicate),
            "children": children, "links": links}


def merge(session, engine, meta_table, survivor_pk, dup_pk, user_id):
    """Fold ``dup_pk`` into ``survivor_pk``. Returns a summary dict."""
    if str(survivor_pk) == str(dup_pk):
        raise ValueError("Survivor and duplicate are the same record.")
    survivor = data_service.get_row(engine, meta_table.phys_name, survivor_pk)
    duplicate = data_service.get_row(engine, meta_table.phys_name, dup_pk)
    if not survivor or not duplicate:
        raise ValueError("Both records must exist.")

    repointed = 0
    for child, fk, _rel in record_service._incoming_m1(session, meta_table):
        repointed += data_service.repoint_fk(engine, child.phys_name, fk, dup_pk, survivor_pk)

    moved_links = 0
    for rel, jname, this_col, other_col, other in record_service._mn_links(session, meta_table):
        dup_ids = data_service.get_links(engine, jname, this_col, dup_pk, other_col)
        if not dup_ids:
            continue
        merged = list(dict.fromkeys(
            data_service.get_links(engine, jname, this_col, survivor_pk, other_col) + dup_ids))
        data_service.set_links(engine, jname, this_col, survivor_pk, other_col, merged)
        data_service.delete_where(engine, jname, this_col, dup_pk)
        moved_links += len(dup_ids)

    fills = _fills(meta_table, survivor, duplicate)
    if fills:  # through the write chokepoint: audit-logged, triggers/SLA fire
        record_service.update(session, engine, meta_table, survivor_pk, fills, user_id)

    record_service.remove(session, engine, meta_table, dup_pk, user_id)
    _log.info("merged %s #%s into #%s (%s FKs repointed, %s links moved, %s fields filled)",
              meta_table.phys_name, dup_pk, survivor_pk, repointed, moved_links, len(fills))
    return {"repointed": repointed, "moved_links": moved_links, "filled": len(fills)}
