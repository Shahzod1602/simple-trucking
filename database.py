import base64
import hashlib
import json
import os
import re
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import psycopg2
import psycopg2.extras
import psycopg2.pool
from argon2 import PasswordHasher

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://ratecon:ratecon@localhost:5432/ratecon",
)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            dsn=DATABASE_URL,
        )
    return _pool


def init_db():
    # Ensure the connection pool is initialized on startup
    _get_pool()
    # L1: serialize the CREATE/ALTER DDL below with the same blocking advisory
    # lock used by run_migrations, so concurrent uvicorn workers (or hosts)
    # don't race the idempotent DDL on a fresh boot.
    lock_conn = psycopg2.connect(DATABASE_URL)
    lock_conn.autocommit = True
    lcur = lock_conn.cursor()
    lcur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))  # blocks
    try:
        _init_db_ddl()
    finally:
        try:
            lcur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
        except Exception:
            pass
        lock_conn.close()

    super_email = os.environ.get("SUPER_ADMIN_EMAIL", "").strip().lower()
    super_pass = os.environ.get("SUPER_ADMIN_PASSWORD", "").strip()
    if super_email and super_pass:
        ensure_superadmin(super_email, super_pass)


def _init_db_ddl():
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
        conn.execute("ALTER TABLE loads ADD COLUMN IF NOT EXISTS motive_dispatch_id INTEGER")
        conn.execute("ALTER TABLE loads ADD COLUMN IF NOT EXISTS customer_id INTEGER")
        conn.execute("ALTER TABLE loads ADD COLUMN IF NOT EXISTS trailer_id INTEGER")

        # ── Customer Management ───────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                mc_number TEXT,
                dot_number TEXT,
                contact_name TEXT,
                email TEXT,
                phone TEXT,
                address TEXT,
                credit_score TEXT,
                payment_terms TEXT,
                notes TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_company ON customers(company_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendors (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                category TEXT,
                contact_name TEXT,
                email TEXT,
                phone TEXT,
                address TEXT,
                tax_id TEXT,
                payment_terms TEXT,
                notes TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vendors_company ON vendors(company_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS locations (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                city TEXT,
                state TEXT,
                zip TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                location_type TEXT,
                contact_name TEXT,
                contact_phone TEXT,
                notes TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_locations_company ON locations(company_id)")

        # ── Fleet Management ──────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trailers (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                unit_number TEXT NOT NULL,
                trailer_type TEXT,
                year TEXT,
                make TEXT,
                vin TEXT,
                license_plate TEXT,
                license_state TEXT,
                status TEXT NOT NULL DEFAULT 'available',
                notes TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trailers_company ON trailers(company_id)")

        # ── Driver documents (CDL, medical card, etc.) ────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS driver_documents (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                driver_group_id INTEGER REFERENCES driver_groups(id) ON DELETE CASCADE,
                doc_type TEXT NOT NULL,
                doc_number TEXT,
                issue_date DATE,
                expiry_date DATE,
                file_path TEXT,
                notes TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_driver_docs_company ON driver_documents(company_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_driver_docs_expiry ON driver_documents(expiry_date)")

        # ── Safety / Compliance ───────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS safety_tasks (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                task_type TEXT,
                driver_group_id INTEGER REFERENCES driver_groups(id) ON DELETE SET NULL,
                trailer_id INTEGER REFERENCES trailers(id) ON DELETE SET NULL,
                due_date DATE,
                completed_at TIMESTAMP,
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'open',
                notes TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_safety_company ON safety_tasks(company_id)")

        # ── Accounting / Bills / Transactions ─────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bills (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                vendor_id INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
                bill_number TEXT,
                amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                bill_date DATE,
                due_date DATE,
                status TEXT NOT NULL DEFAULT 'unpaid',
                category TEXT,
                notes TEXT,
                file_path TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bills_company ON bills(company_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                tx_date DATE NOT NULL,
                tx_type TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                category TEXT,
                description TEXT,
                load_id INTEGER REFERENCES loads(id) ON DELETE SET NULL,
                bill_id INTEGER REFERENCES bills(id) ON DELETE SET NULL,
                driver_group_id INTEGER REFERENCES driver_groups(id) ON DELETE SET NULL,
                reference TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_company_date ON transactions(company_id, tx_date DESC)")

        # ── Maintenance / Work Orders ─────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_orders (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                wo_number TEXT,
                title TEXT NOT NULL,
                equipment_type TEXT,
                equipment_ref TEXT,
                trailer_id INTEGER REFERENCES trailers(id) ON DELETE SET NULL,
                vendor_id INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
                opened_at DATE,
                closed_at DATE,
                cost DOUBLE PRECISION,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal',
                description TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_work_orders_company ON work_orders(company_id)")

        # ── Mailbox (broker emails / notifications) ───────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mailbox_messages (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                dispatcher_id INTEGER REFERENCES dispatchers(id) ON DELETE SET NULL,
                source TEXT NOT NULL DEFAULT 'system',
                from_addr TEXT,
                subject TEXT,
                body TEXT,
                load_id INTEGER REFERENCES loads(id) ON DELETE SET NULL,
                is_read BOOLEAN NOT NULL DEFAULT FALSE,
                attachment_path TEXT,
                received_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mailbox_company_received ON mailbox_messages(company_id, received_at DESC)")

        # ── Email accounts: Relay-connected Gmail/Outlook mailboxes per company ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_accounts (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                relay_account_id TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                email TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                last_polled_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_accounts_company ON email_accounts(company_id)")

        # Extend mailbox_messages for real inbound email (Relay) + threaded reply.
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN IF NOT EXISTS account_id INTEGER REFERENCES email_accounts(id) ON DELETE SET NULL")
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN IF NOT EXISTS external_id TEXT")
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN IF NOT EXISTS thread_id TEXT")
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN IF NOT EXISTS rfc_message_id TEXT")
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN IF NOT EXISTS to_addr TEXT")
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN IF NOT EXISTS attachments_json TEXT")
        # Dedup inbound email by provider message id (partial unique — manual rows have NULL).
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mailbox_ext ON mailbox_messages(company_id, external_id) WHERE external_id IS NOT NULL")

        # Migrate kpi_entries: drop old unique constraint if exists (schema changed)
        try:
            conn.execute("ALTER TABLE kpi_entries DROP CONSTRAINT IF EXISTS kpi_entries_company_id_dispatcher_id_week_start_key")
        except Exception:
            conn.connection.rollback()
        conn.execute("ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS load_id INTEGER REFERENCES loads(id) ON DELETE SET NULL")
        conn.execute("ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS miles DOUBLE PRECISION NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS cost DOUBLE PRECISION NOT NULL DEFAULT 0")

        # ── Single-use Telegram group-link codes ──────────────────────────────
        # Drivers paste a short code (not the dispatcher's API token) into the
        # group chat, so the master credential never travels through Telegram.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_link_codes (
                code TEXT PRIMARY KEY,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                created_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL
            )
        """)

        # ── Real Telegram (MTProto) — connected personal accounts + login state ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_accounts (
                id SERIAL PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                dispatcher_id INTEGER NOT NULL UNIQUE REFERENCES dispatchers(id) ON DELETE CASCADE,
                tg_user_id BIGINT,
                phone TEXT,
                username TEXT,
                name TEXT,
                session_enc TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_logins (
                login_id TEXT PRIMARY KEY,
                dispatcher_id INTEGER NOT NULL REFERENCES dispatchers(id) ON DELETE CASCADE,
                method TEXT NOT NULL,
                phone TEXT,
                phone_code_hash TEXT,
                session_enc TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """)

        # ── Performance indexes on hot tables ─────────────────────────────────
        # loads/dispatchers/groups are scanned on nearly every page; without
        # these every query is a seq-scan that grows with total rows.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_loads_dispatcher ON loads(dispatcher_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_loads_status ON loads(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_loads_group ON loads(group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_loads_motive ON loads(motive_dispatch_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatchers_company ON dispatchers(company_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatchers_email ON dispatchers(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_groups_dispatcher ON driver_groups(dispatcher_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eld_dispatcher ON eld_configs(dispatcher_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kpi_load ON kpi_entries(load_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kpi_lookup ON kpi_entries(company_id, dispatcher_id, week_start)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_tiers_company ON pay_tiers(company_id)")


def ensure_superadmin(email: str, password: str):
    """Create or sync the superadmin account from env vars on every startup."""
    # C1 + H1: never create a credential-less superadmin. A misconfigured/blank
    # deploy must NOT end up with an account that an empty {"email","password"}
    # login can claim. The lifespan calls this unconditionally, so the guard
    # lives here (not only in init_db).
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not (email and password):
        print("[superadmin] WARNING: SUPER_ADMIN_EMAIL/PASSWORD missing; skipping superadmin sync", flush=True)
        return
    if len(password) < 12:
        # Warn but don't block: blocking would stop a live deploy from re-syncing
        # an already-rotated/existing superadmin. Rotation is handled out of band.
        print("[superadmin] WARNING: SUPER_ADMIN_PASSWORD is weak (<12 chars); use a long random secret", flush=True)
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
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ── Password ──────────────────────────────────────────────────────────────────

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash with Argon2id (current). Legacy PBKDF2 hashes still verify below and
    are transparently upgraded on next login."""
    return _ph.hash(password)


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("$argon2"):
        try:
            return _ph.verify(stored, password)
        except Exception:
            return False
    # Legacy PBKDF2 ("salt:hexhash") — accepted, then rehashed to Argon2 on login.
    try:
        salt, h = stored.split(":", 1)
        h2 = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return secrets.compare_digest(h2.hex(), h)
    except Exception:
        return False


def password_needs_rehash(stored: str) -> bool:
    """True when the stored hash is legacy or below current Argon2 parameters."""
    if not stored or not stored.startswith("$argon2"):
        return True
    try:
        return _ph.check_needs_rehash(stored)
    except Exception:
        return False


# ── Money / rate parsing + pay tiers (single source of truth) ─────────────────

def parse_money(value) -> float:
    """Parse a free-text money/number string like '$2,500.00' or '1,234' into a
    float. Returns 0.0 for empty/unparseable input. Extracts the first
    number-like token so values such as '$2,500 + FSC' still yield 2500.0."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else 0.0


def _rate_to_numeric(col: str) -> str:
    """SQL fragment that turns a free-text rate column into a numeric value,
    tolerating '$', commas and trailing text without ever crashing the cast.
    Use inside an f-string SQL query."""
    return (
        f"COALESCE(NULLIF(regexp_replace("
        f"substring({col} FROM '[0-9][0-9,]*\\.?[0-9]*'), ',', '', 'g'), '')"
        f"::numeric, 0)"
    )


def match_tier(gross: float, rpm: float | None, tiers: list[dict]) -> dict | None:
    """Pick the pay tier for a load/period. Highest min_gross first.
    rpm=None means miles are unknown, so the RPM floor is not enforced.
    Returns None when no tier matches (caller should surface that, not pay $0
    silently)."""
    for tier in sorted(tiers, key=lambda t: t.get("min_gross") or 0, reverse=True):
        if gross < (tier.get("min_gross") or 0):
            continue
        max_gross = tier.get("max_gross")
        if max_gross is not None and gross >= max_gross:
            continue
        min_rpm = tier.get("min_rpm") or 0
        if min_rpm and rpm is not None and rpm < min_rpm:
            continue
        return tier
    return None


def tier_earning(gross: float, tier: dict | None) -> float:
    """Commission for a gross amount under a tier, computed with Decimal to
    avoid binary-float drift on money."""
    if not tier:
        return 0.0
    pct = Decimal(str(tier.get("percentage") or 0))
    cents = (Decimal(str(gross)) * pct / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(cents)


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
    # The platform superadmin is never a manageable member of a company, even
    # if its account happens to carry a company_id — exclude it so company/admin
    # actors can't view, edit, reset or assign it.
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM dispatchers WHERE id = %s AND company_id = %s AND role <> 'superadmin'",
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
               WHERE d.company_id = %s AND d.role <> 'superadmin'
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


# ── Sessions (expiring, revocable bearer tokens) ──────────────────────────────

SESSION_TTL_DAYS = 30


def create_session(dispatcher_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, dispatcher_id, created_at, last_used_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (token, dispatcher_id, now, now, now + timedelta(days=SESSION_TTL_DAYS)),
        )
    return token


def get_dispatcher_by_session(token: str) -> dict | None:
    """Resolve an active, unexpired session to its dispatcher and bump last_used."""
    if not token:
        return None
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "SELECT d.* FROM sessions s JOIN dispatchers d ON d.id = s.dispatcher_id "
            "WHERE s.token = %s AND s.expires_at > %s AND d.is_active = TRUE",
            (token, now),
        )
        row = conn.fetchone()
        if not row:
            return None
        conn.execute("UPDATE sessions SET last_used_at = %s WHERE token = %s", (now, token))
        return dict(row)


def delete_session(token: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = %s", (token,))


def delete_sessions_for_dispatcher(dispatcher_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE dispatcher_id = %s", (dispatcher_id,))


def delete_other_sessions(dispatcher_id: int, keep_token: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE dispatcher_id = %s AND token <> %s",
            (dispatcher_id, keep_token),
        )


def purge_expired_sessions() -> int:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < %s", (datetime.now(timezone.utc),))
        return conn.rowcount


# ── Migration runner (versioned DDL in migrations/*.sql) ──────────────────────

_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
_MIGRATION_LOCK_KEY = 911000  # serialize migration runs across workers/hosts


def run_migrations() -> list[str]:
    """Apply *.sql files in migrations/ not yet recorded in schema_migrations.
    Serialized with a blocking Postgres advisory lock so concurrent uvicorn
    workers (or hosts) don't race: one applies, the rest block then skip.
    Each migration runs in its own transaction. Safe to re-run (idempotent)."""
    if not os.path.isdir(_MIGRATIONS_DIR):
        return []
    files = sorted(f for f in os.listdir(_MIGRATIONS_DIR) if f.endswith(".sql"))
    lock_conn = psycopg2.connect(DATABASE_URL)
    lock_conn.autocommit = True
    lcur = lock_conn.cursor()
    lcur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))  # blocks
    done: list[str] = []
    try:
        with get_conn() as conn:
            conn.execute("SELECT name FROM schema_migrations")
            applied = {r["name"] for r in conn.fetchall()}
        for fn in files:
            if fn in applied:
                continue
            sql = open(os.path.join(_MIGRATIONS_DIR, fn), encoding="utf-8").read()
            with get_conn() as conn:
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (fn, datetime.now(timezone.utc)),
                )
            done.append(fn)
            print(f"[migration] applied {fn}", flush=True)
    finally:
        try:
            lcur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
        except Exception:
            pass
        lock_conn.close()
    return done


# ── Cross-worker singleton locks (Postgres advisory locks) ────────────────────

_advisory_lock_conns: list = []  # held open to keep the locks


def acquire_advisory_lock(key: int) -> bool:
    """Try to grab a session-level advisory lock on a dedicated connection.
    Returns True if acquired; the connection is kept open for the process
    lifetime so the lock is held (auto-released when the process exits).
    Ensures only ONE worker/host runs a singleton background task — works
    across restarts and hosts, unlike a /tmp file lock."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        got = bool(cur.fetchone()[0])
        if got:
            _advisory_lock_conns.append(conn)
        else:
            conn.close()
        return got
    except Exception:
        return False


# ── ELD secret encryption at rest (M5, Fernet) ────────────────────────────────
# Key source: ELD_SECRET if set, else derived (stable) from TG_SESSION_SECRET.
# If neither is set we fall back to PASS-THROUGH (no encryption) and warn once,
# so the app keeps working in dev. decrypt_secret returns its input unchanged on
# any invalid token, so legacy PLAINTEXT rows keep decrypting to themselves.

_eld_fernet = None
_eld_fernet_ready = False
_eld_secret_warned = False


def _get_eld_fernet():
    global _eld_fernet, _eld_fernet_ready, _eld_secret_warned
    if _eld_fernet_ready:
        return _eld_fernet
    _eld_fernet_ready = True
    secret = (os.environ.get("ELD_SECRET", "").strip()
              or os.environ.get("TG_SESSION_SECRET", "").strip())
    if not secret:
        if not _eld_secret_warned:
            _eld_secret_warned = True
            print("[eld-secret] WARNING: neither ELD_SECRET nor TG_SESSION_SECRET "
                  "is set; ELD credentials are stored in PLAINTEXT at rest", flush=True)
        _eld_fernet = None
        return None
    try:
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        _eld_fernet = Fernet(key)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[eld-secret] WARNING: could not init Fernet ({exc}); "
              "ELD credentials are stored in PLAINTEXT at rest", flush=True)
        _eld_fernet = None
    return _eld_fernet


def encrypt_secret(plaintext):
    """Encrypt an ELD credential for storage. Pass-through when no key is set."""
    if plaintext is None or plaintext == "":
        return plaintext
    f = _get_eld_fernet()
    if f is None:
        return plaintext
    try:
        return f.encrypt(str(plaintext).encode()).decode()
    except Exception:
        return plaintext


def decrypt_secret(token):
    """Decrypt a stored ELD credential. Returns the input UNCHANGED when no key
    is set or when the value is not a valid Fernet token (legacy plaintext)."""
    if token is None or token == "":
        return token
    f = _get_eld_fernet()
    if f is None:
        return token
    try:
        return f.decrypt(str(token).encode()).decode()
    except Exception:
        # Not a valid token for our key — assume legacy plaintext; return as-is.
        return token


# ── ELD configs ───────────────────────────────────────────────────────────────

def _decrypt_eld_row(row: dict) -> dict:
    """Return an eld_configs row dict with api_key/provider_token decrypted to
    plaintext (so callers — and the response masker — see the real values)."""
    d = dict(row)
    if "api_key" in d:
        d["api_key"] = decrypt_secret(d["api_key"])
    if d.get("provider_token") is not None:
        d["provider_token"] = decrypt_secret(d["provider_token"])
    return d


def get_eld_configs(dispatcher_id: int) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM eld_configs WHERE dispatcher_id = %s", (dispatcher_id,)
        )
        rows = conn.fetchall()
        if rows:
            return [_decrypt_eld_row(r) for r in rows]
        # fallback to company admin's configs
        conn.execute(
            """SELECT ec.* FROM eld_configs ec
               JOIN dispatchers d ON d.id = ec.dispatcher_id
               WHERE d.company_id = (SELECT company_id FROM dispatchers WHERE id = %s)
               AND d.company_id IS NOT NULL""",
            (dispatcher_id,),
        )
        rows = conn.fetchall()
        return [_decrypt_eld_row(r) for r in rows]


def get_eld_config(dispatcher_id: int) -> dict | None:
    configs = get_eld_configs(dispatcher_id)
    return configs[0] if configs else None


def save_eld_config(dispatcher_id: int, provider: str, api_key: str, company: str | None = None, provider_token: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    # M5: encrypt secrets at rest (pass-through when no key configured).
    enc_api_key = encrypt_secret(api_key)
    enc_provider_token = encrypt_secret(provider_token)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO eld_configs (dispatcher_id, provider, api_key, company, provider_token, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (dispatcher_id, provider, enc_api_key, company, enc_provider_token, now),
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
            # L8: refuse to silently move a chat that another company already owns;
            # an explicit unlink is required first. Same-company re-link is fine.
            conn.execute(
                "SELECT company_id FROM dispatchers WHERE id = %s",
                (any_existing["dispatcher_id"],),
            )
            old = conn.fetchone()
            conn.execute(
                "SELECT company_id FROM dispatchers WHERE id = %s",
                (dispatcher_id,),
            )
            new = conn.fetchone()
            old_company = old["company_id"] if old else None
            new_company = new["company_id"] if new else None
            if old_company != new_company:
                raise ValueError(
                    "This Telegram group is already linked to another company; unlink it there first"
                )
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


def create_link_code(dispatcher_id: int, ttl_minutes: int = 30) -> dict:
    """Generate a short single-use code a driver pastes in their group to link
    it to this dispatcher. Expires after ttl_minutes. Keeps the dispatcher's
    API token out of group chat."""
    from datetime import timedelta
    code = secrets.token_hex(3).upper()  # e.g. 'A1B2C3'
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl_minutes)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO telegram_link_codes (code, dispatcher_id, created_at, expires_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (code) DO UPDATE SET dispatcher_id = EXCLUDED.dispatcher_id,
                   created_at = EXCLUDED.created_at, expires_at = EXCLUDED.expires_at""",
            (code, dispatcher_id, now, expires),
        )
    return {"code": code, "expires_at": expires.isoformat(), "ttl_minutes": ttl_minutes}


def consume_link_code(code: str) -> dict | None:
    """Validate and consume a single-use link code, returning the dispatcher
    dict or None. The code is deleted on use and ignored if expired."""
    now = datetime.now(timezone.utc)
    code = (code or "").strip().upper()
    with get_conn() as conn:
        conn.execute(
            "SELECT dispatcher_id FROM telegram_link_codes WHERE code = %s AND expires_at > %s",
            (code, now),
        )
        row = conn.fetchone()
        # Single-use: always remove any matching code, even if expired.
        conn.execute("DELETE FROM telegram_link_codes WHERE code = %s", (code,))
        if not row:
            return None
        conn.execute(
            "SELECT * FROM dispatchers WHERE id = %s AND is_active = TRUE",
            (row["dispatcher_id"],),
        )
        d = conn.fetchone()
        return dict(d) if d else None


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


# ── Real Telegram (MTProto) accounts + transient login state ──────────────────

def get_tg_account(dispatcher_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute("SELECT * FROM telegram_accounts WHERE dispatcher_id = %s", (dispatcher_id,))
        row = conn.fetchone()
        return dict(row) if row else None


def upsert_tg_account(company_id: int, dispatcher_id: int, session_enc: str, me: dict) -> dict:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO telegram_accounts
                 (company_id, dispatcher_id, tg_user_id, phone, username, name, session_enc, is_active, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
               ON CONFLICT (dispatcher_id) DO UPDATE
                 SET tg_user_id = EXCLUDED.tg_user_id, phone = EXCLUDED.phone,
                     username = EXCLUDED.username, name = EXCLUDED.name,
                     session_enc = EXCLUDED.session_enc, is_active = TRUE
               RETURNING *""",
            (company_id, dispatcher_id, me.get("tg_user_id"), me.get("phone"),
             me.get("username"), me.get("name"), session_enc, now),
        )
        return dict(conn.fetchone())


def delete_tg_account(dispatcher_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute("DELETE FROM telegram_accounts WHERE dispatcher_id = %s RETURNING *", (dispatcher_id,))
        row = conn.fetchone()
        return dict(row) if row else None


def create_tg_login(dispatcher_id: int, method: str, *, phone: str | None = None,
                    phone_code_hash: str | None = None, session_enc: str | None = None) -> str:
    login_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        # one pending login per dispatcher; clear stale ones first
        conn.execute("DELETE FROM telegram_logins WHERE dispatcher_id = %s OR created_at < %s",
                     (dispatcher_id, now - timedelta(minutes=10)))
        conn.execute(
            """INSERT INTO telegram_logins (login_id, dispatcher_id, method, phone, phone_code_hash, session_enc, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (login_id, dispatcher_id, method, phone, phone_code_hash, session_enc, now),
        )
    return login_id


def get_tg_login(login_id: str, dispatcher_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM telegram_logins WHERE login_id = %s AND dispatcher_id = %s",
            (login_id, dispatcher_id),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def update_tg_login(login_id: str, *, phone_code_hash: str | None = None, session_enc: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE telegram_logins SET phone_code_hash = COALESCE(%s, phone_code_hash), session_enc = COALESCE(%s, session_enc) WHERE login_id = %s",
            (phone_code_hash, session_enc, login_id),
        )


def delete_tg_login(login_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM telegram_logins WHERE login_id = %s", (login_id,))


# ── Loads ─────────────────────────────────────────────────────────────────────

def create_load(dispatcher_id: int, data: dict, motive_dispatch_id: int | None = None) -> dict | None:
    now = datetime.now(timezone.utc)
    group_id = data.get("group_id")
    # M3: allow the motive dispatch id to be written in the SAME insert (atomic),
    # accepting it either as an explicit arg or inside `data`.
    if motive_dispatch_id is None:
        motive_dispatch_id = data.get("motive_dispatch_id")
    try:
        with get_conn() as conn:
            # M1: a provided group_id must belong to the load owner's company,
            # otherwise a foreign group's driver name / eld_driver_id could be
            # surfaced through the read joins below.
            if group_id is not None:
                conn.execute(
                    """SELECT 1 FROM driver_groups dg
                       JOIN dispatchers d ON dg.dispatcher_id = d.id
                       WHERE dg.id = %s
                         AND d.company_id IS NOT DISTINCT FROM
                             (SELECT company_id FROM dispatchers WHERE id = %s)""",
                    (group_id, dispatcher_id),
                )
                if not conn.fetchone():
                    raise ValueError("Driver group not found")
            conn.execute(
                """INSERT INTO loads
                   (dispatcher_id, group_id, load_number, status,
                    origin_state, destination_state, total_rate_usd, miles,
                    pickup_address, pickup_date, delivery_address, delivery_date,
                    stops_json, current_stop_index, deadhead_miles, broker_name, charge,
                    motive_dispatch_id, created_at)
                   VALUES (%s, %s, %s, 'upcoming', %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    dispatcher_id,
                    group_id,
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
                    motive_dispatch_id,
                    now,
                ),
            )
            row = conn.fetchone()
            load_id = row["id"]
    except psycopg2.IntegrityError:
        # M3: the partial-unique on (dispatcher_id, motive_dispatch_id) fired —
        # this dispatch was already imported (e.g. a concurrent import). Return
        # the existing load instead of crashing.
        if motive_dispatch_id is not None:
            with get_conn() as conn:
                conn.execute(
                    "SELECT id FROM loads WHERE dispatcher_id = %s AND motive_dispatch_id = %s",
                    (dispatcher_id, motive_dispatch_id),
                )
                existing = conn.fetchone()
            if existing:
                return get_load_by_id(existing["id"])
        raise
    return get_load_by_id(load_id)


def get_load(load_id: int, company_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
                    AND dg.dispatcher_id IN (
                        SELECT id FROM dispatchers WHERE company_id IS NOT DISTINCT FROM
                            (SELECT company_id FROM dispatchers WHERE id = l.dispatcher_id))
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
                    AND dg.dispatcher_id IN (
                        SELECT id FROM dispatchers WHERE company_id IS NOT DISTINCT FROM
                            (SELECT company_id FROM dispatchers WHERE id = l.dispatcher_id))
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
                    AND dg.dispatcher_id IN (
                        SELECT id FROM dispatchers WHERE company_id IS NOT DISTINCT FROM
                            (SELECT company_id FROM dispatchers WHERE id = l.dispatcher_id))
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
               "broker_name", "deadhead_miles", "charge", "motive_dispatch_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    # M1: never let a load point at another tenant's driver group.
    if updates.get("group_id") is not None:
        if not get_group(updates["group_id"], company_id):
            raise ValueError("Driver group not found")
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


def get_load_by_motive_dispatch_id(motive_dispatch_id: int, company_id: int) -> dict | None:
    """Motive dispatch_id bo'yicha yukni topish (dublikat import oldini olish)."""
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE l.motive_dispatch_id = %s AND d.company_id = %s""",
            (motive_dispatch_id, company_id),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_loads_with_motive_dispatch(company_id: int) -> list[dict]:
    """Motive bilan sinxronlangan yuklar ro'yxati."""
    with get_conn() as conn:
        conn.execute(
            """SELECT l.*, dg.name as driver_name, dg.eld_driver_id as driver_eld_id,
                      d.name as dispatcher_name
               FROM loads l
               LEFT JOIN driver_groups dg ON l.group_id = dg.id
               LEFT JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE l.motive_dispatch_id IS NOT NULL AND d.company_id = %s
               ORDER BY l.created_at DESC""",
            (company_id,),
        )
        return [dict(r) for r in conn.fetchall()]


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
            f"""SELECT c.id, c.name, c.created_at,
                      (SELECT d2.name FROM dispatchers d2 WHERE d2.company_id = c.id AND d2.role = 'admin' LIMIT 1) as admin_name,
                      (SELECT d2.email FROM dispatchers d2 WHERE d2.company_id = c.id AND d2.role = 'admin' LIMIT 1) as admin_email,
                      COUNT(DISTINCT CASE WHEN d.role = 'user' THEN d.id END) as user_count,
                      COUNT(DISTINCT l.id) as load_count,
                      COALESCE(SUM(CASE WHEN l.status='delivered' THEN {_rate_to_numeric('l.total_rate_usd')} ELSE 0 END), 0) as revenue
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


def company_has_loads(company_id: int) -> bool:
    """True if any dispatcher in the company has created at least one load."""
    with get_conn() as conn:
        conn.execute(
            """SELECT 1 FROM loads l
               JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE d.company_id = %s LIMIT 1""",
            (company_id,),
        )
        return conn.fetchone() is not None


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
            f"SELECT COALESCE(SUM({_rate_to_numeric('total_rate_usd')}), 0) AS total FROM loads WHERE status = 'delivered'"
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
        rows = []
        for r in conn.fetchall():
            d = dict(r)
            # M5: secrets are encrypted at rest; hand the auto-send/poller path
            # the real plaintext key/token.
            if d.get("eld_api_key") is not None:
                d["eld_api_key"] = decrypt_secret(d["eld_api_key"])
            if d.get("eld_provider_token") is not None:
                d["eld_provider_token"] = decrypt_secret(d["eld_provider_token"])
            rows.append(d)
        return rows


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

        gross = parse_money(load.get("total_rate_usd"))
        miles = parse_money(load.get("miles"))
        rpm = gross / miles if miles > 0 else None

        matched_tier = match_tier(gross, rpm, active_tiers)
        load["rpm"] = round(rpm, 2) if rpm is not None else None
        load["earning"] = tier_earning(gross, matched_tier)
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
                    AND dg.dispatcher_id IN (
                        SELECT id FROM dispatchers WHERE company_id IS NOT DISTINCT FROM
                            (SELECT company_id FROM dispatchers WHERE id = l.dispatcher_id))
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
        # The driver group must belong to the caller's company.
        conn.execute(
            """SELECT 1 FROM driver_groups dg JOIN dispatchers d ON dg.dispatcher_id = d.id
               WHERE dg.id = %s AND d.company_id = %s""",
            (driver_group_id, company_id),
        )
        if not conn.fetchone():
            raise ValueError("Driver group not found")
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
    try:
        with get_conn() as conn:
            if load_id:
                # The load must belong to the caller's company (no cross-tenant FK).
                conn.execute(
                    """SELECT 1 FROM loads l JOIN dispatchers d ON l.dispatcher_id = d.id
                       WHERE l.id = %s AND d.company_id = %s""",
                    (load_id, company_id),
                )
                if not conn.fetchone():
                    raise ValueError("Load not found")
                # Fast, friendly dedup check (works even if the partial-unique
                # index was skipped on a dirty DB).
                conn.execute(
                    "SELECT id FROM kpi_entries WHERE load_id = %s AND company_id = %s",
                    (load_id, company_id),
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
    except psycopg2.IntegrityError:
        # M4: a concurrent insert won the partial-unique (company_id, load_id) —
        # surface the same friendly conflict instead of a 500.
        raise ValueError("This load is already assigned to a KPI entry")


def delete_kpi_entry(entry_id: int, company_id: int, dispatcher_id: int | None = None) -> bool:
    """Delete a KPI entry, always scoped to the caller's company so one tenant
    can never delete another tenant's row by guessing its id."""
    with get_conn() as conn:
        if dispatcher_id:
            conn.execute(
                "DELETE FROM kpi_entries WHERE id = %s AND company_id = %s AND dispatcher_id = %s",
                (entry_id, company_id, dispatcher_id),
            )
        else:
            conn.execute(
                "DELETE FROM kpi_entries WHERE id = %s AND company_id = %s",
                (entry_id, company_id),
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
                """SELECT l.id, l.load_number, l.total_rate_usd, l.miles, l.deadhead_miles,
                          l.pickup_date, l.origin_state, l.destination_state, l.broker_name,
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
                """SELECT l.id, l.load_number, l.total_rate_usd, l.miles, l.deadhead_miles,
                          l.pickup_date, l.origin_state, l.destination_state, l.broker_name,
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


# ── Generic CRUD helpers for company-scoped entities ──────────────────────────

# Whitelisted columns per table to prevent SQL injection via field names
_ENTITY_COLUMNS = {
    "customers": ["name", "mc_number", "dot_number", "contact_name", "email", "phone",
                  "address", "credit_score", "payment_terms", "notes"],
    "vendors": ["name", "category", "contact_name", "email", "phone", "address",
                "tax_id", "payment_terms", "notes"],
    "locations": ["name", "address", "city", "state", "zip", "latitude", "longitude",
                  "location_type", "contact_name", "contact_phone", "notes"],
    "trailers": ["unit_number", "trailer_type", "year", "make", "vin", "license_plate",
                 "license_state", "status", "notes"],
    "driver_documents": ["driver_group_id", "doc_type", "doc_number", "issue_date",
                         "expiry_date", "file_path", "notes"],
    "safety_tasks": ["title", "task_type", "driver_group_id", "trailer_id", "due_date",
                     "completed_at", "priority", "status", "notes"],
    "bills": ["vendor_id", "bill_number", "amount", "bill_date", "due_date", "status",
              "category", "notes", "file_path"],
    "transactions": ["tx_date", "tx_type", "amount", "category", "description",
                     "load_id", "bill_id", "driver_group_id", "reference"],
    "work_orders": ["wo_number", "title", "equipment_type", "equipment_ref", "trailer_id",
                    "vendor_id", "opened_at", "closed_at", "cost", "status", "priority",
                    "description"],
    "mailbox_messages": ["dispatcher_id", "source", "from_addr", "to_addr", "subject",
                         "body", "load_id", "is_read", "attachment_path", "received_at"],
    "trucks": ["unit_number", "status", "make", "model", "year", "vin", "license_plate",
               "license_state", "ownership", "odometer", "eld_unit_ref",
               "current_driver_id", "notes"],
    "drivers": ["name", "dba", "status", "driver_type", "truck_id", "phone", "email",
                "cdl_number", "cdl_class", "cdl_state", "cdl_expiry", "medical_expiry",
                "pay_type", "pay_rate", "driver_group_id", "notes"],
    "assignments": ["truck_id", "driver_id", "co_driver_id", "trailer_id", "group_name",
                    "status", "notes"],
    "recruiting_leads": ["name", "phone", "email", "source", "stage", "experience_years",
                         "cdl_class", "notes", "position"],
    "invoices": ["invoice_number", "customer_id", "load_id", "amount", "status",
                 "issue_date", "due_date", "paid_date", "notes"],
}


def _filter_fields(table: str, data: dict) -> dict:
    allowed = set(_ENTITY_COLUMNS.get(table, []))
    # Coerce empty strings to NULL so blank optional date/number inputs don't break
    # typed columns (e.g. cdl_expiry DATE, pay_rate NUMERIC).
    return {k: (None if v == "" else v) for k, v in data.items() if k in allowed}


# L11: cross-tenant FK guard. For each allowlisted entity, the columns below are
# soft INTEGER references to another row that MUST live in the same company, so a
# tenant can't attach its invoice/transaction/assignment to another tenant's
# customer/load/driver/truck by guessing an id. Map: column -> referenced table.
_ENTITY_FKS = {
    "invoices": {"customer_id": "customers", "load_id": "loads"},
    "transactions": {"load_id": "loads", "bill_id": "bills", "driver_group_id": "driver_groups"},
    "drivers": {"truck_id": "trucks", "driver_group_id": "driver_groups"},
    "trucks": {"current_driver_id": "drivers"},
    "assignments": {"truck_id": "trucks", "driver_id": "drivers",
                    "co_driver_id": "drivers", "trailer_id": "trailers"},
    "bills": {"vendor_id": "vendors"},
    "safety_tasks": {"driver_group_id": "driver_groups", "trailer_id": "trailers"},
    "work_orders": {"trailer_id": "trailers", "vendor_id": "vendors"},
    "driver_documents": {"driver_group_id": "driver_groups"},
    "mailbox_messages": {"load_id": "loads", "dispatcher_id": "dispatchers"},
}

# Reference targets that own a company via dispatcher_id rather than a direct
# company_id column.
_FK_VIA_DISPATCHER = {"loads": "l", "driver_groups": "dg"}


def _ref_belongs_to_company(conn, ref_table: str, ref_id, company_id: int) -> bool:
    """True if the referenced row exists and is owned by company_id. ref_table is
    drawn from the hardcoded map above, never user input, so it's safe to inline."""
    if ref_table == "loads":
        conn.execute(
            "SELECT 1 FROM loads l JOIN dispatchers d ON l.dispatcher_id = d.id "
            "WHERE l.id = %s AND d.company_id = %s",
            (ref_id, company_id),
        )
    elif ref_table == "driver_groups":
        conn.execute(
            "SELECT 1 FROM driver_groups dg JOIN dispatchers d ON dg.dispatcher_id = d.id "
            "WHERE dg.id = %s AND d.company_id = %s",
            (ref_id, company_id),
        )
    elif ref_table == "dispatchers":
        conn.execute(
            "SELECT 1 FROM dispatchers WHERE id = %s AND company_id = %s",
            (ref_id, company_id),
        )
    else:
        conn.execute(
            f"SELECT 1 FROM {ref_table} WHERE id = %s AND company_id = %s",
            (ref_id, company_id),
        )
    return conn.fetchone() is not None


def _validate_entity_fks(conn, table: str, fields: dict, company_id: int) -> None:
    fks = _ENTITY_FKS.get(table)
    if not fks:
        return
    for col, ref_table in fks.items():
        ref_id = fields.get(col)
        if ref_id is None:
            continue
        if not _ref_belongs_to_company(conn, ref_table, ref_id, company_id):
            raise ValueError(f"Referenced {col} does not belong to this company")


def list_entities(table: str, company_id: int, limit: int = 500) -> list[dict]:
    if table not in _ENTITY_COLUMNS:
        raise ValueError(f"Unknown table: {table}")
    with get_conn() as conn:
        conn.execute(
            f"SELECT * FROM {table} WHERE company_id = %s ORDER BY id DESC LIMIT %s",
            (company_id, limit),
        )
        return [dict(r) for r in conn.fetchall()]


def get_entity(table: str, entity_id: int, company_id: int) -> dict | None:
    if table not in _ENTITY_COLUMNS:
        raise ValueError(f"Unknown table: {table}")
    with get_conn() as conn:
        conn.execute(
            f"SELECT * FROM {table} WHERE id = %s AND company_id = %s",
            (entity_id, company_id),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def create_entity(table: str, company_id: int, data: dict) -> dict:
    if table not in _ENTITY_COLUMNS:
        raise ValueError(f"Unknown table: {table}")
    fields = _filter_fields(table, data)
    now = datetime.now(timezone.utc)
    cols = ["company_id"] + list(fields.keys()) + ["created_at"]
    vals = [company_id] + list(fields.values()) + [now]
    placeholders = ", ".join(["%s"] * len(vals))
    col_list = ", ".join(cols)
    with get_conn() as conn:
        _validate_entity_fks(conn, table, fields, company_id)
        conn.execute(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) RETURNING *",
            tuple(vals),
        )
        row = conn.fetchone()
        return dict(row)


def update_entity(table: str, entity_id: int, company_id: int, data: dict) -> dict | None:
    if table not in _ENTITY_COLUMNS:
        raise ValueError(f"Unknown table: {table}")
    fields = _filter_fields(table, data)
    if not fields:
        return get_entity(table, entity_id, company_id)
    setters = ", ".join(f"{k} = %s" for k in fields.keys())
    with get_conn() as conn:
        _validate_entity_fks(conn, table, fields, company_id)
        conn.execute(
            f"UPDATE {table} SET {setters} WHERE id = %s AND company_id = %s RETURNING *",
            (*fields.values(), entity_id, company_id),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def delete_entity(table: str, entity_id: int, company_id: int) -> bool:
    if table not in _ENTITY_COLUMNS:
        raise ValueError(f"Unknown table: {table}")
    with get_conn() as conn:
        conn.execute(
            f"DELETE FROM {table} WHERE id = %s AND company_id = %s",
            (entity_id, company_id),
        )
        return conn.rowcount > 0


# ── Email accounts (Relay) + inbound email ────────────────────────────────────

def create_email_account(company_id: int, relay_account_id: str, provider: str, email: str) -> dict:
    """Link a Relay-connected mailbox to a company (idempotent on relay_account_id).

    H2: a relay_account_id that already belongs to a DIFFERENT company must not be
    silently transferred (that would be a cross-tenant mailbox hijack). Same-company
    re-link/upsert stays allowed.
    """
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "SELECT company_id FROM email_accounts WHERE relay_account_id = %s",
            (relay_account_id,),
        )
        existing = conn.fetchone()
        if existing and existing["company_id"] != company_id:
            raise ValueError("relay account already owned by another company")
        conn.execute(
            """INSERT INTO email_accounts (company_id, relay_account_id, provider, email, is_active, created_at)
               VALUES (%s, %s, %s, %s, TRUE, %s)
               ON CONFLICT (relay_account_id) DO UPDATE
                 SET company_id = EXCLUDED.company_id, provider = EXCLUDED.provider,
                     email = EXCLUDED.email, is_active = TRUE
               RETURNING *""",
            (company_id, relay_account_id, provider, email, now),
        )
        return dict(conn.fetchone())


def list_email_accounts(company_id: int) -> list[dict]:
    with get_conn() as conn:
        conn.execute(
            "SELECT * FROM email_accounts WHERE company_id = %s ORDER BY id DESC",
            (company_id,),
        )
        return [dict(r) for r in conn.fetchall()]


def list_all_active_email_accounts() -> list[dict]:
    """Every active connected mailbox across all companies (for the poller)."""
    with get_conn() as conn:
        conn.execute("SELECT * FROM email_accounts WHERE is_active = TRUE ORDER BY id")
        return [dict(r) for r in conn.fetchall()]


def get_email_account(account_id: int, company_id: int | None = None) -> dict | None:
    with get_conn() as conn:
        if company_id is None:
            conn.execute("SELECT * FROM email_accounts WHERE id = %s", (account_id,))
        else:
            conn.execute(
                "SELECT * FROM email_accounts WHERE id = %s AND company_id = %s",
                (account_id, company_id),
            )
        row = conn.fetchone()
        return dict(row) if row else None


def update_email_account_polled(account_id: int) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            "UPDATE email_accounts SET last_polled_at = %s WHERE id = %s",
            (now, account_id),
        )


def delete_email_account(account_id: int, company_id: int) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM email_accounts WHERE id = %s AND company_id = %s RETURNING *",
            (account_id, company_id),
        )
        row = conn.fetchone()
        return dict(row) if row else None


def insert_inbound_email(
    company_id: int,
    account_id: int,
    *,
    external_id: str,
    from_addr: str | None,
    to_addr: str | None,
    subject: str | None,
    body: str | None,
    thread_id: str | None,
    rfc_message_id: str | None,
    attachments_json: str | None,
    received_at,
) -> bool:
    """Store an inbound email. Returns False if already stored (dedup by external_id)."""
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mailbox_messages
                 (company_id, account_id, source, from_addr, to_addr, subject, body,
                  thread_id, rfc_message_id, external_id, attachments_json, is_read,
                  received_at, created_at)
               VALUES (%s, %s, 'email', %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s)
               ON CONFLICT (company_id, external_id) WHERE external_id IS NOT NULL
                 DO NOTHING
               RETURNING id""",
            (company_id, account_id, from_addr, to_addr, subject, body,
             thread_id, rfc_message_id, external_id, attachments_json, received_at, now),
        )
        return conn.fetchone() is not None


def mailbox_external_exists(company_id: int, external_id: str) -> bool:
    """Cheap dedup check so the poller skips already-stored messages."""
    with get_conn() as conn:
        conn.execute(
            "SELECT 1 FROM mailbox_messages WHERE company_id = %s AND external_id = %s LIMIT 1",
            (company_id, external_id),
        )
        return conn.fetchone() is not None


# ── Dashboard summary ─────────────────────────────────────────────────────────

def dashboard_summary(company_id: int) -> dict:
    """Aggregate metrics for the dashboard landing page."""
    with get_conn() as conn:
        conn.execute(
            f"""SELECT
                 COUNT(*) FILTER (WHERE status = 'upcoming')   AS upcoming_loads,
                 COUNT(*) FILTER (WHERE status = 'dispatched') AS active_loads,
                 COUNT(*) FILTER (WHERE status = 'delivered')  AS delivered_loads,
                 COUNT(*)                                       AS total_loads,
                 COALESCE(SUM({_rate_to_numeric('total_rate_usd')}) FILTER (WHERE status = 'delivered'), 0) AS total_revenue
               FROM loads l
               JOIN dispatchers d ON l.dispatcher_id = d.id
               WHERE d.company_id = %s""",
            (company_id,),
        )
        loads_row = conn.fetchone() or {}

        conn.execute(
            """SELECT COUNT(*) AS cnt FROM driver_groups dg
               JOIN dispatchers d ON dg.dispatcher_id = d.id
               WHERE d.company_id = %s""",
            (company_id,),
        )
        drivers_row = conn.fetchone() or {}

        counts = {}
        for tbl in ("customers", "vendors", "trailers", "locations"):
            conn.execute(f"SELECT COUNT(*) AS cnt FROM {tbl} WHERE company_id = %s", (company_id,))
            counts[tbl] = (conn.fetchone() or {}).get("cnt", 0)

        conn.execute(
            "SELECT COUNT(*) AS cnt FROM mailbox_messages WHERE company_id = %s AND is_read = FALSE",
            (company_id,),
        )
        unread = (conn.fetchone() or {}).get("cnt", 0)

        conn.execute(
            """SELECT COUNT(*) AS cnt FROM safety_tasks
               WHERE company_id = %s AND status = 'open'""",
            (company_id,),
        )
        open_safety = (conn.fetchone() or {}).get("cnt", 0)

        conn.execute(
            """SELECT COUNT(*) AS cnt FROM driver_documents
               WHERE company_id = %s AND expiry_date IS NOT NULL
                 AND expiry_date <= (CURRENT_DATE + INTERVAL '30 days')""",
            (company_id,),
        )
        expiring_docs = (conn.fetchone() or {}).get("cnt", 0)

        return {
            "upcoming_loads": int(loads_row.get("upcoming_loads", 0) or 0),
            "active_loads": int(loads_row.get("active_loads", 0) or 0),
            "delivered_loads": int(loads_row.get("delivered_loads", 0) or 0),
            "total_loads": int(loads_row.get("total_loads", 0) or 0),
            "total_revenue": float(loads_row.get("total_revenue", 0) or 0),
            "drivers": int(drivers_row.get("cnt", 0) or 0),
            "customers": int(counts.get("customers", 0)),
            "vendors": int(counts.get("vendors", 0)),
            "trailers": int(counts.get("trailers", 0)),
            "locations": int(counts.get("locations", 0)),
            "unread_mailbox": int(unread or 0),
            "open_safety_tasks": int(open_safety or 0),
            "expiring_documents": int(expiring_docs or 0),
        }
