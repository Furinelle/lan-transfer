# LAN Transfer

局域网文件与文字传输工具。手机和电脑连接同一 Wi-Fi，手机扫码即可互传文件、发送文字、同步剪贴板，无需安装 App。

## 功能

- 📤 **文件传输** — 手机上传文件到电脑，电脑下载文件到手机
- 💬 **文字发送** — 手机直接发文字到电脑
- 📋 **剪贴板同步** — 读取 / 写入电脑剪贴板，支持 3 秒自动同步
- 🕐 **传输历史** — 记录最近 50 条传输记录，重启后保留
- 📡 **扫码连接** — 启动时终端打印 QR 码，手机扫码直达

## 安装

```bash
pip install -r requirements.txt
```

> **系统要求**：Python 3.10+，Windows（剪贴板功能依赖 `pywin32`）

## 启动

双击 `start.bat`，或在命令行运行：

```bash
python main.py
```

启动后扫描终端中的 QR 码，或在手机浏览器输入显示的 URL。

## 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | `8899` | 服务端口 |
| `--host` | 自动检测 LAN IP | 手动指定 IP |
| `--upload-dir` | `~/Downloads/LAN Transfer` | 文件保存目录 |

```bash
# 自定义保存目录
python main.py --upload-dir "D:\MyFiles"

# 自定义端口
python main.py --port 9000
```
