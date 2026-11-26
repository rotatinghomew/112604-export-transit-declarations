"""HTTP/JSON 适配层冒烟测试：起在随机端口，验证查询与办理动作。"""

from __future__ import annotations

import json
from http.client import HTTPConnection
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"

from yardflow.server import create_server  # noqa: E402


class HttpServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = create_server(SAMPLES, host="127.0.0.1", port=0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, body

    def _post(self, payload):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/action", body=json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, body

    def test_health_and_report_endpoints(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, body = self._get(
            "/api/releasable?at=2026-11-27T02:05:00Z")
        self.assertEqual(status, 200)
        self.assertEqual(body["vins"], ["V311"])

        status, body = self._get(
            "/api/line/A-700/L1?at=2026-11-27T02:05:00Z")
        self.assertEqual(status, 200)
        self.assertEqual(body["composition"], ["V310", "V311"])
        self.assertEqual(body["vehicles"][0]["follow_up"]["owner"],
                         "customs_affairs")

    def test_unsupported_after_expiry(self):
        status, body = self._get(
            "/api/unsupported?at=2026-11-27T03:20:00Z")
        self.assertEqual(status, 200)
        self.assertEqual([u["vin"] for u in body["items"]], ["V311"])

    def test_post_action_and_rule_violation_status(self):
        status, body = self._post({"action": "confirm_materials", "vin": "GHOST"})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "unknown_vin")

        status, body = self._post({"action": "release_hold", "vin": "V313"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, body = self._post({"action": "not_a_method"})
        self.assertEqual(status, 400)

    def test_index_html(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        text = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("整车", text)


if __name__ == "__main__":
    unittest.main()
