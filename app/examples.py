"""Built-in example models (schema + sample data).

Each demo is authored with :class:`ModelBuilder`, which emits the canonical
export format consumed by :func:`app.schema_io.import_schema` and
:func:`app.data_io.import_data`. The builder assigns the sequential ids and wires
the cross-references, so demos read declaratively and import unchanged.
"""
import json

from .identifiers import junction_name


class ModelBuilder:
    def __init__(self):
        self.tables, self.fields, self.relations = [], [], []
        self.forms, self.form_fields, self.menus, self.workflows = [], [], [], []
        self._data = {}
        self._phys = {}
        self._n = {"t": 0, "f": 0, "r": 0, "form": 0, "ff": 0, "m": 0, "wf": 0}

    def _id(self, key):
        self._n[key] += 1
        return self._n[key]

    # --- schema -----------------------------------------------------------
    def table(self, phys, label, description=None, track_audit=False,
              soft_delete=False, row_owned=False):
        tid = self._id("t")
        self.tables.append({"id": tid, "phys_name": phys, "label": label,
                            "description": description, "display_field_id": None,
                            "track_audit": track_audit, "soft_delete": soft_delete,
                            "row_owned": row_owned})
        self._phys[tid] = phys
        return tid

    def field(self, table_id, phys, dtype, label=None, length=None, nullable=True,
              is_unique=False, enum=None, default=None, display=False, precision=None,
              scale=None):
        fid = self._id("f")
        pos = sum(1 for f in self.fields if f["table_id"] == table_id)
        self.fields.append({
            "id": fid, "table_id": table_id, "phys_name": phys,
            "label": label or phys.replace("_", " ").capitalize(), "data_type": dtype,
            "length": length, "precision": precision, "scale": scale, "nullable": nullable,
            "default_value": default, "is_unique": is_unique, "position": pos,
            "enum_options": json.dumps(enum) if enum else None,
            "related_table_id": None, "on_delete": None,
        })
        if display:
            next(t for t in self.tables if t["id"] == table_id)["display_field_id"] = fid
        return fid

    def m1(self, from_table, to_table, col, name, nullable=True, on_delete="SET NULL"):
        fid = self._id("f")
        pos = sum(1 for f in self.fields if f["table_id"] == from_table)
        self.fields.append({
            "id": fid, "table_id": from_table, "phys_name": col, "label": name,
            "data_type": "relation", "length": None, "precision": None, "scale": None,
            "nullable": nullable, "default_value": None, "is_unique": False, "position": pos,
            "enum_options": None, "related_table_id": to_table, "on_delete": on_delete,
        })
        rid = self._id("r")
        self.relations.append({
            "id": rid, "name": name, "kind": "m1", "from_table_id": from_table,
            "to_table_id": to_table, "from_field_id": fid, "junction_phys_name": None,
            "on_delete": on_delete, "to_display_field_ids": None, "from_display_field_ids": None,
        })
        return rid

    def mn(self, a_table, b_table, name):
        rid = self._id("r")
        self.relations.append({
            "id": rid, "name": name, "kind": "mn", "from_table_id": a_table,
            "to_table_id": b_table, "from_field_id": None, "junction_phys_name": None,
            "on_delete": None, "to_display_field_ids": None, "from_display_field_ids": None,
        })
        return rid

    def form(self, name, title, table_id, mn=(), description=None, purpose="data"):
        form_id = self._id("form")
        self.forms.append({"id": form_id, "table_id": table_id, "name": name,
                          "title": title, "description": description, "purpose": purpose})
        for f in self.fields:
            if f["table_id"] == table_id:
                self._item(form_id, field_id=f["id"], required=not f["nullable"])
        for rid in mn:
            self._item(form_id, relation_id=rid)
        return form_id

    def view_form(self, name, title, table_id, mn=()):
        """A read-only 'view' form (clickable record page) over all the fields."""
        return self.form(name, title, table_id, mn=mn, purpose="view")

    def workflow(self, table_id, field_id, transitions, initial, layout=None):
        """A status workflow on an enum field. ``transitions`` = [{from,to,roles}]."""
        self.workflows.append({
            "id": self._id("wf"), "table_id": table_id, "field_id": field_id,
            "initial_state": initial, "transitions": json.dumps(transitions),
            "layout": json.dumps(layout or {}),
        })

    def _item(self, form_id, field_id=None, relation_id=None, required=False):
        pos = sum(1 for i in self.form_fields if i["form_id"] == form_id)
        self.form_fields.append({
            "id": self._id("ff"), "form_id": form_id,
            "kind": "field" if field_id else "relation", "field_id": field_id,
            "relation_id": relation_id, "label_override": None, "widget": None,
            "required": required, "readonly": False, "help_text": None, "position": pos,
        })

    def menu_group(self, label):
        mid = self._id("m")
        self.menus.append({"id": mid, "parent_id": None, "label": label, "kind": "group",
                          "target_form_id": None, "target_table_id": None,
                          "position": len(self.menus), "icon": None})
        return mid

    def menu_form(self, label, form_id, parent):
        mid = self._id("m")
        self.menus.append({"id": mid, "parent_id": parent, "label": label, "kind": "form",
                          "target_form_id": form_id, "target_table_id": None,
                          "position": len(self.menus), "icon": None})
        return mid

    # --- data -------------------------------------------------------------
    def rows(self, table_id, rows):
        self._data[self._phys[table_id]] = rows

    def junction_rows(self, from_table, to_table, pairs):
        a, b = self._phys[from_table], self._phys[to_table]
        left, right = f"{a}_id", f"{b}_id"
        if left == right:
            right = f"{b}_id_2"
        self._data[junction_name(a, b)] = [{left: x, right: y} for x, y in pairs]

    # --- output -----------------------------------------------------------
    def schema(self):
        return {"version": 1, "tables": self.tables, "fields": self.fields,
                "relations": self.relations, "forms": self.forms,
                "form_fields": self.form_fields, "menus": self.menus,
                "workflows": self.workflows}

    def data(self):
        return {"version": 1, "tables": self._data}


# --------------------------------------------------------------------------- #
# Demos
# --------------------------------------------------------------------------- #
def build_cmdb():
    b = ModelBuilder()
    team = b.table("team", "Team")
    b.field(team, "name", "string", length=80, nullable=False, display=True)
    env = b.table("environment", "Environment")
    b.field(env, "name", "string", length=40, nullable=False, display=True)
    ci = b.table("configuration_item", "Configuration Item")
    b.field(ci, "name", "string", length=120, nullable=False, display=True)
    b.field(ci, "hostname", "string", length=120)
    b.field(ci, "ip_address", "string", length=45)
    b.field(ci, "ci_type", "enum", enum=["server", "vm", "database", "network", "service"],
            default="server")
    b.field(ci, "status", "enum", enum=["active", "maintenance", "retired"], default="active")
    b.m1(ci, env, "environment_id", "Environment")
    b.m1(ci, team, "owner_team_id", "Owner team")
    app = b.table("application", "Application")
    b.field(app, "name", "string", length=120, nullable=False, display=True)
    b.field(app, "criticality", "enum", enum=["low", "medium", "high", "critical"],
            default="medium")
    b.m1(app, team, "owner_team_id", "Owner team")
    runs_on = b.mn(app, ci, "Runs on")

    g = b.menu_group("CMDB")
    b.menu_form("Configuration items", b.form("cmdb_ci", "Configuration items", ci), g)
    b.menu_form("Applications", b.form("cmdb_app", "Applications", app, mn=[runs_on]), g)
    b.menu_form("Teams", b.form("cmdb_team", "Teams", team), g)
    b.menu_form("Environments", b.form("cmdb_env", "Environments", env), g)

    b.rows(team, [{"id": 1, "name": "Platform"}, {"id": 2, "name": "Payments"}])
    b.rows(env, [{"id": 1, "name": "Production"}, {"id": 2, "name": "Staging"},
                 {"id": 3, "name": "Development"}])
    b.rows(ci, [
        {"id": 1, "name": "web-01", "hostname": "web-01.prod", "ip_address": "10.0.1.11",
         "ci_type": "server", "status": "active", "environment_id": 1, "owner_team_id": 1},
        {"id": 2, "name": "web-02", "hostname": "web-02.prod", "ip_address": "10.0.1.12",
         "ci_type": "server", "status": "active", "environment_id": 1, "owner_team_id": 1},
        {"id": 3, "name": "pay-db", "hostname": "pay-db.prod", "ip_address": "10.0.2.20",
         "ci_type": "database", "status": "active", "environment_id": 1, "owner_team_id": 2},
        {"id": 4, "name": "stg-web", "hostname": "web.stg", "ip_address": "10.1.1.11",
         "ci_type": "vm", "status": "maintenance", "environment_id": 2, "owner_team_id": 1},
    ])
    b.rows(app, [
        {"id": 1, "name": "Storefront", "criticality": "high", "owner_team_id": 1},
        {"id": 2, "name": "Checkout", "criticality": "critical", "owner_team_id": 2},
    ])
    b.junction_rows(app, ci, [(1, 1), (1, 2), (2, 3)])
    return b.schema(), b.data()


def build_library():
    b = ModelBuilder()
    author = b.table("author", "Author")
    b.field(author, "name", "string", length=120, nullable=False, display=True)
    book = b.table("book", "Book")
    b.field(book, "title", "string", length=200, nullable=False, display=True)
    b.field(book, "isbn", "string", length=20)
    b.field(book, "genre", "enum",
            enum=["fiction", "nonfiction", "science", "history", "children"], default="fiction")
    b.field(book, "copies", "integer", default="1")
    b.m1(book, author, "author_id", "Author")
    member = b.table("member", "Member")
    b.field(member, "name", "string", length=120, nullable=False, display=True)
    b.field(member, "email", "string", length=160)
    b.field(member, "joined", "date")
    loan = b.table("loan", "Loan")
    b.field(loan, "loaned_on", "date", nullable=False)
    b.field(loan, "due_on", "date")
    b.field(loan, "returned_on", "date")
    b.m1(loan, book, "book_id", "Book", nullable=False)
    b.m1(loan, member, "member_id", "Member", nullable=False)

    g = b.menu_group("Library")
    b.menu_form("Books", b.form("lib_book", "Books", book), g)
    b.menu_form("Authors", b.form("lib_author", "Authors", author), g)
    b.menu_form("Members", b.form("lib_member", "Members", member), g)
    b.menu_form("Loans", b.form("lib_loan", "Loans", loan), g)

    b.rows(author, [{"id": 1, "name": "Ursula K. Le Guin"}, {"id": 2, "name": "Carl Sagan"},
                    {"id": 3, "name": "Mary Shelley"}])
    b.rows(book, [
        {"id": 1, "title": "A Wizard of Earthsea", "isbn": "9780553262506", "genre": "fiction",
         "copies": 3, "author_id": 1},
        {"id": 2, "title": "Cosmos", "isbn": "9780345539434", "genre": "science", "copies": 2,
         "author_id": 2},
        {"id": 3, "title": "Frankenstein", "isbn": "9780486282114", "genre": "fiction",
         "copies": 4, "author_id": 3},
    ])
    b.rows(member, [
        {"id": 1, "name": "Ada Lovelace", "email": "ada@example.com", "joined": "2023-02-01"},
        {"id": 2, "name": "Alan Turing", "email": "alan@example.com", "joined": "2023-05-20"},
    ])
    b.rows(loan, [
        {"id": 1, "loaned_on": "2024-01-10", "due_on": "2024-01-24", "returned_on": "2024-01-20",
         "book_id": 1, "member_id": 1},
        {"id": 2, "loaned_on": "2024-02-05", "due_on": "2024-02-19", "returned_on": None,
         "book_id": 2, "member_id": 2},
    ])
    return b.schema(), b.data()


def build_helpdesk():
    b = ModelBuilder()
    agent = b.table("agent", "Agent")
    b.field(agent, "name", "string", length=120, nullable=False, display=True)
    b.field(agent, "email", "string", length=160)
    category = b.table("category", "Category")
    b.field(category, "name", "string", length=80, nullable=False, display=True)
    ticket = b.table("ticket", "Ticket")
    b.field(ticket, "subject", "string", length=200, nullable=False, display=True)
    b.field(ticket, "description", "text")
    b.field(ticket, "status", "enum", enum=["open", "in_progress", "resolved", "closed"],
            default="open")
    b.field(ticket, "priority", "enum", enum=["low", "medium", "high", "urgent"], default="medium")
    b.field(ticket, "opened_on", "date")
    b.field(ticket, "requester", "string", length=120)
    b.m1(ticket, category, "category_id", "Category")
    b.m1(ticket, agent, "assignee_id", "Assignee")

    g = b.menu_group("Helpdesk")
    b.menu_form("Tickets", b.form("hd_ticket", "Tickets", ticket), g)
    b.menu_form("Agents", b.form("hd_agent", "Agents", agent), g)
    b.menu_form("Categories", b.form("hd_category", "Categories", category), g)

    b.rows(agent, [{"id": 1, "name": "Sam Carter", "email": "sam@support.example"},
                   {"id": 2, "name": "Lee Wong", "email": "lee@support.example"}])
    b.rows(category, [{"id": 1, "name": "Account"}, {"id": 2, "name": "Billing"},
                      {"id": 3, "name": "Technical"}])
    b.rows(ticket, [
        {"id": 1, "subject": "Cannot log in", "description": "Password reset loops.",
         "status": "open", "priority": "high", "opened_on": "2024-03-01",
         "requester": "jane@acme.test", "category_id": 1, "assignee_id": 1},
        {"id": 2, "subject": "Invoice wrong amount", "description": "Charged twice.",
         "status": "in_progress", "priority": "urgent", "opened_on": "2024-03-02",
         "requester": "bob@acme.test", "category_id": 2, "assignee_id": 2},
    ])
    return b.schema(), b.data()


def build_crm():
    b = ModelBuilder()
    company = b.table("company", "Company")
    b.field(company, "name", "string", length=160, nullable=False, display=True)
    b.field(company, "industry", "string", length=80)
    b.field(company, "website", "string", length=160)
    contact = b.table("contact", "Contact")
    b.field(contact, "name", "string", length=120, nullable=False, display=True)
    b.field(contact, "email", "string", length=160)
    b.m1(contact, company, "company_id", "Company")
    deal = b.table("deal", "Deal")
    b.field(deal, "title", "string", length=160, nullable=False, display=True)
    b.field(deal, "amount", "decimal", precision=12, scale=2)
    b.field(deal, "stage", "enum", enum=["lead", "qualified", "proposal", "won", "lost"],
            default="lead")
    b.field(deal, "close_date", "date")
    b.m1(deal, company, "company_id", "Company")
    b.m1(deal, contact, "contact_id", "Contact")

    g = b.menu_group("CRM")
    b.menu_form("Companies", b.form("crm_company", "Companies", company), g)
    b.menu_form("Contacts", b.form("crm_contact", "Contacts", contact), g)
    b.menu_form("Deals", b.form("crm_deal", "Deals", deal), g)

    b.rows(company, [{"id": 1, "name": "Acme Corp", "industry": "Manufacturing",
                      "website": "acme.test"},
                     {"id": 2, "name": "Globex", "industry": "Energy", "website": "globex.test"}])
    b.rows(contact, [{"id": 1, "name": "Jane Roe", "email": "jane@acme.test", "company_id": 1},
                     {"id": 2, "name": "Max Power", "email": "max@globex.test", "company_id": 2}])
    b.rows(deal, [
        {"id": 1, "title": "Acme renewal", "amount": "12000.00", "stage": "proposal",
         "close_date": "2024-04-30", "company_id": 1, "contact_id": 1},
        {"id": 2, "title": "Globex expansion", "amount": "48000.00", "stage": "qualified",
         "close_date": "2024-06-15", "company_id": 2, "contact_id": 2},
    ])
    return b.schema(), b.data()


def build_projects():
    b = ModelBuilder()
    project = b.table("project", "Project")
    b.field(project, "name", "string", length=160, nullable=False, display=True)
    b.field(project, "description", "text")
    b.field(project, "start_date", "date")
    label = b.table("label", "Label")
    b.field(label, "name", "string", length=60, nullable=False, display=True)
    task = b.table("task", "Task")
    b.field(task, "title", "string", length=200, nullable=False, display=True)
    b.field(task, "status", "enum", enum=["todo", "doing", "done"], default="todo")
    b.field(task, "priority", "enum", enum=["low", "medium", "high"], default="medium")
    b.field(task, "due_date", "date")
    b.field(task, "assignee", "string", length=120)
    b.m1(task, project, "project_id", "Project", nullable=False)
    task_labels = b.mn(task, label, "Labels")

    g = b.menu_group("Projects")
    b.menu_form("Tasks", b.form("pr_task", "Tasks", task, mn=[task_labels]), g)
    b.menu_form("Projects", b.form("pr_project", "Projects", project), g)
    b.menu_form("Labels", b.form("pr_label", "Labels", label), g)

    b.rows(project, [{"id": 1, "name": "Website revamp", "description": "Q2 redesign.",
                      "start_date": "2024-04-01"},
                     {"id": 2, "name": "Mobile app", "description": "MVP build.",
                      "start_date": "2024-05-01"}])
    b.rows(label, [{"id": 1, "name": "frontend"}, {"id": 2, "name": "backend"},
                   {"id": 3, "name": "urgent"}])
    b.rows(task, [
        {"id": 1, "title": "Design homepage", "status": "doing", "priority": "high",
         "due_date": "2024-04-15", "assignee": "Ada", "project_id": 1},
        {"id": 2, "title": "Set up CI", "status": "todo", "priority": "medium",
         "due_date": "2024-04-20", "assignee": "Alan", "project_id": 2},
    ])
    b.junction_rows(task, label, [(1, 1), (1, 3), (2, 2)])
    return b.schema(), b.data()


def build_hr():
    b = ModelBuilder()
    dept = b.table("department", "Department")
    b.field(dept, "name", "string", length=100, nullable=False, display=True)
    emp = b.table("employee", "Employee")
    b.field(emp, "name", "string", length=120, nullable=False, display=True)
    b.field(emp, "email", "string", length=160)
    b.field(emp, "position", "enum", enum=["junior", "mid", "senior", "lead", "manager"],
            default="mid")
    b.field(emp, "hired_on", "date")
    b.m1(emp, dept, "department_id", "Department")
    b.m1(emp, emp, "manager_id", "Manager")  # self-relation

    g = b.menu_group("HR")
    b.menu_form("Employees", b.form("hr_employee", "Employees", emp), g)
    b.menu_form("Departments", b.form("hr_department", "Departments", dept), g)

    b.rows(dept, [{"id": 1, "name": "Engineering"}, {"id": 2, "name": "Sales"}])
    b.rows(emp, [
        {"id": 1, "name": "Grace Hopper", "email": "grace@co.test", "position": "manager",
         "hired_on": "2018-01-15", "department_id": 1, "manager_id": None},
        {"id": 2, "name": "Dennis Ritchie", "email": "dennis@co.test", "position": "senior",
         "hired_on": "2019-03-01", "department_id": 1, "manager_id": 1},
        {"id": 3, "name": "Radia Perlman", "email": "radia@co.test", "position": "lead",
         "hired_on": "2020-06-10", "department_id": 2, "manager_id": 1},
    ])
    return b.schema(), b.data()


def build_netcmdb():
    """A large network CMDB: ~12 tables, ~50 rows each, multiple status workflows."""
    b = ModelBuilder()
    N = 50

    def cyc(seq, i):
        return seq[i % len(seq)]

    def fk(i, offset=0):
        return ((i + offset) % N) + 1          # a valid 1..N foreign key

    def lay(states):
        return {s: {"x": 40 + i * 180, "y": 60 + (i % 2) * 90} for i, s in enumerate(states)}

    DEV = ["planned", "provisioning", "active", "maintenance", "decommissioned"]
    DEV_TX = [{"from": "planned", "to": "provisioning", "roles": []},
              {"from": "provisioning", "to": "active", "roles": []},
              {"from": "active", "to": "maintenance", "roles": []},
              {"from": "maintenance", "to": "active", "roles": []},
              {"from": "active", "to": "decommissioned", "roles": []},
              {"from": "maintenance", "to": "decommissioned", "roles": []}]

    # ---- tables + fields -------------------------------------------------
    org = b.table("organization", "Organization")
    b.field(org, "name", "string", length=120, nullable=False, display=True)
    b.field(org, "industry", "enum",
            enum=["telecom", "finance", "retail", "healthcare", "government"], default="telecom")
    b.field(org, "tier", "enum", enum=["bronze", "silver", "gold", "platinum"], default="silver")

    vendor = b.table("vendor", "Vendor")
    b.field(vendor, "name", "string", length=120, nullable=False, display=True)
    b.field(vendor, "category", "enum", enum=["network", "server", "cloud", "telco"],
            default="network")

    site = b.table("site", "Site")
    b.field(site, "name", "string", length=120, nullable=False, display=True)
    b.field(site, "code", "string", length=12, is_unique=True)
    b.field(site, "city", "string", length=80)
    b.field(site, "country", "string", length=60)
    site_st = b.field(site, "status", "enum", enum=["planned", "building", "live", "closed"],
                      default="planned")
    b.m1(site, org, "organization_id", "Organization")

    rack = b.table("rack", "Rack")
    b.field(rack, "name", "string", length=80, nullable=False, display=True)
    b.field(rack, "units", "integer", default="42")
    b.m1(rack, site, "site_id", "Site")

    def device_table(phys, label, prefix, with_rack=True):
        tid = b.table(phys, label, track_audit=True)
        b.field(tid, "name", "string", length=120, nullable=False, display=True)
        b.field(tid, "mgmt_ip", "string", length=45)
        b.field(tid, "model", "string", length=80)
        st = b.field(tid, "status", "enum", enum=DEV, default="planned")
        b.m1(tid, site, "site_id", "Site")
        b.m1(tid, vendor, "vendor_id", "Vendor")
        if with_rack:
            b.m1(tid, rack, "rack_id", "Rack")
        b.workflow(tid, st, DEV_TX, "planned", layout=lay(DEV))
        return tid, prefix

    router, _ = device_table("router", "Router", "rtr")
    switch, _ = device_table("switch", "Switch", "sw")
    ap, _ = device_table("access_point", "Access point", "ap", with_rack=False)
    server = b.table("server", "Server", track_audit=True)
    b.field(server, "name", "string", length=120, nullable=False, display=True)
    b.field(server, "hostname", "string", length=120)
    b.field(server, "mgmt_ip", "string", length=45)
    srv_st = b.field(server, "status", "enum", enum=DEV, default="planned")
    b.m1(server, site, "site_id", "Site")
    b.m1(server, vendor, "vendor_id", "Vendor")
    b.m1(server, rack, "rack_id", "Rack")
    b.workflow(server, srv_st, DEV_TX, "planned", layout=lay(DEV))

    subnet = b.table("ip_subnet", "IP subnet")
    b.field(subnet, "cidr", "string", length=20, nullable=False, is_unique=True, display=True)
    b.field(subnet, "vlan", "integer")
    b.m1(subnet, site, "site_id", "Site")

    circuit = b.table("circuit", "Circuit")
    b.field(circuit, "name", "string", length=120, nullable=False, display=True)
    b.field(circuit, "bandwidth_mbps", "integer", default="100")
    circ_st = b.field(circuit, "status", "enum",
                      enum=["ordered", "installing", "active", "cancelled"], default="ordered")
    b.m1(circuit, site, "site_id", "Site")
    b.m1(circuit, vendor, "carrier_id", "Carrier")
    b.workflow(circuit, circ_st,
               [{"from": "ordered", "to": "installing", "roles": []},
                {"from": "installing", "to": "active", "roles": []},
                {"from": "ordered", "to": "cancelled", "roles": []},
                {"from": "active", "to": "cancelled", "roles": []}],
               "ordered", layout=lay(["ordered", "installing", "active", "cancelled"]))

    CR = ["draft", "submitted", "approved", "rejected", "implemented", "closed"]
    cr = b.table("change_request", "Change request", track_audit=True, soft_delete=True)
    b.field(cr, "title", "string", length=160, nullable=False, display=True)
    b.field(cr, "description", "text")
    b.field(cr, "risk", "enum", enum=["low", "medium", "high"], default="low")
    cr_st = b.field(cr, "status", "enum", enum=CR, default="draft")
    b.m1(cr, site, "site_id", "Affected site")
    b.workflow(cr, cr_st,
               [{"from": "draft", "to": "submitted", "roles": []},
                {"from": "submitted", "to": "approved", "roles": ["designer"]},
                {"from": "submitted", "to": "rejected", "roles": ["designer"]},
                {"from": "approved", "to": "implemented", "roles": []},
                {"from": "implemented", "to": "closed", "roles": []},
                {"from": "rejected", "to": "draft", "roles": []}],
               "draft", layout=lay(CR))

    INC = ["new", "triaged", "in_progress", "resolved", "closed"]
    incident = b.table("incident", "Incident", track_audit=True, soft_delete=True)
    b.field(incident, "title", "string", length=160, nullable=False, display=True)
    b.field(incident, "severity", "enum", enum=["sev1", "sev2", "sev3", "sev4"], default="sev3")
    inc_st = b.field(incident, "status", "enum", enum=INC, default="new")
    b.m1(incident, site, "site_id", "Site")
    affected = b.mn(incident, router, "Affected routers")
    b.workflow(incident, inc_st,
               [{"from": "new", "to": "triaged", "roles": []},
                {"from": "triaged", "to": "in_progress", "roles": []},
                {"from": "in_progress", "to": "resolved", "roles": []},
                {"from": "resolved", "to": "in_progress", "roles": []},
                {"from": "resolved", "to": "closed", "roles": []}],
               "new", layout=lay(INC))

    b.workflow(site, site_st,
               [{"from": "planned", "to": "building", "roles": []},
                {"from": "building", "to": "live", "roles": []},
                {"from": "live", "to": "closed", "roles": []}],
               "planned", layout=lay(["planned", "building", "live", "closed"]))

    # ---- forms (data + view) + menu -------------------------------------
    g = b.menu_group("Network CMDB")
    b.menu_form("Organizations", b.form("nc_org", "Organizations", org), g)
    b.menu_form("Sites", b.form("nc_site", "Sites", site), g)
    b.menu_form("Vendors", b.form("nc_vendor", "Vendors", vendor), g)
    b.menu_form("Racks", b.form("nc_rack", "Racks", rack), g)
    b.menu_form("Routers", b.form("nc_router", "Routers", router), g)
    b.menu_form("Switches", b.form("nc_switch", "Switches", switch), g)
    b.menu_form("Access points", b.form("nc_ap", "Access points", ap), g)
    b.menu_form("Servers", b.form("nc_server", "Servers", server), g)
    b.menu_form("IP subnets", b.form("nc_subnet", "IP subnets", subnet), g)
    b.menu_form("Circuits", b.form("nc_circuit", "Circuits", circuit), g)
    b.menu_form("Change requests", b.form("nc_cr", "Change requests", cr), g)
    b.menu_form("Incidents", b.form("nc_incident", "Incidents", incident, mn=[affected]), g)
    # read-only view pages (clickable records)
    for phys, title, tid, mn in [("organization", "Organization", org, ()),
                                 ("site", "Site", site, ()), ("vendor", "Vendor", vendor, ()),
                                 ("router", "Router", router, ()), ("switch", "Switch", switch, ()),
                                 ("access_point", "Access point", ap, ()),
                                 ("server", "Server", server, ()), ("circuit", "Circuit", circuit, ()),
                                 ("change_request", "Change request", cr, ()),
                                 ("incident", "Incident", incident, (affected,))]:
        b.view_form(f"nc_{phys}_view", title, tid, mn=mn)

    # ---- data (organization first so the loader's row-count check sees 50) ----
    INDUSTRY = ["telecom", "finance", "retail", "healthcare", "government"]
    TIER = ["bronze", "silver", "gold", "platinum"]
    CITY = ["Berlin", "Paris", "Madrid", "Rome", "Oslo", "Lisbon", "Vienna", "Dublin"]
    COUNTRY = ["DE", "FR", "ES", "IT", "NO", "PT", "AT", "IE"]
    VCAT = ["network", "server", "cloud", "telco"]
    MODEL = ["MX204", "QFX5100", "AP-515", "C9300", "DL380", "ASR1001"]
    RISK = ["low", "medium", "high"]
    SEV = ["sev1", "sev2", "sev3", "sev4"]

    b.rows(org, [{"id": i, "name": f"Org {i:02d}", "industry": cyc(INDUSTRY, i),
                  "tier": cyc(TIER, i)} for i in range(1, N + 1)])
    b.rows(vendor, [{"id": i, "name": f"Vendor {i:02d}", "category": cyc(VCAT, i)}
                    for i in range(1, N + 1)])
    b.rows(site, [{"id": i, "name": f"Site {i:02d}", "code": f"S{i:03d}", "city": cyc(CITY, i),
                   "country": cyc(COUNTRY, i), "status": cyc(["planned", "building", "live", "closed"], i),
                   "organization_id": fk(i)} for i in range(1, N + 1)])
    b.rows(rack, [{"id": i, "name": f"Rack {i:02d}", "units": cyc([24, 42, 48], i),
                   "site_id": fk(i)} for i in range(1, N + 1)])

    def device_rows(prefix, with_rack=True, with_model=True):
        out = []
        for i in range(1, N + 1):
            row = {"id": i, "name": f"{prefix}-{i:03d}", "mgmt_ip": f"10.{fk(i)}.0.{i}",
                   "status": cyc(DEV, i), "site_id": fk(i), "vendor_id": fk(i, 3)}
            if with_model:
                row["model"] = cyc(MODEL, i)
            if with_rack:
                row["rack_id"] = fk(i, 1)
            out.append(row)
        return out

    b.rows(router, device_rows("rtr"))
    b.rows(switch, device_rows("sw"))
    b.rows(ap, device_rows("ap", with_rack=False))
    b.rows(server, [{"id": i, "name": f"srv-{i:03d}", "hostname": f"srv-{i:03d}.net",
                     "mgmt_ip": f"10.{fk(i)}.1.{i}", "status": cyc(DEV, i), "site_id": fk(i),
                     "vendor_id": fk(i, 3), "rack_id": fk(i, 1)} for i in range(1, N + 1)])
    b.rows(subnet, [{"id": i, "cidr": f"10.{i}.0.0/24", "vlan": 100 + i, "site_id": fk(i)}
                    for i in range(1, N + 1)])
    b.rows(circuit, [{"id": i, "name": f"Circuit {i:02d}", "bandwidth_mbps": cyc([100, 500, 1000, 10000], i),
                      "status": cyc(["ordered", "installing", "active", "cancelled"], i),
                      "site_id": fk(i), "carrier_id": fk(i, 5)} for i in range(1, N + 1)])
    b.rows(cr, [{"id": i, "title": f"CR-{i:04d} change at site {fk(i)}",
                 "description": "Auto-generated demo change.", "risk": cyc(RISK, i),
                 "status": cyc(CR, i), "site_id": fk(i)} for i in range(1, N + 1)])
    b.rows(incident, [{"id": i, "title": f"INC-{i:04d} issue at site {fk(i)}",
                       "severity": cyc(SEV, i), "status": cyc(INC, i), "site_id": fk(i)}
                      for i in range(1, N + 1)])
    b.junction_rows(incident, router, [(i, fk(i, 9)) for i in range(1, N + 1)])
    return b.schema(), b.data()


EXAMPLES = {
    "cmdb": {"title": "CMDB", "build": build_cmdb,
             "description": "Configuration items, environments, teams and applications "
                            "(with a runs-on many-to-many)."},
    "library": {"title": "Library (book borrowing)", "build": build_library,
                "description": "Authors, books, members and loans — a lending library."},
    "helpdesk": {"title": "Helpdesk", "build": build_helpdesk,
                 "description": "Support tickets with status/priority, categories and agents."},
    "crm": {"title": "CRM / sales", "build": build_crm,
            "description": "Companies, contacts and deals with a sales-stage pipeline."},
    "projects": {"title": "Projects & tasks", "build": build_projects,
                 "description": "Projects, tasks (status/priority) and a tasks-labels many-to-many."},
    "hr": {"title": "HR / employees", "build": build_hr,
           "description": "Departments and employees with a manager self-relation."},
    "netcmdb": {"title": "Network CMDB (large)", "build": build_netcmdb,
                "description": "12 tables (organizations, sites, racks, routers, switches, access "
                               "points, servers, subnets, circuits, change requests, incidents) with "
                               "~50 rows each and multiple status workflows, view pages and audit/Trash."},
}
