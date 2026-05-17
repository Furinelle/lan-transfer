import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main


class UploadLimitTests(unittest.TestCase):
    def setUp(self):
        self.upload_dir = tempfile.TemporaryDirectory()
        self.old_upload_dir = main.UPLOAD_DIR
        self.old_max_file_bytes = getattr(main, "max_file_bytes", None)
        self.old_get_free_space = getattr(main, "get_free_space", None)
        self.old_history = main.history
        main.UPLOAD_DIR = Path(self.upload_dir.name)
        main.max_file_bytes = 5
        main.history = []
        main.access_token = "test-token"
        self.client = TestClient(main.app)

    def tearDown(self):
        main.UPLOAD_DIR = self.old_upload_dir
        if self.old_max_file_bytes is None:
            delattr(main, "max_file_bytes")
        else:
            main.max_file_bytes = self.old_max_file_bytes
        if self.old_get_free_space is None:
            if hasattr(main, "get_free_space"):
                delattr(main, "get_free_space")
        else:
            main.get_free_space = self.old_get_free_space
        main.history = self.old_history
        self.upload_dir.cleanup()

    def test_upload_rejects_file_over_configured_limit_and_removes_partial_file(self):
        response = self.client.post(
            "/api/files/upload",
            headers={"X-LAN-Token": "test-token"},
            files={"files": ("large.bin", b"abcdef", "application/octet-stream")},
        )

        self.assertEqual(response.status_code, 413)
        self.assertFalse((main.UPLOAD_DIR / "large.bin").exists())

    def test_upload_rejects_when_disk_space_is_too_low(self):
        main.max_file_bytes = 0
        main.get_free_space = lambda _path: 0

        response = self.client.post(
            "/api/files/upload",
            headers={"X-LAN-Token": "test-token"},
            files={"files": ("small.bin", b"abc", "application/octet-stream")},
        )

        self.assertEqual(response.status_code, 507)
        self.assertFalse((main.UPLOAD_DIR / "small.bin").exists())

    def test_reserve_upload_path_creates_unique_names_atomically(self):
        (main.UPLOAD_DIR / "same.txt").write_text("existing", encoding="utf-8")

        first = main.reserve_upload_path("same.txt")
        second = main.reserve_upload_path("same.txt")

        self.assertEqual(first.name, "same_1.txt")
        self.assertEqual(second.name, "same_2.txt")
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_max_file_gb_zero_disables_limit(self):
        self.assertEqual(main.max_file_bytes_from_gb(0), 0)

    def test_max_file_gb_converts_to_bytes(self):
        self.assertEqual(main.max_file_bytes_from_gb(5), 5 * 1024**3)


if __name__ == "__main__":
    unittest.main()
