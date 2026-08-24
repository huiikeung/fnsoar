#!/usr/bin/env python3
"""
fnSoar Admin Server
- HTTP admin panel on port 9099 (for direct access)
- Unix socket gateway for fnOS iframe integration
- Manages config.yaml, service start/stop, proxy providers, logs
"""
import os
import re
import sys
import json
import time
import ssl
import base64
import hashlib
import socket
import signal
import threading
import subprocess
import shutil
import urllib.request
import tempfile
import zipfile
import tarfile
import yaml
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer
from urllib.parse import urlparse, parse_qs, quote, unquote_to_bytes
from pathlib import Path

# ── Paths (set by service-setup or fallback) ──────────────────────────────
TRIM_APPNAME  = os.environ.get("MIHOMO_APP_NAME", "fnnas.fnsoar")
TRIM_PKGVAR   = os.environ.get("MIHOMO_DATA_DIR", "/vol1/@appdata/fnnas.fnsoar")
TRIM_APPDEST  = os.environ.get("MIHOMO_DEST_DIR", "/vol1/@appcenter/fnnas.fnsoar")
ADMIN_PORT    = int(os.environ.get("MIHOMO_ADMIN_PORT", "9099"))
SOCKET_PATH   = os.environ.get("MIHOMO_GATEWAY_SOCK",
                                f"/vol1/@appcenter/{TRIM_APPNAME}/fnsoar.sock")

CONFIG_FILE   = f"{TRIM_PKGVAR}/config.yaml"
ICONS_FILE    = f"{TRIM_PKGVAR}/icons.yaml"
ICON_DIR      = f"{TRIM_PKGVAR}/icons"

# ── 图标名称映射：从 icons.yaml 加载短名→data URI ─────────────────────
_ICONS_MAP = None
def _get_icons_map():
    global _ICONS_MAP
    if _ICONS_MAP is None:
        try:
            if os.path.exists(ICONS_FILE):
                with open(ICONS_FILE, "r", encoding="utf-8") as f:
                    _ICONS_MAP = yaml.safe_load(f.read()) or {}
            else:
                _ICONS_MAP = {}
        except Exception:
            _ICONS_MAP = {}
    return _ICONS_MAP
LOG_FILE      = f"{TRIM_PKGVAR}/{TRIM_APPNAME}.log"
PID_FILE      = f"{TRIM_PKGVAR}/{TRIM_APPNAME}.pid"
PROFILES_DIR  = f"{TRIM_PKGVAR}/profiles"
ACTIVE_FILE   = f"{TRIM_PKGVAR}/active"
MIHOMO_BIN    = f"{TRIM_APPDEST}/bin/mihomo"
ADMIN_DIR     = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = f"{TRIM_PKGVAR}/dashboard"
MIHOMO_CTRL_PORT = int(os.environ.get("MIHOMO_CTRL_PORT", "9090"))
MIHOMO_CTRL_HOST = os.environ.get("MIHOMO_CTRL_HOST", "127.0.0.1")

# ── Utils ────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[admin] {msg}", flush=True)

def read_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"# Error reading config: {e}\n"

# ── 图标本地化：远程图标下载到本机 ICON_DIR 缓存，之后从本地读取 ────────
_ICON_MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "application/octet-stream": ".png", "application/xml": ".svg", "text/xml": ".svg",
}
_ICON_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

def localize_icon(url):
    """把 url 指向的图标缓存到本地，返回(本地绝对路径, content_type)；失败返回 (None,None)。
    命中缓存时无需联网，直接读本地文件。"""
    try:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        os.makedirs(ICON_DIR, exist_ok=True)
        # 命中缓存
        for f in os.listdir(ICON_DIR):
            if f.startswith(key + "."):
                ct = _ICON_EXT_MIME.get("." + f.rsplit(".", 1)[-1].lower(), "image/png")
                return os.path.join(ICON_DIR, f), ct
        # 下载
        req = urllib.request.Request(url, headers={"User-Agent": "fnSoar/1.0"})
        with urllib.request.urlopen(req, timeout=3, context=_SSL_CTX) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "image/png").split(";")[0].strip().lower()
        ext = _ICON_MIME_EXT.get(ctype, ".png")
        path = os.path.join(ICON_DIR, key + ext)
        with open(path, "wb") as f:
            f.write(data)
        return path, ctype
    except Exception as e:
        log(f"icon localize failed: {url}: {e}")
        return None, None

def cached_icon_path(url):
    """只查缓存不下载，命中返回(本地路径, content_type)，否则 (None,None)。"""
    try:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        for f in os.listdir(ICON_DIR) if os.path.isdir(ICON_DIR) else []:
            if f.startswith(key + "."):
                ct = _ICON_EXT_MIME.get("." + f.rsplit(".", 1)[-1].lower(), "image/png")
                return os.path.join(ICON_DIR, f), ct
    except Exception:
        pass
    return None, None

def embedded_icon(url_or_data):
    """把图标转成可内嵌的 data: URI（短名→icons.yaml 解析，data: 直接返回，URL 下载本地化，3s超时）。"""
    if not isinstance(url_or_data, str) or not url_or_data:
        return url_or_data
    low = url_or_data.strip().lower()
    if low.startswith("data:"):
        return url_or_data
    # 短名解析：从 icons.yaml 查找
    imap = _get_icons_map()
    if imap and url_or_data in imap:
        return imap[url_or_data]
    if not (low.startswith("http://") or low.startswith("https://")):
        return url_or_data
    path, ctype = localize_icon(url_or_data)
    if path:
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:{ctype};base64,{b64}"
        except Exception:
            return url_or_data
    return url_or_data


def expand_api_icons(value, icon_base=""):
    """Convert icons.yaml names to stable HTTP URLs for dashboards.

    ``icon_base`` preserves the fnOS gateway prefix when the dashboard is
    accessed through the Unix-socket gateway instead of the TCP port.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "icon" and isinstance(item, str):
                icon_map = _get_icons_map()
                if item in icon_map:
                    # Return data directly. Dashboards may use mihomo's
                    # controller (9090) as API base, so /api/icon would be
                    # requested from mihomo instead of this admin server.
                    result[key] = embedded_icon(item)
                else:
                    result[key] = embedded_icon(item)
            else:
                result[key] = expand_api_icons(item, icon_base)
        return result
    if isinstance(value, list):
        return [expand_api_icons(item, icon_base) for item in value]
    return value


# ── 应用版本读取：依次回退 VERSION 文件 / app.json 的 version 字段 ──────
def _get_app_version():
    """Return the app version string. Checks several candidates for robustness:
    1. $TRIM_APPDEST/VERSION            (plain text file)
    2. $TRIM_APPDEST/app.json           (JSON "version")
    3. <admin dir>/../app.json          (source-tree fallback)
    Returns "unknown" if none is found."""
    candidates = [
        os.path.join(TRIM_APPDEST, "VERSION"),
        os.path.join(TRIM_APPDEST, "app.json"),
        os.path.join(os.path.dirname(ADMIN_DIR), "app.json"),
    ]
    for c in candidates:
        try:
            if not os.path.exists(c):
                continue
            if c.endswith(".json"):
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ver = (data.get("version") or "").strip()
                if ver:
                    return ver
            else:
                with open(c, "r", encoding="utf-8") as f:
                    ver = f.read().strip()
                if ver:
                    return ver
        except Exception:
            continue
    return "unknown"


# ── 面板升级（zashboard / metacubexd 一键升级） ─────────────────────────
# 每个面板从 GitHub Release 下载官方打包产物，解压后原子替换 dashboard/<name>，
# 失败时回滚到备份，避免损坏现有面板。
_DASHBOARD_RELEASES = {
    # 面板名 -> (GitHub repo, 该面板发布资产匹配器, 是否需跨层解压+子目录探测)
    "zashboard": {
        "repo": "Zephyruso/zashboard",
        "asset_match": lambda n: n.endswith(".zip") and n.startswith("dist"),
        "archive": "zip",
        "subdir": "zashboard",
    },
    "metacubexd": {
        "repo": "MetaCubeX/metacubexd",
        "asset_match": lambda n: n.endswith(".tgz"),
        "archive": "tgz",
        "subdir": "metacubexd",
    },
}

def _read_dashboard_version(name):
    """Try to read a small version marker inside a dashboard directory."""
    probe = os.path.join(DASHBOARD_DIR, name, "VERSION")
    if os.path.exists(probe):
        try:
            with open(probe, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return None

def _github_latest(repo):
    """Query GitHub release 'latest'. Uses the gh-proxy mirror (same as the
    config.yaml geo URLs) so unauthenticated GitHub API rate limits are less
    likely to break the dashboard version check. An optional MIHOMO_GITHUB_TOKEN
    is attached when available (raises limit and enables private-repo access)."""
    import urllib.request as _ureq
    gh_api = os.environ.get("MIHOMO_GITHUB_API", "https://api.github.com")
    token = os.environ.get("MIHOMO_GITHUB_TOKEN", "")
    # Prefer the api.github.com endpoint but route through gh-proxy when
    # unauthenticated; if a token is set, query api.github.com directly.
    if not token and gh_api == "https://api.github.com":
        gh_api = "https://gh-proxy.com/https://api.github.com"
    url = f"{gh_api}/repos/{repo}/releases/latest"
    headers = {"User-Agent": "ClashMini/1.0", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = _ureq.Request(url, headers=headers)
    with _ureq.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def dashboard_latest_info(name):
    """Return (latest_version, download_url, current_version) for a dashboard.
    Raises on network/API errors."""
    meta = _DASHBOARD_RELEASES.get(name)
    if not meta:
        raise ValueError(f"unknown dashboard: {name}")
    d = _github_latest(meta["repo"])
    latest = (d.get("tag_name", "") or "").lstrip("v")
    url = ""
    for a in d.get("assets", []):
        if meta["asset_match"](a.get("name", "")):
            url = a.get("browser_download_url", ""); break
    if not url:
        raise ValueError("no downloadable asset found for " + meta["repo"])
    current = _read_dashboard_version(name)
    return latest, url, current

def _safe_extract(archive_path, name, dest_dir):
    """Extract a zip/tgz archive into dest_dir. Guard against path traversal."""
    meta = _DASHBOARD_RELEASES.get(name, {})
    import re as _re
    if meta.get("archive") == "tgz":
        with tarfile.open(archive_path, "r:gz") as tf:
            for m in tf.getmembers():
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    raise ValueError("unsafe path in archive: " + m.name)
            tf.extractall(dest_dir)
    else:
        with zipfile.ZipFile(archive_path) as zf:
            for n in zf.namelist():
                if n.startswith("/") or ".." in n.split("/"):
                    raise ValueError("unsafe path in archive: " + n)
            zf.extractall(dest_dir)

def _find_extracted_root(dest_dir):
    """After extraction, locate the real web-root subdirectory. Prefer a dir
    that contains index.html; otherwise use dest_dir itself."""
    for entry in ("index.html",):
        if os.path.exists(os.path.join(dest_dir, entry)):
            return dest_dir
    # look one level deep
    for sub in sorted(os.listdir(dest_dir)):
        full = os.path.join(dest_dir, sub)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, "index.html")):
            return full
    return dest_dir

def install_dashboard(name):
    """Download latest release for `name` and atomically replace its directory."""
    latest, url, current = dashboard_latest_info(name)
    meta = _DASHBOARD_RELEASES[name]
    target = os.path.join(DASHBOARD_DIR, name)
    os.makedirs(DASHBOARD_DIR, exist_ok=True)

    tmpdir = tempfile.mkdtemp(prefix=f"{name}-upd-")
    archive_path = os.path.join(tmpdir, "pkg")
    try:
        # 下载
        import urllib.request as _ureq
        req = _ureq.Request(url, headers={"User-Agent": "ClashMini/1.0"})
        with _ureq.urlopen(req, timeout=180) as resp:
            with open(archive_path, "wb") as f:
                shutil.copyfileobj(resp, f)

        # 解压到独立目录
        stage_root = os.path.join(tmpdir, "stage")
        os.makedirs(stage_root, exist_ok=True)
        _safe_extract(archive_path, name, stage_root)
        web_root = _find_extracted_root(stage_root)

        # 写入版本标记
        vfile = os.path.join(web_root, "VERSION")
        with open(vfile, "w", encoding="utf-8") as f:
            f.write(latest)

        # 备份旧目录
        backup = None
        if os.path.exists(target):
            backup = target + ".bak"
            if os.path.exists(backup):
                shutil.rmtree(backup)
            os.rename(target, backup)

        # 原子移动
        try:
            shutil.move(web_root, target)
        except Exception:
            # 回滚旧目录
            if backup and os.path.exists(backup) and not os.path.exists(target):
                os.rename(backup, target)
            raise

        # 清理备份与临时目录
        if backup and os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"success": True, "version": latest, "previous": current}
    except Exception as e:
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
        return {"success": False, "error": str(e), "version": latest, "previous": current}


def write_config(content):
    """Save config safely via /var/apps/<app>/etc path (writable)."""
    # backup first
    if os.path.exists(CONFIG_FILE):
        shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    return True

def live_patch_config(payload):
    """热切换 mihomo 运行配置（不改文件、不重启引擎）。
    通过 PATCH /configs 立即生效。返回 (ok, status_or_error)。"""
    import urllib.request as _ureq
    import urllib.error as _uerror
    url = f"http://{MIHOMO_CTRL_HOST}:{MIHOMO_CTRL_PORT}/configs"
    body = json.dumps(payload).encode()
    req = _ureq.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    }, method="PATCH")
    try:
        with _ureq.urlopen(req, timeout=10) as resp:
            resp.read()
            return (resp.getcode() in (200, 204)), resp.getcode()
    except _uerror.HTTPError as e:
        return False, e.code
    except Exception as e:
        return False, str(e)

def set_mode_in_config(mode):
    """只精确改写 config.yaml 的 mode 行（保留其余字节与锚点），随后台校验。
    返回 (ok, message)。避免 yaml.safe_dump 全量重写破坏锚点结构。"""
    txt = read_config()
    if not txt.strip():
        return False, "config 为空"
    if not re.search(r"(?m)^mode:\s", txt):
        return False, "未能定位 mode 行"
    # 只把顶层 'mode: xxx' 改为目标值，其余保持原样（含 &default / <<: 锚点）
    new_txt = re.sub(r"(?m)^mode:.*$", f"mode: {mode}", txt, count=1)
    write_config(new_txt)
    return True, "ok"

def set_tun_in_config(enable):
    """只精确改写 config.yaml 里 tun: 段下的 enable 行（保留其余字节与锚点）。"""
    txt = read_config()
    if not txt.strip():
        return False, "config 为空"
    # 匹配顶层 tun: 段内的 enable: 行（缩进的行，位于 tun: 之后）：
    # 必须落在 tun: 段的缩进范围内，避免误改其它段。这里采用行级定位：
    # 先找顶层 'tun:' 行号，再找其下方第一个缩进不足的边界，在该区间内替换 enable。
    new_txt = _replace_tun_enable(txt, enable)
    if new_txt is None:
        # 找不到 tun 段：回退到 yaml 方式补一个 tun 段
        try:
            cfg = yaml.safe_load(txt) or {}
            cfg.setdefault("tun", {})["enable"] = enable
            write_config(yaml.safe_dump(cfg, allow_unicode=True, default_flow_style=False))
            return True, "ok"
        except Exception as e:
            return False, str(e)
    write_config(new_txt)
    return True, "ok"

def _replace_tun_enable(txt, enable):
    lines = txt.splitlines(keepends=True)
    tun_idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^tun:\s*$", ln):
            tun_idx = i
            break
    if tun_idx is None:
        return None
    # tun: 段的缩进基准（该行本身的缩进，通常是 0，但可能被嵌套）
    indent0 = len(lines[tun_idx]) - len(lines[tun_idx].lstrip())
    # 扫描 tun 段内的行，直到缩进 <= indent0 的下一段
    replaced = False
    for j in range(tun_idx + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        cur_indent = len(ln) - len(ln.lstrip())
        if cur_indent <= indent0:
            break  # 已离开 tun 段
        if re.match(r"^(\s*)enable:\s*(true|false|null|True|False)\s*$", ln):
            lines[j] = f"{' ' * cur_indent}enable: {'true' if enable else 'false'}\n"
            replaced = True
            break
    if not replaced:
        return None
    return "".join(lines)

def is_running():
    if not os.path.exists(PID_FILE):
        return False, None
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, 0)
        return True, pid
    except (ValueError, OSError):
        return False, None

def _find_engine_pids():
    """Find mihomo engine process(es) by real binary name (comm is truncated
    to 15 chars: 'mihomo-amd64.re', 'mihomo-arm64.re')."""
    pids = []
    try:
        out = subprocess.run(["pgrep", "-f", r"bin/mihomo-(amd64|arm64)\.real"],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.split():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    except Exception:
        pass
    return pids

def _port_free(port, host="127.0.0.1"):
    """Check whether the controller port is free (no listener)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex((host, port)) != 0
    except Exception:
        return True

def start_service():
    running, pid = is_running()
    if running:
        if pid and not _find_engine_pids():
            running = False
        else:
            return {"success": False, "error": "服务已在运行"}
    if not os.path.exists(MIHOMO_BIN):
        return {"success": False, "error": f"找不到 mihomo: {MIHOMO_BIN}"}
    try:
        # 启动服务时默认开启 TUN
        set_tun_in_config(True)
        for _ in range(10):
            if _port_free(MIHOMO_CTRL_PORT):
                break
            time.sleep(0.5)
        # 直接启动 wrapper（会自动拉起 admin、写 PID 文件、exec 引擎）
        env = dict(os.environ)
        env.setdefault("MIHOMO_APP_NAME", TRIM_APPNAME)
        env.setdefault("MIHOMO_DATA_DIR", TRIM_PKGVAR)
        env.setdefault("MIHOMO_DEST_DIR", TRIM_APPDEST)
        with open(f"{TRIM_PKGVAR}/{TRIM_APPNAME}.log", "a") as logf:
            subprocess.Popen([MIHOMO_BIN, "-d", TRIM_PKGVAR],
                             stdout=logf, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
        # 等待引擎起来（最多 20s）
        for _ in range(40):
            time.sleep(0.5)
            running, pid = is_running()
            if running and _find_engine_pids():
                return {"success": True, "pid": pid}
        running, pid = is_running()
        return {"success": running, "pid": pid}
    except Exception as e:
        return {"success": False, "error": str(e)}

def stop_service():
    running, pid = is_running()
    if not running and not _find_engine_pids():
        return {"success": True, "message": "服务未运行"}
    try:
        # 1) 终止真实引擎进程（SIGTERM 优雅退出）
        for ep in _find_engine_pids():
            try:
                os.kill(ep, signal.SIGTERM)
            except OSError:
                pass
        # 2) 终止 PID 文件记录的进程
        try:
            if pid:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        # 3) 兜底：wrapper 与启动器进程
        for pat in (r"bin/mihomo-amd64(\.real)?", r"bin/mihomo-arm64(\.real)?",
                    r"bin/mihomo -d"):
            try:
                subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=5)
            except Exception:
                pass
        time.sleep(1.5)
        # 4) 确认已停，必要时 SIGKILL
        for ep in _find_engine_pids():
            try:
                os.kill(ep, signal.SIGKILL)
            except OSError:
                pass
        # 5) 等待控制端口完全释放（最多 8s），避免新实例 bind 失败
        for _ in range(16):
            if _port_free(MIHOMO_CTRL_PORT):
                break
            time.sleep(0.5)
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except OSError:
            pass
        # 关闭服务时默认关闭 TUN
        set_tun_in_config(False)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

def extract_proxy_providers(yaml_text):
    """Parse proxy-providers section from yaml text."""
    try:
        data = yaml.safe_load(yaml_text) or {}
        return data.get("proxy-providers", {}) or {}
    except Exception:
        return {}

def _provider_block(name, url, interval, ptype="http"):
    block = f"  {name}:\n"
    block += f"    type: {ptype}\n"
    block += f"    url: \"{url}\"\n"
    block += f"    interval: {int(interval)}\n"
    block += "    health-check:\n"
    block += "      enable: true\n"
    block += "      url: https://www.gstatic.com/generate_204\n"
    block += "      interval: 180\n"
    return block

def _valid_provider_name(name):
    if not name or not name.strip():
        return False, "订阅名称不能为空"
    key = name.strip()
    if any(c in key for c in " \t:\n{}[]#,"):
        return False, "订阅名称不能包含空格或特殊字符"
    return True, key

def edit_provider_add(name, url, interval=3600, ptype="http"):
    """Insert/replace a proxy-provider entry in config.yaml text. No restart."""
    ok, key = _valid_provider_name(name)
    if not ok:
        return {"success": False, "error": key}
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return {"success": False, "error": "订阅链接必须以 http:// 或 https:// 开头"}
    try:
        interval = int(interval)
        if interval <= 0:
            interval = 3600
    except Exception:
        interval = 3600
    block = _provider_block(key, url, interval, ptype)
    content = read_config()
    # 1) proxy-providers: {}  → 展开为块
    m = re.search(r'^proxy-providers:\s*\{\}\s*$', content, re.M)
    if m:
        content = content[:m.start()] + "proxy-providers:\n" + block + content[m.end():]
        write_config(content)
        return {"success": True, "message": f"已添加订阅「{key}」"}
    # 2) 已有 proxy-providers: 块 → 替换同名或追加
    m = re.search(r'^proxy-providers:\s*$', content, re.M)
    if m:
        prefix = content[:m.start()]
        suffix = content[m.start():]
        # 替换同名：删除该 key 的旧块（不重启版）
        head, rest, found = _strip_provider_block(suffix, key)
        if found:
            write_config(prefix + "proxy-providers:\n" + head + block + rest)
            return {"success": True, "message": f"已更新订阅「{key}」"}
        # 追加：新块插入到 section 末尾（所有现有 key 块之后、顶层行之前）
        head2, rest2, _ = _strip_provider_block(suffix, "\x00never")
        write_config(prefix + "proxy-providers:\n" + head2 + block + rest2)
        return {"success": True, "message": f"已添加订阅「{key}」"}
    # 3) 完全不存在 → 追加到文件末尾
    content = content.rstrip("\n") + "\n\nproxy-providers:\n" + block
    write_config(content)
    return {"success": True, "message": f"已添加订阅「{key}」"}

def _strip_provider_block(section_text, key):
    """From text starting at 'proxy-providers:', remove the block for key.
    Returns (head_without_block, rest_after_section, found)."""
    lines = section_text.splitlines(keepends=True)
    head = []
    i = 1  # skip the 'proxy-providers:' header line (index 0)
    found = False
    while i < len(lines):
        ln = lines[i]
        if re.match(r'^\S', ln):           # 顶层 → 块结束
            break
        if re.match(r'^  \S', ln):         # 2 空格 key
            m2 = re.match(r'^  ([^:]+):', ln)
            if m2 and m2.group(1).strip() == key:
                found = True
                i += 1
                while i < len(lines) and re.match(r'^    ', lines[i]):
                    i += 1
                continue
        head.append(ln)
        i += 1
    rest = "".join(lines[i:]) if i < len(lines) else ""
    return "".join(head), rest, found

def edit_provider_delete(name):
    """Remove a proxy-provider entry from config.yaml text. No restart."""
    ok, key = _valid_provider_name(name)
    if not ok:
        return {"success": False, "error": key}
    content = read_config()
    m = re.search(r'^proxy-providers:\s*\{\}\s*$', content, re.M)
    if m:
        return {"success": False, "error": f"未找到订阅源「{key}」"}
    m = re.search(r'^proxy-providers:\s*$', content, re.M)
    if not m:
        return {"success": False, "error": "未找到 proxy-providers 配置"}
    prefix = content[:m.start()]
    head, rest, found = _strip_provider_block(content[m.start():], key)
    if not found:
        return {"success": False, "error": f"未找到订阅源「{key}」"}
    # 若块内已无任何 provider key → 折叠回 {}
    remaining = [ln for ln in head.splitlines(keepends=True) if re.match(r'^  \S', ln)]
    if remaining:
        new_section = "proxy-providers:\n" + head + rest
    else:
        new_section = "proxy-providers: {}\n" + rest.lstrip("\n")
    write_config(prefix + new_section)
    return {"success": True, "message": f"已删除订阅「{key}」"}

# ── 配置方案（Profiles）：本地 config.yaml 与下载的订阅自带配置 ──────
# 本地 config.yaml 永远保留；每一个"订阅自带配置"下载后按名称存到
# profiles/<name>.yaml（不会占用 config.yaml），通过引擎的 -f 参数被直接使用。
# active 状态记录当前使用哪一个："local" 或 profile 文件名。

def _http_get(url, timeout=20):
    import urllib.request as _ureq
    req = _ureq.Request(url, headers={
        "User-Agent": "fnSoar/1.0 (fnOS)",
        "Accept": "application/yaml,application/x-yaml,text/yaml,text/plain,*/*",
    })
    with _ureq.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data

# 本地偏好键：下载/上传订阅配置时，把本地 config.yaml 的这些设置合并进订阅配置
# （对齐 clash-verge-rev 的 merge 增强——用户本地偏好覆盖订阅，节点/规则保留）


def _test_config(path):
    """mihomo -t 校验配置；返回 (ok, msg)。"""
    try:
        r = subprocess.run([MIHOMO_BIN, "-t", "-d", TRIM_PKGVAR, "-f", path],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, "配置校验通过"
        err = (r.stderr or r.stdout or "").strip().splitlines()
        return False, (err[-1] if err else "配置校验失败")
    except Exception as e:
        return False, f"校验异常: {e}"

_IPINFO_CACHE = {"t": 0, "data": None}
_SYSINFO_CACHE = {"t": 0, "data": None}

def get_ip_info():
    """公网 IP 信息，带 60s 内存缓存（首页每次加载都调用，避免每次外网查询拖慢页面）。"""
    now = int(time.time())
    if _IPINFO_CACHE["data"] is not None and now - _IPINFO_CACHE["t"] < 60:
        return _IPINFO_CACHE["data"]
    data = _get_ipinfo_uncached()
    # 查询成功(拿到真实 ip)才缓存
    if data and data.get("ip") and data["ip"] != "-":
        _IPINFO_CACHE["t"] = now
        _IPINFO_CACHE["data"] = data
    return data

def _get_ipinfo_uncached():
    """Query the public exit IP via external APIs (best effort, clash-verge-rev style).
    Tries multiple services in order; returns the first success.
    Field set matches Clash Verge Rev's IP-info card:
    ip / asn(自治域) / isp(服务商) / organization(组织) / location(位置) / timezone(时区)."""
    import urllib.request as _ureq
    import ssl
    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE

    def _fetch(url, timeout=6, use_ctx=False):
        req = _ureq.Request(url, headers={"User-Agent": "curl/8"}, method="GET")
        kw = {"timeout": timeout}
        if url.startswith("https"):
            kw["context"] = _ctx
        with _ureq.urlopen(req, **kw) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    # Service 1: api.ip.sb (most complete fields)
    try:
        d = _fetch("https://api.ip.sb/geoip")
        return {
            "ip": d.get("ip") or "-",
            "country": d.get("country") or "",
            "countryCode": (d.get("country_code") or "").upper(),
            "region": d.get("region") or "",
            "city": d.get("city") or "",
            "isp": d.get("isp") or d.get("organization") or "",
            "organization": d.get("organization") or "",
            "asn": d.get("asn") or "",
            "asn_organization": d.get("asn_organization") or "",
            "timezone": d.get("timezone") or "",
            "latitude": d.get("latitude"),
            "longitude": d.get("longitude"),
        }
    except Exception:
        pass
    # Service 2: ipapi.co
    try:
        d = _fetch("https://ipapi.co/json")
        return {
            "ip": d.get("ip") or "-",
            "country": d.get("country_name") or "",
            "countryCode": (d.get("country_code") or "").upper(),
            "region": d.get("region") or "",
            "city": d.get("city") or "",
            "isp": d.get("org") or "",
            "organization": d.get("org") or "",
            "asn": d.get("asn") or "",
            "asn_organization": d.get("asn") or "",
            "timezone": d.get("timezone") or "",
            "latitude": d.get("latitude"),
            "longitude": d.get("longitude"),
        }
    except Exception:
        pass
    # Service 3: ip-api.com (HTTP only, supports Chinese)
    try:
        d = _fetch(
            "http://ip-api.com/json/?lang=zh-CN&fields=status,query,country,countryCode,regionName,city,isp,as,org,timezone")
        if d.get("status") == "success":
            asn = ""
            asn_org = ""
            m = re.match(r"^(AS\d+)\s*(.*)$", d.get("as") or "")
            if m:
                asn = m.group(1).replace("AS", "")
                asn_org = m.group(2)
            return {
                "ip": d.get("query") or "-",
                "country": d.get("country") or "",
                "countryCode": (d.get("countryCode") or "").upper(),
                "region": d.get("regionName") or "",
                "city": d.get("city") or "",
                "isp": d.get("isp") or "",
                "organization": d.get("org") or "",
                "asn": asn,
                "asn_organization": asn_org or d.get("org") or "",
                "timezone": d.get("timezone") or "",
                "latitude": d.get("lat"),
                "longitude": d.get("lon"),
            }
    except Exception:
        pass
    # Service 4: ipinfo.io
    try:
        d = _fetch("https://ipinfo.io/json")
        org = d.get("org") or ""
        asn = ""
        m = re.match(r"^AS(\d+)\s*(.*)$", org)
        if m:
            asn = m.group(1)
            org = m.group(2)
        loc = (d.get("loc") or "").split(",")
        return {
            "ip": d.get("ip") or "-",
            "country": d.get("country") or "",
            "countryCode": (d.get("country") or "").upper(),
            "region": d.get("region") or "",
            "city": d.get("city") or "",
            "isp": org,
            "organization": org,
            "asn": asn,
            "asn_organization": org,
            "timezone": d.get("timezone") or "",
            "latitude": float(loc[0]) if len(loc) > 0 and loc[0] else None,
            "longitude": float(loc[1]) if len(loc) > 1 and loc[1] else None,
        }
    except Exception:
        pass
    # All services failed - return error object so frontend can show message
    return {"ip": "-", "error": "无法获取 IP 信息，请检查网络连接"}

def get_system_info():
    """系统信息,带 2s 缓存避免每次 sleep(0.4) 采样 CPU。"""
    now = time.time()
    if _SYSINFO_CACHE["data"] is not None and now - _SYSINFO_CACHE["t"] < 2:
        return _SYSINFO_CACHE["data"]
    info = get_system_info_cached()
    _SYSINFO_CACHE["t"] = now
    _SYSINFO_CACHE["data"] = info
    return info

def get_system_info_cached():
    """Read host CPU/memory/disk/uptime info (best effort)."""
    info = {}
    # CPU usage from /proc/stat (two samples)
    try:
        def _cpu():
            with open("/proc/stat") as f:
                parts = f.readline().split()
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            return idle, total
        i1, t1 = _cpu()
        time.sleep(0.4)
        i2, t2 = _cpu()
        d_total = t2 - t1
        d_idle = i2 - i1
        info["cpu_percent"] = round(100 * (1 - d_idle / d_total), 1) if d_total else 0
    except Exception:
        info["cpu_percent"] = None
    # Memory
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0])  # kB
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        info["mem_total"] = total * 1024
        info["mem_used"] = max(0, (total - avail)) * 1024
        info["mem_percent"] = round(100 * (1 - avail / total), 1) if total else 0
    except Exception:
        info["mem_total"] = info["mem_used"] = info["mem_percent"] = None
    # Load
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        info["load1"] = parts[0]
        info["load5"] = parts[1]
        info["load15"] = parts[2]
    except Exception:
        pass
    # Uptime
    try:
        with open("/proc/uptime") as f:
            info["uptime"] = float(f.read().split()[0])
    except Exception:
        pass
    # Disk
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        info["disk_total"] = total
        info["disk_used"] = total - free
        info["disk_percent"] = round(100 * (1 - free / total), 1) if total else 0
    except Exception:
        info["disk_total"] = info["disk_used"] = info["disk_percent"] = None
    # Hostname & kernel
    try:
        import platform
        info["hostname"] = platform.node()
        info["platform"] = platform.platform()
    except Exception:
        pass
    # App version
    try:
        info["app_version"] = _get_app_version()
    except Exception:
        pass
    return info

# ── HTTP Request Handler ─────────────────────────────────────────────────
class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Unix socket clients have no IP (client_address is a bare string like ""),
        # so address_string() would raise IndexError and kill the whole request —
        # that made every fnOS gateway request fail (empty reply). Guard it.
        try:
            addr = self.address_string()
        except Exception:
            addr = "unix"
        log(f"{addr} - {fmt % args}")

    def _strip_gateway_prefix(self, path):
        """fnOS gateway may forward /app/fnnas.fnsoar/... with the prefix intact.
        Strip it so routing below works either way."""
        prefix = os.environ.get("MIHOMO_GATEWAY_PREFIX", "/app/fnnas.fnsoar")
        if path == prefix or path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
        return path

    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _send_file(self, path, content_type="text/html"):
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _send_icon(self, path, content_type="image/png"):
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    # ── fnSoar Clash API reverse proxy ─────────────────────────────────
    _CLASH_API_PREFIXES = (
        "/version", "/proxies", "/group", "/rules", "/configs", "/traffic",
        "/connections", "/logs", "/providers/proxies", "/providers/rules",
    )

    def _maybe_proxy_clash_api(self, path, method="GET", body=b""):
        """If path is a known Clash API endpoint, proxy to mihomo.
        Returns True when the response was already sent (never falls through)."""
        if any(path == p or path.startswith(p + "/") or path.startswith(p + "?")
               for p in self._CLASH_API_PREFIXES):
            qs = urlparse(self.path).query
            sub = path.lstrip("/")
            if qs:
                sub += "?" + qs
            self._proxy_mihomo(sub, self.headers, method, body)
            return True
        return False

    def _proxy_mihomo(self, sub_path, headers, method="GET", body=b""):
        """Forward /api/mihomo/<rest> or catch-all → http://mihomo-ctrl:9090/<rest>.
        Always sends a response; returns nothing meaningful."""
        try:
            import urllib.request as _ureq
            import urllib.error as _uerror
            url = f"http://{MIHOMO_CTRL_HOST}:{MIHOMO_CTRL_PORT}/{sub_path}"
            fwd_headers = {
                "Accept": headers.get("Accept", "application/json"),
                "Content-Type": headers.get("Content-Type", "application/json"),
            }
            # If the controller has a secret configured, attach basic auth.
            try:
                cfg = yaml.safe_load(read_config()) or {}
                ec = cfg.get("external-controller", "")
                if isinstance(ec, dict):
                    secret = ec.get("secret")
                elif isinstance(ec, str) and ":" in ec:
                    # '0.0.0.0:9090' form — no secret.
                    secret = None
                else:
                    secret = None
                if secret:
                    import base64 as _b64
                    token = _b64.b64encode(f":{secret}".encode()).decode()
                    fwd_headers["Authorization"] = f"Basic {token}"
            except Exception:
                pass
            req = _ureq.Request(url, data=body if body else None,
                                headers=fwd_headers, method=method)
            try:
                with _ureq.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                    if (resp.headers.get("Content-Type", "")
                            .split(";", 1)[0].strip().lower()
                            == "application/json"):
                        try:
                            icon_base = ""
                            if hasattr(self, "path"):
                                prefix = os.environ.get("MIHOMO_GATEWAY_PREFIX", "/app/fnnas.fnsoar")
                                if self.path == prefix or self.path.startswith(prefix + "/"):
                                    icon_base = prefix
                            data = json.dumps(
                                expand_api_icons(json.loads(data), icon_base),
                                ensure_ascii=False,
                            ).encode("utf-8")
                        except (TypeError, ValueError):
                            pass
                    self.send_response(resp.getcode())
                    self.send_header("Content-Type",
                                     resp.headers.get("Content-Type",
                                                      "application/json"))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            except _uerror.HTTPError as e:
                # 上游 mihomo 返回了明确的错误状态码（400/404/503 等），
                # 原样透传给前端，而不是全部伪装成 502。
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type",
                                 e.headers.get("Content-Type",
                                               "application/json"))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self._send_json({"error": str(e)}, 502)

    # ── WebSocket proxy (for /traffic, /logs live updates) ───────────
    def _maybe_proxy_websocket(self, path):
        """Forward WebSocket upgrade requests (e.g. /traffic) to mihomo.
        Returns True when the path was handled."""
        if self.headers.get("Upgrade", "").lower() != "websocket":
            return False
        if not any(path == p or path.startswith(p + "/") or path.startswith(p + "?")
                   for p in ("/traffic", "/logs", "/connections")):
            return False
        try:
            import socket as _sock
            qs = urlparse(self.path).query
            sub = path.lstrip("/")
            if qs:
                sub += "?" + qs
            # Connect to the real controller and replay the upgrade request.
            upstream = _sock.create_connection(
                (MIHOMO_CTRL_HOST, MIHOMO_CTRL_PORT), timeout=10)
            req = (f"GET /{sub} HTTP/1.1\r\n"
                   f"Host: {MIHOMO_CTRL_HOST}:{MIHOMO_CTRL_PORT}\r\n"
                   f"Upgrade: websocket\r\n"
                   f"Connection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {self.headers.get('Sec-WebSocket-Key','')}\r\n"
                   f"Sec-WebSocket-Version: {self.headers.get('Sec-WebSocket-Version','13')}\r\n"
                   f"Origin: {self.headers.get('Origin','')}\r\n\r\n")
            upstream.sendall(req.encode("utf-8"))
            # Read upstream's 101 response head.
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                resp += chunk
            # Forward the handshake response to the client.
            self.connection.sendall(resp)
            status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 101 " not in status_line:
                # Upstream refused the upgrade; drain and close.
                try:
                    while True:
                        chunk = upstream.recv(4096)
                        if not chunk:
                            break
                        self.connection.sendall(chunk)
                except Exception:
                    pass
                upstream.close()
                return True
            # Bidirectional byte relay.
            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except Exception:
                    pass
                finally:
                    try:
                        dst.shutdown(_sock.SHUT_WR)
                    except Exception:
                        pass
            t1 = threading.Thread(target=pump, args=(self.connection, upstream), daemon=True)
            t2 = threading.Thread(target=pump, args=(upstream, self.connection), daemon=True)
            t1.start(); t2.start()
            t1.join(); t2.join()
            try: upstream.close()
            except Exception: pass
            return True
        except Exception as e:
            log(f"WebSocket proxy error for {path}: {e}")
            return True

    # ── Dashboard static files (zashboard, metacubexd) ────────────────
    def _try_serve_dashboard(self, path):
        """Serve files under /zashboard/* and /metacubexd/* from DASHBOARD_DIR.
        Returns False if the path is not under a dashboard prefix,
        the response was served otherwise (including SPA fallback)."""
        for sub in ("zashboard", "metacubexd"):
            if path == f"/{sub}" or path == f"/{sub}/":
                return self._send_file(f"{DASHBOARD_DIR}/{sub}/index.html", "text/html")
            if path.startswith(f"/{sub}/"):
                rel = path[len(f"/{sub}/"):]
                target = f"{DASHBOARD_DIR}/{sub}/{rel}"
                if os.path.exists(target) and os.path.isfile(target):
                    ext = os.path.splitext(target)[1].lower()
                    mime = {
                        ".html": "text/html",
                        ".css": "text/css",
                        ".js": "application/javascript",
                        ".json": "application/json",
                        ".svg": "image/svg+xml",
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".ico": "image/x-icon",
                        ".woff": "font/woff",
                        ".woff2": "font/woff2",
                        ".ttf": "font/ttf",
                    }.get(ext, "application/octet-stream")
                    return self._send_file(target, mime)
                # Dashboard builds may emit icon names as relative image URLs
                # (for example /zashboard/ChatGPT). Resolve those names from
                # icons.yaml before falling back to the SPA entry point.
                icon_name = unquote_to_bytes(rel).decode("utf-8", "replace")
                icon_map = _get_icons_map()
                if "/" not in icon_name and icon_name in icon_map:
                    value = icon_map[icon_name]
                    if isinstance(value, str) and value.lower().startswith("data:"):
                        try:
                            header, encoded = value.split(",", 1)
                            ctype = header[5:].split(";", 1)[0] or "image/png"
                            data = (base64.b64decode(encoded)
                                    if "base64" in header.lower()
                                    else unquote_to_bytes(encoded))
                            self.send_response(200)
                            self.send_header("Content-Type", ctype)
                            self.send_header("Cache-Control", "public, max-age=86400")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()
                            self.wfile.write(data)
                            return True
                        except Exception:
                            return self.send_error(500, "invalid named icon")
                    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
                        local, ctype = localize_icon(value)
                        if local:
                            return self._send_icon(local, ctype or "image/png")
                        return self.send_error(502, "icon fetch failed")
                # SPA fallback: serve index.html for unknown sub-paths
                idx = f"{DASHBOARD_DIR}/{sub}/index.html"
                if os.path.exists(idx):
                    return self._send_file(idx, "text/html")
                return False
        return False

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self._strip_gateway_prefix(urlparse(self.path).path)
        # API endpoints
        if path == "/api/status":
            running, pid = is_running()
            return self._send_json({"running": running, "pid": pid,
                                    "control_port": MIHOMO_CTRL_PORT})
        if path == "/api/config":
            return self._send_json({"config": read_config()})
        if path == "/api/proxy-providers":
            yaml_text = read_config()
            providers = extract_proxy_providers(yaml_text)
            return self._send_json({"providers": providers})
        if path == "/api/group-icons":
            # 策略组图标 + 配置顺序：读 config.yaml 中 proxy-groups 各组的 icon 与排列顺序；
            # http(s) 图标已本地化的用 base64 data:URI 内嵌，前端一次拿到即可秒渲染（无需逐张请求）
            try:
                cfg = yaml.safe_load(read_config()) or {}
                icons, order, hidden = {}, [], {}
                for g in (cfg.get("proxy-groups") or []):
                    if isinstance(g, dict) and g.get("name"):
                        if g.get("icon"):
                            icons[g["name"]] = embedded_icon(g["icon"])
                        if g.get("hidden"):
                            hidden[g["name"]] = True
                        order.append(g["name"])
                return self._send_json({"icons": icons, "order": order, "hidden": hidden})
            except Exception:
                return self._send_json({"icons": {}, "order": []})

        if path == "/api/icon":
            # 图标本地化：接受 config 里填写的 http(s) 图标 URL，下载到本地缓存后读取
            qs = parse_qs(urlparse(self.path).query)

            # Serve named icons from icons.yaml as normal HTTP images.
            name = (qs.get("name") or [""])[0]
            icon_map = _get_icons_map()
            if name in icon_map:
                value = icon_map[name]
                if isinstance(value, str) and value.lower().startswith("data:"):
                    try:
                        header, encoded = value.split(",", 1)
                        ctype = header[5:].split(";", 1)[0] or "image/png"
                        if "base64" in header.lower():
                            data = base64.b64decode(encoded)
                        else:
                            from urllib.parse import unquote_to_bytes
                            data = unquote_to_bytes(encoded)
                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except Exception:
                        return self.send_error(500, "invalid named icon")
                if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
                    local, ctype = localize_icon(value)
                    if local:
                        if not ctype:
                            ctype = _ICON_EXT_MIME.get("." + local.rsplit(".", 1)[-1].lower(), "image/png")
                        return self._send_icon(local, ctype)
                    return self.send_error(502, "icon fetch failed")

            url = (qs.get("url") or [""])[0].strip()
            if not url or not url.lower().startswith(("http://", "https://")):
                return self.send_error(400, "invalid icon url")
            local, ctype = localize_icon(url)
            if local:
                if not ctype:
                    ctype = _ICON_EXT_MIME.get("." + local.rsplit(".", 1)[-1].lower(), "image/png")
                return self._send_icon(local, ctype)
            return self.send_error(502, "icon fetch failed")

        if path == "/api/providers/update":
            # 触发指定订阅源立即更新 (PUT /providers/proxies/{name})
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            if not name:
                return self._send_json({"success": False, "error": "缺少订阅名称"}, 400)
            return self._proxy_mihomo(
                f"providers/proxies/{quote(name)}",
                self.headers, "PUT", b"")
        if path == "/api/logs":
            lines = 200
            try:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    content = "".join(f.readlines()[-lines:])
            except FileNotFoundError:
                content = "日志文件不存在"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
            return
        if path == "/api/ipinfo":
            return self._send_json(get_ip_info())
        if path == "/api/systeminfo":
            return self._send_json(get_system_info())
        if path == "/api/unlock/items":
            # 解锁测试：默认测试项列表（Pending），内容对齐 Clash Verge Rev
            from media_unlock import default_unlock_items
            return self._send_json(default_unlock_items())
        if path == "/api/tun":
            # 真实 TUN 状态：读 config.yaml 的 tun.enable（mihomo GET /configs 返回
            # 的是静态快照，动态 PUT 不生效，故以配置文件为准）
            try:
                cfg = yaml.safe_load(read_config()) or {}
                tun = cfg.get("tun") or {}
                return self._send_json({"enable": bool(tun.get("enable", False))})
            except Exception:
                return self._send_json({"enable": False})
        if path == "/api/mode":
            # 真实代理模式：优先读引擎实时 mode（热切换后立即生效），
            # 引擎不可用时回退到 config.yaml 的 mode。
            try:
                import urllib.request as _ureq
                url = f"http://{MIHOMO_CTRL_HOST}:{MIHOMO_CTRL_PORT}/configs"
                with _ureq.urlopen(_ureq.Request(url, headers={"Accept": "application/json"}), timeout=3) as resp:
                    d = json.loads(resp.read())
                    if d.get("mode"):
                        return self._send_json({"mode": d["mode"]})
            except Exception:
                pass
            try:
                cfg = yaml.safe_load(read_config()) or {}
                return self._send_json({"mode": cfg.get("mode", "rule")})
            except Exception:
                return self._send_json({"mode": "rule"})
        if path == "/api/version":
            try:
                import urllib.request as _ureq
                core_ver = "unknown"
                try:
                    url = f"http://{MIHOMO_CTRL_HOST}:{MIHOMO_CTRL_PORT}/version"
                    with _ureq.urlopen(_ureq.Request(url, headers={"Accept": "application/json"}), timeout=3) as resp:
                        d = json.loads(resp.read())
                        core_ver = d.get("version", "unknown")
                except Exception:
                    pass
                app_ver = _get_app_version()
                return self._send_json({"core": core_ver, "app": app_ver, "config_dir": TRIM_PKGVAR, "core_dir": TRIM_APPDEST})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/check-update":
            try:
                import urllib.request as _ureq
                url = "https://api.github.com/repos/huiikeung/fnclash/releases/latest"
                req = _ureq.Request(url, headers={"User-Agent": "ClashMini/1.0", "Accept": "application/vnd.github+json"})
                with _ureq.urlopen(req, timeout=10) as resp:
                    d = json.loads(resp.read())
                    latest = d.get("tag_name", "").lstrip("v")
                    cur = "unknown"
                    try:
                        vf = os.path.join(TRIM_APPDEST, "VERSION")
                        if os.path.exists(vf):
                            with open(vf, "r") as f: cur = f.read().strip()
                    except Exception:
                        pass
                    return self._send_json({"latest": latest, "current": cur, "url": d.get("html_url", ""), "has_update": latest != cur and latest != ""})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/core-latest":
            try:
                import urllib.request as _ureq
                api = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
                req = _ureq.Request(api, headers={"User-Agent": "ClashMini/1.0", "Accept": "application/vnd.github+json"})
                with _ureq.urlopen(req, timeout=15) as resp:
                    d = json.loads(resp.read())
                ver = (d.get("tag_name", "") or "").lstrip("v")
                dl = ""
                for a in d.get("assets", []):
                    n = a.get("name", "")
                    if "linux-amd64" in n and "compatible" in n and n.endswith(".gz"):
                        dl = a.get("browser_download_url", ""); break
                if not dl:
                    for a in d.get("assets", []):
                        n = a.get("name", "")
                        if n.startswith("mihomo-linux-amd64-") and n.endswith(".gz") and "compatible" not in n:
                            dl = a.get("browser_download_url", ""); break
                return self._send_json({"success": True, "version": ver, "url": dl})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/dashboards":
            # 查询面板升级状态：已安装版本 / 最新版本 / 是否有更新
            try:
                result = []
                for name in ("zashboard", "metacubexd"):
                    try:
                        latest, url, current = dashboard_latest_info(name)
                        has = bool(latest) and (not current or latest != current)
                        result.append({
                            "name": name,
                            "current": current or "",
                            "latest": latest,
                            "url": url,
                            "has_update": has,
                        })
                    except Exception as e:
                        result.append({"name": name, "error": str(e),
                                       "current": _read_dashboard_version(name) or ""})
                return self._send_json({"dashboards": result})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        # Clash API proxy: dashboards under /zashboard or /metacubexd that try to hit
        # absolute paths like /version, /proxies, etc. should be forwarded to mihomo.
        if self._maybe_proxy_websocket(path):
            return
        if self._maybe_proxy_clash_api(path):
            return
        # Dashboard static files (zashboard, metacubexd) — served from /vol1/@appdata/fnnas.fnsoar/dashboard/
        served = self._try_serve_dashboard(path)
        if served is not False:
            return served
        # Admin panel static files
        if path == "/" or path == "":
            return self._send_file(f"{ADMIN_DIR}/index.html", "text/html")
        if path == "/ui" or path == "/ui/" or path.startswith("/ui/"):
            # Web UI: redirect to admin panel root
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path.endswith(".html"):
            return self._send_file(f"{ADMIN_DIR}{path}", "text/html")
        if path.endswith(".css"):
            return self._send_file(f"{ADMIN_DIR}{path}", "text/css")
        if path.endswith(".js"):
            return self._send_file(f"{ADMIN_DIR}{path}", "application/javascript")
        if path.endswith(".png"):
            return self._send_file(f"{ADMIN_DIR}{path}", "image/png")
        self.send_error(404)

    def do_POST(self):
        """POST 端点：服务开关 / TUN / GEO 更新 / 内核更新 / 订阅管理 / 模式 / 配置保存等。"""
        path = self._strip_gateway_prefix(urlparse(self.path).path)
        body = self._read_body()
        if path == "/api/dashboard-update":
            try:
                data = json.loads(body) if body else {}
                name = (data.get("name") or "").strip()
                if name not in _DASHBOARD_RELEASES:
                    return self._send_json({"success": False, "error": "不支持的面板: " + name}, 400)
                result = install_dashboard(name)
                return self._send_json(result, 200 if result.get("success") else 500)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/service":
            try:
                data = json.loads(body) if body else {}
                enable = bool(data.get("enable"))
                if enable:
                    ok = start_service()
                else:
                    ok = stop_service()
                if not ok:
                    return self._send_json({"success": False, "error": "操作失败"}, 500)
                return self._send_json({"success": True, "message": "服务已" + ("启动" if enable else "停止")})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/tun":
            try:
                data = json.loads(body) if body else {}
                enable = bool(data.get("enable"))
            except Exception:
                return self._send_json({"success": False, "error": "参数错误"}, 400)
            try:
                ok, msg = set_tun_in_config(enable)
                if not ok:
                    return self._send_json({"success": False, "error": "修改 TUN 配置失败: " + msg}, 500)
                return self._send_json({"success": True,
                                        "message": "TUN 已" + ("开启" if enable else "关闭") + "（重启服务后生效）"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/update-geo":
            try:
                import subprocess
                cmd = [MIHOMO_BIN, "-d", TRIM_PKGVAR, "update", "geodata"]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    return self._send_json({"success": True, "message": "GEO 数据已更新"})
                else:
                    return self._send_json({"success": False, "error": r.stderr.strip() or "更新失败"}, 500)
            except subprocess.TimeoutExpired:
                return self._send_json({"success": False, "error": "更新超时"}, 500)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/update-core":
            try:
                data = json.loads(body) if body else {}
                url = data.get("url", "")
                if not url:
                    import urllib.request as _ureq
                    api = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
                    req = _ureq.Request(api, headers={"User-Agent": "ClashMini/1.0", "Accept": "application/vnd.github+json"})
                    with _ureq.urlopen(req, timeout=15) as resp:
                        d = json.loads(resp.read())
                    # 优先 compatible（兼容旧 CPU），其次 amd64
                    for a in d.get("assets", []):
                        n = a.get("name", "")
                        if "linux-amd64" in n and "compatible" in n and n.endswith(".gz"):
                            url = a.get("browser_download_url", ""); break
                    if not url:
                        for a in d.get("assets", []):
                            n = a.get("name", "")
                            if n.startswith("mihomo-linux-amd64-") and n.endswith(".gz") and "compatible" not in n:
                                url = a.get("browser_download_url", ""); break
                if not url:
                    return self._send_json({"success": False, "error": "无法获取 mihomo 下载地址"}, 400)
                import subprocess, gzip
                dest = os.path.join(TRIM_APPDEST, "bin", "mihomo-amd64.real.new")
                tmp = dest + ".gz"
                r = subprocess.run(["wget", "-q", "-O", tmp, url], capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    return self._send_json({"success": False, "error": "下载失败: " + (r.stderr.strip() or "")}, 500)
                try:
                    with gzip.open(tmp, "rb") as fin, open(dest, "wb") as fout:
                        fout.write(fin.read())
                except Exception:
                    os.rename(tmp, dest)
                finally:
                    if os.path.exists(tmp): os.remove(tmp)
                os.chmod(dest, 0o755)
                try:
                    vr = subprocess.run([dest, "-v"], capture_output=True, text=True, timeout=10)
                    if vr.returncode != 0:
                        os.remove(dest)
                        return self._send_json({"success": False, "error": "下载的文件无法执行，已回滚"}, 500)
                except Exception:
                    os.remove(dest)
                    return self._send_json({"success": False, "error": "下载的文件无法执行，已回滚"}, 500)
                old = os.path.join(TRIM_APPDEST, "bin", "mihomo-amd64.real")
                bak = old + ".bak"
                if os.path.exists(bak): os.remove(bak)
                if os.path.exists(old): os.rename(old, bak)
                os.rename(dest, old)
                return self._send_json({"success": True, "message": "内核已更新，请点击「重启内核」生效"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/restart-core":
            try:
                import subprocess
                cli = "/usr/local/bin/appcenter-cli"
                cmd = f"cd /vol1/@appcenter/fnnas.fnsoar && {cli} restart fnnas.fnsoar"
                subprocess.Popen(["sh", "-c", "sleep 1 && " + cmd + " >/dev/null 2>&1 &"], start_new_session=True)
                return self._send_json({"success": True, "message": "正在重启内核…"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/mode":
            try:
                data = json.loads(body) if body else {}
                mode = data.get("mode", "")
            except Exception:
                return self._send_json({"success": False, "error": "参数错误"}, 400)
            if mode not in ("rule", "global", "direct"):
                return self._send_json({"success": False, "error": "模式必须是 rule/global/direct"}, 400)
            try:
                ok, st = live_patch_config({"mode": mode})
                if not ok:
                    set_mode_in_config(mode)
                    stop_service()
                    time.sleep(1)
                    start_service()
                    return self._send_json({"success": True, "message": "已切换为" + {"rule":"规则","global":"全局","direct":"直连"}[mode] + "模式"})
                set_mode_in_config(mode)
                return self._send_json({"success": True, "message": "已切换为" + {"rule":"规则","global":"全局","direct":"直连"}[mode] + "模式"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/providers":
            try:
                data = json.loads(body) if body else {}
                name = data.get("name", "")
                url = data.get("url", "")
                interval = data.get("interval", 3600)
                ptype = data.get("type", "http")
                result = edit_provider_add(name, url, interval, ptype)
                if not result.get("success"):
                    return self._send_json(result, 400)
                stop_service()
                time.sleep(1)
                start_service()
                return self._send_json(result)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/providers/delete":
            try:
                data = json.loads(body) if body else {}
                result = edit_provider_delete(data.get("name", ""))
                if not result.get("success"):
                    return self._send_json(result, 400)
                stop_service()
                time.sleep(1)
                start_service()
                return self._send_json(result)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/config":
            try:
                data = json.loads(body) if body else {}
                cfg_text = data.get("config", "")
                if write_config(cfg_text):
                    stop_service()
                    time.sleep(1)
                    start_service()
                    return self._send_json({"success": True, "message": "配置已保存！服务正在重启..."})
                return self._send_json({"success": False, "error": "保存失败"}, 500)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/config-section":
            try:
                data = json.loads(body) if body else {}
                section = data.get("section", "")
                yaml_text = data.get("yaml", "")
                if not section:
                    return self._send_json({"success": False, "error": "缺少 section"}, 400)
                text = read_config()
                import re as _re
                pattern = r'^' + _re.escape(section) + r':.*?(?=^\S|\Z)'
                replacement = yaml_text if yaml_text.endswith('\n') else yaml_text + '\n'
                new_text = _re.sub(pattern, replacement, text, count=1, flags=_re.MULTILINE|_re.DOTALL)
                if new_text == text:
                    return self._send_json({"success": False, "error": f"未找到 section: {section}"}, 400)
                if write_config(new_text):
                    return self._send_json({"success": True, "message": f"{section} 已保存"})
                return self._send_json({"success": False, "error": "保存失败"}, 500)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/config-section-reset":
            try:
                data = json.loads(body) if body else {}
                section = data.get("section", "")
                if not section:
                    return self._send_json({"success": False, "error": "缺少 section"}, 400)
                default_cfg = f"{TRIM_APPDEST}/default-config/config.yaml"
                if not os.path.exists(default_cfg):
                    return self._send_json({"success": False, "error": "默认模板不存在"}, 500)
                with open(default_cfg, "r") as f:
                    default_text = f.read()
                import re as _re
                pattern = r'^' + _re.escape(section) + r':.*?(?=^\S|\Z)'
                m = _re.search(pattern, default_text, _re.MULTILINE|_re.DOTALL)
                if not m:
                    return self._send_json({"success": False, "error": f"默认模板中未找到 section: {section}"}, 400)
                default_section = m.group(0)
                text = read_config()
                new_text = _re.sub(pattern, default_section if default_section.endswith('\n') else default_section + '\n', text, count=1, flags=_re.MULTILINE|_re.DOTALL)
                if write_config(new_text):
                    return self._send_json({"success": True, "message": f"{section} 已重置为默认"})
                return self._send_json({"success": False, "error": "保存失败"}, 500)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        self.send_error(404)

    def _read_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(content_length) if content_length else b""

    def do_PUT(self):
        """Proxy PUT requests (e.g. /proxies/:sel, /configs mode) to mihomo."""
        path = self._strip_gateway_prefix(urlparse(self.path).path)
        if path == "/api/providers/update":
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            if not name:
                return self._send_json({"success": False, "error": "缺少订阅名称"}, 400)
            return self._proxy_mihomo(
                f"providers/proxies/{quote(name)}",
                self.headers, "PUT", b"")
        if self._maybe_proxy_clash_api(path, "PUT", self._read_body()):
            return
        qs = urlparse(self.path).query
        sub = path.lstrip("/")
        if qs:
            sub += "?" + qs
        return self._proxy_mihomo(sub, self.headers, "PUT", self._read_body())

    def do_DELETE(self):
        """Proxy DELETE requests to mihomo (e.g. cache flush)."""
        path = self._strip_gateway_prefix(urlparse(self.path).path)
        if self._maybe_proxy_clash_api(path, "DELETE", b""):
            return
        qs = urlparse(self.path).query
        sub = path.lstrip("/")
        if qs:
            sub += "?" + qs
        return self._proxy_mihomo(sub, self.headers, "DELETE", b"")

# ── Servers ──────────────────────────────────────────────────────────────
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # SO_REUSEADDR allows a quick restart even if the previous socket is
    # still in TIME_WAIT (prevents "Address already in use" after a crash).
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

class UnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True

class UnixHTTPHandler(AdminHandler):
    """Same handler, but for Unix socket — fnOS gateway proxies here."""
    pass

def start_unix_socket_server():
    """Run a Unix socket HTTP server for fnOS gateway integration.

    Failures here are non-fatal: the admin panel is fully usable over the
    TCP port (9099) even without the Unix gateway."""
    try:
        sock_dir = os.path.dirname(SOCKET_PATH)
        os.makedirs(sock_dir, exist_ok=True)
        if os.path.exists(SOCKET_PATH):
            try: os.remove(SOCKET_PATH)
            except OSError: pass
        server = UnixHTTPServer(SOCKET_PATH, UnixHTTPHandler)
        os.chmod(SOCKET_PATH, 0o666)
        log(f"Unix socket gateway listening at {SOCKET_PATH}")
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server
    except Exception as e:
        log(f"WARNING: Unix socket gateway disabled: {e}")
        return None

def main():
    log(f"Starting fnSoar Admin Server")
    log(f"  HTTP port : {ADMIN_PORT}")
    log(f"  Unix sock : {SOCKET_PATH}")
    log(f"  Config    : {CONFIG_FILE}")

    # Start Unix socket server (for fnOS iframe integration) — non-fatal if it fails
    start_unix_socket_server()



    # Start HTTP server (for direct browser access)
    httpd = ThreadedHTTPServer(("0.0.0.0", ADMIN_PORT), AdminHandler)
    log(f"HTTP admin panel: http://0.0.0.0:{ADMIN_PORT}")

    # Graceful shutdown.
    # NOTE: httpd.shutdown() must NOT be called from the main thread — the
    # handler runs in the same thread that is blocked in serve_forever(), so
    # a direct call deadlocks forever and the process ignores SIGTERM. That
    # left orphaned admin servers holding port 9099 ("端口占用" on the next
    # install). Run shutdown in a helper thread and force-exit.
    def shutdown_handler(sig, frame):
        log("Shutting down...")
        try:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        except Exception:
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    httpd.serve_forever()

if __name__ == "__main__":
    main()
