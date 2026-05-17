import unittest

from fastapi.testclient import TestClient

import main


class AccessTokenTests(unittest.TestCase):
    def setUp(self):
        main.access_token = "test-token"
        main.server_ip = "127.0.0.1"
        main.server_port = 8899
        main.history = []
        self.client = TestClient(main.app)

    def test_api_rejects_missing_token(self):
        response = self.client.get("/api/history")

        self.assertEqual(response.status_code, 403)

    def test_api_accepts_header_token(self):
        response = self.client.get(
            "/api/history",
            headers={"X-LAN-Token": "test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"history": []})

    def test_api_accepts_query_token_and_returns_tokenized_url(self):
        response = self.client.get("/api/info?token=test-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["url"],
            "http://127.0.0.1:8899/?token=test-token",
        )
        self.assertEqual(response.json()["maxFileBytes"], main.max_file_bytes)


if __name__ == "__main__":
    unittest.main()
