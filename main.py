import argparse
import asyncio
import json
import os
import socket
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
import win32clipboard
import win32con
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = Path.home() / "Downloads" / "LAN Transfer"
HISTORY_FILE = BASE_DIR / "history.json"
FRONTEND_DIR = BASE_DIR / "frontend"
MAX_HISTORY = 50
DEFAULT_PORT = 8899

# Virtual/non-LAN interface name prefixes to skip during IP detection
EXCLUDED_IFACE = ("VMware", "VMnet", "vEthernet", "Bluetooth",
                  "Loopback", "Teredo", "tap", "cfw", "isatap")

# ── State ──────────────────────────────────────────────────────────────────────
history: list = []
history_lock = asyncio.Lock()
clipboard_lock = threading.Lock()
server_ip: str = ""
server_port: int = DEFAULT_PORT


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

    for keyword in ("Wi-Fi", "WLAN", "Ethernet"):
        match = next((ip for name, ip in candidates if keyword in name), None)
        if match:
            return match
    if candidates:
        return candidates[0][1]
    return socket.gethostbyname(socket.gethostname())


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


# ── Clipboard helpers (Windows) ────────────────────────────────────────────────
def _read_clipboard_sync() -> Optional[str]:
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
    return None


def _write_clipboard_sync(text: str):
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


# ── Startup / shutdown ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global server_ip
    UPLOAD_DIR.mkdir(exist_ok=True)
    load_history()
    if not server_ip:
        server_ip = detect_lan_ip()

    url = f"http://{server_ip}:{server_port}"
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
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class TextPayload(BaseModel):
    content: str


class ClipboardPayload(BaseModel):
    content: str


# ── File endpoints ─────────────────────────────────────────────────────────────
@app.post("/api/files/upload")
async def upload_files(files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        name = Path(f.filename or "unnamed").name  # strip any path traversal
        dest = UPLOAD_DIR / name
        counter = 1
        while dest.exists():
            stem, suffix = Path(name).stem, Path(name).suffix
            dest = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
        data = await f.read()
        dest.write_bytes(data)
        await append_history({
            "id": str(uuid.uuid4()), "type": "file", "direction": "received",
            "content": dest.name, "filename": dest.name, "size": len(data),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        saved.append({"filename": dest.name, "size": len(data)})
    return {"files": saved}


@app.get("/api/files/list")
async def list_files():
    files = sorted(
        ({"filename": p.name, "size": p.stat().st_size,
          "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
         for p in UPLOAD_DIR.iterdir() if p.is_file()),
        key=lambda x: x["modified"], reverse=True,
    )
    return {"files": files}


@app.get("/api/files/download/{filename}")
async def download_file(filename: str):
    path = UPLOAD_DIR / Path(filename).name
    if not path.exists() or not path.is_file():
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
            "url": f"http://{server_ip}:{server_port}"}


@app.get("/api/qr")
async def get_qr():
    img = qrcode.make(f"http://{server_ip}:{server_port}")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/")
async def serve_frontend():
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text(encoding="utf-8"))


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="LAN Transfer Tool")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", type=str, default=None, help="Override detected LAN IP")
    parser.add_argument("--upload-dir", type=str, default=None, help="Custom upload directory")
    args = parser.parse_args()

    server_port = args.port
    if args.host:
        server_ip = args.host
    if args.upload_dir:
        UPLOAD_DIR = Path(args.upload_dir)

    uvicorn.run(app, host="0.0.0.0", port=args.port)
