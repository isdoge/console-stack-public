# -*- coding: utf-8 -*-
"""Regression tests for the production CORS allowlist."""
import os
import pathlib
import sys
import unittest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["CONSOLE_AUTH_ENABLED"] = "false"
os.environ["CONSOLE_CORS_ORIGINS"] = "http://localhost:3000"

import app as console_app  # noqa: E402


class CorsPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = console_app.app.test_client()

    def test_allowed_origin_is_returned(self):
        response = self.client.get(
            "/api/health",
            headers={"Origin": "http://localhost:3000"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"),
            "http://localhost:3000",
        )
        self.assertEqual(response.headers.get("Vary"), "Origin")

    def test_disallowed_origin_is_not_returned(self):
        response = self.client.get(
            "/api/health",
            headers={"Origin": "https://evil.example"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertNotIn("Access-Control-Allow-Credentials", response.headers)

    def test_disallowed_preflight_is_not_returned(self):
        response = self.client.options(
            "/api/health",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_retired_oplog_callback_variants_return_gone(self):
        for path in ("/api/oplog", "/api/oplog/", "/api/oplog/legacy"):
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    data={"host": "nas", "svc": "test", "act": "restart"},
                )
                self.assertEqual(response.status_code, 410)

    def test_retired_trigger_variants_return_gone(self):
        for path in ("/api/trigger", "/api/trigger/", "/api/trigger/legacy"):
            with self.subTest(path=path):
                response = self.client.post(path, json={"action_id": "legacy"})
                self.assertEqual(response.status_code, 410)

    def test_retired_actions_page_variants_return_gone(self):
        for path in ("/actions", "/actions/", "/actions/legacy"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 410)


if __name__ == "__main__":
    unittest.main()
