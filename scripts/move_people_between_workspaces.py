"""Move a group of people from one workspace to another, with everything theirs.

Written for 2026-11-12: Artbox Solutions got its own workspace, and every
person with an @artboxsolutions.com email (employee record or login) moves out
of cgb-solutions into it, keeping their logins (same password), punches with
their map locations, attendance days, regularisations, leave, salary master,
bank accounts, profiles, lifecycle events, probation, notifications and their
imported payslips.

    .venv/Scripts/python.exe scripts/move_people_between_workspaces.py \
        --db-url postgresql://... [--production] \
        --source cgb-solutions --target artbox-solutions --domain artboxsolutions.com \
        [--keep CDL25-732] [--history-company target|source] \
        [--master-key-file FILE] [--apply] [--report FILE]

Without --apply everything runs inside a transaction that is rolled back, and
the report says what would have happened. A database that is not on this
machine needs --production as well, so a copy-and-paste cannot reach it.

--keep (employee code or email, repeatable) leaves that person and their login
in the source although their email matches (2026-11-12: Lipika Prasad, CGB's
HR admin, uses hr@artboxsolutions.com and stays in CGB).

--history-company decides the company on moved payslips of past runs: "target"
= the target's own company; "source" = an inactive copy of the company that ran
them (name and address only, no PAN/TAN/PF/ESI numbers), so an old payslip
still shows who paid it. Required when payslips move.

How:
  1. Settings the moved rows need are COPIED to the target (new ids): departments,
     the locations and projects these people use, holidays, leave types and
     policies, salary components and structures, payroll rule sets, letter
     templates, onboarding templates, custom fields, missing notification
     templates. A target row with the same natural key (code) is reused. The
     source's default shift is mapped onto the target's default shift, whose
     timings are set to the source's. The source's company maps to the target's
     own company. Workspace settings: attendance rules, letter defaults, the Ask
     Miraa companion settings and the letterhead design (with the target's name
     and address, no signature) are copied.
  2. People rows are MOVED in place (same ids, tenant changed), so logins keep
     their password hash and every link between moved rows stays valid.
  3. Payslips: each source payroll run that holds a moved payslip gets a copy in
     the target (same period, status, dates); the payslips move into it and both
     runs' totals are recomputed from their payslips.
  4. Links that would cross workspaces are emptied: a staying person's manager
     who moved, a moved person's manager who stayed, "approved by" a person on
     the other side. Every one is listed in the report and the audit row.
  5. The audit trail is insert-only: the source keeps its rows; the target gets
     a copy of every audit row about a moved row, plus one summary row each side.
  6. Geocoded punch addresses for the moved punches are copied to the target.

Every source table holding a row that references a moved person must be
classified below, or nothing is done. Raw SQL by table name: the live database
can be a migration ahead of or behind this code.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

#: table -> column naming the moved employee.
BY_EMPLOYEE: dict[str, str] = {
    "employees": "id",
    "attendance_days": "employee_id",
    "attendance_punches": "employee_id",
    "attendance_regularisations": "employee_id",
    "employee_bank_accounts": "employee_id",
    "employee_documents": "employee_id",
    "employee_profiles": "employee_id",
    "employee_salaries": "employee_id",
    "exit_records": "employee_id",
    "leave_balance_adjustments": "employee_id",
    "leave_balances": "employee_id",
    "leave_requests": "employee_id",
    "lifecycle_events": "employee_id",
    "onboarding_plans": "employee_id",
    "payslips": "employee_id",
    "probation_reviews": "employee_id",
}
#: table -> column naming the moved login.
BY_USER: dict[str, str] = {
    "users": "id",
    "notifications": "recipient_user_id",
    "password_reset_tokens": "user_id",
    "step_up_challenges": "user_id",
}
#: table -> (column, parent table): moves with its moved parent.
CHILDREN: dict[str, tuple[str, str]] = {
    "employee_salary_components": ("employee_salary_id", "employee_salaries"),
    "onboarding_tasks": ("plan_id", "onboarding_plans"),
}
MOVED_TABLES = set(BY_EMPLOYEE) | set(BY_USER) | set(CHILDREN)

#: A staying row may point at a moved person only through one of these columns
#: (who approved, who is the manager...): the link is emptied. Any other column
#: stops the move — that row probably belongs to the person and must be classified.
CLEARABLE_SUFFIXES = (
    "_by_user_id",
    "approver_user_id",
    "assignee_user_id",
    "owner_user_id",
    "manager_id",
    "buddy_employee_id",
    "head_employee_id",
)

#: Settings copied to the target, in dependency order:
#: (table, natural key columns or None, which source rows).
#: "all" = every source row; "used" = only rows the moved rows point at.
SETTINGS: list[tuple[str, tuple[str, ...] | None, str]] = [
    ("departments", ("code",), "all"),
    ("locations", ("code",), "used"),
    ("projects", ("code",), "used"),
    ("holidays", None, "all"),
    ("leave_types", ("code",), "all"),
    ("leave_policies", None, "all"),
    ("salary_components", ("code",), "all"),
    ("salary_structures", None, "all"),
    ("salary_structure_components", None, "all"),
    ("payroll_rule_sets", ("rule_type", "jurisdiction", "effective_from"), "all"),
    ("letter_templates", ("code", "version"), "all"),
    ("letter_template_fields", None, "all"),
    ("notification_templates", ("event_key", "channel", "locale", "version"), "all"),
    ("onboarding_templates", ("name",), "all"),
    ("onboarding_template_tasks", None, "all"),
    ("custom_field_definitions", ("entity", "key"), "all"),
]
SETTINGS_TABLES = {t for t, _, _ in SETTINGS}
#: Mapped, never copied: source row -> the target's own row.
MAPPED_ONLY = {"legal_entities", "shifts"}

TENANT_SETTINGS_COPIED = ("attendance", "letter_defaults", "companion")
SHIFT_FIELDS = (
    "start_time",
    "end_time",
    "break_minutes",
    "grace_in_minutes",
    "grace_out_minutes",
    "half_day_threshold_minutes",
    "full_day_threshold_minutes",
    "is_night_shift",
    "weekly_off_days",
    "alternate_off_weeks",
)


class MoveError(RuntimeError):
    pass


#: Values encrypted with the workspace's own derived key (app/core/crypto.py
#: encrypt_secret): they must be re-encrypted for the target, or they become
#: unreadable there. Values with the platform field key ("encf:v1:") move as-is.
TENANT_SECRET_PREFIX = "enc:v1:"


class Rekeyer:
    """Decrypts with the source workspace's key, encrypts with the target's.

    Same derivation as app/core/crypto._tenant_key, from the master key of the
    server that wrote the values (production's SECRET_ENCRYPTION_KEY).
    """

    def __init__(self, master_key: str):
        self.master = master_key.encode("utf-8")

    def _fernet(self, tenant_id):
        import base64
        import hashlib

        from cryptography.fernet import Fernet

        derived = hashlib.pbkdf2_hmac(
            "sha256", self.master, b"askmira-tenant-secret:" + str(tenant_id).encode("ascii"),
            iterations=1_000, dklen=32,
        )
        return Fernet(base64.urlsafe_b64encode(derived))

    def rekey(self, value: str, source, target) -> str:
        from cryptography.fernet import InvalidToken

        try:
            plain = self._fernet(source).decrypt(value[len(TENANT_SECRET_PREFIX):].encode("ascii"))
        except InvalidToken as exc:
            raise MoveError(
                "A stored secret could not be decrypted with this master key: wrong key for this database."
            ) from exc
        return TENANT_SECRET_PREFIX + self._fernet(target).encrypt(plain).decode("ascii")


def q(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall() if cur.description else []


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


#: Columns of a copied history company that are left empty: the company's
#: statutory numbers stay with the workspace that owns it.
HISTORY_COMPANY_BLANK = ("pan", "tan", "gstin", "pf_registration_no", "esi_registration_no", "logo_key")


class Mover:
    def __init__(
        self,
        cur,
        source: str,
        target: str,
        domain: str,
        keep: list[str] | tuple[str, ...] = (),
        history_company: str | None = None,
        dry_run: bool = True,
    ):
        self.cur = cur
        self.keep = [k.strip() for k in keep if k.strip()]
        self.history_company = history_company
        self.dry_run = dry_run
        rows = q(cur, "select id, slug, name, settings from tenants where slug in (%s, %s)", (source, target))
        by_slug = {r[1]: r for r in rows}
        if source not in by_slug or target not in by_slug:
            raise MoveError(f"Workspace not found: {source if source not in by_slug else target}")
        self.src, self.src_name, self.src_settings = by_slug[source][0], by_slug[source][2], by_slug[source][3] or {}
        self.tgt, self.tgt_name, self.tgt_settings = by_slug[target][0], by_slug[target][2], by_slug[target][3] or {}
        self.domain = domain.lower().lstrip("@")
        self.report: dict = {"copied": {}, "reused": {}, "moved": {}, "links_cleared": {}, "runs": []}
        self.fks = self._foreign_keys()
        self.columns = {}

    # -- schema --------------------------------------------------------------
    def _foreign_keys(self) -> dict[str, list[tuple[str, str, bool]]]:
        """table -> [(column, target table, nullable)] for single-column FKs."""
        out: dict[str, list[tuple[str, str, bool]]] = {}
        for tbl, col, target, notnull in q(
            self.cur,
            """
            select r.relname, a.attname, f.relname, a.attnotnull
            from pg_constraint c
            join pg_class r on r.oid = c.conrelid
            join pg_class f on f.oid = c.confrelid
            join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
            where c.contype = 'f' and array_length(c.conkey, 1) = 1
              and r.relnamespace = 'public'::regnamespace and a.attname <> 'tenant_id'
            """,
        ):
            out.setdefault(tbl, []).append((col, target, not notnull))
        return out

    def cols(self, table: str) -> list[str]:
        if table not in self.columns:
            self.columns[table] = [
                r[0]
                for r in q(
                    self.cur,
                    "select column_name from information_schema.columns "
                    "where table_schema = 'public' and table_name = %s order by ordinal_position",
                    (table,),
                )
            ]
        return self.columns[table]

    # -- who moves -----------------------------------------------------------
    def select_people(self):
        like = f"%@{self.domain}"
        c = self.cur
        # People asked to stay: by employee code, or by email (work email or login).
        c.execute("create temp table mv_keep (id uuid primary key) on commit drop")
        codes = [k for k in self.keep if "@" not in k]
        emails = [k.lower() for k in self.keep if "@" in k]
        for k in self.keep:
            hit = q(
                c,
                "select 1 from employees where tenant_id = %(s)s and (employee_code = %(k)s "
                "or lower(work_email) = lower(%(k)s)) union all "
                "select 1 from users where tenant_id = %(s)s and lower(email) = lower(%(k)s)",
                {"s": self.src, "k": k},
            )
            if not hit:
                raise MoveError(f"--keep {k}: no employee code or email like that in {self.src_name}.")
        c.execute(
            "insert into mv_keep select id from employees where tenant_id = %(s)s "
            "and (employee_code = any(%(codes)s::text[]) or lower(work_email) = any(%(emails)s::text[]))",
            {"s": self.src, "codes": codes, "emails": emails},
        )
        c.execute(
            "insert into mv_keep select id from users where tenant_id = %(s)s "
            "and (employee_id in (select id from mv_keep) or lower(email) = any(%(emails)s::text[])) "
            "on conflict do nothing",
            {"s": self.src, "emails": emails},
        )
        c.execute("create temp table mv_people (kind text, id uuid primary key) on commit drop")
        c.execute(
            """
            insert into mv_people
            select 'employee', e.id from employees e
            where e.tenant_id = %(s)s and (lower(e.work_email) like %(l)s or lower(e.personal_email) like %(l)s
              or e.id in (select employee_id from users where tenant_id = %(s)s and lower(email) like %(l)s))
              and e.id not in (select id from mv_keep)
            """,
            {"s": self.src, "l": like},
        )
        c.execute(
            """
            insert into mv_people
            select 'user', u.id from users u
            where u.tenant_id = %(s)s and (lower(u.email) like %(l)s
              or u.employee_id in (select id from mv_people where kind = 'employee'))
              and u.id not in (select id from mv_keep)
            """,
            {"s": self.src, "l": like},
        )
        # A login whose employee record would stay behind, or the reverse, is refused.
        bad = q(
            c,
            """
            select u.email from users u join mv_people m on m.id = u.id
            where u.employee_id is not null and u.employee_id not in (select id from mv_people)
            """,
        )
        if bad:
            raise MoveError(f"Login moves without its employee record: {bad}")
        left = q(
            c,
            """
            select u.email from users u where u.id in (select id from mv_keep)
              and u.employee_id in (select id from mv_people where kind = 'employee')
            """,
        )
        if left:
            raise MoveError(f"Employee record moves but its login is kept: {left}")
        self.report["kept"] = [
            dict(zip(("code", "name", "work_email", "login", "role"), r))
            for r in q(
                c,
                "select e.employee_code, trim(e.first_name || ' ' || coalesce(e.last_name, '')), e.work_email, "
                "u.email, u.role::text from employees e left join users u on u.employee_id = e.id "
                "where e.id in (select id from mv_keep) order by e.employee_code",
            )
        ]
        self.employees = [r[0] for r in q(c, "select id from mv_people where kind='employee'")]
        self.users = [r[0] for r in q(c, "select id from mv_people where kind='user'")]
        self.people = set(self.employees) | set(self.users)
        if not self.employees:
            raise MoveError(f"No one in {self.src_name} has an @{self.domain} email.")
        # Emails must not already exist as logins in the target.
        clash = q(
            c,
            "select email from users where tenant_id = %s and lower(email) in "
            "(select lower(email) from users where id = any(%s))",
            (self.tgt, self.users),
        )
        if clash:
            raise MoveError(f"Logins already in the target: {clash}")
        codes = q(
            c,
            "select employee_code from employees where tenant_id = %s and employee_code in "
            "(select employee_code from employees where id = any(%s))",
            (self.tgt, self.employees),
        )
        if codes:
            raise MoveError(f"Employee codes already used in the target: {codes}")

    def collect_rows(self):
        """mv_rows(tbl, id): every row that moves."""
        c = self.cur
        c.execute("create temp table mv_rows (tbl text, id uuid, primary key (tbl, id)) on commit drop")
        for tbl, col in {**BY_EMPLOYEE, **BY_USER}.items():
            if tbl not in self._tenant_tables():
                continue
            ids = self.employees if tbl in BY_EMPLOYEE else self.users
            c.execute(
                f"insert into mv_rows select %s, id from {ident(tbl)} "
                f"where tenant_id = %s and {ident(col)} = any(%s)",
                (tbl, self.src, ids),
            )
        for tbl, (col, parent) in CHILDREN.items():
            c.execute(
                f"insert into mv_rows select %s, x.id from {ident(tbl)} x "
                f"where x.tenant_id = %s and x.{ident(col)} in (select id from mv_rows where tbl = %s)",
                (tbl, self.src, parent),
            )
        for tbl, n in q(c, "select tbl, count(*) from mv_rows group by tbl order by tbl"):
            self.report["moved"][tbl] = n
        # Kept outside the transaction's temp tables, for the check after the commit.
        self.moved_ids: dict[str, list] = {}
        for tbl, rid in q(c, "select tbl, id from mv_rows"):
            self.moved_ids.setdefault(tbl, []).append(rid)

    def _tenant_tables(self) -> set[str]:
        if not hasattr(self, "_tt"):
            self._tt = {
                r[0]
                for r in q(
                    self.cur,
                    "select c.table_name from information_schema.columns c "
                    "join information_schema.tables t on t.table_schema = c.table_schema "
                    "and t.table_name = c.table_name where c.table_schema = 'public' "
                    "and c.column_name = 'tenant_id' and t.table_type = 'BASE TABLE'",
                )
            }
        return self._tt

    def check_classified(self):
        """Every staying row that points at a moved person must be clearable."""
        problems = []
        self.clear_staying: list[tuple[str, str, int]] = []
        for tbl, links in sorted(self.fks.items()):
            if tbl == "audit_logs" or tbl not in self._tenant_tables():
                continue
            for col, target, nullable in links:
                if target not in ("users", "employees"):
                    continue
                n = q(
                    self.cur,
                    f"select count(*) from {ident(tbl)} x where x.tenant_id = %s and x.{ident(col)} = any(%s) "
                    f"and not exists (select 1 from mv_rows m where m.tbl = %s and m.id = x.id)",
                    (self.src, list(self.people), tbl),
                )[0][0]
                if not n:
                    continue
                if nullable and col.endswith(CLEARABLE_SUFFIXES):
                    self.clear_staying.append((tbl, col, n))
                else:
                    problems.append(f"{tbl}.{col} ({n} rows)")
        if problems:
            raise MoveError(
                "Rows that stay in the source but belong to moved people; classify them first: "
                + ", ".join(problems)
            )

    # -- settings ------------------------------------------------------------
    def build_map(self):
        c = self.cur
        c.execute("create temp table mv_map (tbl text, old uuid, new uuid, copy bool, primary key (tbl, old)) on commit drop")
        # Company: the source's companies -> the target's (only) active company.
        tgt_entities = q(c, "select id from legal_entities where tenant_id = %s and is_active order by created_at", (self.tgt,))
        if len(tgt_entities) != 1:
            raise MoveError(f"Target must have exactly one active company, has {len(tgt_entities)}.")
        self.tgt_entity = tgt_entities[0][0]
        c.execute(
            "insert into mv_map select 'legal_entities', id, %s, false from legal_entities where tenant_id = %s",
            (self.tgt_entity, self.src),
        )
        # Shift: source shifts -> target default shift, timings aligned to the source default.
        tgt_shift = q(c, "select id from shifts where tenant_id = %s and is_default", (self.tgt,))
        src_shift = q(c, "select id from shifts where tenant_id = %s and is_default", (self.src,))
        if not tgt_shift or not src_shift:
            raise MoveError("Both workspaces need a default shift.")
        self.tgt_shift, self.src_shift = tgt_shift[0][0], src_shift[0][0]
        c.execute(
            "insert into mv_map select 'shifts', id, %s, false from shifts where tenant_id = %s",
            (self.tgt_shift, self.src),
        )
        others = q(c, "select code from shifts where tenant_id = %s and not is_default", (self.src,))
        if others:
            self.report["note_shifts"] = f"source non-default shifts mapped to the target default: {others}"

        for tbl, natural, which in SETTINGS:
            if tbl not in self._tenant_tables():
                continue
            where = "x.tenant_id = %(s)s"
            if which == "used":
                refs = self._used_by_moved_rows(tbl)
                where += " and x.id = any(%(used)s)"
            else:
                refs = None
            if natural:
                match = " and ".join(f"y.{ident(k)} is not distinct from x.{ident(k)}" for k in natural)
                existing = f"(select y.id from {ident(tbl)} y where y.tenant_id = %(t)s and {match})"
                c.execute(
                    f"insert into mv_map select %(tbl)s, x.id, coalesce({existing}, gen_random_uuid()), "
                    f"{existing} is null from {ident(tbl)} x where {where}",
                    {"s": self.src, "t": self.tgt, "tbl": tbl, "used": refs},
                )
            else:
                c.execute(
                    f"insert into mv_map select %(tbl)s, x.id, gen_random_uuid(), true from {ident(tbl)} x where {where}",
                    {"s": self.src, "tbl": tbl, "used": refs},
                )

    def _used_by_moved_rows(self, setting: str) -> list:
        """Ids of `setting` rows referenced by moved rows (or by holidays)."""
        ids: set = set()
        copied_all = {t for t, _, which in SETTINGS if which == "all"}
        for tbl in sorted(MOVED_TABLES | copied_all):
            for col, target, _ in self.fks.get(tbl, []):
                if target != setting:
                    continue
                if tbl in MOVED_TABLES:
                    sql = (
                        f"select distinct x.{ident(col)} from {ident(tbl)} x join mv_rows m "
                        f"on m.tbl = %s and m.id = x.id where x.{ident(col)} is not null"
                    )
                    args = (tbl,)
                else:
                    sql = (
                        f"select distinct {ident(col)} from {ident(tbl)} where tenant_id = %s "
                        f"and {ident(col)} is not null"
                    )
                    args = (self.src,)
                ids |= {r[0] for r in q(self.cur, sql, args)}
        return list(ids)

    def _expr(self, table: str, col: str, alias: str = "x") -> str:
        """SQL for a column's value once in the target."""
        if col == "tenant_id":
            return "%(t)s"
        for fcol, target, nullable in self.fks.get(table, []):
            if fcol != col:
                continue
            ref = f"{alias}.{ident(col)}"
            if target in ("users", "employees"):
                return f"(case when {ref} = any(%(people)s) then {ref} else null end)"
            if target in MOVED_TABLES:
                if target == "payroll_runs":
                    return f"(select new from mv_map where tbl = 'payroll_runs' and old = {ref})"
                moved = f"exists (select 1 from mv_rows where tbl = '{target}' and id = {ref})"
                return f"(case when {moved} then {ref} else null end)"
            if target in SETTINGS_TABLES | MAPPED_ONLY | {"payroll_runs"}:
                return f"(select new from mv_map where tbl = '{target}' and old = {ref})"
            if target == "tenants":
                return "%(t)s"
            # A setting this script does not copy (issued letters, batches, whatsapp templates...).
            return "null" if nullable else ref
        return f"{alias}.{ident(col)}"

    def copy_settings(self):
        c = self.cur
        params = {"s": self.src, "t": self.tgt, "people": list(self.people)}
        for tbl, _, _ in SETTINGS:
            if tbl not in self._tenant_tables():
                continue
            cols = self.cols(tbl)
            exprs = []
            for col in cols:
                if col == "id":
                    exprs.append("m.new")
                elif col == "updated_at":
                    exprs.append("now()")
                else:
                    exprs.append(self._expr(tbl, col))
            for col, target, _ in self.fks.get(tbl, []):
                expr = self._expr(tbl, col)
                if "mv_map" not in expr:
                    continue
                missing = q(
                    c,
                    f"select count(*) from {ident(tbl)} x join mv_map m on m.tbl = %(tbl)s and m.old = x.id "
                    f"and m.copy where x.{ident(col)} is not null and ({expr}) is null",
                    {**params, "tbl": tbl},
                )[0][0]
                if missing:
                    raise MoveError(f"Copying {tbl}: {missing} rows point at {target} rows that are not copied.")
            c.execute(
                f"insert into {ident(tbl)} ({', '.join(ident(k) for k in cols)}) "
                f"select {', '.join(exprs)} from {ident(tbl)} x "
                f"join mv_map m on m.tbl = %(tbl)s and m.old = x.id and m.copy",
                {**params, "tbl": tbl},
            )
            copied = c.rowcount
            reused = q(c, "select count(*) from mv_map where tbl = %s and not copy", (tbl,))[0][0]
            if copied:
                self.report["copied"][tbl] = copied
            if reused:
                self.report["reused"][tbl] = reused
        # A copied setting must not point at a source row of a table we do not map.
        # Target default shift gets the source default's timings; company gets the default shift.
        sets = ", ".join(f"{ident(k)} = s.{ident(k)}" for k in SHIFT_FIELDS if k in self.cols("shifts"))
        c.execute(
            f"update shifts t set {sets}, updated_at = now() from shifts s where t.id = %s and s.id = %s",
            (self.tgt_shift, self.src_shift),
        )
        c.execute(
            "update legal_entities t set default_shift_id = %s, "
            "salary_days_basis = s.salary_days_basis, salary_fixed_days = s.salary_fixed_days, updated_at = now() "
            "from legal_entities s where t.id = %s and s.id = (select old from mv_map where tbl='legal_entities' "
            "and new = %s order by old limit 1)",
            (self.tgt_shift, self.tgt_entity, self.tgt_entity),
        )
        # Workspace settings.
        new_settings = dict(self.tgt_settings)
        for key in TENANT_SETTINGS_COPIED:
            if key in self.src_settings:
                new_settings[key] = self.src_settings[key]
        if self.src_settings.get("letterhead"):
            profile = self.tgt_settings.get("profile") or {}
            entity = q(c, "select name, registered_address from legal_entities where id = %s", (self.tgt_entity,))[0]
            letterhead = dict(self.src_settings["letterhead"])
            letterhead.update(
                company_name=profile.get("legal_name") or entity[0],
                address=entity[1] or profile.get("address_line1") or "",
                email=profile.get("contact_email") or "",
                phone=profile.get("contact_phone") or "",
                website=profile.get("website") or "",
                logo_key=None,
                signature_key=None,
                signatory_name="",
            )
            new_settings["letterhead"] = letterhead
        c.execute("update tenants set settings = %s, updated_at = now() where id = %s", (psycopg2.extras.Json(new_settings), self.tgt))
        self.report["workspace_settings_copied"] = [k for k in (*TENANT_SETTINGS_COPIED, "letterhead") if k in self.src_settings]

    # -- payroll runs --------------------------------------------------------
    def copy_runs(self):
        c = self.cur
        c.execute(
            """
            insert into mv_map select 'payroll_runs', r.run_id, gen_random_uuid(), true
            from (select distinct p.run_id from payslips p
                  join mv_rows m on m.tbl = 'payslips' and m.id = p.id) r
            """
        )
        if not q(c, "select count(*) from mv_map where tbl = 'payroll_runs'")[0][0]:
            return
        if self.history_company == "source":
            self._copy_history_companies()
            entity_expr = "(select new from mv_map where tbl = 'legal_entities_history' and old = x.legal_entity_id)"
        elif self.history_company == "target":
            entity_expr = None
        else:
            raise MoveError(
                "Payslips move with these people: choose --history-company target (the target's own "
                "company on old payslips) or source (a copy of the company that ran those payrolls)."
            )
        self.report["history_company"] = self.history_company
        cols = self.cols("payroll_runs")
        exprs = []
        for col in cols:
            if col == "id":
                exprs.append("m.new")
            elif col == "legal_entity_id" and entity_expr:
                exprs.append(entity_expr)
            elif col == "import_details":
                exprs.append(
                    "coalesce(x.import_details, '{}'::jsonb) || jsonb_build_object('moved_from', "
                    "jsonb_build_object('workspace', %(sname)s, 'run_id', x.id::text, 'moved_at', now()::text, "
                    "'note', 'Payslips of people moved to this workspace; file figures are the whole register'))"
                )
            elif col == "updated_at":
                exprs.append("now()")
            else:
                exprs.append(self._expr("payroll_runs", col))
        c.execute(
            f"insert into payroll_runs ({', '.join(ident(k) for k in cols)}) select {', '.join(exprs)} "
            f"from payroll_runs x join mv_map m on m.tbl = 'payroll_runs' and m.old = x.id",
            {"t": self.tgt, "people": list(self.people), "sname": self.src_name},
        )
        self.report["copied"]["payroll_runs"] = c.rowcount
        emptied = q(
            c,
            """
            select x.id from payroll_runs x join mv_map m on m.tbl='payroll_runs' and m.old = x.id
            where not exists (select 1 from payslips p where p.run_id = x.id
              and not exists (select 1 from mv_rows r where r.tbl='payslips' and r.id = p.id))
            """,
        )
        if emptied:
            raise MoveError(f"{len(emptied)} source runs would be left without payslips; decide first.")
        # Other things paid in those runs (arrears, loans...) do not move: refuse if any belong to moved people.

    def _copy_history_companies(self):
        """An inactive copy, in the target, of each company that ran the copied runs."""
        c = self.cur
        info = {
            name: (nullable == "YES", dtype)
            for name, nullable, dtype in q(
                c,
                "select column_name, is_nullable, data_type from information_schema.columns "
                "where table_schema = 'public' and table_name = 'legal_entities'",
            )
        }
        sources = q(
            c,
            "select distinct x.legal_entity_id from payroll_runs x "
            "join mv_map m on m.tbl = 'payroll_runs' and m.old = x.id",
        )
        made = []
        for (old,) in sources:
            code, name = q(c, "select code, name from legal_entities where id = %s", (old,))[0]
            new_code, n = f"{code}-HIST", 2
            while q(c, "select 1 from legal_entities where tenant_id = %s and code = %s", (self.tgt, new_code)):
                new_code, n = f"{code}-HIST{n}", n + 1
            new = q(c, "select gen_random_uuid()")[0][0]
            c.execute("insert into mv_map values ('legal_entities_history', %s, %s, true)", (old, new))
            exprs = []
            for col in self.cols("legal_entities"):
                nullable, dtype = info[col]
                if col == "id":
                    exprs.append("%(new)s")
                elif col == "tenant_id":
                    exprs.append("%(t)s")
                elif col == "code":
                    exprs.append("%(code)s")
                elif col == "is_active":
                    exprs.append("false")
                elif col == "default_shift_id":
                    exprs.append("null")
                elif col == "updated_at":
                    exprs.append("now()")
                elif col in HISTORY_COMPANY_BLANK and nullable:
                    exprs.append("null")
                elif col == "pt_registrations":
                    exprs.append("null" if nullable else f"'{{}}'::{dtype}")
                else:
                    exprs.append(f"x.{ident(col)}")
            c.execute(
                f"insert into legal_entities ({', '.join(ident(k) for k in self.cols('legal_entities'))}) "
                f"select {', '.join(exprs)} from legal_entities x where x.id = %(old)s",
                {"new": new, "t": self.tgt, "code": new_code, "old": old},
            )
            made.append({"name": name, "code": new_code, "active": False})
        self.report["history_companies"] = made

    def recompute_runs(self):
        c = self.cur
        pairs = q(c, "select old, new from mv_map where tbl = 'payroll_runs'")
        run_ids = [o for o, _ in pairs] + [n for _, n in pairs]
        c.execute(
            """
            update payroll_runs r set
              employee_count = s.n, total_gross = s.g, total_deductions = s.d,
              total_net = s.net, total_employer_cost = s.ec, updated_at = now()
            from (select run_id, count(*) n, coalesce(sum(gross_earnings),0) g, coalesce(sum(total_deductions),0) d,
                         coalesce(sum(net_pay),0) net, coalesce(sum(employer_cost),0) ec
                  from payslips where run_id = any(%s) group by run_id) s
            where r.id = s.run_id
            """,
            (run_ids,),
        )
        for old, new in pairs:
            row = q(
                c,
                "select a.period_year, a.period_month, a.employee_count, b.employee_count, b.total_net "
                "from payroll_runs a, payroll_runs b where a.id = %s and b.id = %s",
                (old, new),
            )[0]
            self.report["runs"].append(
                {"period": f"{row[0]}-{row[1]:02d}", "source_left": row[2], "target_slips": row[3], "target_net": str(row[4])}
            )

    # -- move ----------------------------------------------------------------
    def move_rows(self):
        c = self.cur
        params = {"t": self.tgt, "people": list(self.people)}
        # Staying rows' links to moved people.
        for tbl, col, n in self.clear_staying:
            c.execute(
                f"update {ident(tbl)} x set {ident(col)} = null where x.tenant_id = %(s)s "
                f"and x.{ident(col)} = any(%(people)s) "
                f"and not exists (select 1 from mv_rows m where m.tbl = %(tbl)s and m.id = x.id)",
                {"s": self.src, "people": list(self.people), "tbl": tbl},
            )
            self.report["links_cleared"][f"{self.src_name}: {tbl}.{col}"] = c.rowcount
        # Moved rows: tenant and every foreign key rewritten.
        for tbl in sorted(MOVED_TABLES):
            if tbl not in self._tenant_tables():
                continue
            sets = ["tenant_id = %(t)s", "updated_at = now()"] if "updated_at" in self.cols(tbl) else ["tenant_id = %(t)s"]
            for col, target, nullable in self.fks.get(tbl, []):
                if col == "tenant_id":
                    continue
                expr = self._expr(tbl, col, alias=ident(tbl))
                if expr == f"{ident(tbl)}.{ident(col)}":
                    continue
                # Count what is being emptied (cross-workspace links).
                if target in ("users", "employees") or (target in MOVED_TABLES and target != "payroll_runs"):
                    n = q(
                        c,
                        f"select count(*) from {ident(tbl)} where id in (select id from mv_rows where tbl = %(tbl)s) "
                        f"and {ident(col)} is not null and ({expr}) is null",
                        {**params, "tbl": tbl},
                    )[0][0]
                    if n:
                        if not nullable:
                            raise MoveError(f"{tbl}.{col}: {n} moved rows point at rows that stay; cannot empty.")
                        self.report["links_cleared"][f"moved: {tbl}.{col}"] = n
                elif expr != "null":
                    missing = q(
                        c,
                        f"select count(*) from {ident(tbl)} where id in (select id from mv_rows where tbl = %(tbl)s) "
                        f"and {ident(col)} is not null and ({expr}) is null",
                        {**params, "tbl": tbl},
                    )[0][0]
                    if missing:
                        raise MoveError(f"{tbl}.{col}: {missing} moved rows point at {target} rows not in the target.")
                elif nullable:
                    n = q(
                        c,
                        f"select count(*) from {ident(tbl)} where id in (select id from mv_rows where tbl = %(tbl)s) "
                        f"and {ident(col)} is not null",
                        {"tbl": tbl},
                    )[0][0]
                    if n:
                        self.report["links_cleared"][f"moved: {tbl}.{col} -> {target}"] = n
                sets.append(f"{ident(col)} = {expr}")
            c.execute(
                f"update {ident(tbl)} set {', '.join(sets)} where id in (select id from mv_rows where tbl = %(tbl)s)",
                {**params, "tbl": tbl},
            )
            if c.rowcount != self.report["moved"].get(tbl, 0):
                raise MoveError(f"{tbl}: moved {c.rowcount} rows, expected {self.report['moved'].get(tbl, 0)}")

    def find_secrets(self) -> list[tuple[str, str, object, str]]:
        """(table, column, id, value) of every tenant-keyed value in a moved row."""
        found = []
        nested = []
        for tbl in sorted(MOVED_TABLES & self._tenant_tables()):
            types = dict(
                q(
                    self.cur,
                    "select column_name, data_type from information_schema.columns "
                    "where table_schema='public' and table_name=%s",
                    (tbl,),
                )
            )
            for col, dt in types.items():
                if dt in ("text", "character varying"):
                    for rid, val in q(
                        self.cur,
                        f"select x.id, x.{ident(col)} from {ident(tbl)} x join mv_rows m on m.tbl = %s "
                        f"and m.id = x.id where x.{ident(col)} like %s",
                        (tbl, TENANT_SECRET_PREFIX + "%"),
                    ):
                        found.append((tbl, col, rid, val))
                elif dt in ("json", "jsonb"):
                    n = q(
                        self.cur,
                        f"select count(*) from {ident(tbl)} x join mv_rows m on m.tbl = %s and m.id = x.id "
                        f"where x.{ident(col)}::text like %s",
                        (tbl, '%"' + TENANT_SECRET_PREFIX + "%"),
                    )[0][0]
                    if n:
                        nested.append(f"{tbl}.{col} ({n})")
        if nested:
            raise MoveError("Tenant-encrypted values inside JSON, not handled: " + ", ".join(nested))
        return found

    def rekey_secrets(self, rekeyer: Rekeyer | None):
        secrets = self.find_secrets()
        summary: dict[str, int] = {}
        for tbl, col, _, _ in secrets:
            summary[f"{tbl}.{col}"] = summary.get(f"{tbl}.{col}", 0) + 1
        if secrets and rekeyer is None and self.dry_run:
            # A dry run is rolled back anyway: say what --apply will need, carry on checking.
            self.report["re_encrypt_needs_key"] = summary
            return
        if secrets and rekeyer is None:
            raise MoveError(
                "These moved values are encrypted with the source workspace's key and would be "
                f"unreadable in the target: {summary}. Pass --master-key-file (the server's "
                "SECRET_ENCRYPTION_KEY) to re-encrypt them."
            )
        for tbl, col, rid, val in secrets:
            self.cur.execute(
                f"update {ident(tbl)} set {ident(col)} = %s where id = %s",
                (rekeyer.rekey(val, self.src, self.tgt), rid),
            )
        self.report["re_encrypted"] = summary

    def copy_addresses(self):
        c = self.cur
        c.execute(
            """
            insert into punch_addresses (id, tenant_id, lat_key, lng_key, address, created_at, updated_at)
            select gen_random_uuid(), %(t)s, a.lat_key, a.lng_key, a.address, a.created_at, now()
            from punch_addresses a
            where a.tenant_id = %(s)s and (a.lat_key, a.lng_key) in (
              select round(p.latitude::numeric, 4), round(p.longitude::numeric, 4) from attendance_punches p
              join mv_rows m on m.tbl = 'attendance_punches' and m.id = p.id where p.latitude is not null)
            and not exists (select 1 from punch_addresses b where b.tenant_id = %(t)s
              and b.lat_key = a.lat_key and b.lng_key = a.lng_key)
            """,
            {"s": self.src, "t": self.tgt},
        )
        self.report["copied"]["punch_addresses"] = c.rowcount

    def copy_audit(self):
        c = self.cur
        cols = self.cols("audit_logs")
        exprs = []
        for col in cols:
            if col == "id":
                exprs.append("gen_random_uuid()")
            elif col == "tenant_id":
                exprs.append("%(t)s")
            elif col == "actor_user_id":
                exprs.append("(case when x.actor_user_id = any(%(people)s) then x.actor_user_id else null end)")
            else:
                exprs.append(f"x.{ident(col)}")
        c.execute(
            f"insert into audit_logs ({', '.join(ident(k) for k in cols)}) select {', '.join(exprs)} "
            f"from audit_logs x where x.tenant_id = %(s)s and x.entity_id in (select id from mv_rows)",
            {"s": self.src, "t": self.tgt, "people": list(self.people)},
        )
        self.report["copied"]["audit_logs"] = c.rowcount

    def summary_audit(self):
        c = self.cur
        n_emp = len(self.employees)
        n_usr = len(self.users)
        changes = psycopg2.extras.Json(
            {
                "move": "people between workspaces",
                "from": self.src_name,
                "to": self.tgt_name,
                "domain": self.domain,
                "employees": n_emp,
                "logins": n_usr,
                "moved": self.report["moved"],
                "copied": self.report["copied"],
                "links_cleared": self.report["links_cleared"],
            }
        )
        for tid, text in (
            (self.src, f"{n_emp} employee records and {n_usr} logins (@{self.domain}) moved to {self.tgt_name} with their attendance, leave, salaries and payslips."),
            (self.tgt, f"{n_emp} employee records and {n_usr} logins (@{self.domain}) moved in from {self.src_name} with their attendance, leave, salaries and payslips; settings copied."),
        ):
            c.execute(
                "insert into audit_logs (id, tenant_id, actor_type, action, entity_type, entity_id, changes, summary, created_at, updated_at) "
                "values (gen_random_uuid(), %s, 'system', 'UPDATE', 'tenant', %s, %s, %s, now(), now())",
                (tid, tid, changes, text),
            )

    def leftovers(self) -> list[str]:
        """Rows of the moved people (or of their moved rows) still in the source.

        Also run after the commit: a punch or notification written for a moved
        person while the move ran would land in the source.
        """
        c = self.cur
        problems = []
        for tbl in sorted(MOVED_TABLES & self._tenant_tables()):
            if tbl in CHILDREN:
                col, parent = CHILDREN[tbl]
                ids = self.moved_ids.get(parent, [])
            else:
                col = BY_EMPLOYEE.get(tbl) or BY_USER.get(tbl)
                ids = self.employees if tbl in BY_EMPLOYEE else self.users
            if not ids:
                continue
            n = q(c, f"select count(*) from {ident(tbl)} where tenant_id = %s and {ident(col)} = any(%s)", (self.src, ids))[0][0]
            if n:
                problems.append(f"{tbl}: {n} rows of moved people are in the source")
        return problems

    def verify(self, before_src: dict[str, int]):
        c = self.cur
        problems = []
        # Every moved row is in the target.
        for tbl, n in self.report["moved"].items():
            there = q(
                c,
                f"select count(*) from {ident(tbl)} where tenant_id = %s and id in (select id from mv_rows where tbl = %s)",
                (self.tgt, tbl),
            )[0][0]
            if there != n:
                problems.append(f"{tbl}: {there} of {n} moved rows are in the target")
        # No new row in either workspace points across (the super admin's links
        # to a workspace it set up were there before and stay).
        for key, n in self.cross_links().items():
            if n > self.cross_before.get(key, 0):
                problems.append(f"{key}: {n} rows point to another workspace (was {self.cross_before.get(key, 0)})")
        # The source lost only what moved. Other people keep working meanwhile,
        # so its tables may have grown; they must never have lost more.
        after = self.counts(self.src)
        for tbl in sorted(set(before_src) | set(after)):
            lost = before_src.get(tbl, 0) - after.get(tbl, 0)
            expected = self.report["moved"].get(tbl, 0) - (1 if tbl == "audit_logs" else 0)
            if lost > expected:
                problems.append(f"{tbl}: the source lost {lost} rows, only {expected} moved")
        # Run totals equal their payslips.
        bad = q(
            c,
            """
            select r.id from payroll_runs r left join (select run_id, count(*) n, sum(net_pay) net from payslips group by run_id) s
              on s.run_id = r.id where r.tenant_id in (%s, %s) and r.id in (select old from mv_map where tbl='payroll_runs'
              union select new from mv_map where tbl='payroll_runs') and (r.employee_count <> s.n or r.total_net <> s.net)
            """,
            (self.src, self.tgt),
        )
        if bad:
            problems.append(f"{len(bad)} runs whose totals do not match their payslips")
        # Last, right before the commit: nothing was written for the moved people meanwhile.
        problems += self.leftovers()
        if problems:
            raise MoveError("Verification failed:\n  " + "\n  ".join(problems))

    def cross_links(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for tbl, links in self.fks.items():
            if tbl == "audit_logs" or tbl not in self._tenant_tables():
                continue
            for col, target, _ in links:
                if target not in self._tenant_tables():
                    continue
                n = q(
                    self.cur,
                    f"select count(*) from {ident(tbl)} x join {ident(target)} y on y.id = x.{ident(col)} "
                    f"where x.tenant_id in (%s, %s) and y.tenant_id <> x.tenant_id",
                    (self.src, self.tgt),
                )[0][0]
                if n:
                    out[f"{tbl}.{col} -> {target}"] = n
        return out

    def counts(self, tenant) -> dict[str, int]:
        tables = sorted(self._tenant_tables())
        parts = [f"select '{t}', (select count(*) from {ident(t)} where tenant_id = %(t)s)" for t in tables]
        return {r[0]: r[1] for r in q(self.cur, " union all ".join(parts), {"t": tenant}) if r[1]}

    def run(self, rekeyer: Rekeyer | None = None, paused: list | None = None):
        before_src = self.counts(self.src)
        before_tgt = self.counts(self.tgt)
        self.cross_before = self.cross_links()
        self.select_people()
        self.collect_rows()
        self.check_classified()
        self.rekey_secrets(rekeyer)
        self.build_map()
        self.copy_settings()
        self.copy_runs()
        self.move_rows()
        if paused:
            # Logins paused for the move open again, in the target, as the move commits.
            self.cur.execute(
                "update users set is_active = true where id = any(%s) and tenant_id = %s", (paused, self.tgt)
            )
            if self.cur.rowcount != len(paused):
                raise MoveError(f"Re-enabled {self.cur.rowcount} of {len(paused)} paused logins.")
            self.report["logins_paused_during_move"] = len(paused)
        self.recompute_runs()
        self.copy_addresses()
        self.copy_audit()
        self.summary_audit()
        self.verify(before_src)
        after_tgt = self.counts(self.tgt)
        self.report["target_rows"] = {t: (before_tgt.get(t, 0), n) for t, n in after_tgt.items()}
        self.report["people"] = [
            dict(zip(("code", "name", "work_email", "status", "login", "role"), r))
            for r in q(
                self.cur,
                "select e.employee_code, trim(e.first_name || ' ' || coalesce(e.last_name, '')), e.work_email, "
                "e.status::text, u.email, u.role::text from employees e left join users u on u.employee_id = e.id "
                "where e.id = any(%s) order by e.employee_code",
                (self.employees,),
            )
        ]
        return self.report


def _audit(cur, tenant_id, changes: dict, summary: str) -> None:
    cur.execute(
        "insert into audit_logs (id, tenant_id, actor_type, action, entity_type, entity_id, changes, summary, "
        "created_at, updated_at) values (gen_random_uuid(), %s, 'system', 'UPDATE', 'tenant', %s, %s, %s, now(), now())",
        (tenant_id, tenant_id, psycopg2.extras.Json(changes), summary),
    )


def pause_logins(url: str, args, keep: list[str]) -> tuple[list, list, object]:
    """Pause the moving logins, in a transaction of its own, so nothing is written
    for them while the move runs (every request re-reads the login and refuses an
    inactive one). The move re-enables them in the target as it commits."""
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            m = Mover(cur, args.source, args.target, args.domain, keep=keep, history_company=args.history_company)
            m.select_people()
            ids = [
                r[0]
                for r in q(
                    cur,
                    "update users set is_active = false, updated_at = now() where id = any(%s) and is_active returning id",
                    (m.users,),
                )
            ]
            emails = [r[0] for r in q(cur, "select email from users where id = any(%s) order by email", (ids,))] if ids else []
            _audit(cur, m.src, {"paused_logins": emails}, f"{len(ids)} logins paused for a minute while they move to {m.tgt_name}.")
        conn.commit()
        return ids, emails, m.src
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def resume_logins(url: str, ids: list, tenant_id) -> int:
    """Re-enable paused logins after a move that did not complete."""
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("update users set is_active = true, updated_at = now() where id = any(%s) and not is_active", (ids,))
            n = cur.rowcount
            _audit(cur, tenant_id, {"re_enabled_logins": n}, f"{n} logins re-enabled: the move did not complete.")
        conn.commit()
        return n
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", required=True, help="postgresql:// URL of the database to change")
    ap.add_argument(
        "--production",
        action="store_true",
        help="required when --db-url is not a database on this machine",
    )
    ap.add_argument(
        "--keep",
        action="append",
        default=[],
        help="employee code or email to leave in the source (repeat, or comma-separate)",
    )
    ap.add_argument(
        "--history-company",
        choices=("target", "source"),
        help="company on moved payslips of past runs: the target's own, or a copy of the one that ran them",
    )
    ap.add_argument("--no-pause", action="store_true", help="with --apply: do not pause the moving logins")
    ap.add_argument("--drain", type=int, default=10, help="seconds to let requests in flight finish after pausing")
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", help="write the JSON report here")
    ap.add_argument(
        "--master-key-file",
        help="file holding the server's SECRET_ENCRYPTION_KEY, to re-encrypt tenant-keyed values",
    )
    args = ap.parse_args()
    rekeyer = None
    if args.master_key_file:
        # A bare key, or an env file with a SECRET_ENCRYPTION_KEY= line (e.g. backend/.env).
        text = Path(args.master_key_file).read_text(encoding="utf-8")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        named = [ln for ln in lines if ln.startswith("SECRET_ENCRYPTION_KEY=")]
        if named:
            key = named[-1].split("=", 1)[1].strip().strip('"').strip("'")
        elif len(lines) == 1 and "=" not in lines[0]:
            key = lines[0]
        else:
            raise SystemExit("--master-key-file: no SECRET_ENCRYPTION_KEY= line and not a single bare key.")
        if not key:
            raise SystemExit("--master-key-file: the key is empty.")
        rekeyer = Rekeyer(key)
    url = args.db_url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    host = urlparse(url).hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1") and not args.production:
        raise SystemExit(f"Refusing: {host} is not a database on this machine. Add --production to run on it.")
    keep = [k.strip() for value in args.keep for k in value.split(",") if k.strip()]
    paused: list = []
    src_id = None
    if args.apply and not args.no_pause:
        paused, paused_emails, src_id = pause_logins(url, args, keep)
        print(
            f"Paused {len(paused)} logins for the move ({', '.join(paused_emails)}); "
            f"waiting {args.drain} s for requests in flight.",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(args.drain)
    conn = psycopg2.connect(url)
    conn.autocommit = False
    mover = None
    try:
        with conn.cursor() as cur:
            cur.execute("set local lock_timeout = '20s'")
            mover = Mover(
                cur,
                args.source,
                args.target,
                args.domain,
                keep=keep,
                history_company=args.history_company,
                dry_run=not args.apply,
            )
            report = mover.run(rekeyer, paused)
        if args.apply:
            conn.commit()
            report["result"] = "COMMITTED"
        else:
            conn.rollback()
            report["result"] = "DRY RUN - rolled back"
    except BaseException:
        conn.rollback()
        if paused:
            n = resume_logins(url, paused, src_id)
            print(f"Re-enabled {n} paused logins; nothing was moved.", file=sys.stderr, flush=True)
        raise
    finally:
        conn.close()
    if args.apply:
        # After the commit: was anything written for the moved people while it ran?
        check = psycopg2.connect(url)
        try:
            check.set_session(readonly=True)
            with check.cursor() as cur:
                mover.cur = cur
                found = mover.leftovers() + [
                    f"{k}: {n} rows point to another workspace (was {mover.cross_before.get(k, 0)})"
                    for k, n in mover.cross_links().items()
                    if n > mover.cross_before.get(k, 0)
                ]
            report["after_commit_check"] = found or "clean"
        finally:
            check.close()
    out = json.dumps(report, indent=2, default=str)
    if args.report:
        Path(args.report).write_text(out, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
