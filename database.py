import hashlib
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime

DB_PATH = "ratecon.db"


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatchers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT,
                password_hash TEXT,
                token TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(dispatchers)")}
        for col, defn in [
            ("email", "TEXT"),
            ("password_hash", "TEXT"),
            ("role", "TEXT NOT NULL DEFAULT 'user'"),
            ("is_active", "INTEGER NOT NULL DEFAULT 1"),
            ("company_id", "INTEGER REFERENCES companies(id) ON DELETE SET NULL"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE dispatchers ADD COLUMN {col} {defn}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS driver_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                eld_driver_id TEXT,
                UNIQUE(chat_id)
            )
        """)
        dg_existing = {row[1] for row in conn.execute("PRAGMA table_info(driver_groups)")}
        if "eld_driver_id" not in dg_existing:
            conn.execute("ALTER TABLE driver_groups ADD COLUMN eld_driver_id TEXT")
        if "eld_driver_name" not in dg_existing:
            conn.execute("ALTER TABLE driver_groups ADD COLUMN eld_driver_name TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS eld_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                api_key TEXT NOT NULL,
                company TEXT,
                provider_token TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        eld_existing = {row[1] for row in conn.execute("PRAGMA table_info(eld_configs)")}
        if "company" not in eld_existing:
            conn.execute("ALTER TABLE eld_configs ADD COLUMN company TEXT")
        if "provider_token" not in eld_existing:
            conn.execute("ALTER TABLE eld_configs ADD COLUMN provider_token TEXT")

        # Migrate eld_configs to multi-connection (no UNIQUE constraints on dispatcher/provider)
        eld_table_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='eld_configs'"
        ).fetchone()
        eld_table_sql = (eld_table_row[0] or "").upper() if eld_table_row else ""
        has_old_unique = (
            "DISPATCHER_ID INTEGER NOT NULL UNIQUE" in eld_table_sql
            or "UNIQUE(DISPATCHER_ID" in eld_table_sql
        )
        if has_old_unique:
            conn.execute("""CREATE TABLE IF NOT EXISTS eld_configs_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                api_key TEXT NOT NULL,
                company TEXT,
                provider_token TEXT,
                updated_at TEXT NOT NULL
            )""")
            conn.execute("""
                INSERT INTO eld_configs_new (id, dispatcher_id, provider, api_key, company, provider_token, updated_at)
                SELECT id, dispatcher_id, provider, api_key, company, provider_token, updated_at
                FROM eld_configs
            """)
            conn.execute("DROP TABLE eld_configs")
            conn.execute("ALTER TABLE eld_configs_new RENAME TO eld_configs")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS loads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                group_id INTEGER REFERENCES driver_groups(id) ON DELETE SET NULL,
                load_number TEXT,
                status TEXT NOT NULL DEFAULT 'upcoming',
                origin_state TEXT,
                destination_state TEXT,
                total_rate_usd TEXT,
                miles TEXT,
                pickup_address TEXT,
                pickup_date TEXT,
                delivery_address TEXT,
                delivery_date TEXT,
                stops_json TEXT,
                current_stop_index INTEGER NOT NULL DEFAULT 0,
                eta_utc TEXT,
                eta_miles REAL,
                last_eta_update TEXT,
                created_at TEXT NOT NULL
            )
        """)
        loads_existing = {row[1] for row in conn.execute("PRAGMA table_info(loads)")}
        if "stops_json" not in loads_existing:
            conn.execute("ALTER TABLE loads ADD COLUMN stops_json TEXT")
        if "current_stop_index" not in loads_existing:
            conn.execute("ALTER TABLE loads ADD COLUMN current_stop_index INTEGER NOT NULL DEFAULT 0")
        if "file_path" not in loads_existing:
            conn.execute("ALTER TABLE loads ADD COLUMN file_path TEXT")
        if "auto_send_hours" not in loads_existing:
            conn.execute("ALTER TABLE loads ADD COLUMN auto_send_hours INTEGER DEFAULT 0")
        if "last_auto_send" not in loads_existing:
            conn.execute("ALTER TABLE loads ADD COLUMN last_auto_send TEXT")
        if "eta_alert_sent" not in loads_existing:
            conn.execute("ALTER TABLE loads ADD COLUMN eta_alert_sent INTEGER DEFAULT 0")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_info (
                dispatcher_id INTEGER PRIMARY KEY REFERENCES dispatchers(id) ON DELETE CASCADE,
                company_name  TEXT,
                address       TEXT,
                phone         TEXT,
                email         TEXT,
                mc_number     TEXT,
                dot_number    TEXT,
                bank_name     TEXT,
                bank_account  TEXT,
                bank_routing  TEXT,
                payment_terms TEXT NOT NULL DEFAULT 'Net 30',
                updated_at    TEXT NOT NULL
            )
        """)

        ci_cols = {row[1] for row in conn.execute("PRAGMA table_info(company_info)")}
        if "google_maps_key" not in ci_cols:
            conn.execute("ALTER TABLE company_info ADD COLUMN google_maps_key TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS global_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS load_pods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                load_id INTEGER NOT NULL REFERENCES loads(id) ON DELETE CASCADE,
                file_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pay_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                dispatcher_id INTEGER REFERENCES dispatchers(id) ON DELETE CASCADE,
                min_gross REAL NOT NULL DEFAULT 0,
                max_gross REAL,
                min_rpm REAL NOT NULL DEFAULT 0,
                percentage REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

    super_email = os.environ.get("SUPER_ADMIN_EMAIL", "").strip().lower()
    super_pass = os.environ.get("SUPER_ADMIN_PASSWORD", "").strip()
    if super_email and super_pass:
        ensure_superadmin(super_email, super_pass)


def ensure_superadmin(email: str, password: str):
    """Create or sync the superadmin account from env vars on every startup."""
    ph = hash_password(password)
    token = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM dispatchers WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE dispatchers SET role = 'superadmin', password_hash = ?, is_active = 1 WHERE email = ?",
                (ph, email),
            )
        else:
            conn.execute(
                "INSERT INTO dispatchers (name, email, password_hash, token, role, is_active, created_at) VALUES (?, ?, ?, ?, 'superadmin', 1, ?)",
                ("Super Admin", email, ph, token, now),
            )


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Password ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}:{h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        h2 = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return secrets.compare_digest(h2.hex(), h)
    except Exception:
        return False


# ── Dispatchers ───────────────────────────────────────────────────────────────

def count_dispatchers() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM dispatchers").fetchone()[0]


def create_dispatcher(name: str, email: str, password: str) -> dict:
    token = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    ph = hash_password(password)
    role = "admin" if count_dispatchers() == 0 else "user"
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO dispatchers (name, email, password_hash, token, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, email.lower().strip(), ph, token, role, now),
        )
        return {"id": cur.lastrowid, "name": name, "email": email, "token": token, "role": role}


def create_company_user(company_id: int, name: str, email: str, password: str, role: str = "user") -> dict:
    token = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    ph = hash_password(password)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO dispatchers (name, email, password_hash, token, role, company_id, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (name, email.lower().strip(), ph, token, role, company_id, now),
        )
        return {"id": cur.lastrowid, "name": name, "email": email, "token": token, "role": role, "company_id": company_id}


def get_dispatcher_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM dispatchers WHERE email = ? AND is_active = 1", (email.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None


def get_dispatcher_by_token(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM dispatchers WHERE token = ? AND is_active = 1", (token,)
        ).fetchone()
        return dict(row) if row else None


def get_all_dispatchers() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.id, d.name, d.email, d.role, d.is_active, d.created_at, d.company_id,
                      c.name as company_name,
                      COUNT(DISTINCT dg.id) as driver_count,
                      COUNT(DISTINCT l.id) as load_count
               FROM dispatchers d
               LEFT JOIN companies c ON d.company_id = c.id
               LEFT JOIN driver_groups dg ON dg.dispatcher_id = d.id
               LEFT JOIN loads l ON l.dispatcher_id = d.id
               GROUP BY d.id
               ORDER BY d.created_at"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_dispatcher_in_company(dispatcher_id: int, company_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM dispatchers WHERE id = ? AND company_id = ?",
            (dispatcher_id, company_id),
        ).fetchone()
        return dict(row) if row else None


def get_company_dispatchers(company_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.id, d.name, d.email, d.role, d.is_active, d.created_at,
                      COUNT(DISTINCT dg.id) as driver_count,
                      COUNT(DISTINCT l.id) as load_count
               FROM dispatchers d
               LEFT JOIN driver_groups dg ON dg.dispatcher_id = d.id
               LEFT JOIN loads l ON l.dispatcher_id = d.id
               WHERE d.company_id = ?
               GROUP BY d.id
               ORDER BY d.role DESC, d.created_at""",
            (company_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_dispatcher(dispatcher_id: int, **fields) -> bool:
    allowed = {"role", "is_active", "name"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    setters = ", ".join(f"{k} = ?" for k in updates)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE dispatchers SET {setters} WHERE id = ?",
            (*updates.values(), dispatcher_id),
        )
        return cur.rowcount > 0


def update_password(dispatcher_id: int, new_password: str) -> bool:
    ph = hash_password(new_password)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE dispatchers SET password_hash = ? WHERE id = ?", (ph, dispatcher_id)
        )
        return cur.rowcount > 0


def delete_dispatcher(dispatcher_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM dispatchers WHERE id = ?", (dispatcher_id,))
        return cur.rowcount > 0


# ── ELD configs ───────────────────────────────────────────────────────────────

def get_eld_configs(dispatcher_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM eld_configs WHERE dispatcher_id = ?", (dispatcher_id,)
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
        # fallback to company admin's configs
        rows = conn.execute(
            """SELECT ec.* FROM eld_configs ec
               JOIN dispatchers d ON d.id = ec.dispatcher_id
               WHERE d.company_id = (SELECT company_id FROM dispatchers WHERE id = ?)
               AND d.company_id IS NOT NULL""",
            (dispatcher_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_eld_config(dispatcher_id: int) -> dict | None:
    configs = get_eld_configs(dispatcher_id)
    return configs[0] if configs else None


def save_eld_config(dispatcher_id: int, provider: str, api_key: str, company: str | None = None, provider_token: str | None = None) -> dict:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO eld_configs (dispatcher_id, provider, api_key, company, provider_token, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (dispatcher_id, provider, api_key, company, provider_token, now),
        )
        return {"id": cur.lastrowid, "dispatcher_id": dispatcher_id, "provider": provider}


def delete_eld_config(dispatcher_id: int, provider: str | None = None, config_id: int | None = None) -> bool:
    if config_id is None and not provider:
        return False
    with get_conn() as conn:
        if config_id is not None:
            cur = conn.execute(
                "DELETE FROM eld_configs WHERE dispatcher_id = ? AND id = ?",
                (dispatcher_id, config_id),
            )
        elif provider:
            cur = conn.execute(
                "DELETE FROM eld_configs WHERE dispatcher_id = ? AND provider = ?",
                (dispatcher_id, provider),
            )
        else:
            cur = conn.execute(
                "DELETE FROM eld_configs WHERE dispatcher_id = ?", (dispatcher_id,)
            )
        return cur.rowcount > 0


def set_group_driver(group_id: int, company_id: int, eld_driver_id: str | None, eld_driver_name: str | None = None) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE driver_groups SET eld_driver_id = ?, eld_driver_name = ? WHERE id = ?
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)""",
            (eld_driver_id, eld_driver_name, group_id, company_id),
        )
        return cur.rowcount > 0


# ── Driver groups ─────────────────────────────────────────────────────────────

def link_group(dispatcher_id: int, chat_id: int, name: str) -> dict:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM driver_groups WHERE chat_id = ? AND dispatcher_id = ?",
            (chat_id, dispatcher_id),
        ).fetchone()
        if existing:
            return dict(existing)

        any_existing = conn.execute(
            "SELECT * FROM driver_groups WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if any_existing:
            conn.execute(
                "UPDATE driver_groups SET dispatcher_id = ?, name = ? WHERE chat_id = ?",
                (dispatcher_id, name, chat_id),
            )
            row = conn.execute(
                "SELECT * FROM driver_groups WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return dict(row)

        cur = conn.execute(
            "INSERT INTO driver_groups (dispatcher_id, chat_id, name) VALUES (?, ?, ?)",
            (dispatcher_id, chat_id, name),
        )
        return {"id": cur.lastrowid, "dispatcher_id": dispatcher_id, "chat_id": chat_id, "name": name}


def get_groups(company_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT dg.* FROM driver_groups dg
               JOIN dispatchers d ON dg.dispatcher_id = d.id
               WHERE d.company_id = ?""",
            (company_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_group(group_id: int, company_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT dg.* FROM driver_groups dg
               JOIN dispatchers d ON dg.dispatcher_id = d.id
               WHERE dg.id = ? AND d.company_id = ?""",
            (group_id, company_id),
        ).fetchone()
        return dict(row) if row else None


def rename_group(group_id: int, company_id: int, name: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE driver_groups SET name = ? WHERE id = ?
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)""",
            (name, group_id, company_id),
        )
        return cur.rowcount > 0


def delete_group(group_id: int, company_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """DELETE FROM driver_groups WHERE id = ?
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)""",
            (group_id, company_id),
        )
        return cur.rowcount > 0


# ── Loads ─────────────────────────────────────────────────────────────────────

def create_load(dispatcher_id: int, data: dict) -> dict:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO loads
               (dispatcher_id, group_id, load_number, status,
                origin_state, destination_state, total_rate_usd, miles,
                pickup_address, pickup_date, delivery_address, delivery_date,
                stops_json, current_stop_index, created_at)
               VALUES (?, ?, ?, 'upcoming', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                dispatcher_id,
                data.get("group_id"),
                data.get("load_number"),
                data.get("origin_state"),
                data.get("destination_state"),
                data.get("total_rate_usd"),
                data.get("miles"),
                data.get("pickup_address"),
                data.get("pickup_date"),
                data.get("delivery_address"),
                data.get("delivery_date"),
                data.get("stops_json"),
                now,
            ),
        )
        return get_load_by_id(cur.lastrowid)


def get_load(load_id: int, company_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE l.id = ? AND d.company_id = ?""",
            (load_id, company_id),
        ).fetchone()
        return dict(row) if row else None


def get_load_by_id(load_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE l.id = ?""",
            (load_id,),
        ).fetchone()
        return dict(row) if row else None


def get_loads(company_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE d.company_id = ?
               ORDER BY l.created_at DESC""",
            (company_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_load_status(load_id: int, company_id: int, status: str, current_stop_index: int | None = None) -> bool:
    with get_conn() as conn:
        if current_stop_index is not None:
            cur = conn.execute(
                """UPDATE loads SET status = ?, current_stop_index = ? WHERE id = ?
                   AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)""",
                (status, current_stop_index, load_id, company_id),
            )
        else:
            cur = conn.execute(
                """UPDATE loads SET status = ? WHERE id = ?
                   AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)""",
                (status, load_id, company_id),
            )
        return cur.rowcount > 0


def update_load_stop_index(load_id: int, company_id: int, index: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE loads SET current_stop_index = ? WHERE id = ?
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)""",
            (index, load_id, company_id),
        )
        return cur.rowcount > 0


def update_load_eta(load_id: int, eta_utc: str, eta_miles: float) -> bool:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE loads SET eta_utc = ?, eta_miles = ?, last_eta_update = ?, eta_alert_sent = 0 WHERE id = ?",
            (eta_utc, eta_miles, now, load_id),
        )
        return cur.rowcount > 0


def delete_load(load_id: int, company_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """DELETE FROM loads WHERE id = ?
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)""",
            (load_id, company_id),
        )
        return cur.rowcount > 0


def update_load_file(load_id: int, file_path: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE loads SET file_path = ? WHERE id = ?",
            (file_path, load_id),
        )
        return cur.rowcount > 0


def update_load_auto_send(load_id: int, company_id: int, hours: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE loads SET auto_send_hours = ? WHERE id = ?
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = ?)""",
            (hours, load_id, company_id),
        )
        return cur.rowcount > 0


def update_last_auto_send(load_id: int) -> bool:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE loads SET last_auto_send = ? WHERE id = ?",
            (now, load_id),
        )
        return cur.rowcount > 0


def set_eta_alert_sent(load_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE loads SET eta_alert_sent = 1 WHERE id = ?",
            (load_id,),
        )
        return cur.rowcount > 0


def get_active_load_by_chat_id(chat_id: int) -> dict | None:
    """Returns the most recent dispatched load for a given Telegram chat_id."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      dg.chat_id as group_chat_id, d.name as dispatcher_name, d.company_id
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE dg.chat_id = ? AND l.status = 'dispatched'
               ORDER BY l.created_at DESC
               LIMIT 1""",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def get_upcoming_load_by_chat_id(chat_id: int) -> dict | None:
    """Returns the most recent upcoming load for a given Telegram chat_id."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      dg.chat_id as group_chat_id, d.name as dispatcher_name, d.company_id
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE dg.chat_id = ? AND l.status = 'upcoming'
               ORDER BY l.created_at DESC
               LIMIT 1""",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def get_dispatched_loads_for_alerts() -> list[dict]:
    """Returns dispatched loads with ETA set that haven't had an alert sent yet."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.chat_id as group_chat_id
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               WHERE l.status = 'dispatched'
                 AND l.eta_utc IS NOT NULL
                 AND l.eta_alert_sent = 0
                 AND dg.chat_id IS NOT NULL"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_company_info(dispatcher_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM company_info WHERE dispatcher_id = ?", (dispatcher_id,)
        ).fetchone()
        return dict(row) if row else None


def save_company_info(dispatcher_id: int, data: dict) -> None:
    now = datetime.utcnow().isoformat()
    fields = ["company_name", "address", "phone", "email", "mc_number",
              "dot_number", "bank_name", "bank_account", "bank_routing", "payment_terms"]
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO company_info (dispatcher_id, {', '.join(fields)}, updated_at)
                VALUES (?, {', '.join('?' for _ in fields)}, ?)
                ON CONFLICT(dispatcher_id) DO UPDATE SET
                {', '.join(f'{f} = excluded.{f}' for f in fields)},
                updated_at = excluded.updated_at""",
            (dispatcher_id, *[data.get(f) for f in fields], now),
        )


def get_global_setting(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM global_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_global_setting(key: str, value: str) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO global_settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, now),
        )


def get_google_maps_key(dispatcher_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT google_maps_key FROM company_info WHERE dispatcher_id = ?", (dispatcher_id,)
        ).fetchone()
        return row["google_maps_key"] if row else None


def save_google_maps_key(dispatcher_id: int, key: str) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO company_info (dispatcher_id, google_maps_key, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(dispatcher_id) DO UPDATE SET
               google_maps_key = excluded.google_maps_key,
               updated_at = excluded.updated_at""",
            (dispatcher_id, key, now),
        )


def add_load_pod(load_id: int, file_path: str, filename: str) -> dict:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO load_pods (load_id, file_path, filename, uploaded_at) VALUES (?, ?, ?, ?)",
            (load_id, file_path, filename, now),
        )
        return {"id": cur.lastrowid, "load_id": load_id, "filename": filename, "uploaded_at": now}


def get_load_pods(load_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM load_pods WHERE load_id = ? ORDER BY uploaded_at",
            (load_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_load_pod(pod_id: int, load_id: int) -> str | None:
    """Returns file_path if deleted, else None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT file_path FROM load_pods WHERE id = ? AND load_id = ?", (pod_id, load_id)
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM load_pods WHERE id = ?", (pod_id,))
        return row["file_path"]


# ── Companies ─────────────────────────────────────────────────────────────────

def create_company(name: str) -> dict:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO companies (name, created_at) VALUES (?, ?)", (name, now))
        return {"id": cur.lastrowid, "name": name, "created_at": now}


def get_all_companies() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.created_at,
                      (SELECT d2.name FROM dispatchers d2 WHERE d2.company_id = c.id AND d2.role = 'admin' LIMIT 1) as admin_name,
                      (SELECT d2.email FROM dispatchers d2 WHERE d2.company_id = c.id AND d2.role = 'admin' LIMIT 1) as admin_email,
                      COUNT(DISTINCT CASE WHEN d.role = 'user' THEN d.id END) as user_count,
                      COUNT(DISTINCT l.id) as load_count,
                      COALESCE(SUM(CASE WHEN l.status='delivered' THEN CAST(l.total_rate_usd AS REAL) ELSE 0 END), 0) as revenue
               FROM companies c
               LEFT JOIN dispatchers d ON d.company_id = c.id
               LEFT JOIN loads l ON l.dispatcher_id = d.id
               GROUP BY c.id
               ORDER BY c.created_at"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_company(company_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        return dict(row) if row else None


def update_company(company_id: int, name: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE companies SET name = ? WHERE id = ?", (name, company_id))
        return cur.rowcount > 0


def delete_company(company_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        return cur.rowcount > 0


def get_admin_stats() -> dict:
    """Global stats for superadmin panel."""
    with get_conn() as conn:
        total_companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        total_admins = conn.execute(
            "SELECT COUNT(*) FROM dispatchers WHERE role = 'admin'"
        ).fetchone()[0]
        total_users = conn.execute(
            "SELECT COUNT(*) FROM dispatchers WHERE role = 'user'"
        ).fetchone()[0]
        total_drivers = conn.execute("SELECT COUNT(*) FROM driver_groups").fetchone()[0]
        total_loads = conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0]
        total_revenue = conn.execute(
            "SELECT SUM(CAST(total_rate_usd AS REAL)) FROM loads WHERE status = 'delivered'"
        ).fetchone()[0] or 0
        dispatched = conn.execute(
            "SELECT COUNT(*) FROM loads WHERE status = 'dispatched'"
        ).fetchone()[0]
        delivered = conn.execute(
            "SELECT COUNT(*) FROM loads WHERE status = 'delivered'"
        ).fetchone()[0]
    return {
        "total_companies": total_companies,
        "total_admins": total_admins,
        "total_accounts": total_users,
        "total_drivers": total_drivers,
        "total_loads": total_loads,
        "total_revenue": round(total_revenue, 2),
        "dispatched_loads": dispatched,
        "delivered_loads": delivered,
    }


def get_loads_for_auto_send() -> list[dict]:
    """Returns dispatched loads with auto-send enabled, including ELD config."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      dg.chat_id as group_chat_id,
                      ec.provider as eld_provider, ec.api_key as eld_api_key,
                      ec.company as eld_company, ec.provider_token as eld_provider_token
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN eld_configs ec ON l.dispatcher_id = ec.dispatcher_id
               WHERE l.status = 'dispatched'
                 AND l.auto_send_hours > 0
                 AND dg.chat_id IS NOT NULL
                 AND dg.eld_driver_id IS NOT NULL
                 AND ec.provider IS NOT NULL"""
        ).fetchall()
        return [dict(r) for r in rows]


# ── Pay Tiers ─────────────────────────────────────────────────────────────────

def get_pay_tiers(company_id: int) -> list[dict]:
    """Returns all tiers for a company (both company-wide and per-worker)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pt.*, d.name as dispatcher_name
               FROM pay_tiers pt
               LEFT JOIN dispatchers d ON pt.dispatcher_id = d.id
               WHERE pt.company_id = ?
               ORDER BY pt.dispatcher_id NULLS FIRST, pt.min_gross DESC""",
            (company_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_pay_tier(company_id: int, dispatcher_id: int | None,
                    min_gross: float, max_gross: float | None,
                    min_rpm: float, percentage: float) -> dict:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO pay_tiers (company_id, dispatcher_id, min_gross, max_gross, min_rpm, percentage, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (company_id, dispatcher_id, min_gross, max_gross, min_rpm, percentage, now),
        )
        return {"id": cur.lastrowid, "company_id": company_id, "dispatcher_id": dispatcher_id,
                "min_gross": min_gross, "max_gross": max_gross, "min_rpm": min_rpm,
                "percentage": percentage, "created_at": now}


def delete_pay_tier(tier_id: int, company_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM pay_tiers WHERE id = ? AND company_id = ?",
            (tier_id, company_id),
        )
        return cur.rowcount > 0


def get_earnings(company_id: int, dispatcher_id: int | None = None) -> list[dict]:
    """Calculate earnings for delivered loads using pay tiers.
    If dispatcher_id given, returns only that worker's earnings.
    """
    with get_conn() as conn:
        if dispatcher_id:
            rows = conn.execute(
                """SELECT l.*, d.name as dispatcher_name, d.id as worker_id
                   FROM loads l
                   JOIN dispatchers d ON l.dispatcher_id = d.id
                   WHERE d.company_id = ? AND l.dispatcher_id = ? AND l.status = 'delivered'
                   ORDER BY l.created_at DESC""",
                (company_id, dispatcher_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT l.*, d.name as dispatcher_name, d.id as worker_id
                   FROM loads l
                   JOIN dispatchers d ON l.dispatcher_id = d.id
                   WHERE d.company_id = ? AND l.status = 'delivered'
                   ORDER BY l.created_at DESC""",
                (company_id,),
            ).fetchall()

        tiers = get_pay_tiers(company_id)
        company_tiers = [t for t in tiers if t["dispatcher_id"] is None]

        result = []
        for row in rows:
            load = dict(row)
            worker_id = load["worker_id"]
            worker_tiers = [t for t in tiers if t["dispatcher_id"] == worker_id]
            active_tiers = worker_tiers if worker_tiers else company_tiers

            gross = float(load.get("total_rate_usd") or 0)
            miles = float(load.get("miles") or 0)
            rpm = gross / miles if miles > 0 else 0

            earning = 0.0
            matched_tier = None
            for tier in sorted(active_tiers, key=lambda t: t["min_gross"], reverse=True):
                gross_ok = gross >= tier["min_gross"]
                max_ok = tier["max_gross"] is None or gross < tier["max_gross"]
                rpm_ok = rpm >= tier["min_rpm"]
                if gross_ok and max_ok and rpm_ok:
                    earning = round(gross * tier["percentage"] / 100, 2)
                    matched_tier = tier
                    break

            load["rpm"] = round(rpm, 2)
            load["earning"] = earning
            load["tier_percentage"] = matched_tier["percentage"] if matched_tier else None
            result.append(load)

        return result
