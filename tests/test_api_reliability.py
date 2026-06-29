import os
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import database


def _db_available() -> bool:
    """True if the configured Postgres can actually be reached. Lets the suite
    COLLECT (and the pure-unit tests still run) when no DATABASE_URL / DB is
    present, instead of erroring out in setUpClass."""
    try:
        with database.get_conn() as conn:
            conn.execute("SELECT 1")
            conn.fetchone()
        return True
    except Exception:
        return False


# Evaluated once at import so the DB-backed classes can be skipped cleanly.
_DB_OK = _db_available()
_DB_REASON = "requires a reachable Postgres (DATABASE_URL)"


@unittest.skipUnless(_DB_OK, _DB_REASON)
class ApiReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DISABLE_BOT_POLLER"] = "1"
        os.environ["DISABLE_TRUCK_PREFETCH"] = "1"
        # Tests run against the configured Postgres (database.py is PG-only).
        # Use a unique suffix per run + delete the company in teardown so the
        # suite is idempotent and leaves no rows behind.
        database.init_db()
        database.run_migrations()
        cls.suffix = uuid.uuid4().hex[:8]
        cls.user_email = f"usera-{cls.suffix}@testco.local"
        cls.company = database.create_company(f"Test Co {cls.suffix}")
        cls.admin = database.create_company_user(
            cls.company["id"], "Admin", f"admin-{cls.suffix}@testco.local", "secret123", role="admin"
        )
        # Auth now uses expiring server-side sessions, not the legacy
        # dispatchers.token column.
        cls.session_token = database.create_session(cls.admin["id"])
        cls.auth = {"Authorization": f"Bearer {cls.session_token}"}
        cls.client = TestClient(app_module.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        # Remove everything this run created. dispatchers.company_id is ON DELETE
        # SET NULL, so delete the dispatchers explicitly first (sessions cascade
        # from the dispatcher), then the company.
        try:
            with database.get_conn() as conn:
                conn.execute("DELETE FROM dispatchers WHERE company_id = %s", (cls.company["id"],))
                conn.execute("DELETE FROM companies WHERE id = %s", (cls.company["id"],))
        except Exception:
            pass

    def test_health_endpoint(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get("ok"))
        # L5: the anonymous health probe must expose only liveness + a timestamp.
        # Internal recon data ("checks": cache sizes, poller flags, ELD counts) is
        # now gated behind superadmin auth and must NOT leak to anonymous callers.
        self.assertIn("ts", data)
        self.assertNotIn("checks", data)

    def test_company_user_create_writes_audit_log(self):
        r = self.client.post(
            "/api/company/users",
            headers=self.auth,
            json={"name": "User A", "email": self.user_email, "password": "secret123"},
        )
        self.assertEqual(r.status_code, 200)

        logs = self.client.get("/api/audit/logs?limit=20", headers=self.auth)
        self.assertEqual(logs.status_code, 200)
        actions = [x.get("action") for x in logs.json().get("logs", [])]
        self.assertIn("company_user.create", actions)

    def test_delete_missing_eld_config_returns_404(self):
        r = self.client.delete("/api/eld/config?config_id=999999", headers=self.auth)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json().get("detail"), "ELD config not found")

    def test_eld_rate_limit_on_map_endpoint(self):
        app_module._eld_call_windows.clear()
        with patch.object(app_module, "_get_dispatcher_trucks", return_value=[]):
            for _ in range(app_module.ELD_RATE_LIMIT_MAX_CALLS):
                ok = self.client.get("/api/map/locations", headers=self.auth)
                self.assertEqual(ok.status_code, 200)
            limited = self.client.get("/api/map/locations", headers=self.auth)
            self.assertEqual(limited.status_code, 429)

    def test_legacy_token_rejected(self):
        # The old never-expiring dispatchers.token must no longer authenticate.
        bad = {"Authorization": f"Bearer {self.admin['token']}"}
        r = self.client.get("/api/me", headers=bad)
        self.assertEqual(r.status_code, 401)

    def test_logout_revokes_session(self):
        tok = database.create_session(self.admin["id"])
        h = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.client.get("/api/me", headers=h).status_code, 200)
        self.assertEqual(self.client.post("/api/logout", headers=h).status_code, 200)
        self.assertEqual(self.client.get("/api/me", headers=h).status_code, 401)

    def test_password_hashed_with_argon2(self):
        d = database.get_dispatcher_by_id(self.admin["id"])
        self.assertTrue(d["password_hash"].startswith("$argon2"))
        self.assertTrue(database.verify_password("secret123", d["password_hash"]))

    def test_register_rejects_short_password(self):
        # Short password is rejected (400) before any company/user is created.
        r = self.client.post(
            "/api/register",
            json={"company_name": "X Co", "name": "X", "email": f"x-{self.suffix}@x.local", "password": "short"},
        )
        self.assertEqual(r.status_code, 400)


@unittest.skipUnless(_DB_OK, _DB_REASON)
class MultiTenantIsolationTests(unittest.TestCase):
    """L7: cross-tenant (IDOR) regression guard. Two companies are created and we
    assert company A can neither read, modify, nor delete company B's loads, KPI
    entries, or generic /api/entities rows. The positive (company B can touch its
    own row) checks prove the 403/404s are about TENANCY, not a missing row."""

    WEEK = "2026-01-05"  # a Monday; fixed so all-entries lookups are deterministic

    @classmethod
    def setUpClass(cls):
        os.environ["DISABLE_BOT_POLLER"] = "1"
        os.environ["DISABLE_TRUCK_PREFETCH"] = "1"
        database.init_db()
        database.run_migrations()
        cls.suffix = uuid.uuid4().hex[:8]

        # ── Company A (the attacker) ────────────────────────────────────────
        cls.company_a = database.create_company(f"Tenant A {cls.suffix}")
        cls.admin_a = database.create_company_user(
            cls.company_a["id"], "Admin A", f"admin-a-{cls.suffix}@iso.local", "secret123", role="admin"
        )
        cls.auth_a = {"Authorization": f"Bearer {database.create_session(cls.admin_a['id'])}"}

        # ── Company B (the victim) + its private data ───────────────────────
        cls.company_b = database.create_company(f"Tenant B {cls.suffix}")
        cls.admin_b = database.create_company_user(
            cls.company_b["id"], "Admin B", f"admin-b-{cls.suffix}@iso.local", "secret123", role="admin"
        )
        cls.auth_b = {"Authorization": f"Bearer {database.create_session(cls.admin_b['id'])}"}

        cls.load_b = database.create_load(cls.admin_b["id"], {
            "load_number": f"B-{cls.suffix}",
            "broker_name": "Broker B",
            "origin_state": "TX",
            "destination_state": "CA",
            "total_rate_usd": "$2,500.00",
            "miles": "500",
        })
        cls.kpi_b = database.add_kpi_entry(
            cls.company_b["id"], cls.admin_b["id"], cls.WEEK,
            cls.load_b["id"], 500.0, 2500.0,
        )
        cls.customer_b = database.create_entity(
            "customers", cls.company_b["id"], {"name": f"Customer B {cls.suffix}"}
        )
        cls.client = TestClient(app_module.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        try:
            with database.get_conn() as conn:
                for cid in (cls.company_a["id"], cls.company_b["id"]):
                    # FK order: children that reference loads/dispatchers first.
                    conn.execute("DELETE FROM kpi_entries WHERE company_id = %s", (cid,))
                    conn.execute("DELETE FROM customers WHERE company_id = %s", (cid,))
                    conn.execute(
                        "DELETE FROM loads WHERE dispatcher_id IN "
                        "(SELECT id FROM dispatchers WHERE company_id = %s)",
                        (cid,),
                    )
                    conn.execute("DELETE FROM dispatchers WHERE company_id = %s", (cid,))
                    conn.execute("DELETE FROM companies WHERE id = %s", (cid,))
        except Exception:
            pass

    # ── Loads ───────────────────────────────────────────────────────────────
    def test_loads_list_excludes_other_company(self):
        r = self.client.get("/api/loads", headers=self.auth_a)
        self.assertEqual(r.status_code, 200)
        ids = [l["id"] for l in r.json().get("loads", [])]
        self.assertNotIn(self.load_b["id"], ids)
        # Sanity: company B does see its own load.
        rb = self.client.get("/api/loads", headers=self.auth_b)
        self.assertIn(self.load_b["id"], [l["id"] for l in rb.json().get("loads", [])])

    def test_cannot_edit_other_company_load(self):
        r = self.client.put(
            f"/api/loads/{self.load_b['id']}",
            headers=self.auth_a,
            json={"load_number": "HACKED"},
        )
        self.assertEqual(r.status_code, 404)
        # The victim's load_number must be unchanged.
        self.assertEqual(database.get_load_by_id(self.load_b["id"])["load_number"], f"B-{self.suffix}")

    def test_cannot_change_other_company_load_status(self):
        r = self.client.patch(
            f"/api/loads/{self.load_b['id']}/status",
            headers=self.auth_a,
            json={"status": "delivered"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(database.get_load_by_id(self.load_b["id"])["status"], "upcoming")

    def test_cannot_delete_other_company_load(self):
        r = self.client.delete(f"/api/loads/{self.load_b['id']}", headers=self.auth_a)
        self.assertEqual(r.status_code, 404)
        # Load must still exist after the cross-tenant delete attempt.
        self.assertIsNotNone(database.get_load_by_id(self.load_b["id"]))

    # ── KPI entries ─────────────────────────────────────────────────────────
    def test_cannot_delete_other_company_kpi_entry(self):
        r = self.client.delete(f"/api/kpi/entry/{self.kpi_b['id']}", headers=self.auth_a)
        self.assertEqual(r.status_code, 404)

    def test_kpi_all_entries_excludes_other_company(self):
        r = self.client.get(f"/api/kpi/all-entries?week_start={self.WEEK}", headers=self.auth_a)
        self.assertEqual(r.status_code, 200)
        a_entry_ids = {
            e.get("id")
            for disp in r.json().get("dispatchers", [])
            for e in disp.get("entries", [])
        }
        self.assertNotIn(self.kpi_b["id"], a_entry_ids)
        # Sanity: company B's own report does include it.
        rb = self.client.get(f"/api/kpi/all-entries?week_start={self.WEEK}", headers=self.auth_b)
        b_entry_ids = {
            e.get("id")
            for disp in rb.json().get("dispatchers", [])
            for e in disp.get("entries", [])
        }
        self.assertIn(self.kpi_b["id"], b_entry_ids)

    # ── Generic entities (/api/entities/{slug}) ─────────────────────────────
    def test_entities_list_excludes_other_company(self):
        r = self.client.get("/api/entities/customers", headers=self.auth_a)
        self.assertEqual(r.status_code, 200)
        ids = [x.get("id") for x in r.json().get("items", [])]
        self.assertNotIn(self.customer_b["id"], ids)

    def test_cannot_read_other_company_entity(self):
        r = self.client.get(f"/api/entities/customers/{self.customer_b['id']}", headers=self.auth_a)
        self.assertEqual(r.status_code, 404)
        # Sanity: company B can read its own row.
        rb = self.client.get(f"/api/entities/customers/{self.customer_b['id']}", headers=self.auth_b)
        self.assertEqual(rb.status_code, 200)

    def test_cannot_update_other_company_entity(self):
        r = self.client.patch(
            f"/api/entities/customers/{self.customer_b['id']}",
            headers=self.auth_a,
            json={"name": "HACKED"},
        )
        self.assertEqual(r.status_code, 404)
        row = database.get_entity("customers", self.customer_b["id"], self.company_b["id"])
        self.assertEqual(row["name"], f"Customer B {self.suffix}")

    def test_cannot_delete_other_company_entity(self):
        r = self.client.delete(f"/api/entities/customers/{self.customer_b['id']}", headers=self.auth_a)
        self.assertEqual(r.status_code, 404)
        self.assertIsNotNone(
            database.get_entity("customers", self.customer_b["id"], self.company_b["id"])
        )


class MoneyMathTests(unittest.TestCase):
    """L7: pure-unit money math — no DB connection required, so these run even
    when DATABASE_URL is absent."""

    def test_parse_money_variants(self):
        self.assertEqual(database.parse_money("$2,500.00"), 2500.0)
        self.assertEqual(database.parse_money("$2,500 + FSC"), 2500.0)
        self.assertEqual(database.parse_money(""), 0.0)
        self.assertEqual(database.parse_money("1,234"), 1234.0)
        self.assertEqual(database.parse_money(None), 0.0)
        self.assertEqual(database.parse_money("no number here"), 0.0)
        # Numeric pass-through.
        self.assertEqual(database.parse_money(1675), 1675.0)
        self.assertEqual(database.parse_money(1675.5), 1675.5)

    def test_match_tier_selects_highest_bracket(self):
        tiers = [
            {"min_gross": 0, "max_gross": 5000, "min_rpm": 0, "percentage": 5},
            {"min_gross": 5000, "max_gross": 10000, "min_rpm": 0, "percentage": 8},
            {"min_gross": 10000, "max_gross": None, "min_rpm": 2.0, "percentage": 10},
        ]
        self.assertEqual(database.match_tier(3000, 2.0, tiers)["percentage"], 5)
        # rpm unknown (None) does not enforce the RPM floor.
        self.assertEqual(database.match_tier(6000, None, tiers)["percentage"], 8)
        self.assertEqual(database.match_tier(12000, 2.5, tiers)["percentage"], 10)
        # Top bracket fails its RPM floor and lower brackets are out of range ->
        # None (caller must surface this, not silently pay $0).
        self.assertIsNone(database.match_tier(12000, 1.5, tiers))

    def test_match_tier_rpm_floor(self):
        single = [{"min_gross": 0, "max_gross": None, "min_rpm": 2.0, "percentage": 10}]
        self.assertIsNone(database.match_tier(10000, 1.0, single))      # below floor
        self.assertIsNotNone(database.match_tier(10000, None, single))  # unknown rpm ok
        self.assertIsNotNone(database.match_tier(10000, 3.0, single))   # above floor

    def test_tier_earning_commission(self):
        self.assertEqual(database.tier_earning(3000, {"percentage": 5}), 150.0)
        self.assertEqual(database.tier_earning(6000, {"percentage": 8}), 480.0)
        self.assertEqual(database.tier_earning(12000, {"percentage": 10}), 1200.0)
        # No tier matched -> zero commission.
        self.assertEqual(database.tier_earning(12000, None), 0.0)
        # Decimal rounding (ROUND_HALF_UP at the cent).
        self.assertEqual(database.tier_earning(1000.50, {"percentage": 7.5}), 75.04)


class InvoiceRenderTests(unittest.TestCase):
    """L7 / M12 / M13: the invoice generator only depends on database.parse_money
    (pure), so it can be rendered without a DB. We spy on the Paragraph factory to
    read back exactly what text the PDF is built from."""

    def setUp(self):
        try:
            import invoice  # noqa: F401
        except Exception as exc:  # pragma: no cover - reportlab missing
            self.skipTest(f"invoice/reportlab unavailable: {exc}")

    def _render_and_capture(self, load, company, number="INV-1"):
        import invoice
        captured = []
        real_paragraph = invoice.Paragraph

        def spy(text, style):
            captured.append(str(text))
            return real_paragraph(text, style)

        with patch.object(invoice, "Paragraph", spy):
            pdf = invoice.generate_invoice(load, company, number)
        return pdf, captured

    def test_invoice_uses_broker_name_and_no_doubled_dollar(self):
        load = {
            "load_number": "L-100",
            "broker_name": "ACME Brokerage LLC",   # M13: must use broker_name, not 'broker'
            "total_rate_usd": "$2,500.00",         # already has a '$' -> must not double
            "charge": "$250.00",
            "origin_state": "TX",
            "destination_state": "CA",
            "miles": "500",
        }
        company = {"company_name": "My Carrier", "payment_terms": "Net 30"}
        pdf, captured = self._render_and_capture(load, company)

        # It produced a real PDF.
        self.assertTrue(pdf.startswith(b"%PDF"))
        # M13: BILL TO shows the broker_name, not the "Broker / Shipper" placeholder.
        self.assertTrue(
            any("ACME Brokerage LLC" in t for t in captured),
            "BILL TO did not render broker_name",
        )
        self.assertFalse(any("Broker / Shipper" in t for t in captured))
        # M12: amounts are formatted exactly once -> never a doubled '$$'.
        self.assertFalse(any("$$" in t for t in captured), f"doubled dollar sign in {captured}")
        # rate ($2,500) + charge ($250) -> $2,750.00 total, rendered once.
        self.assertTrue(any("$2,750.00" in t for t in captured), f"missing/incorrect total in {captured}")

    def test_money_formatter_normalizes_once(self):
        import invoice
        self.assertEqual(invoice._money("$1,675.00"), "$1,675.00")
        self.assertEqual(invoice._money(1675), "$1,675.00")
        self.assertEqual(invoice._money("$2,500 + FSC"), "$2,500.00")
        self.assertEqual(invoice._money(""), "$0.00")

    def test_invoice_falls_back_when_no_broker(self):
        load = {"load_number": "L-1", "total_rate_usd": "1000", "miles": "10"}
        company = {"company_name": "My Carrier"}
        _pdf, captured = self._render_and_capture(load, company)
        self.assertTrue(any("Broker / Shipper" in t for t in captured))


class BotCommandNormalizationTests(unittest.TestCase):
    """L7: the Telegram command parser strips a trailing @botname and lowercases,
    then matches exactly. The /help branch touches no DB, so we can exercise the
    real normalization with _reply mocked."""

    def setUp(self):
        try:
            import bot_poller  # noqa: F401
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"bot_poller unavailable: {exc}")

    def _run(self, text):
        import bot_poller
        replies = []
        with patch.object(bot_poller, "_reply", lambda message, t: replies.append(t)):
            bot_poller._handle_update({"message": {"text": text, "chat": {"id": 1}}})
        return replies

    def test_help_command_variants_normalize(self):
        # Trailing @botname is stripped and case is ignored.
        self.assertTrue(any("/delivered" in r for r in self._run("/Help@SomeBot")))
        self.assertTrue(any("/delivered" in r for r in self._run("/HELP")))

    def test_unknown_or_suffixed_command_does_not_fire(self):
        # "/helpxyz" must NOT match "/help" (exact match after normalization).
        self.assertEqual(self._run("/helpxyz"), [])
        # Plain (non-slash) text is ignored entirely.
        self.assertEqual(self._run("hello world"), [])


if __name__ == "__main__":
    unittest.main()
