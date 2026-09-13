"""End-to-end tests for the PharmaTrack JSON API."""

import os
import tempfile
import unittest

# Configure the application before importing it. database.db reads this path
# at import time, making every test run isolated from pharmacy.db.
_TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["PHARMATRACK_DB_PATH"] = os.path.join(_TEST_DIRECTORY.name, "test-pharmacy.db")
os.environ["JWT_SECRET_KEY"] = "test-only-jwt-secret-that-is-long-enough"
os.environ["DATABASE_URL"] = ""  # force SQLite even though .env may set one
os.environ.pop("PHARMATRACK_ENV", None)

from api import auth
from app import app
from database.db import get_db_connection
from database.queries import create_user, create_pharmacy


class ApiTestCase(unittest.TestCase):
    password = "Correct-Horse-Battery-9"

    def setUp(self):
        self.client = app.test_client()
        conn = get_db_connection()
        try:
            for table in ("token_blocklist", "login_attempt", "loss_report", "stock_movement", "product_batch", "product", "user"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()
        finally:
            conn.close()

        create_user("Admin", "admin", self.password)
        create_user("Pharmacist", "pharmacist", self.password)
        create_user("Public User", "user", self.password)

    def login(self, name, password=None):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"name": name, "password": password or self.password},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def authorization_header(self, name):
        return {"Authorization": f"Bearer {self.login(name)['access_token']}"}

    def create_product(self, headers, name="Test Medicine", initial_quantity=5, **extra):
        body = {
            "name": name,
            "category": "Test",
            "strength": "500mg",
            "dosage_form": "Tablet",
            "batch_number": f"{name[:4]}-001",
            "expiry_date": "2027-01-01",
            "initial_quantity": initial_quantity,
            **extra,
        }
        response = self.client.post("/api/v1/products", headers=headers, json=body)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["product_id"]

    def test_authentication_refresh_and_logout(self):
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/login",
                json={"name": "Admin", "password": "wrong-password"},
            ).status_code,
            401,
        )

        tokens = self.login("Admin")
        self.assertIn("access_token", tokens)
        self.assertIn("refresh_token", tokens)

        response = self.client.post(
            "/api/v1/auth/refresh",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_token", response.get_json())

        response = self.client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get(
            "/api/v1/reports/dashboard",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Authentication token has been revoked.")

    def test_role_permissions_and_safe_public_inventory(self):
        pharmacy_headers = self.authorization_header("Pharmacist")
        self.create_product(pharmacy_headers, name="Normal Medicine")
        self.create_product(pharmacy_headers, name="Controlled Medicine", is_controlled=True)

        public_headers = self.authorization_header("Public User")
        response = self.client.get("/api/v1/products", headers=public_headers)
        self.assertEqual(response.status_code, 200)
        products = response.get_json()["products"]
        self.assertEqual([product["name"] for product in products], ["Normal Medicine"])
        self.assertNotIn("current_stock", products[0])
        self.assertIn("in_stock", products[0])

        response = self.client.get("/api/v1/products/not-a-real-product", headers=public_headers)
        self.assertEqual(response.status_code, 403)

        response = self.client.get(
            "/api/v1/reports/dashboard",
            headers=self.authorization_header("Admin"),
        )
        self.assertEqual(response.status_code, 200)

    def test_validation_and_no_negative_stock(self):
        pharmacy_headers = self.authorization_header("Pharmacist")

        response = self.client.post(
            "/api/v1/products",
            headers=pharmacy_headers,
            json={"name": "Bad Boolean", "category": "Test", "strength": "500mg",
                  "dosage_form": "Tablet", "batch_number": "BB-1",
                  "expiry_date": "2027-01-01", "requires_prescription": "false"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "requires_prescription must be true or false.")

        response = self.client.post(
            "/api/v1/products",
            headers=pharmacy_headers,
            data="{}",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 400)

        product_id = self.create_product(pharmacy_headers, initial_quantity=5)
        response = self.client.get(f"/api/v1/products/{product_id}/batches", headers=pharmacy_headers)
        batch_id = response.get_json()["batches"][0]["id"]

        response = self.client.post(
            "/api/v1/movements",
            headers=pharmacy_headers,
            json={"product_batch_id": batch_id, "movement_type": "sale", "quantity": 6},
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.get_json()["error"].startswith("Insufficient stock."))

        response = self.client.post(
            "/api/v1/movements",
            headers=pharmacy_headers,
            json={"product_batch_id": batch_id, "movement_type": "sale", "quantity": 5},
        )
        self.assertEqual(response.status_code, 201)

        response = self.client.post(
            "/api/v1/movements",
            headers=pharmacy_headers,
            json={"product_batch_id": batch_id, "movement_type": "sale", "quantity": 1},
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_can_provision_a_read_only_user(self):
        response = self.client.post(
            "/api/v1/users",
            headers=self.authorization_header("Admin"),
            json={
                "name": "Mobile Customer",
                "password": self.password,
                "role": "user",
            },
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["role"], "user")

        response = self.client.post(
            "/api/v1/auth/login",
            json={"name": "Mobile Customer", "password": self.password},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["role"], "user")

    def test_login_includes_pharmacy_id(self):
        tokens = self.login("Pharmacist")
        self.assertIn("pharmacy_id", tokens)
        self.assertIsNotNone(tokens["pharmacy_id"])

    def test_products_are_scoped_to_the_callers_pharmacy(self):
        pharmacy_headers = self.authorization_header("Pharmacist")
        self.create_product(pharmacy_headers, name="Our Medicine")

        other_pharmacy_id = create_pharmacy(name="Second Pharmacy")
        create_user("Second Pharmacist", "pharmacist", self.password, pharmacy_id=other_pharmacy_id)
        other_headers = self.authorization_header("Second Pharmacist")
        self.create_product(other_headers, name="Their Medicine")

        response = self.client.get("/api/v1/products", headers=pharmacy_headers)
        products = response.get_json()["products"]
        self.assertEqual(products[0]["name"], "Our Medicine")
        self.assertIn("pharmacy_name", products[0])

        response = self.client.get("/api/v1/products", headers=other_headers)
        products = response.get_json()["products"]
        self.assertEqual(products[0]["name"], "Their Medicine")

        our_product_id = self.create_product(pharmacy_headers, name="Scoped Batch")
        response = self.client.get(f"/api/v1/products/{our_product_id}", headers=other_headers)
        self.assertEqual(response.status_code, 404)
        response = self.client.get(f"/api/v1/products/{our_product_id}/batches", headers=other_headers)
        self.assertEqual(response.status_code, 404)

    def test_movements_are_scoped_to_the_callers_pharmacy(self):
        pharmacy_headers = self.authorization_header("Pharmacist")
        product_id = self.create_product(pharmacy_headers)
        response = self.client.get(f"/api/v1/products/{product_id}/batches", headers=pharmacy_headers)
        batch_id = response.get_json()["batches"][0]["id"]

        other_pharmacy_id = create_pharmacy(name="Second Pharmacy")
        create_user("Second Pharmacist", "pharmacist", self.password, pharmacy_id=other_pharmacy_id)
        response = self.client.post(
            "/api/v1/movements",
            headers=self.authorization_header("Second Pharmacist"),
            json={"product_batch_id": batch_id, "movement_type": "sale", "quantity": 1},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Product batch not found.")

    def test_public_pharmacy_and_popular_endpoints(self):
        self.create_product(self.authorization_header("Pharmacist"), name="Amoxicillin", price_per_unit=250)
        self.create_product(self.authorization_header("Pharmacist"), name="Controlled", is_controlled=True)

        response = self.client.get("/api/v1/pharmacies")
        self.assertEqual(response.status_code, 200)
        pharmacies = response.get_json()["pharmacies"]
        self.assertTrue(pharmacies)
        self.assertIn("opening_hours", pharmacies[0])

        pharmacy_id = pharmacies[0]["id"]
        response = self.client.get(f"/api/v1/pharmacies/{pharmacy_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["pharmacy"]["id"], pharmacy_id)

        response = self.client.get(f"/api/v1/pharmacies/{pharmacy_id}/products")
        self.assertEqual(response.status_code, 200)
        names = [p["name"] for p in response.get_json()["products"]]
        self.assertIn("Amoxicillin", names)
        self.assertNotIn("Controlled", names)

        response = self.client.get("/api/v1/pharmacies/does-not-exist")
        self.assertEqual(response.status_code, 404)

        response = self.client.get("/api/v1/products/popular")
        self.assertEqual(response.status_code, 200)
        popular = response.get_json()["products"]
        self.assertTrue(popular)
        self.assertEqual(popular[0]["name"], "Amoxicillin")
        self.assertEqual(popular[0]["cheapest_price"], 250)
        self.assertEqual(popular[0]["pharmacy_count"], 1)
        self.assertTrue(popular[0]["any_in_stock"])

        response = self.client.get("/api/v1/products/search?q=Amoxi")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["name"] for p in response.get_json()["products"]], ["Amoxicillin"])

        response = self.client.get("/api/v1/products/search?q=")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["products"], [])

    def test_api_login_is_rate_limited(self):
        headers = {"REMOTE_ADDR": "198.51.100.17"}
        for _ in range(auth.API_MAX_LOGIN_ATTEMPTS):
            response = self.client.post(
                "/api/v1/auth/login",
                json={"name": "Admin", "password": "wrong"},
                environ_overrides=headers,
            )
            self.assertEqual(response.status_code, 401)

        response = self.client.post(
            "/api/v1/auth/login",
            json={"name": "Admin", "password": "wrong"},
            environ_overrides=headers,
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)


if __name__ == "__main__":
    unittest.main(verbosity=2)

