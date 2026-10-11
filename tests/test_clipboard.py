import subprocess
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import main


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.token = patch.object(main, "access_token", "test-token")
        self.token.start()
        self.addCleanup(self.token.stop)
        self.client = TestClient(main.app)
        self.headers = {"X-LAN-Token": "test-token"}

    def test_macos_uses_native_commands_with_unicode_and_no_shell(self):
        text = "中文剪贴板 🌷\nsecond line"
        with patch.object(main.sys, "platform", "darwin"), patch.object(main.subprocess, "run", return_value=Mock(stdout=text)) as run:
            read = self.client.get("/api/clipboard/read", headers=self.headers)
            self.assertEqual(read.json(), {"content": text})
            self.assertEqual(run.call_args.args[0], ["/usr/bin/pbpaste"])
            write = self.client.post("/api/clipboard/write", headers=self.headers, json={"content": text})
            self.assertEqual(write.json(), {"ok": True})
            self.assertEqual(run.call_args.args[0], ["/usr/bin/pbcopy"])
            self.assertEqual(run.call_args.kwargs["input"], text)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["encoding"], "utf-8")
                self.assertEqual(call.kwargs["env"]["LC_CTYPE"], "en_US.UTF-8")
                self.assertTrue(call.kwargs["check"])
                self.assertEqual(call.kwargs["timeout"], 5)
                self.assertFalse(call.kwargs.get("shell", False))

    def test_macos_failures_are_reported_instead_of_success(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("pbcopy", 5), subprocess.CalledProcessError(1, "pbcopy")):
            with self.subTest(failure=type(failure).__name__), patch.object(main.sys, "platform", "darwin"), patch.object(main.subprocess, "run", side_effect=failure):
                self.assertEqual(self.client.get("/api/clipboard/read", headers=self.headers).status_code, 503)
                response = self.client.post("/api/clipboard/write", headers=self.headers, json={"content": "test"})
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("ok", response.json())

    def test_unsupported_platform_keeps_file_and_text_service_available(self):
        with patch.object(main.sys, "platform", "linux"), patch.object(main.subprocess, "run") as run:
            self.assertEqual(self.client.get("/api/clipboard/read", headers=self.headers).status_code, 501)
            self.assertEqual(self.client.post("/api/clipboard/write", headers=self.headers, json={"content": "test"}).status_code, 501)
            info = self.client.get("/api/info", headers=self.headers)
            self.assertEqual(info.status_code, 200)
            self.assertFalse(info.json()["clipboardAvailable"])
            self.assertEqual(self.client.get("/api/text/list", headers=self.headers).status_code, 200)
            run.assert_not_called()

    def test_windows_unicode_clipboard_and_retry_failure(self):
        clipboard = Mock()
        clipboard.GetClipboardData.return_value = "Windows 中文"
        with (
            patch.object(main.sys, "platform", "win32"),
            patch.object(main, "win32clipboard", clipboard, create=True),
            patch.object(main, "win32con", Mock(CF_UNICODETEXT=13), create=True),
            patch.object(main.time, "sleep"),
        ):
            self.assertEqual(main._read_clipboard_sync(), "Windows 中文")
            main._write_clipboard_sync("Windows 中文")
            clipboard.SetClipboardText.assert_called_once_with("Windows 中文", 13)
            self.assertEqual(clipboard.CloseClipboard.call_count, 2)
            clipboard.OpenClipboard.side_effect = OSError("busy")
            response = self.client.post("/api/clipboard/write", headers=self.headers, json={"content": "test"})
            self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
