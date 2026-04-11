import hashlib
import json
import os
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://ratecon:ratecon@localhost:5432/ratecon",
)


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatchers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                password_hash TEXT,
                token TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'user',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL,
                company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS driver_groups (
                id SERIAL PRIMARY KEY,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                chat_id BIGINT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                eld_driver_id TEXT,
                eld_driver_name TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS eld_configs (
                id SERIAL PRIMARY KEY,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                api_key TEXT NOT NULL,
                company TEXT,
                provider_token TEXT,
                updated_at TIMESTAMP NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS loads (
                id SERIAL PRIMARY KEY,
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
                eta_miles DOUBLE PRECISION,
                last_eta_update TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                file_path TEXT,
                auto_send_hours INTEGER DEFAULT 0,
                last_auto_send TIMESTAMP,
                eta_alert_sent BOOLEAN DEFAULT FALSE
            )
        """)

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
                google_maps_key TEXT,
                updated_at    TIMESTAMP NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS global_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS load_pods (
                id SERIAL PRIMARY KEY,
                load_id INTEGER NOT NULL REFERENCES loads(id) ON DELETE CASCADE,
                file_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                uploaded_at TIMESTAMP NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pay_tiers (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                dispatcher_id INTEGER REFERENCES dispatchers(id) ON DELETE CASCADE,
                min_gross DOUBLE PRECISION NOT NULL DEFAULT 0,
                max_gross DOUBLE PRECISION,
                min_rpm DOUBLE PRECISION NOT NULL DEFAULT 0,
                percentage DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id SERIAL PRIMARY KEY,
                actor_dispatcher_id INTEGER REFERENCES dispatchers(id) ON DELETE SET NULL,
                company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_company_created ON audit_logs(company_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor_created ON audit_logs(actor_dispatcher_id, created_at DESC)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS kpi_comments (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                driver_group_id INTEGER NOT NULL REFERENCES driver_groups(id) ON DELETE CASCADE,
                week_start DATE NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL,
                UNIQUE(company_id, driver_group_id, week_start)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS kpi_entries (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                week_start DATE NOT NULL,
                load_id INTEGER REFERENCES loads(id) ON DELETE SET NULL,
                miles DOUBLE PRECISION NOT NULL DEFAULT 0,
                cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL
            )
        """)

        # Migrations for new load columns
        conn.execute("ALTER TABLE loads ADD COLUMN IF NOT EXISTS broker_name TEXT")
        conn.execute("ALTER TABLE loads ADD COLUMN IF NOT EXISTS deadhead_miles TEXT")
        conn.execute("ALTER TABLE loads ADD COLUMN IF NOT EXISTS charge TEXT")

        # Migrate kpi_entries: drop old unique constraint if exists (schema changed)
        try:
            conn.execute("ALTER TABLE kpi_entries DROP CONSTRAINT IF EXISTS kpi_entries_company_id_dispatcher_id_week_start_key")
        except Exception:
            conn.connection.rollback()
        conn.execute("ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS load_id INTEGER REFERENCES loads(id) ON DELETE SET NULL")
        conn.execute("ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS miles DOUBLE PRECISION NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS cost DOUBLE PRECISION NOT NULL DEFAULT 0")

    super_email = os.environ.get("SUPER_ADMIN_EMAIL", "").strip().lower()
    super_pass = os.environ.get("SUPER_ADMIN_PASSWORD", "").strip()
    if super_email and super_pass:
        ensure_superadmin(super_email, super_pass)


def ensure_superadmin(email: str, password: str):
    """Create or sync the superadmin account from env vars on every startup."""
    ph = hash_password(password)
    token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "SELECT id FROM dispatchers WHERE email = %s", (email,)
        )
        existing = conn.fetchone()
        if existing:
            conn.execute(
                "UPDATE dispatchers SET role = 'superadmin', password_hash = %s, is_active = TRUE WHERE email = %s",
                (ph, email),
            )
        else:
            conn.execute(
                "INSERT INTO dispatchers (name, email, password_hash, token, role, is_active, created_at) VALUES (%s, %s, %s, %s, 'superadmin', TRUE, %s)",
                ("Super Admin", email, ph, token, now),
            )


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        yield cur
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
        conn.execute("SELECT COUNT(*) AS cnt FROM dispatchers")
        return conn.fetchone()["cnt"]


def create_dispatcher(name: str, email: str, password: str) -> dict:
    token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    ph = hash_password(password)
    role = "admin" if count_dispatchers() == 0 else "user"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO dispatchers (name, email, password_hash, token, role, created_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, email.lower().strip(), ph, token, role, now),
        )
        row = conn.fetchone()
        return {"id": row["id"], "name": name, "email": email, "token": token, "role": role}


def create_company_user(company_id: int, name: str, email: str, password: str, role: str = "user") -> dict:
    token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    ph = hash_password(password)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO dispatchers (name, email, password_hash, token, role, company_id, is_active, created_at) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s) RETURNING id",
            (name, email.lower().strip(), ph, token, role, company_id, now),
        )
        row = conn.fetchone()
        return {"id": row["id"], "name": name, "email": email, "token": token, "role": role, "company_id": company_id}


def get_dispatcher_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM dispatchers WHERE email = %s AND is_active = TRUE", (email.lower().strip(),)
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_dispatcher_by_token(token: str) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM dispatchers WHERE token = %s AND is_active = TRUE", (token,)
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_dispatcher_by_id(dispatcher_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM dispatchers WHERE id = %s",
            (dispatcher_id,),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_all_dispatchers() -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            """SELECT d.id, d.name, d.email, d.role, d.is_active, d.created_at, d.company_id,
                      c.name as company_name,
                      COUNT(DISTINCT dg.id) as driver_count,
                      COUNT(DISTINCT l.id) as load_count
               FROM dispatchers d
               LEFT JOIN companies c ON d.company_id = c.id
               LEFT JOIN driver_groups dg ON dg.dispatcher_id = d.id
               LEFT JOIN loads l ON l.dispatcher_id = d.id
               GROUP BY d.id, d.name, d.email, d.role, d.is_active, d.created_at, d.company_id, c.name
               ORDER BY d.created_at"""
        )
        return [dict(r) for r in conn.fetchall()]


def get_dispatcher_in_company(dispatcher_id: int, company_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM dispatchers WHERE id = %s AND company_id = %s",
            (dispatcher_id, company_id),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_company_dispatchers(company_id: int) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            """SELECT d.id, d.name, d.email, d.role, d.is_active, d.created_at,
                      COUNT(DISTINCT dg.id) as driver_count,
                      COUNT(DISTINCT l.id) as load_count
               FROM dispatchers d
               LEFT JOIN driver_groups dg ON dg.dispatcher_id = d.id
               LEFT JOIN loads l ON l.dispatcher_id = d.id
               WHERE d.company_id = %s
               GROUP BY d.id, d.name, d.email, d.role, d.is_active, d.created_at
               ORDER BY d.role DESC, d.created_at""",
            (company_id,),
        )
        return [dict(r) for r in conn.fetchall()]


def get_dispatchers_with_eld_configs() -> list[int]:
    with get_conn() as conn:
        conn.execute(
            "SELECT DISTINCT dispatcher_id FROM eld_configs ORDER BY dispatcher_id"
        )
        return [int(r["dispatcher_id"]) for r in conn.fetchall()]


def update_dispatcher(dispatcher_id: int, **fields) -> bool:
    allowed = {"role", "is_active", "name"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    setters = ", ".join(f"{k} = %s" for k in updates)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE dispatchers SET {setters} WHERE id = %s",
            (*updates.values(), dispatcher_id),
        )
        return conn.rowcount > 0


def update_password(dispatcher_id: int, new_password: str) -> bool:
    ph = hash_password(new_password)
    with get_conn() as conn:
        conn.execute(
            "UPDATE dispatchers SET password_hash = %s WHERE id = %s", (ph, dispatcher_id)
        )
        return conn.rowcount > 0


def delete_dispatcher(dispatcher_id: int) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM dispatchers WHERE id = %s", (dispatcher_id,))
        return conn.rowcount > 0


# ── ELD configs ───────────────────────────────────────────────────────────────

def get_eld_configs(dispatcher_id: int) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM eld_configs WHERE dispatcher_id = %s", (dispatcher_id,)
        )
        rows = conn.fetchall()
        if rows:
            return [dict(r) for r in rows]
        # fallback to company admin's configs
        conn.execute(
            """SELECT ec.* FROM eld_configs ec
               JOIN dispatchers d ON d.id = ec.dispatcher_id
               WHERE d.company_id = (SELECT company_id FROM dispatchers WHERE id = %s)
               AND d.company_id IS NOT NULL""",
            (dispatcher_id,),
        )
        rows = conn.fetchall()
        return [dict(r) for r in rows]


def get_eld_config(dispatcher_id: int) -> dict | None:
    configs = get_eld_configs(dispatcher_id)
    return configs[0] if configs else None


def save_eld_config(dispatcher_id: int, provider: str, api_key: str, company: str | None = None, provider_token: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO eld_configs (dispatcher_id, provider, api_key, company, provider_token, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (dispatcher_id, provider, api_key, company, provider_token, now),
        )
        row = conn.fetchone()
        return {"id": row["id"], "dispatcher_id": dispatcher_id, "provider": provider}


def delete_eld_config(dispatcher_id: int, provider: str | None = None, config_id: int | None = None) -> bool:
    if config_id is None and not provider:
        return False
    with get_conn() as conn:
        if config_id is not None:
            conn.execute(
                "DELETE FROM eld_configs WHERE dispatcher_id = %s AND id = %s",
                (dispatcher_id, config_id),
            )
        elif provider:
            conn.execute(
                "DELETE FROM eld_configs WHERE dispatcher_id = %s AND provider = %s",
                (dispatcher_id, provider),
            )
        else:
            conn.execute(
                "DELETE FROM eld_configs WHERE dispatcher_id = %s", (dispatcher_id,)
            )
        return conn.rowcount > 0


def set_group_driver(group_id: int, company_id: int, eld_driver_id: str | None, eld_driver_name: str | None = None) -> bool:
    with get_conn() as conn:
        conn.execute(
            """UPDATE driver_groups SET eld_driver_id = %s, eld_driver_name = %s WHERE id = %s
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
            (eld_driver_id, eld_driver_name, group_id, company_id),
        )
        return conn.rowcount > 0


# ── Driver groups ─────────────────────────────────────────────────────────────

def link_group(dispatcher_id: int, chat_id: int, name: str) -> dict:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM driver_groups WHERE chat_id = %s AND dispatcher_id = %s",
            (chat_id, dispatcher_id),
        )
        existing = conn.fetchone()
        if existing:
            return dict(existing)

        conn.execute(
            "SELECT * FROM driver_groups WHERE chat_id = %s", (chat_id,)
        )
        any_existing = conn.fetchone()
        if any_existing:
            conn.execute(
                "UPDATE driver_groups SET dispatcher_id = %s, name = %s WHERE chat_id = %s",
                (dispatcher_id, name, chat_id),
            )
            conn.execute(
                "SELECT * FROM driver_groups WHERE chat_id = %s", (chat_id,)
            )
            row = conn.fetchone()
            return dict(row)

        conn.execute(
            "INSERT INTO driver_groups (dispatcher_id, chat_id, name) VALUES (%s, %s, %s) RETURNING id",
            (dispatcher_id, chat_id, name),
        )
        row = conn.fetchone()
        return {"id": row["id"], "dispatcher_id": dispatcher_id, "chat_id": chat_id, "name": name}


def get_groups(company_id: int) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            """SELECT dg.* FROM driver_groups dg
               JOIN dispatchers d ON dg.dispatcher_id = d.id
               WHERE d.company_id = %s""",
            (company_id,),
        )
        return [dict(r) for r in conn.fetchall()]


def get_group(group_id: int, company_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            """SELECT dg.* FROM driver_groups dg
               JOIN dispatchers d ON dg.dispatcher_id = d.id
               WHERE dg.id = %s AND d.company_id = %s""",
            (group_id, company_id),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def rename_group(group_id: int, company_id: int, name: str) -> bool:
    with get_conn() as conn:
        conn.execute(
            """UPDATE driver_groups SET name = %s WHERE id = %s
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
            (name, group_id, company_id),
        )
        return conn.rowcount > 0


def delete_group(group_id: int, company_id: int) -> bool:
    with get_conn() as conn:
        conn.execute(
            """DELETE FROM driver_groups WHERE id = %s
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
            (group_id, company_id),
        )
        return conn.rowcount > 0


# ── Loads ─────────────────────────────────────────────────────────────────────

def create_load(dispatcher_id: int, data: dict) -> dict:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO loads
               (dispatcher_id, group_id, load_number, status,
                origin_state, destination_state, total_rate_usd, miles,
                pickup_address, pickup_date, delivery_address, delivery_date,
                stops_json, current_stop_index, deadhead_miles, broker_name, charge, created_at)
               VALUES (%s, %s, %s, 'upcoming', %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s) RETURNING id""",
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
                data.get("deadhead_miles"),
                data.get("broker_name"),
                data.get("charge"),
                now,
            ),
        )
        row = conn.fetchone()
        load_id = row["id"]
    return get_load_by_id(load_id)


def get_load(load_id: int, company_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE l.id = %s AND d.company_id = %s""",
            (load_id, company_id),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_load_by_id(load_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE l.id = %s""",
            (load_id,),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_loads(company_id: int) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE d.company_id = %s
               ORDER BY l.created_at DESC""",
            (company_id,),
        )
        return [dict(r) for r in conn.fetchall()]


def update_load_status(load_id: int, company_id: int, status: str, current_stop_index: int | None = None) -> bool:
    with get_conn() as conn:
        if current_stop_index is not None:
            conn.execute(
                """UPDATE loads SET status = %s, current_stop_index = %s WHERE id = %s
                   AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
                (status, current_stop_index, load_id, company_id),
            )
        else:
            conn.execute(
                """UPDATE loads SET status = %s WHERE id = %s
                   AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
                (status, load_id, company_id),
            )
        return conn.rowcount > 0


def update_load_fields(load_id: int, company_id: int, fields: dict) -> bool:
    allowed = {"group_id", "load_number", "origin_state", "destination_state",
               "total_rate_usd", "miles", "pickup_address", "pickup_date",
               "delivery_address", "delivery_date", "stops_json",
               "broker_name", "deadhead_miles", "charge"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    setters = ", ".join(f"{k} = %s" for k in updates)
    with get_conn() as conn:
        conn.execute(
            f"""UPDATE loads SET {setters} WHERE id = %s
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
            (*updates.values(), load_id, company_id),
        )
        return conn.rowcount > 0


def update_load_stop_index(load_id: int, company_id: int, index: int) -> bool:
    with get_conn() as conn:
        conn.execute(
            """UPDATE loads SET current_stop_index = %s WHERE id = %s
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
            (index, load_id, company_id),
        )
        return conn.rowcount > 0


def update_load_eta(load_id: int, eta_utc: str, eta_miles: float) -> bool:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "UPDATE loads SET eta_utc = %s, eta_miles = %s, last_eta_update = %s, eta_alert_sent = FALSE WHERE id = %s",
            (eta_utc, eta_miles, now, load_id),
        )
        return conn.rowcount > 0


def delete_load(load_id: int, company_id: int) -> bool:
    with get_conn() as conn:
        conn.execute(
            """DELETE FROM loads WHERE id = %s
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
            (load_id, company_id),
        )
        return conn.rowcount > 0


def update_load_file(load_id: int, file_path: str) -> bool:
    with get_conn() as conn:
        conn.execute(
            "UPDATE loads SET file_path = %s WHERE id = %s",
            (file_path, load_id),
        )
        return conn.rowcount > 0


def update_load_auto_send(load_id: int, company_id: int, hours: int) -> bool:
    with get_conn() as conn:
        conn.execute(
            """UPDATE loads SET auto_send_hours = %s WHERE id = %s
               AND dispatcher_id IN (SELECT id FROM dispatchers WHERE company_id = %s)""",
            (hours, load_id, company_id),
        )
        return conn.rowcount > 0


def update_last_auto_send(load_id: int) -> bool:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "UPDATE loads SET last_auto_send = %s WHERE id = %s",
            (now, load_id),
        )
        return conn.rowcount > 0


def set_eta_alert_sent(load_id: int) -> bool:
    with get_conn() as conn:
        conn.execute(
            "UPDATE loads SET eta_alert_sent = TRUE WHERE id = %s",
            (load_id,),
        )
        return conn.rowcount > 0


def get_active_load_by_chat_id(chat_id: int) -> dict | None:
    """Returns the most recent dispatched load for a given Telegram chat_id."""
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      dg.chat_id as group_chat_id, d.name as dispatcher_name, d.company_id
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE dg.chat_id = %s AND l.status = 'dispatched'
               ORDER BY l.created_at DESC
               LIMIT 1""",
            (chat_id,),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_upcoming_load_by_chat_id(chat_id: int) -> dict | None:
    """Returns the most recent upcoming load for a given Telegram chat_id."""
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      dg.chat_id as group_chat_id, d.name as dispatcher_name, d.company_id
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE dg.chat_id = %s AND l.status = 'upcoming'
               ORDER BY l.created_at DESC
               LIMIT 1""",
            (chat_id,),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_dispatched_loads_for_alerts() -> list[dict]:
    """Returns dispatched loads with ETA set that haven't had an alert sent yet."""
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.chat_id as group_chat_id
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               WHERE l.status = 'dispatched'
                 AND l.eta_utc IS NOT NULL
                 AND l.eta_alert_sent = FALSE
                 AND dg.chat_id IS NOT NULL"""
        )
        return [dict(r) for r in conn.fetchall()]


def get_company_info(dispatcher_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM company_info WHERE dispatcher_id = %s", (dispatcher_id,)
        )
        row = conn.fetchone()
        return dict(row) if row else None


def save_company_info(dispatcher_id: int, data: dict) -> None:
    now = datetime.now(timezone.utc)
    fields = ["company_name", "address", "phone", "email", "mc_number",
              "dot_number", "bank_name", "bank_account", "bank_routing", "payment_terms"]
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO company_info (dispatcher_id, {', '.join(fields)}, updated_at)
                VALUES (%s, {', '.join('%s' for _ in fields)}, %s)
                ON CONFLICT(dispatcher_id) DO UPDATE SET
                {', '.join(f'{f} = EXCLUDED.{f}' for f in fields)},
                updated_at = EXCLUDED.updated_at""",
            (dispatcher_id, *[data.get(f) for f in fields], now),
        )


def get_global_setting(key: str) -> str | None:
    with get_conn() as conn:
        conn.execute(
            "SELECT value FROM global_settings WHERE key = %s", (key,)
        )
        row = conn.fetchone()
        return row["value"] if row else None


def set_global_setting(key: str, value: str) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO global_settings (key, value, updated_at) VALUES (%s, %s, %s)
               ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
            (key, value, now),
        )


def get_google_maps_key(dispatcher_id: int) -> str | None:
    with get_conn() as conn:
        conn.execute(
            "SELECT google_maps_key FROM company_info WHERE dispatcher_id = %s", (dispatcher_id,)
        )
        row = conn.fetchone()
        return row["google_maps_key"] if row else None


def save_google_maps_key(dispatcher_id: int, key: str) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO company_info (dispatcher_id, google_maps_key, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT(dispatcher_id) DO UPDATE SET
               google_maps_key = EXCLUDED.google_maps_key,
               updated_at = EXCLUDED.updated_at""",
            (dispatcher_id, key, now),
        )


def add_load_pod(load_id: int, file_path: str, filename: str) -> dict:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO load_pods (load_id, file_path, filename, uploaded_at) VALUES (%s, %s, %s, %s) RETURNING id",
            (load_id, file_path, filename, now),
        )
        row = conn.fetchone()
        return {"id": row["id"], "load_id": load_id, "filename": filename, "uploaded_at": now.isoformat()}


def get_load_pods(load_id: int) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM load_pods WHERE load_id = %s ORDER BY uploaded_at",
            (load_id,),
        )
        return [dict(r) for r in conn.fetchall()]


def delete_load_pod(pod_id: int, load_id: int) -> str | None:
    """Returns file_path if deleted, else None."""
    with get_conn() as conn:
        conn.execute(
            "SELECT file_path FROM load_pods WHERE id = %s AND load_id = %s", (pod_id, load_id)
        )
        row = conn.fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM load_pods WHERE id = %s", (pod_id,))
        return row["file_path"]


# ── Companies ─────────────────────────────────────────────────────────────────

def create_company(name: str) -> dict:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute("INSERT INTO companies (name, created_at) VALUES (%s, %s) RETURNING id", (name, now))
        row = conn.fetchone()
        return {"id": row["id"], "name": name, "created_at": now.isoformat()}


def get_all_companies() -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            """SELECT c.id, c.name, c.created_at,
                      (SELECT d2.name FROM dispatchers d2 WHERE d2.company_id = c.id AND d2.role = 'admin' LIMIT 1) as admin_name,
                      (SELECT d2.email FROM dispatchers d2 WHERE d2.company_id = c.id AND d2.role = 'admin' LIMIT 1) as admin_email,
                      COUNT(DISTINCT CASE WHEN d.role = 'user' THEN d.id END) as user_count,
                      COUNT(DISTINCT l.id) as load_count,
                      COALESCE(SUM(CASE WHEN l.status='delivered' THEN CAST(REPLACE(REPLACE(l.total_rate_usd,'$',''),',','') AS DOUBLE PRECISION) ELSE 0 END), 0) as revenue
               FROM companies c
               LEFT JOIN dispatchers d ON d.company_id = c.id
               LEFT JOIN loads l ON l.dispatcher_id = d.id
               GROUP BY c.id, c.name, c.created_at
               ORDER BY c.created_at"""
        )
        return [dict(r) for r in conn.fetchall()]


def get_company(company_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
        row = conn.fetchone()
        return dict(row) if row else None


def update_company(company_id: int, name: str) -> bool:
    with get_conn() as conn:
        conn.execute("UPDATE companies SET name = %s WHERE id = %s", (name, company_id))
        return conn.rowcount > 0


def delete_company(company_id: int) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM companies WHERE id = %s", (company_id,))
        return conn.rowcount > 0


def get_admin_stats() -> dict:
    """Global stats for superadmin panel."""
    with get_conn() as conn:
        conn.execute("SELECT COUNT(*) AS cnt FROM companies")
        total_companies = conn.fetchone()["cnt"]
        conn.execute("SELECT COUNT(*) AS cnt FROM dispatchers WHERE role = 'admin'")
        total_admins = conn.fetchone()["cnt"]
        conn.execute("SELECT COUNT(*) AS cnt FROM dispatchers WHERE role = 'user'")
        total_users = conn.fetchone()["cnt"]
        conn.execute("SELECT COUNT(*) AS cnt FROM driver_groups")
        total_drivers = conn.fetchone()["cnt"]
        conn.execute("SELECT COUNT(*) AS cnt FROM loads")
        total_loads = conn.fetchone()["cnt"]
        conn.execute(
            "SELECT COALESCE(SUM(CAST(REPLACE(REPLACE(total_rate_usd,'$',''),',','') AS DOUBLE PRECISION)), 0) AS total FROM loads WHERE status = 'delivered'"
        )
        total_revenue = conn.fetchone()["total"]
        conn.execute("SELECT COUNT(*) AS cnt FROM loads WHERE status = 'dispatched'")
        dispatched = conn.fetchone()["cnt"]
        conn.execute("SELECT COUNT(*) AS cnt FROM loads WHERE status = 'delivered'")
        delivered = conn.fetchone()["cnt"]
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
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      dg.chat_id as group_chat_id,
                      ec.provider as eld_provider, ec.api_key as eld_api_key,
                      ec.company as eld_company, ec.provider_token as eld_provider_token
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN eld_configs ec ON ec.id = (
                   SELECT ec2.id
                   FROM eld_configs ec2
                   WHERE ec2.dispatcher_id = l.dispatcher_id
                   ORDER BY ec2.updated_at DESC, ec2.id DESC
                   LIMIT 1
               )
               WHERE l.status = 'dispatched'
                 AND l.auto_send_hours > 0
                 AND dg.chat_id IS NOT NULL
                 AND dg.eld_driver_id IS NOT NULL
                 AND ec.provider IS NOT NULL"""
        )
        return [dict(r) for r in conn.fetchall()]


def create_audit_log(
    actor_dispatcher_id: int | None,
    company_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    metadata_json = json.dumps(metadata or {}, ensure_ascii=True, separators=(",", ":"))
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO audit_logs (
                   actor_dispatcher_id, company_id, action, entity_type, entity_id, metadata_json, created_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (actor_dispatcher_id, company_id, action, entity_type, entity_id, metadata_json, now),
        )
        row = conn.fetchone()
        return {
            "id": row["id"],
            "actor_dispatcher_id": actor_dispatcher_id,
            "company_id": company_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata": metadata or {},
            "created_at": now.isoformat(),
        }


def get_audit_logs(company_id: int, limit: int = 100) -> list[dict]:
    safe_limit = max(1, min(int(limit), 500))
    with get_conn() as conn:
        conn.execute(
            """SELECT al.*, d.name as actor_name, d.email as actor_email
               FROM audit_logs al
               LEFT JOIN dispatchers d ON d.id = al.actor_dispatcher_id
               WHERE al.company_id = %s
               ORDER BY al.created_at DESC
               LIMIT %s""",
            (company_id, safe_limit),
        )
        result = []
        for row in conn.fetchall():
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata_json") or "{}")
            except Exception:
                item["metadata"] = {}
            result.append(item)
        return result


# ── Pay Tiers ─────────────────────────────────────────────────────────────────

def get_pay_tiers(company_id: int) -> list[dict]:
    """Returns all tiers for a company (both company-wide and per-worker)."""
    with get_conn() as conn:
        conn.execute(
            """SELECT pt.*, d.name as dispatcher_name
               FROM pay_tiers pt
               LEFT JOIN dispatchers d ON pt.dispatcher_id = d.id
               WHERE pt.company_id = %s
               ORDER BY pt.dispatcher_id NULLS FIRST, pt.min_gross DESC""",
            (company_id,),
        )
        return [dict(r) for r in conn.fetchall()]


def create_pay_tier(company_id: int, dispatcher_id: int | None,
                    min_gross: float, max_gross: float | None,
                    min_rpm: float, percentage: float) -> dict:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO pay_tiers (company_id, dispatcher_id, min_gross, max_gross, min_rpm, percentage, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (company_id, dispatcher_id, min_gross, max_gross, min_rpm, percentage, now),
        )
        row = conn.fetchone()
        return {"id": row["id"], "company_id": company_id, "dispatcher_id": dispatcher_id,
                "min_gross": min_gross, "max_gross": max_gross, "min_rpm": min_rpm,
                "percentage": percentage, "created_at": now.isoformat()}


def delete_pay_tier(tier_id: int, company_id: int) -> bool:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM pay_tiers WHERE id = %s AND company_id = %s",
            (tier_id, company_id),
        )
        return conn.rowcount > 0


def get_earnings(company_id: int, dispatcher_id: int | None = None) -> list[dict]:
    """Calculate earnings for delivered loads using pay tiers.
    If dispatcher_id given, returns only that worker's earnings.
    """
    with get_conn() as conn:
        if dispatcher_id:
            conn.execute(
                """SELECT l.*, d.name as dispatcher_name, d.id as worker_id
                   FROM loads l
                   JOIN dispatchers d ON l.dispatcher_id = d.id
                   WHERE d.company_id = %s AND l.dispatcher_id = %s AND l.status = 'delivered'
                   ORDER BY l.created_at DESC""",
                (company_id, dispatcher_id),
            )
        else:
            conn.execute(
                """SELECT l.*, d.name as dispatcher_name, d.id as worker_id
                   FROM loads l
                   JOIN dispatchers d ON l.dispatcher_id = d.id
                   WHERE d.company_id = %s AND l.status = 'delivered'
                   ORDER BY l.created_at DESC""",
                (company_id,),
            )
        rows = conn.fetchall()

    tiers = get_pay_tiers(company_id)
    company_tiers = [t for t in tiers if t["dispatcher_id"] is None]

    result = []
    for row in rows:
        load = dict(row)
        worker_id = load["worker_id"]
        worker_tiers = [t for t in tiers if t["dispatcher_id"] == worker_id]
        active_tiers = worker_tiers if worker_tiers else company_tiers

        def _parse_num(v):
            if not v:
                return 0.0
            return float(str(v).replace("$", "").replace(",", "").strip() or 0)

        gross = _parse_num(load.get("total_rate_usd"))
        miles = _parse_num(load.get("miles"))
        rpm = gross / miles if miles > 0 else None

        earning = 0.0
        matched_tier = None
        for tier in sorted(active_tiers, key=lambda t: t["min_gross"], reverse=True):
            gross_ok = gross >= tier["min_gross"]
            max_ok = tier["max_gross"] is None or gross < tier["max_gross"]
            # skip RPM check if miles data is missing
            rpm_ok = rpm is None or rpm >= tier["min_rpm"]
            if gross_ok and max_ok and rpm_ok:
                earning = round(gross * tier["percentage"] / 100, 2)
                matched_tier = tier
                break

        load["rpm"] = round(rpm, 2) if rpm is not None else None
        load["earning"] = earning
        load["tier_percentage"] = matched_tier["percentage"] if matched_tier else None
        result.append(load)

    return result


# ── KPI Board ─────────────────────────────────────────────────────────────────


def get_weekly_kpi(company_id: int, week_start: str) -> list[dict]:
    """Get all loads for a company within a specific week (Mon-Sun), grouped by driver.
    week_start is ISO date string like '2026-03-30' (Monday).
    Returns loads with driver info for the KPI board."""
    from datetime import timedelta
    week_start_dt = datetime.strptime(week_start, "%Y-%m-%d").date()
    week_end_dt = week_start_dt + timedelta(days=7)
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.id as driver_group_id,
                      dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name, d.id as worker_id
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE d.company_id = %s
                 AND l.status IN ('dispatched', 'delivered')
                 AND l.pickup_date >= %s AND l.pickup_date < %s
               ORDER BY dg.name, l.pickup_date""",
            (company_id, week_start_dt.isoformat(), week_end_dt.isoformat()),
        )
        return [dict(r) for r in conn.fetchall()]


def get_kpi_comments(company_id: int, week_start: str) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            """SELECT kc.*, dg.name as driver_name
               FROM kpi_comments kc
               LEFT JOIN driver_groups dg ON kc.driver_group_id = dg.id
               WHERE kc.company_id = %s AND kc.week_start = %s""",
            (company_id, week_start),
        )
        return [dict(r) for r in conn.fetchall()]


def upsert_kpi_comment(company_id: int, driver_group_id: int, week_start: str, comment: str):
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO kpi_comments (company_id, driver_group_id, week_start, comment, updated_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT(company_id, driver_group_id, week_start)
               DO UPDATE SET comment = EXCLUDED.comment, updated_at = EXCLUDED.updated_at""",
            (company_id, driver_group_id, week_start, comment, now),
        )


# ── KPI Entries (dispatcher self-report) ──────────────────────────────────────

def add_kpi_entry(company_id: int, dispatcher_id: int, week_start: str,
                  load_id: int | None, miles: float, cost: float) -> dict:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        if load_id:
            conn.execute(
                "SELECT id FROM kpi_entries WHERE load_id = %s", (load_id,)
            )
            if conn.fetchone():
                raise ValueError("This load is already assigned to a KPI entry")
        conn.execute(
            """INSERT INTO kpi_entries (company_id, dispatcher_id, week_start, load_id, miles, cost, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (company_id, dispatcher_id, week_start, load_id, miles, cost, now),
        )
        row = conn.fetchone()
        return {"id": row["id"]}


def delete_kpi_entry(entry_id: int, dispatcher_id: int) -> bool:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM kpi_entries WHERE id = %s AND dispatcher_id = %s",
            (entry_id, dispatcher_id),
        )
        return conn.rowcount > 0


def get_kpi_entries_for_dispatcher(company_id: int, dispatcher_id: int, week_start: str) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            """SELECT ke.*, l.load_number, l.broker_name, l.origin_state, l.destination_state,
                      dg.name as driver_name
               FROM kpi_entries ke
               LEFT JOIN loads l ON ke.load_id = l.id
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               WHERE ke.company_id = %s AND ke.dispatcher_id = %s AND ke.week_start = %s
               ORDER BY ke.id""",
            (company_id, dispatcher_id, week_start),
        )
        return [dict(r) for r in conn.fetchall()]


def get_dispatcher_loads_for_select(dispatcher_id: int, company_id: int, is_admin: bool = False) -> list[dict]:
    """Get loads for KPI entry dropdown. Admins see all company loads."""
    with get_conn() as conn:
        if is_admin:
            conn.execute(
                """SELECT l.id, l.load_number, l.total_rate_usd, l.miles, l.pickup_date,
                          l.origin_state, l.destination_state, l.broker_name,
                          dg.name as driver_name
                   FROM loads l
                   LEFT JOIN driver_groups dg ON l.group_id = dg.id
                   JOIN dispatchers d ON l.dispatcher_id = d.id
                   WHERE d.company_id = %s
                     AND l.status IN ('dispatched', 'delivered')
                   ORDER BY l.pickup_date DESC
                   LIMIT 50""",
                (company_id,),
            )
        else:
            conn.execute(
                """SELECT l.id, l.load_number, l.total_rate_usd, l.miles, l.pickup_date,
                          l.origin_state, l.destination_state, l.broker_name,
                          dg.name as driver_name
                   FROM loads l
                   LEFT JOIN driver_groups dg ON l.group_id = dg.id
                   WHERE l.dispatcher_id = %s
                     AND l.status IN ('dispatched', 'delivered')
                   ORDER BY l.pickup_date DESC
                   LIMIT 50""",
                (dispatcher_id,),
            )
        return [dict(r) for r in conn.fetchall()]


def get_all_kpi_entries(company_id: int, week_start: str) -> list[dict]:
    """Get all KPI entries grouped by dispatcher for admin view."""
    with get_conn() as conn:
        conn.execute(
            """SELECT ke.*, d.name as dispatcher_name, l.load_number, l.broker_name,
                      l.origin_state, l.destination_state
               FROM kpi_entries ke
               JOIN dispatchers d ON ke.dispatcher_id = d.id
               LEFT JOIN loads l ON ke.load_id = l.id
               WHERE ke.company_id = %s AND ke.week_start = %s
               ORDER BY d.name, ke.id""",
            (company_id, week_start),
        )
        return [dict(r) for r in conn.fetchall()]
