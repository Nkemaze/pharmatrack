"""End-to-end tests for the pharmacy self-registration + admin approval flow.

The hosted flow is exercised on SQLite by setting PHARMATRACK_HOSTED=1
(so it does not need a live PostgreSQL server to test the web routes).

Covers:
- Registering a pharmacy creates a pending application (no auto-login).
- Pending / suspended / rejected accounts are blocked from both the web
  login and the JSON API login.
- Admin approval unlocks the account; suspending re-locks it.
- Non-active tenants stay hidden from the public API.
"""

import os
import tempfile
import unittest

_TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["PHARMATRACK_DB_PATH"] = os.path.join(_TEST_DIRECTORY.name, "test-registration.db")
os.environ["JWT_SECRET_KEY"] = "test-only-jwt-secret-for-registration-flow"
os.environ["PHARMATRACK_HOSTED"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("PHARMATRACK_ENV", None)

from app import app
from database.db import get_db_connection
from database.queries import (
    create_pharmacy_registration, create_user, update_pharmacy_status,
    delete_pharmacy_application, get_pharmacies_with_applicant,
    get_pending_pharmacy_count, login_status_block,
)


class RegistrationFlowTest(unittest.TestCase):
    password = "Correct-Horse-Battery-9"

    def setUp(self):
        self.client = app.test_client()
        self._cleanup()
        # The platform administrator (no pharmacy tenant).
        create_user("PlatformAdmin", "admin", self.password)

    def _cleanup(self):
        conn = get_db_connection()
        try:
            # Deleting the tenant + user tables may reference rows created by
            # other suites (loss_report, stock_movement, ...) that survived
            # their own setUps, so drop the FK guard for this cleanup only.
            conn.execute("PRAGMA foreign_keys = OFF")
            for table in ("token_blocklist", "login_attempt", "loss_report",
                          "stock_movement", "product_batch", "product",
                          "settings", "user", "pharmacy"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()

    def register_pharmacy(self, name="CityCare Pharmacy", email="citycare@example.com"):
        response = self.client.post(
            "/register",
            data={
                "role": "pharmacy",
                "pharmacy_name": name,
                "email": email,
                "address": "12 Aminu Kano Crescent, Wuse II",
                "city": "Abuja",
                "phone": "0803 111 1111",
                "password": self.password,
                "confirm_password": self.password,
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn(b"awaiting administrator approval", response.data)
        return response

    def web_login(self, name, password=None):
        return self.client.post(
            "/login",
            data={"name": name, "password": password or self.password},
        )

    def api_login(self, name, password=None):
        return self.client.post(
            "/api/v1/auth/login",
            json={"name": name, "password": password or self.password},
        )

    def admin_headers(self):
        tokens = self.api_login("PlatformAdmin")
        self.assertEqual(tokens.status_code, 200, tokens.get_data(as_text=True))
        return {"Authorization": f"Bearer {tokens.get_json()['access_token']}"}

    def test_register_creates_a_pending_application_without_autologin(self):
        self.register_pharmacy()

        conn = get_db_connection()
        try:
            pharmacy = conn.execute(
                "SELECT status FROM pharmacy WHERE name = 'CityCare Pharmacy'"
            ).fetchone()
            user_row = conn.execute(
                'SELECT role, status FROM "user" WHERE name = ?',
                ("citycare@example.com",),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(pharmacy["status"], "pending")
        self.assertEqual(user_row["role"], "pharmacist")
        self.assertEqual(user_row["status"], "pending")

        # No auto-login: the session must not be set.
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

        self.assertEqual(get_pending_pharmacy_count(), 1)
        rows = get_pharmacies_with_applicant()
        self.assertEqual(rows[0]["name"], "CityCare Pharmacy")
        self.assertEqual(rows[0]["applicant_name"], "citycare@example.com")

    def test_pending_registration_cannot_sign_in_web_or_api(self):
        self.register_pharmacy()

        response = self.web_login("citycare@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"awaiting approval", response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

        response = self.api_login("citycare@example.com")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], login_status_block(
            {"status": "pending", "pharmacy_status": "pending"}))

    def test_approve_unlocks_login_and_suspend_blocks_again(self):
        self.register_pharmacy()
        pharmacy = get_pharmacies_with_applicant()[0]

        update_pharmacy_status(pharmacy["id"], "active", user_status="active")
        self.assertEqual(get_pending_pharmacy_count(), 0)

        response = self.web_login("citycare@example.com")
        self.assertEqual(response.status_code, 302)  # redirect to dashboard
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("user_name"), "citycare@example.com")

        response = self.api_login("citycare@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["role"], "pharmacy")

        # Suspend: pharmacy and its users are locked again, immediately.
        update_pharmacy_status(pharmacy["id"], "suspended", user_status="suspended")
        response = self.api_login("citycare@example.com")
        self.assertEqual(response.status_code, 403)
        self.assertIn("suspended", response.get_json()["error"])

        # Reactivate restores access.
        update_pharmacy_status(pharmacy["id"], "active", user_status="active")
        self.assertEqual(self.api_login("citycare@example.com").status_code, 200)

    def test_rejected_application_cannot_sign_in(self):
        self.register_pharmacy()
        pharmacy = get_pharmacies_with_applicant()[0]

        update_pharmacy_status(pharmacy["id"], "rejected", user_status="pending")
        response = self.web_login("citycare@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"not approved", response.data)
        self.assertEqual(self.api_login("citycare@example.com").status_code, 403)

    def test_non_active_pharmacies_are_hidden_from_the_public(self):
        self.register_pharmacy()
        pharmacy = get_pharmacies_with_applicant()[0]

        response = self.client.get("/api/v1/pharmacies")
        public_names = [p["name"] for p in response.get_json()["pharmacies"]]
        self.assertNotIn("CityCare Pharmacy", public_names)

        update_pharmacy_status(pharmacy["id"], "active", user_status="active")
        response = self.client.get("/api/v1/pharmacies")
        public_names = [p["name"] for p in response.get_json()["pharmacies"]]
        self.assertIn("CityCare Pharmacy", public_names)

    def test_delete_removes_only_pending_or_rejected_applications(self):
        registration_pharmacy_id = create_pharmacy_registration(
            name="Delete Me Chemists", email="delete-me@example.com",
            password=self.password,
        )
        update_pharmacy_status(registration_pharmacy_id, "active", user_status="active")
        # Cannot delete an active tenant.
        self.assertFalse(delete_pharmacy_application(registration_pharmacy_id))

        update_pharmacy_status(registration_pharmacy_id, "rejected", user_status="pending")
        self.assertTrue(delete_pharmacy_application(registration_pharmacy_id))

        conn = get_db_connection()
        try:
            pharmacy = conn.execute(
                "SELECT id FROM pharmacy WHERE id = ?", (registration_pharmacy_id,)
            ).fetchone()
            user_row = conn.execute(
                'SELECT id FROM "user" WHERE name = ?', ("delete-me@example.com",)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(pharmacy)
        self.assertIsNone(user_row)

    def test_admin_manage_page_is_admin_only(self):
        # Anonymous must be refused.
        self.assertEqual(self.client.get("/settings/pharmacies").status_code, 302)

        # A regular pharmacy (approved) is not an admin → 403.
        self.register_pharmacy()
        pharmacy = get_pharmacies_with_applicant()[0]
        update_pharmacy_status(pharmacy["id"], "active", user_status="active")
        self.web_login("citycare@example.com")
        self.assertEqual(self.client.get("/settings/pharmacies").status_code, 403)

        # A fresh application is still pending for the admin to approve.
        self.register_pharmacy(name="Greenleaf Chemists", email="greenleaf@example.com")

        # Log the platform admin back in via the web UI.
        response = self.web_login("PlatformAdmin")
        self.assertEqual(response.status_code, 302)

        response = self.client.get("/settings/pharmacies")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"CityCare Pharmacy", response.data)
        self.assertIn(b"Greenleaf Chemists", response.data)

        # Approving via the admin route flips the application to active.
        pending = get_pharmacies_with_applicant()
        pending_pharmacy = next(p for p in pending if p["status"] == "pending")
        self.client.post(
            f"/settings/pharmacies/{pending_pharmacy['id']}/approve",
            follow_redirects=True,
        )
        self.assertEqual(
            next(p for p in get_pharmacies_with_applicant() if p["id"] == pending_pharmacy["id"])["status"],
            "active",
        )


class ErrorHandlingTest(unittest.TestCase):
    """Friendly branded error pages for the browser, JSON for the API,
    and safe post-login redirects."""

    password = "Correct-Horse-Battery-9"

    @classmethod
    def setUpClass(cls):
        from app import app

        cls.app = app

        def _raise_for_test():
            raise RuntimeError("intentional test failure")

        def _raise_api_for_test():
            raise RuntimeError("intentional api test failure")

        # The shared app may already have served a request from another test
        # module, which freezes route registration. Unfreeze briefly, add two
        # throwaway routes that intentionally blow up, then re-freeze.
        app._got_first_request = False
        app.add_url_rule("/__error_test__", "__error_test__", _raise_for_test)
        app.add_url_rule(
            "/api/v1/__api_error_test__", "__api_error_test__", _raise_api_for_test)
        app._got_first_request = True

    def setUp(self):
        from database.queries import create_user
        from database.db import get_db_connection

        conn = get_db_connection()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            for table in ("token_blocklist", "login_attempt", "loss_report",
                          "stock_movement", "product_batch", "product",
                          "settings", "user", "pharmacy"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()
        create_user("PlatformAdmin", "admin", self.password)
        self.client = self.app.test_client()

    def web_login(self, name, password=None):
        return self.client.post(
            "/login",
            data={"name": name, "password": password or self.password, "next": ""},
        )

    def test_404_shows_branded_page(self):
        response = self.client.get("/no/such/page")
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"Page not found", response.data)
        self.assertIn(b"err-card", response.data)

    def test_404_for_api_is_json(self):
        response = self.client.get("/api/v1/no/such/resource")
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"Not found", response.data)
        self.assertNotIn(b"err-card", response.data)

    def test_403_shows_branded_page(self):
        # An admin is deliberately barred from pharmacist inventory routes.
        self.web_login("PlatformAdmin")
        response = self.client.get("/products")
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Access denied", response.data)
        self.assertIn(b"err-card", response.data)

    def test_500_shows_branded_page(self):
        response = self.client.get("/__error_test__")
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"Something went wrong", response.data)
        self.assertIn(b"err-card", response.data)

    def test_500_for_api_is_json(self):
        response = self.client.get("/api/v1/__api_error_test__")
        self.assertEqual(response.status_code, 500)
        self.assertIn(b'"error"', response.data)
        self.assertIn(b"Internal server error", response.data)
        self.assertNotIn(b"err-card", response.data)

    def test_unsafe_next_falls_back_to_dashboard(self):
        # A crafted external 'next' must not become the redirect target.
        response = self.client.post(
            "/login",
            data={
                "name": "PlatformAdmin",
                "password": self.password,
                "next": "https://evil.example/phish",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

        # Scheme-relative doubles (//...) are also rejected.
        response = self.client.post(
            "/login",
            data={
                "name": "PlatformAdmin",
                "password": self.password,
                "next": "//evil.example",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_same_site_next_is_kept(self):
        response = self.client.post(
            "/login",
            data={
                "name": "PlatformAdmin",
                "password": self.password,
                "next": "/movements",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/movements", response.headers["Location"])


class ExpiryRegressionTest(unittest.TestCase):
    """get_dashboard_data must tolerate a product_batch with an empty
    expiry_date (the single-row scenario that caused a local-only 500
    when SQLite held dirty data the live PostgreSQL never saw)."""

    def setUp(self):
        self.client = app.test_client()
        conn = get_db_connection()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            for table in ("token_blocklist", "login_attempt", "loss_report",
                          "stock_movement", "product_batch", "product",
                          "settings", "user", "pharmacy"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()
        create_user("DashboardAdmin", "admin", "Safe-Battery-9")

    def _get_session(self):
        r = self.client.post(
            "/login",
            data={"name": "DashboardAdmin", "password": "Safe-Battery-9"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)

    def test_empty_expiry_date_does_not_crash_dashboard(self):
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO pharmacy (id, name, status) VALUES (?, ?, 'active')",
                ("ph-1", "Test Pharmacy"),
            )
            conn.execute(
                "INSERT INTO product (id, name, pharmacy_id) VALUES (?, ?, ?)",
                ("prod-1", "Panadol", "ph-1"),
            )
            conn.execute(
                "INSERT INTO product_batch (id, product_id, expiry_date) VALUES (?, ?, '')",
                ("batch-1", "prod-1"),
            )
            conn.commit()
        finally:
            conn.close()

        self._get_session()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn(b"Dashboard", r.data)


class AdminAddPharmacyTest(unittest.TestCase):
    """The admin-provisioned pharmacy flow: an admin creates a pharmacy
    account directly (name + email + temporary password), and it is usable
    immediately without an application step."""

    password = "Correct-Horse-Battery-9"

    def setUp(self):
        self.client = app.test_client()
        conn = get_db_connection()
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            for table in ("token_blocklist", "login_attempt", "loss_report",
                          "stock_movement", "product_batch", "product",
                          "settings", "user", "pharmacy"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()
        create_user("AdminUser", "admin", self.password)

    def _login_admin(self):
        r = self.client.post("/login", data={"name": "AdminUser", "password": self.password})
        self.assertEqual(r.status_code, 302)

    def test_add_pharmacy_creates_active_account(self):
        self._login_admin()
        r = self.client.post("/settings/pharmacies/add", data={
            "name": "Harbor Pharmacy",
            "email": "harbor@example.com",
            "password": "Temporary-Pass-9",
            "city": "Lagos",
        })
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Pharmacy account created", body)
        self.assertIn("Harbor Pharmacy", body)

        # It is active immediately: the pharmacist login works on the web
        # login (no pending block), unlike the self-registration flow.
        login = self.client.post(
            "/login",
            data={"name": "harbor@example.com", "password": "Temporary-Pass-9"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302)

    def test_add_pharmacy_rejects_duplicate_email(self):
        self._login_admin()
        r = self.client.post("/settings/pharmacies/add", data={
            "name": "Harbor Pharmacy",
            "email": "harbor@example.com",
            "password": "Temporary-Pass-9",
        })
        self.assertEqual(r.status_code, 200)
        r2 = self.client.post("/settings/pharmacies/add", data={
            "name": "Harbor Second",
            "email": "harbor@example.com",
            "password": "Another-Pass-9",
        })
        self.assertEqual(r2.status_code, 200)
        self.assertIn("already exists", r2.get_data(as_text=True))

    def test_add_pharmacy_requires_admin(self):
        r = self.client.post("/settings/pharmacies/add", data={
            "name": "Sneaky Pharmacy",
            "email": "sneaky@example.com",
            "password": "Temporary-Pass-9",
        })
        # Anonymous is redirected to the login page, not allowed to create.
        self.assertIn(r.status_code, (302, 401, 403))

    def test_add_pharmacy_short_password_rejected(self):
        self._login_admin()
        r = self.client.post("/settings/pharmacies/add", data={
            "name": "Short Pass Pharmacy",
            "email": "shortpass@example.com",
            "password": "short",
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("at least 8 characters", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)