import os
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import database


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
        self.assertIn("checks", data)

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


if __name__ == "__main__":
    unittest.main()
