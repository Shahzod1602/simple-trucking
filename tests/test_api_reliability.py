import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import database


class ApiReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DISABLE_BOT_POLLER"] = "1"
        os.environ["DISABLE_TRUCK_PREFETCH"] = "1"
        cls._tmp = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(cls._tmp.name, "test.db")
        database.init_db()

        company = database.create_company("Test Co")
        cls.admin = database.create_company_user(
            company["id"], "Admin", "admin@testco.local", "secret123", role="admin"
        )
        cls.auth = {"Authorization": f"Bearer {cls.admin['token']}"}
        cls.client = TestClient(app_module.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls._tmp.cleanup()

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
            json={"name": "User A", "email": "usera@testco.local", "password": "secret123"},
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


if __name__ == "__main__":
    unittest.main()
