import runpy
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class TransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        state = patch.multiple(main, UPLOAD_DIR=self.uploads, HISTORY_FILE=self.root / "history.json",
                               history=[], access_token="test-token", max_file_bytes=1024)
        state.start()
        self.addCleanup(state.stop)
        self.client = TestClient(main.app)
        self.headers = {"X-LAN-Token": "test-token"}

    def test_file_and_text_roundtrip_with_duplicate_names(self):
        for expected in ("中文.txt", "中文_1.txt"):
            response = self.client.post("/api/files/upload", headers=self.headers, files={"files": ("中文.txt", b"hello", "text/plain")})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["files"][0]["filename"], expected)
            download = self.client.get(f"/api/files/download/{expected}", headers=self.headers)
            self.assertEqual(download.content, b"hello")
        self.assertEqual(len(self.client.get("/api/files/list", headers=self.headers).json()["files"]), 2)
        self.assertEqual(self.client.post("/api/text/send", headers=self.headers, json={"content": "手机 → Mac 🌷"}).status_code, 200)
        self.assertEqual(self.client.get("/api/text/list", headers=self.headers).json()["texts"][0]["content"], "手机 → Mac 🌷")
        self.assertEqual(self.client.delete("/api/files/中文.txt", headers=self.headers).status_code, 200)
        self.assertFalse((self.uploads / "中文.txt").exists())

    def test_file_symlink_does_not_expose_an_outside_file(self):
        outside = self.root / "private.txt"
        outside.write_text("disposable secret", encoding="utf-8")
        try:
            (self.uploads / "link.txt").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Symlinks are unavailable: {exc}")
        self.assertEqual(self.client.get("/api/files/list", headers=self.headers).json(), {"files": []})
        self.assertEqual(self.client.get("/api/files/download/link.txt", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.delete("/api/files/link.txt", headers=self.headers).status_code, 200)
        self.assertEqual(outside.read_text(), "disposable secret")

    def test_macos_ip_detection_ignores_tunnel_and_prefers_wifi(self):
        addresses = {
            "utun4": [SimpleNamespace(family=socket.AF_INET, address="198.18.0.1")],
            "en1": [SimpleNamespace(family=socket.AF_INET, address="192.168.2.2")],
            "en0": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.2")],
        }
        with patch.object(main.psutil, "net_if_addrs", return_value=addresses):
            self.assertEqual(main.detect_lan_ip(), "192.168.1.2")
        with patch.object(main.psutil, "net_if_addrs", return_value={}):
            self.assertEqual(main.detect_lan_ip(), "127.0.0.1")

    def test_entry_point_binds_only_to_requested_interface(self):
        with patch.object(main.sys, "argv", [main.__file__, "--host", "127.0.0.1", "--port", "18999"]), patch.object(main.uvicorn, "run") as run:
            runpy.run_path(main.__file__, run_name="__main__")
        self.assertEqual(run.call_args.kwargs, {"host": "127.0.0.1", "port": 18999, "access_log": False})


if __name__ == "__main__":
    unittest.main()
