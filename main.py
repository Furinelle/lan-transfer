import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import psutil
import qrcode
import uvicorn
if sys.platform == "win32":
    import win32clipboard
    import win32con
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = Path.home() / "Downloads" / "LAN Transfer"
HISTORY_FILE = BASE_DIR / "history.json"
FRONTEND_DIR = BASE_DIR / "frontend"
MAX_HISTORY = 50
DEFAULT_PORT = 8899
DEFAULT_MAX_FILE_BYTES = 10 * 1024**3
UPLOAD_CHUNK_SIZE = 4 * 1024**2

# Virtual/non-LAN interface name prefixes to skip during IP detection
EXCLUDED_IFACE = ("VMware", "VMnet", "vEthernet", "Bluetooth",
                  "Loopback", "Teredo", "tap", "cfw", "isatap",
                  "utun", "awdl", "llw")

# ── State ──────────────────────────────────────────────────────────────────────
history: list = []
history_lock = asyncio.Lock()
clipboard_lock = threading.Lock()
server_ip: str = ""
server_port: int = DEFAULT_PORT
access_token: str = secrets.token_urlsafe(24)
max_file_bytes: int = DEFAULT_MAX_FILE_BYTES


# ── IP Detection ───────────────────────────────────────────────────────────────
def detect_lan_ip() -> str:
    candidates = []
    for iface_name, addrs in psutil.net_if_addrs().items():
        if any(iface_name.startswith(p) for p in EXCLUDED_IFACE):
            continue
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            if addr.address.startswith("127.") or addr.address.startswith("169.254."):
                continue
            candidates.append((iface_name, addr.address))

    for keyword in ("Wi-Fi", "WLAN", "Ethernet", "en0", "en1"):
        match = next((ip for name, ip in candidates if keyword in name), None)
        if match:
            return match
    if candidates:
        return candidates[0][1]
    return "127.0.0.1"


def build_access_url() -> str:
    return f"http://{server_ip}:{server_port}/?token={quote(access_token, safe='')}"


def request_has_access(request: Request) -> bool:
    provided_tokens = (
        request.headers.get("x-lan-token"),
        request.query_params.get("token"),
    )
    return any(
        token is not None and secrets.compare_digest(token, access_token)
        for token in provided_tokens
    )


def max_file_bytes_from_gb(value: float) -> int:
    if value <= 0:
        return 0
    return int(value * 1024**3)


def get_free_space(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def reserve_upload_path(filename: str) -> Path:
    name = Path(filename or "unnamed").name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 0
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        candidate_name = name if counter == 0 else f"{stem}_{counter}{suffix}"
        candidate = UPLOAD_DIR / candidate_name
        try:
            with candidate.open("xb"):
                return candidate
        except FileExistsError:
            counter += 1


# ── History helpers ────────────────────────────────────────────────────────────
def load_history():
    global history
    if not HISTORY_FILE.exists():
        return
    try:
        entries = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        history = entries[-MAX_HISTORY:]
    except Exception:
        HISTORY_FILE.rename(HISTORY_FILE.with_suffix(".json.bak"))
        history = []


def save_history():
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HISTORY_FILE)


async def append_history(entry: dict):
    async with history_lock:
        history.insert(0, entry)
        del history[MAX_HISTORY:]
        save_history()


async def write_upload_file(upload: UploadFile, dest: Path) -> int:
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                size += len(chunk)
                if max_file_bytes and size > max_file_bytes:
                    raise HTTPException(413, "File too large")
                if get_free_space(dest.parent) < len(chunk):
                    raise HTTPException(507, "Insufficient disk space")
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return size


# ── Clipboard helpers ─────────────────────────────────────────────────────────
def _read_clipboard_sync() -> Optional[str]:
    if sys.platform == "darwin":
        try:
            with clipboard_lock:
                result = subprocess.run(
                    ["/usr/bin/pbpaste"], capture_output=True, encoding="utf-8",
                    check=True, timeout=5, env={**os.environ, "LC_CTYPE": "en_US.UTF-8"},
                )
            return result.stdout or None
        except (OSError, subprocess.SubprocessError):
            raise HTTPException(503, "无法读取电脑剪贴板，请在已登录的桌面会话中重试") from None
    if sys.platform != "win32":
        raise HTTPException(501, "当前系统不支持剪贴板，文件和文字传输仍可用")
    for _ in range(3):
        try:
            with clipboard_lock:
                win32clipboard.OpenClipboard()
                try:
                    return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
        except TypeError:
            return None
        except Exception:
            time.sleep(0.05)
    raise HTTPException(503, "无法读取电脑剪贴板，请稍后重试")


def _write_clipboard_sync(text: str):
    if sys.platform == "darwin":
        try:
            with clipboard_lock:
                subprocess.run(
                    ["/usr/bin/pbcopy"], input=text, capture_output=True, encoding="utf-8",
                    check=True, timeout=5, env={**os.environ, "LC_CTYPE": "en_US.UTF-8"},
                )
            return
        except (OSError, subprocess.SubprocessError):
            raise HTTPException(503, "无法写入电脑剪贴板，请在已登录的桌面会话中重试") from None
    if sys.platform != "win32":
        raise HTTPException(501, "当前系统不支持剪贴板，文件和文字传输仍可用")
    for _ in range(3):
        try:
            with clipboard_lock:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
            return
        except Exception:
            time.sleep(0.05)
    raise HTTPException(503, "无法写入电脑剪贴板，请稍后重试")


# ── Startup / shutdown ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global server_ip
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    load_history()
    if not server_ip:
        server_ip = detect_lan_ip()

    url = build_access_url()
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)

    print(f"\nLAN Transfer  |  {url}")
    print("━" * 52)
    qr.print_ascii(invert=True)
    print("━" * 52)
    print("Scan QR or open URL above on your phone.")
    print("Press Ctrl+C to stop.\n")

    yield
    save_history()


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    if request.url.path.startswith("/api/") and not request_has_access(request):
        return JSONResponse({"detail": "Invalid or missing access token"}, status_code=403)
    if request.url.path == "/api/files/upload":
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                required_bytes = int(content_length)
            except ValueError:
                required_bytes = 0
            if required_bytes > 0 and get_free_space(UPLOAD_DIR) < required_bytes:
                return JSONResponse({"detail": "Insufficient disk space"}, status_code=507)
    return await call_next(request)


class TextPayload(BaseModel):
    content: str


class ClipboardPayload(BaseModel):
    content: str


# ── File endpoints ─────────────────────────────────────────────────────────────
@app.post("/api/files/upload")
async def upload_files(files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        dest = reserve_upload_path(f.filename or "unnamed")
        size = await write_upload_file(f, dest)
        await append_history({
            "id": str(uuid.uuid4()), "type": "file", "direction": "received",
            "content": dest.name, "filename": dest.name, "size": size,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        saved.append({"filename": dest.name, "size": size})
    return {"files": saved}


@app.get("/api/files/list")
async def list_files():
    files = sorted(
        ({"filename": p.name, "size": p.stat().st_size,
          "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
         for p in UPLOAD_DIR.iterdir() if not p.is_symlink() and p.is_file()),
        key=lambda x: x["modified"], reverse=True,
    )
    return {"files": files}


@app.get("/api/files/download/{filename}")
async def download_file(filename: str):
    path = UPLOAD_DIR / Path(filename).name
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, "File not found")
    encoded = quote(path.name, encoding="utf-8")
    return FileResponse(
        path,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


@app.delete("/api/files/{filename}")
async def delete_file(filename: str):
    path = UPLOAD_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"ok": True}


# ── Text endpoints ─────────────────────────────────────────────────────────────
@app.post("/api/text/send")
async def send_text(payload: TextPayload):
    await append_history({
        "id": str(uuid.uuid4()), "type": "text", "direction": "received",
        "content": payload.content, "preview": payload.content[:200],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}


@app.get("/api/text/list")
async def list_texts():
    async with history_lock:
        texts = [h for h in history if h.get("type") == "text"]
    return {"texts": texts}


# ── Clipboard endpoints ────────────────────────────────────────────────────────
@app.get("/api/clipboard/read")
async def read_clipboard():
    loop = asyncio.get_event_loop()
    content = await loop.run_in_executor(None, _read_clipboard_sync)
    if content is None:
        return {"content": None, "error": "剪贴板为空或包含非文本内容"}
    return {"content": content}


@app.post("/api/clipboard/write")
async def write_clipboard(payload: ClipboardPayload):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _write_clipboard_sync, payload.content)
    return {"ok": True}


# ── History endpoints ──────────────────────────────────────────────────────────
@app.get("/api/history")
async def get_history():
    async with history_lock:
        return {"history": list(history)}


@app.delete("/api/history")
async def clear_history():
    async with history_lock:
        history.clear()
        save_history()
    return {"ok": True}


@app.delete("/api/history/{entry_id}")
async def delete_history_entry(entry_id: str):
    async with history_lock:
        for i, h in enumerate(history):
            if h.get("id") == entry_id:
                history.pop(i)
                save_history()
                return {"ok": True}
    raise HTTPException(404, "Entry not found")


# ── Info + QR ──────────────────────────────────────────────────────────────────
@app.get("/api/info")
async def get_info():
    return {"ip": server_ip, "port": server_port,
            "url": build_access_url(), "maxFileBytes": max_file_bytes,
            "clipboardAvailable": sys.platform in ("win32", "darwin")}


@app.get("/api/qr")
async def get_qr():
    img = qrcode.make(build_access_url())
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/")
async def serve_frontend():
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text(encoding="utf-8"))


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="LAN Transfer Tool")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", type=str, default=None, help="Bind to this IP instead of the detected LAN IP")
    parser.add_argument("--upload-dir", type=str, default=None, help="Custom upload directory")
    parser.add_argument(
        "--max-file-gb",
        type=float,
        default=DEFAULT_MAX_FILE_BYTES / 1024**3,
        help="Maximum size per uploaded file in GB; 0 disables the limit",
    )
    args = parser.parse_args()

    server_port = args.port
    max_file_bytes = max_file_bytes_from_gb(args.max_file_gb)
    server_ip = args.host or detect_lan_ip()
    if args.upload_dir:
        UPLOAD_DIR = Path(args.upload_dir)

    uvicorn.run(app, host=server_ip, port=args.port, access_log=False)
