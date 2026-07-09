"""Dependency & impact map.

Walks the *real* relations from a root record (Configuration Item) outward to a
bounded depth, in either direction, and returns a node-link graph for the
topology view (``user/topology.html`` + ``static/topology.js``):

- **downstream / impact** — who depends on this CI: incoming M:1 (other tables'
  FK pointing at this row), via ``record_service._incoming_m1``.
- **upstream / dependency** — what this CI depends on: outgoing M:1 (this row's
  own FK values), resolved against the parent table.
- **peers** — M:N links (undirected), via ``record_service._mn_links``; only
  expanded in the ``both`` view, where they carry no clear direction.

Read-only: no writes, so the ``record_service`` write chokepoint is untouched.
Every neighbour table is gated by ``helpers.can_view`` and every row read goes
through ``record_service.list_records`` (soft-delete + ownership scoping). The
``max_nodes`` / ``depth`` caps bound query fan-out for a single map. Table and
column names come only from metadata and flow through SQLAlchemy objects; values
are bound parameters.
"""
from collections import deque

from flask import url_for
from sqlalchemy import select

from . import data_service, record_service
from .db import engine_for_table
from .forms.builder import display_field_name
from .helpers import can_view
from .metadata.models import MetaField, MetaRelation, MetaTable

DIRECTIONS = ("upstream", "downstream", "both")


def _node_id(table_id, pk):
    return f"{table_id}:{pk}"


def graph_for(session, user, table, pk, *, direction="both", depth=2, max_nodes=150):
    """Build the dependency/impact graph rooted at (``table``, ``pk``).

    Returns ``{root, root_label, nodes, edges, truncated, direction, depth}``.
    The caller is responsible for checking read access to the *root* table; every
    other table reached is gated here by ``can_view``.
    """
    if direction not in DIRECTIONS:
        direction = "both"
    user_id = user.id
    is_designer = bool(user.is_designer)
    want_up = direction in ("upstream", "both")
    want_down = direction in ("downstream", "both")

    nodes, edges, edge_seen = {}, [], set()
    state = {"truncated": False}
    disp_cache = {}

    def disp(t):
        if t.id not in disp_cache:
            disp_cache[t.id] = display_field_name(session, t)
        return disp_cache[t.id]

    from .companies import allowed_for_user
    allowed_companies = None if is_designer else allowed_for_user(session, user_id)
    company_col = {}                    # table id -> company column name (or None)

    def _company_visible(t, row_pk):
        """Company-scoped users only see nodes inside their company subtree."""
        if allowed_companies is None:
            return True
        if t.id not in company_col:
            cf = next((f for f in t.fields if f.data_type == "company"), None)
            company_col[t.id] = cf.phys_name if cf else None
        col = company_col[t.id]
        if col is None:
            return True                 # unscoped table
        row = data_service.get_row(engine_for_table(t), t.phys_name, row_pk)
        return bool(row) and row.get(col) in allowed_companies

    def add_node(t, row_pk, d):
        nid = _node_id(t.id, row_pk)
        if nid in nodes:
            return nid, False
        if len(nodes) >= max_nodes:
            state["truncated"] = True
            return None, False
        if not _company_visible(t, row_pk):
            return None, False
        eng = engine_for_table(t)
        labels = data_service.labels_for(eng, t.phys_name, [row_pk], [disp(t)])
        nodes[nid] = {
            "id": nid, "table_id": t.id, "table_label": t.label,
            "pk": str(row_pk), "label": labels[0] if labels else f"#{row_pk}",
            "url": url_for("user.record_view", table_id=t.id, pk=row_pk),
            "topo_url": url_for("user.record_topology", table_id=t.id, pk=row_pk,
                                direction=direction, depth=depth),
            "depth": d,
        }
        return nid, True

    def add_edge(src, dst, label, kind, directed):
        if not src or not dst or src == dst:
            return
        key = (src, dst, kind)
        if key in edge_seen:
            return
        edge_seen.add(key)
        edges.append({"source": src, "target": dst, "label": label or "",
                      "kind": kind, "directed": directed})

    root_nid, _ = add_node(table, pk, 0)
    queue = deque([(table, pk, 0)])
    while queue:
        t, rpk, d = queue.popleft()
        if d >= depth:
            continue
        cur = _node_id(t.id, rpk)
        if cur not in nodes:
            continue
        eng = engine_for_table(t)

        # downstream: incoming M:1 — rows whose FK points at this record
        if want_down:
            for child, fk_col, rel in record_service._incoming_m1(session, t):
                if not can_view(session, user, child.id):
                    continue
                rows, _total = record_service.list_records(
                    engine_for_table(child), child, user_id=user_id,
                    is_designer=is_designer,
                    filters=[{"col": fk_col, "op": "eq", "value": rpk}],
                    per_page=max_nodes)
                for row in rows:
                    cpk = row[child.pk_col]
                    nid, new = add_node(child, cpk, d + 1)
                    add_edge(nid, cur, rel.name or fk_col, "m1", True)
                    if new:
                        queue.append((child, cpk, d + 1))

        # upstream: outgoing M:1 — this record's own FK values
        if want_up:
            row = data_service.get_row(eng, t.phys_name, rpk) or {}
            for rel in session.scalars(select(MetaRelation).where(
                    MetaRelation.kind == "m1", MetaRelation.from_table_id == t.id)):
                parent = session.get(MetaTable, rel.to_table_id)
                fk = session.get(MetaField, rel.from_field_id) if rel.from_field_id else None
                if not parent or not fk or not can_view(session, user, parent.id):
                    continue
                ppk = row.get(fk.phys_name)
                if ppk in (None, ""):
                    continue
                nid, new = add_node(parent, ppk, d + 1)
                add_edge(cur, nid, rel.name or fk.phys_name, "m1", True)
                if new:
                    queue.append((parent, ppk, d + 1))

        # peers: M:N (undirected) — only in the 'both' view
        if direction == "both":
            for rel, jname, this_col, other_col, other in record_service._mn_links(session, t):
                if not can_view(session, user, other.id):
                    continue
                for oid in data_service.get_links(eng, jname, this_col, rpk, other_col):
                    nid, new = add_node(other, oid, d + 1)
                    add_edge(cur, nid, rel.name or other.label, "mn", False)
                    if new:
                        queue.append((other, oid, d + 1))

    return {
        "root": root_nid,
        "root_label": nodes[root_nid]["label"] if root_nid else f"#{pk}",
        "nodes": list(nodes.values()),
        "edges": edges,
        "truncated": state["truncated"],
        "direction": direction,
        "depth": depth,
    }
