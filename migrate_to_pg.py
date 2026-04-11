"""
One-time migration script: SQLite -> PostgreSQL.
Run inside the Docker container after PostgreSQL is up:
    docker exec ratecon_app python migrate_to_pg.py
"""
import os
import sqlite3
import psycopg2
import psycopg2.extras

SQLITE_PATH = os.environ.get("SQLITE_PATH", "ratecon.db")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://ratecon:ratecon_secret@db:5432/ratecon",
)

TABLES_IN_ORDER = [
    "companies",
    "dispatchers",
    "driver_groups",
    "eld_configs",
    "loads",
    "company_info",
    "global_settings",
    "load_pods",
    "pay_tiers",
    "audit_logs",
    "schema_migrations",
]

# Columns that were INTEGER booleans in SQLite -> BOOLEAN in PG
BOOL_COLUMNS = {
    "dispatchers": {"is_active"},
    "loads": {"eta_alert_sent"},
}


def migrate():
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg2.connect(DATABASE_URL)
    pg_cur = pg_conn.cursor()

    for table in TABLES_IN_ORDER:
        rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"  {table}: 0 rows (skip)")
            continue

        columns = rows[0].keys()
        bool_cols = BOOL_COLUMNS.get(table, set())

        placeholders = ", ".join(["%s"] * len(columns))
        col_list = ", ".join(columns)
        insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

        count = 0
        for row in rows:
            values = []
            for col in columns:
                val = row[col]
                if col in bool_cols and val is not None:
                    val = bool(val)
                values.append(val)
            try:
                pg_cur.execute(insert_sql, values)
                count += 1
            except Exception as e:
                pg_conn.rollback()
                print(f"  {table}: error on row {dict(row)}: {e}")
                continue

        pg_conn.commit()
        print(f"  {table}: {count} rows migrated")

        # Reset sequence for tables with SERIAL id
        if "id" in columns and table not in ("company_info", "global_settings", "schema_migrations"):
            pg_cur.execute(f"SELECT setval('{table}_id_seq', COALESCE((SELECT MAX(id) FROM {table}), 1))")
            pg_conn.commit()

    pg_cur.close()
    pg_conn.close()
    sqlite_conn.close()
    print("\nMigration complete!")


if __name__ == "__main__":
    print(f"Migrating from {SQLITE_PATH} to PostgreSQL...")
    migrate()
