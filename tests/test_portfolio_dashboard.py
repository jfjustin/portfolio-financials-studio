"""Integration checks for the independent browser dashboard.

Run: python tests/test_portfolio_dashboard.py
Always uses a throwaway Studio database, never a real deployment's data.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="pfs-dashboard-test-"),
                  DATABASE_URL="", AUTH_MODE="none", LLM_PROVIDER="none",
                  LAKE_BACKEND="none", LAKE_AUTO_SYNC="false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings


class DashboardTest(unittest.TestCase):
    def test_routes_and_isolation(self):
        with TestClient(app) as client:
            before = client.get("/api/submissions").json()
            studio = client.get("/")
            self.assertEqual(studio.status_code, 200)
            self.assertIn('id="nav-list"', studio.text)
            self.assertIn('id="clear-btn"', studio.text)
            self.assertIn('aria-label="Studio pages"', studio.text)
            self.assertNotIn('portfolio-dashboard/dashboard.js', studio.text)
            dashboard = client.get("/portfolio-dashboard")
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn('id="portfolio-dashboard-root"', dashboard.text)
            self.assertIn('private, no-store', dashboard.headers['cache-control'])
            self.assertNotIn('js/app.js', dashboard.text)
            self.assertNotIn('id="clear-btn"', dashboard.text)
            self.assertEqual(client.get("/api/submissions").json(), before)
            self.assertEqual(client.get("/static/portfolio-dashboard/dashboard.js").status_code, 200)

    def test_optional_snapshot_is_escaped(self):
        path = settings.data_dir / 'portfolio-dashboard.json'
        try:
            path.write_text(json.dumps({'filename': '</script><img src=x onerror=alert(1)>'}))
            with TestClient(app) as client:
                response = client.get('/portfolio-dashboard')
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('</script><img', response.text)
                self.assertIn(r'\u003c/script\u003e', response.text)
            path.write_text('{bad JSON')
            with TestClient(app) as client:
                self.assertEqual(client.get('/portfolio-dashboard').status_code, 200)
        finally:
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
