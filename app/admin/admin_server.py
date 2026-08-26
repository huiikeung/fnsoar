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
SERVICE_STATE_FILE = f"{TRIM_PKGVAR}/service.state"
PROFILES_DIR  = f"{TRIM_PKGVAR}/profiles"
ACTIVE_FILE   = f"{TRIM_PKGVAR}/active"
# 订阅元数据（官网/更新间隔等响应头信息，mihomo 不解析，这里单独持久化）
SUB_META_FILE = f"{TRIM_PKGVAR}/sub-meta.json"
MIHOMO_BIN    = f"{TRIM_APPDEST}/bin/mihomo"
# The engine is a managed child of the panel daemon. fnOS tracks the admin
# server (bin/mihomo wrapper) as the app daemon; the engine is launched via
# bin/engine-start which applies host-transparent rules and drops privileges.
ENGINE_START  = f"{TRIM_APPDEST}/bin/engine-start"
ADMIN_DIR     = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = f"{TRIM_PKGVAR}/dashboard"
HOST_TRANSPARENT_SCRIPT = os.path.join(ADMIN_DIR, "host_transparent.sh")
HOST_TRANSPARENT_PORT = int(os.environ.get("MIHOMO_REDIR_PORT", "7892"))
_PROVIDER_RESTART_LOCK = threading.Lock()
_PROVIDER_RESTARTING = False
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

_icon_memo = {}   # url/shortname -> 已内嵌的 data: URI，避免每次请求重复读文件+base64
def embedded_icon(url_or_data):
    """把图标转成可内嵌的 data: URI（短名→icons.yaml 解析，data: 直接返回，URL 下载本地化，3s超时）。
    结果按图标键缓存，重复请求直接命中，避免磁盘读取与 base64 编码拖慢页面。"""
    if not isinstance(url_or_data, str) or not url_or_data:
        return url_or_data
    low = url_or_data.strip().lower()
    if low.startswith("data:"):
        return url_or_data
    # 缓存命中（短名 / 已本地化的 http(s) URL 均可安全缓存）
    memo_key = url_or_data.strip()
    if memo_key in _icon_memo:
        return _icon_memo[memo_key]
    imap = _get_icons_map()
    if imap and url_or_data in imap:
        _icon_memo[memo_key] = imap[url_or_data]
        return _icon_memo[memo_key]
    if not (low.startswith("http://") or low.startswith("https://")):
        return url_or_data
    path, ctype = localize_icon(url_or_data)
    if path:
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            _icon_memo[memo_key] = f"data:{ctype};base64,{b64}"
            return _icon_memo[memo_key]
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
    3. $TRIM_APPDEST/manifest          (fnOS package metadata)
    4. <admin dir>/../app.json          (source-tree fallback)
    Returns "unknown" if none is found."""
    env_ver = os.environ.get("TRIM_APPVER", "").strip()
    if env_ver:
        return env_ver
    candidates = [
        os.path.join(TRIM_APPDEST, "VERSION"),
        os.path.join(TRIM_APPDEST, "app.json"),
        os.path.join(TRIM_APPDEST, "manifest"),
        os.path.join(os.path.dirname(ADMIN_DIR), "app.json"),
        os.path.join(os.path.dirname(ADMIN_DIR), "manifest"),
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
                    raw = f.read().strip()
                if c.endswith("manifest"):
                    match = re.search(r"(?m)^version\s*=\s*([^\s#]+)", raw)
                    ver = match.group(1).strip() if match else ""
                else:
                    ver = raw
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
    """Try to read a small version marker inside a dashboard directory.
    1. VERSION file (written by install_dashboard)
    2. metacubexd: appVersion embedded in index.html (e.g. appVersion:"1.273.0")"""
    probe = os.path.join(DASHBOARD_DIR, name, "VERSION")
    if os.path.exists(probe):
        try:
            with open(probe, "r", encoding="utf-8") as f:
                ver = f.read().strip()
            if ver:
                return ver
        except Exception:
            pass
    index_path = os.path.join(DASHBOARD_DIR, name, "index.html")
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(2 * 1024 * 1024)
            m = re.search(r'appVersion["\s:=]+"?([0-9][0-9.]*)', content)
            if m and m.group(1).strip():
                return m.group(1).strip()
        except Exception:
            pass
    return None

def _github_latest(repo):
    """Query GitHub release 'latest' using the default GitHub API address
    (https://api.github.com). An optional MIHOMO_GITHUB_TOKEN is attached when
    available (raises the rate limit and enables private-repo access)."""
    import urllib.request as _ureq
    gh_api = os.environ.get("MIHOMO_GITHUB_API", "https://api.github.com").rstrip("/")
    token = os.environ.get("MIHOMO_GITHUB_TOKEN", "")
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

def _metacubexd_config_content():
    """metacubexd 默认后端是 http://127.0.0.1:9090，HTTPS 页面访问会被浏览器
    （尤其 iOS Safari/WebView）按混合内容拦截。这里返回动态 config.js 内容，
    在面板前端计算同源网关后端（请求 /configs、/proxies 等 API 时拼上 fnOS
    网关前缀，由 Admin 服务代理到 Mihomo）。
    不做磁盘改写 —— 面板升级完全替换目录后依然生效。"""
    return """window.__METACUBEXD_CONFIG__ = {
  defaultBackendURL: (function () {
    // 同源后端：fnOS 网关前缀（如 /app/fnnas.fnsoar）+ 当前 origin。
    // 避免 HTTPS 页面访问 HTTP 后端被浏览器拦截，由 Admin 服务代理到 Mihomo。
    var path = window.location.pathname || '';
    var m = path.match(/^(\\/[^/]+\\/[^/]+)/);
    var gw = m ? m[1] : '';
    return window.location.origin + gw;
  })(),
  githubToken: '',
}
"""

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

def ensure_config_initialized():
    """首次安装/重装后：若 config.yaml 缺失或为空，自动从默认模板恢复，
    保证引擎启动时配置被加载（策略组/订阅正常显示），无需用户手动加载。"""
    try:
        if os.path.exists(CONFIG_FILE) and os.path.getsize(CONFIG_FILE) > 0:
            return True
        template = os.path.join(TRIM_APPDEST, "default-config", "config.yaml")
        if not os.path.exists(template):
            log("WARNING: 默认配置模板不存在，跳过 config.yaml 初始化")
            return False
        try:
            os.makedirs(TRIM_PKGVAR, exist_ok=True)
        except Exception:
            pass
        shutil.copy2(template, CONFIG_FILE)
        log("已从默认模板初始化 config.yaml")
        return True
    except Exception as e:
        log(f"WARNING: config.yaml 初始化失败: {e}")
        return False

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
    """Return the real engine state, not only the wrapper PID file.
    fnOS can briefly leave a stale/missing PID during reinstall or wrapper
    handoff while mihomo is already listening; the UI must still show ON.
    NOTE: since v1.0.68 the PID file tracks the ADMIN daemon (which is
    always alive while the panel is open), so engine status must never be
    derived from it — 9090 (engine controller) is the authoritative check."""
    # 9090 是引擎控制端口；端口在监听时服务就是运行中。优先使用它，
    # 避免每次点击左栏都扫描进程导致状态短暂误判。
    if not _port_free(MIHOMO_CTRL_PORT):
        pids = _find_engine_pids()
        return True, (pids[0] if pids else None)
    pids = _find_engine_pids()
    if pids:
        pid = pids[0]
        return True, pid
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

def _read_service_state():
    """Last saved engine state: True=on, False/absent=off (first install default off)."""
    try:
        val = open(SERVICE_STATE_FILE, "r").read().strip().lower()
        return val == "on"
    except Exception:
        return False

def _write_service_state(on):
    try:
        with open(SERVICE_STATE_FILE, "w") as f:
            f.write("on\n" if on else "off\n")
    except Exception:
        pass

def _global_ipv6_available():
    """Return whether the NAS currently has a usable global IPv6 route/address."""
    try:
        route = subprocess.run(["/usr/sbin/ip", "-6", "route", "show", "default"],
                               capture_output=True, text=True, timeout=2)
        if route.returncode != 0 or not route.stdout.strip():
            return False
        addr = subprocess.run(["/usr/sbin/ip", "-6", "addr", "show", "scope", "global"],
                              capture_output=True, text=True, timeout=2)
        return addr.returncode == 0 and "inet6 " in addr.stdout
    except Exception:
        return False


def _tun_enabled_from_config():
    try:
        cfg = yaml.safe_load(read_config()) or {}
        return bool((cfg.get("tun") or {}).get("enable", False))
    except Exception:
        return False

def _host_transparent_enabled():
    # Host REDIRECT is the non-TUN replacement for NAS-local programs.
    # It is enabled by default when TUN is explicitly disabled.
    try:
        cfg = yaml.safe_load(read_config()) or {}
        # Existing installations predate this key: when TUN is explicitly off,
        # default to host interception to match the user's expected behavior.
        return bool(cfg.get("host-transparent", True)) and not _tun_enabled_from_config()
    except Exception:
        return False

def _ensure_host_transparent_config():
    """Ensure an existing installation has host-transparent safe defaults."""
    if not _host_transparent_enabled():
        return True
    txt = read_config()
    changed = False
    if not re.search(r"(?m)^redir-port:\s*", txt):
        marker = "mixed-port: 7890\n"
        if marker not in txt:
            return False
        txt = txt.replace(marker, marker + "redir-port: 7892\n", 1)
        changed = True
    if not re.search(r"(?m)^tproxy-port:\s*", txt):
        marker = "redir-port: 7892\n"
        txt = txt.replace(marker, marker + "tproxy-port: 7893\n", 1)
        changed = True
    if _tun_enabled_from_config():
        return True
    # Preserve the user's IPv6 preference. Host-transparent mode does not
    # silently change dns.ipv6; users who need leak prevention can disable it.
    txt2 = txt
    # The host mode uses local DNS redirection; fake-ip depends on TUN DNS
    # hijacking and cannot be used safely here.
    txt2 = re.sub(r"(?m)^(\s+enhanced-mode:)\s*fake-ip\s*$", r"\1 redir-host", txt2, count=1)
    if not re.search(r"(?m)^\s+listen:\s*127\.0\.0\.1:1053\s*$", txt2):
        txt2 = re.sub(r"(?m)^(\s+fake-ip-range:.*)$", r"\1\n  listen: 127.0.0.1:1053", txt2, count=1)
    changed = changed or txt2 != txt
    if changed:
        write_config(txt2)
    return True

def _apply_host_transparent():
    if not _ensure_host_transparent_config():
        return {"success": False, "error": "无法写入 redir-port: 7892"}
    if not _host_transparent_enabled():
        return {"success": True, "enabled": False}
    if os.geteuid() != 0:
        return {"success": False, "error": "主机透明代理需要 root 权限"}
    try:
        r = subprocess.run([HOST_TRANSPARENT_SCRIPT, "apply"], capture_output=True, text=True, timeout=10)
        if r.returncode:
            return {"success": False, "error": r.stderr.strip() or "iptables 规则应用失败"}
        return {"success": True, "enabled": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _cleanup_host_transparent():
    try:
        subprocess.run([HOST_TRANSPARENT_SCRIPT, "cleanup"], capture_output=True, text=True, timeout=10)
    except Exception:
        pass

def _restart_service_background():
    """Restart Mihomo without holding the subscription HTTP request open."""
    global _PROVIDER_RESTARTING
    try:
        stopped = stop_service()
        if not stopped.get("success"):
            log("provider restart stop failed: " + str(stopped))
            return
        time.sleep(1)
        started = start_service()
        if not started.get("success"):
            log("provider restart start failed: " + str(started))
    finally:
        with _PROVIDER_RESTART_LOCK:
            _PROVIDER_RESTARTING = False

def request_provider_restart():
    global _PROVIDER_RESTARTING
    with _PROVIDER_RESTART_LOCK:
        if _PROVIDER_RESTARTING:
            return False
        _PROVIDER_RESTARTING = True
    threading.Thread(target=_restart_service_background, daemon=True).start()
    return True

def start_service():
    running, pid = is_running()
    if running:
        if pid and not _find_engine_pids():
            running = False
        else:
            return {"success": False, "error": "服务已在运行"}
    if not os.path.exists(ENGINE_START):
        return {"success": False, "error": f"找不到引擎启动器: {ENGINE_START}"}
    try:
        # TUN 是独立开关：启动服务时尊重 config.yaml 中用户保存的状态。
        # 不要在这里强制开启，否则“服务”和“TUN”无法独立控制。
        # REDIRECT 入口必须在启动 Mihomo 前写入，不能等进程启动后再改。
        if _host_transparent_enabled() and not _ensure_host_transparent_config():
            return {"success": False, "error": "无法准备主机透明代理 redir-port"}
        for _ in range(10):
            if _port_free(MIHOMO_CTRL_PORT):
                break
            time.sleep(0.5)
        # 引擎是面板守护的子进程，通过 bin/engine-start 启动
        # （它会处理 host-transparent 防火墙规则并按需降权执行引擎）。
        env = dict(os.environ)
        env.setdefault("MIHOMO_APP_NAME", TRIM_APPNAME)
        env.setdefault("MIHOMO_DATA_DIR", TRIM_PKGVAR)
        env.setdefault("MIHOMO_DEST_DIR", TRIM_APPDEST)
        with open(f"{TRIM_PKGVAR}/{TRIM_APPNAME}.log", "a") as logf:
            subprocess.Popen([ENGINE_START, "-d", TRIM_PKGVAR],
                             stdout=logf, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
        # 等待引擎起来（最多 20s）
        for _ in range(40):
            time.sleep(0.5)
            running, pid = is_running()
            if running and _find_engine_pids():
                transparent = _apply_host_transparent()
                if not transparent.get("success"):
                    stop_service()
                    return {"success": False, "error": "主机透明代理启用失败: " + transparent.get("error", "未知错误")}
                return {"success": True, "pid": pid, "host_transparent": transparent.get("enabled", False)}
        running, pid = is_running()
        if running:
            transparent = _apply_host_transparent()
            if not transparent.get("success"):
                stop_service()
                return {"success": False, "error": "主机透明代理启用失败: " + transparent.get("error", "未知错误")}
        return {"success": running}
    except Exception as e:
        return {"success": False, "error": str(e)}

def stop_service():
    # Remove host interception before stopping the listener, otherwise a failed
    # connection can be redirected to a dead redir-port.
    _cleanup_host_transparent()
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
        # 注意：PID_FILE 现在是 fnOS 面板守护进程（admin server）的 PID，
        # 由 fnOS 的 start_daemon 管理，不能在这里删除，否则 fnOS 会把
        # 应用误判为"未运行"、桌面窗口消失。
        # 停止 Mihomo 后由内核负责释放 TUN；不要改写用户保存的 TUN 开关：
        # 这样下次启动时可以按用户最后保存的状态恢复。
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

def load_sub_meta():
    """读取订阅元数据（name -> {home, updateInterval, ...}）。"""
    try:
        if os.path.exists(SUB_META_FILE):
            with open(SUB_META_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}

def save_sub_meta(meta):
    """写入订阅元数据。"""
    try:
        os.makedirs(os.path.dirname(SUB_META_FILE), exist_ok=True)
        with open(SUB_META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"save sub-meta failed: {e}")

def _provider_block(name, url, interval, ptype="http"):
    block = f"  {name}:\n"
    block += f"    type: {ptype}\n"
    block += f"    url: \"{url}\"\n"
    block += f"    interval: {int(interval)}\n"
    block += "    health-check:\n"
    block += "      enable: true\n"
    block += "      url: https://www.gstatic.com/generate_204\n"
    block += "      interval: 180\n"
    return block + "\n"  # 与其他 provider 之间保留一行空行

def _valid_provider_name(name):
    if not name or not name.strip():
        return False, "订阅名称不能为空"
    key = name.strip()
    if any(c in key for c in " \t:\n{}[]#,"):
        return False, "订阅名称不能包含空格或特殊字符"
    return True, key

def _sanitize_provider_name(raw):
    """把订阅返回的名称清洗成合法的 YAML provider 键（保留中文/字母/数字/连字符）。"""
    if not raw:
        return ""
    name = str(raw).strip().strip('"\' ')
    name = re.sub(r'[\s\t:\n{}\[\]#,\\/]+', "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:60]

def probe_subscription_meta(url, timeout=7):
    """探测订阅响应头，返回完整元数据 dict（对齐 clash-verge-rev 的能力）：
    - name: Content-Disposition filename* / filename、Profile-Title、或 YAML profile.name
    - updateInterval: profile-update-interval 响应头（小时，转成秒）
    - home: profile-web-page-url 响应头（机场官网）
    请求带超时，失败返回 {} 不阻塞添加流程。"""
    import urllib.request as _ureq
    meta = {}
    try:
        req = _ureq.Request(url, headers={"User-Agent": f"fnSoar/{_get_app_version()}"})
        with _ureq.urlopen(req, timeout=timeout) as resp:
            hd = {k.lower(): v for k, v in resp.headers.items()}
            # 1) 订阅标题响应头（多数机场/面板下发）
            for k in ("profile-title", "subscription-title"):
                if hd.get(k) and hd[k].strip():
                    meta["name"] = _sanitize_provider_name(hd[k])
                    break
            # 2) Content-Disposition → filename*（RFC 5987）或 filename
            if not meta.get("name"):
                cd = hd.get("content-disposition", "") or ""
                m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)?''([^;]+)", cd, re.I)
                if m:
                    fname = urllib.parse.unquote(m.group(1).strip().strip('"'))
                    if fname:
                        meta["name"] = _sanitize_provider_name(fname)
                else:
                    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
                    if m:
                        fname = m.group(1).strip().strip('"')
                        if fname:
                            meta["name"] = _sanitize_provider_name(fname)
            # 3) profile-update-interval：更新间隔（小时 → 秒）
            ui = hd.get("profile-update-interval", "")
            if ui:
                try:
                    hrs = int(str(ui).strip())
                    if hrs > 0:
                        meta["updateInterval"] = hrs * 3600
                except Exception:
                    pass
            # 4) profile-web-page-url：机场官网
            home = hd.get("profile-web-page-url", "")
            if home and home.strip():
                meta["home"] = home.strip()
            # 5) YAML 顶层 profile.name（完整读取，很多机场把 profile 放在文件末尾）
            #    单独 try 包裹：读 body 超时/失败不影响前面已从响应头解析出的元数据。
            if not meta.get("name"):
                try:
                    data = resp.read().decode("utf-8", "ignore")
                    data = data.lstrip("\ufeff")
                    try:
                        doc = yaml.safe_load(data) or {}
                        prof = doc.get("profile") or {}
                        if isinstance(prof, dict) and prof.get("name"):
                            meta["name"] = _sanitize_provider_name(str(prof["name"]))
                    except Exception:
                        m = re.search(r'(?m)^profile:\s*\n\s*name:\s*["\']?([^"\'\n]+)', data)
                        if m and m.group(1).strip():
                            meta["name"] = _sanitize_provider_name(m.group(1).strip())
                except Exception as e:
                    log(f"probe subscription body skipped: {url}: {e}")
    except Exception as e:
        log(f"probe subscription meta failed: {url}: {e}")
    return meta

def probe_subscription_name(url, timeout=7):
    """从订阅响应中提取真实名称（兼容旧调用）。"""
    return probe_subscription_meta(url, timeout).get("name", "")

def _is_token_like(s):
    """判断字符串是否像 token/hash（长、无中文、纯字母数字连字符）。"""
    if not s or len(s) < 6:
        return False
    # 包含中文 → 不是 token
    if re.search(r'[\u4e00-\u9fff]', s):
        return False
    # 纯 hex 超过 12 位 → 像 hash
    if re.match(r'^[0-9a-fA-F]{12,}$', s):
        return True
    # 纯字母数字连字符超过 20 位 → 像 token
    if len(s) > 20 and re.match(r'^[a-zA-Z0-9_-]+$', s):
        return True
    return False

def fallback_provider_name(url):
    """无标题可用时的兜底名称：URL 末段（非泛用词、非 token）→ 域名 → 订阅。"""
    try:
        from urllib.parse import urlparse, unquote as _unquote
        last = _unquote(urlparse(url).path.strip("/").rsplit("/", 1)[-1]) if urlparse(url).path.strip("/") else ""
        generic = {"sub", "clash", "link", "api", "subscribe", "get", "download", "upload", "feed"}
        if last and last.lower() not in generic and not _is_token_like(last):
            return _sanitize_provider_name(last)
        host = urlparse(url).hostname
        if host:
            return _sanitize_provider_name(host)
    except Exception:
        pass
    return "订阅"

def edit_provider_add(name, url, interval=3600, ptype="http"):
    """Insert/replace a proxy-provider entry in config.yaml text. No restart.
    名称缺省时自动探测订阅真实名称（对齐 clash-verge-rev），探测失败回退域名。
    同时应用订阅响应头里下发的更新间隔（profile-update-interval）。"""
    meta = {}
    if not name or not name.strip():
        meta = probe_subscription_meta(url)
        derived = meta.get("name") or fallback_provider_name(url)
        name = derived
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
    # 响应头 profile-update-interval 下发的更新间隔优先（仅在未显式传有效间隔时）
    if not meta:
        meta = probe_subscription_meta(url)
    if meta.get("updateInterval") and meta["updateInterval"] > 0 and interval == 3600:
        interval = int(meta["updateInterval"])
    # 持久化订阅元数据（官网 home / 更新间隔），供订阅卡片显示「官网」跳转
    if meta.get("home") or meta.get("updateInterval"):
        all_meta = load_sub_meta()
        entry = all_meta.get(key, {})
        if meta.get("home"):
            entry["home"] = meta["home"]
        if meta.get("updateInterval"):
            entry["updateInterval"] = meta["updateInterval"]
        all_meta[key] = entry
        save_sub_meta(all_meta)
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
# （merge 增强——用户本地偏好覆盖订阅，节点/规则保留）


def _real_engine_bin():
    """Resolve the arch-specific REAL engine binary. The bin/mihomo wrapper is
    now the panel daemon (execs admin_server.py), so CLI invocations like
    `mihomo -t` / `mihomo update geodata` must run against the real binary."""
    m = (os.uname().machine or "").lower()
    if m in ("x86_64", "amd64"):
        return f"{TRIM_APPDEST}/bin/mihomo-amd64.real"
    return f"{TRIM_APPDEST}/bin/mihomo-arm64.real"


def _test_config(path):
    """mihomo -t 校验配置；返回 (ok, msg)。"""
    try:
        r = subprocess.run([_real_engine_bin(), "-t", "-d", TRIM_PKGVAR, "-f", path],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, "配置校验通过"
        err = (r.stderr or r.stdout or "").strip().splitlines()
        return False, (err[-1] if err else "配置校验失败")
    except Exception as e:
        return False, f"校验异常: {e}"

_IPINFO_CACHE = {"t": 0, "data": None}
_SYSINFO_CACHE = {"t": 0, "data": None}
# Clash API 只读 GET 短缓存：key = "METHOD:path" -> (content_type, body_bytes, ts)
_PROXY_GET_CACHE = {}  # 受锁保护
_PROXY_GET_LOCK = threading.Lock()

def get_ip_info():
    """公网 IP 信息，带 300s 内存缓存（首页每次加载都调用，避免每次外网查询拖慢页面）。
    多个服务并发请求、最快成功者胜出，单服务 3s 超时。
    IPv6 出口单独走 60s 短缓存（_get_ipv6_info），不随 IPv4 数据缓存 300s：
    否则服务刚启动/引擎未就绪时的一次失败回退（本机地址）会锁 5 分钟，
    用户会误以为 TUN 没有走代理。"""
    now = int(time.time())
    if _IPINFO_CACHE["data"] is not None and now - _IPINFO_CACHE["t"] < 300:
        data = dict(_IPINFO_CACHE["data"])
    else:
        data = _get_ipinfo_uncached()
        # 查询成功(拿到真实 ip)才缓存
        if data and data.get("ip") and data["ip"] != "-":
            _IPINFO_CACHE["t"] = now
            _IPINFO_CACHE["data"] = dict(data)
    if data and data.get("ip") and data["ip"] != "-":
        v6, geo6 = _get_ipv6_info()
        if v6:
            data["ipv6"] = v6
            if geo6:
                data["ipv6_countryCode"] = geo6.get("countryCode") or ""
                loc = " · ".join([geo6.get("region") or "", geo6.get("city") or ""]).strip(" ·")
                data["ipv6_location"] = loc or geo6.get("country") or ""
        else:
            data["ipv6"] = ""
    return data

_IPV6_CACHE = {"t": 0, "addr": "", "geo": None}

def _get_ipv6_info():
    """IPv6 exit address + geo with a short 60s cache. A failed lookup is NOT
    cached so the next page refresh retries it."""
    now = int(time.time())
    if _IPV6_CACHE["addr"] and now - _IPV6_CACHE["t"] < 60:
        return _IPV6_CACHE["addr"], _IPV6_CACHE["geo"]
    addr = _get_ipv6_exit()
    if not addr:
        return "", None
    geo = _get_ipv6_geo(addr)
    _IPV6_CACHE["t"] = now
    _IPV6_CACHE["addr"] = addr
    _IPV6_CACHE["geo"] = geo
    return addr, geo

def _get_ipv6_geo(addr):
    """Best effort country/region/city for an IPv6 address (ip-api.com, v6
    supported, no key). Returns None when the lookup fails."""
    import urllib.request as _ureq
    try:
        url = ("http://ip-api.com/json/" + addr +
               "?lang=zh-CN&fields=status,country,countryCode,regionName,city")
        req = _ureq.Request(url, headers={"User-Agent": "curl/8"})
        with _ureq.urlopen(req, timeout=3) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        if d.get("status") != "success":
            return None
        return {
            "country": d.get("country") or "",
            "countryCode": (d.get("countryCode") or "").upper(),
            "region": d.get("regionName") or "",
            "city": d.get("city") or "",
        }
    except Exception:
        return None

def _local_global_ipv6s():
    """Set of the NAS's own global IPv6 addresses (used to reject direct-exit
    echo results that did not go through the proxy)."""
    addrs = set()
    try:
        out = subprocess.run(["/usr/sbin/ip", "-6", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            m = re.search(r"\binet6\s+([0-9a-fA-F:]+)/\d+", line)
            if m and ":" in m.group(1):
                addrs.add(m.group(1).lower())
    except Exception:
        pass
    return addrs

def _get_ipv6_exit():
    """Public IPv6 exit address. Uses AF_INET6 sockets (urllib would resolve to
    an IPv4 address first and echo the node's IPv4 instead), querying echo
    services that are matched by the proxy rules (ipify) concurrently. Results
    equal to the NAS's own global IPv6 are rejected — they mean the request
    went DIRECT and would show the local address instead of the proxy exit.
    Falls back to the local global IPv6 only when every proxy query fails.
    Returns '' when IPv6 is unsupported."""
    import ipaddress
    import socket as _sock
    import ssl as _ssl
    import concurrent.futures as _cf

    local_addrs = _local_global_ipv6s()

    def _try_v6(host, port=443):
        infos = _sock.getaddrinfo(host, port, _sock.AF_INET6, _sock.SOCK_STREAM)
        if not infos:
            raise ValueError("no AAAA record")
        s = _sock.socket(_sock.AF_INET6, _sock.SOCK_STREAM)
        s.settimeout(6)
        try:
            s.connect(infos[0][4])
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                ss.sendall(("GET / HTTP/1.1\r\nHost: %s\r\nUser-Agent: curl/8\r\n"
                            "Accept: */*\r\nConnection: close\r\n\r\n" % host).encode())
                chunks = []
                while True:
                    d = ss.recv(4096)
                    if not d:
                        break
                    chunks.append(d)
            body = b"".join(chunks).split(b"\r\n\r\n", 1)[-1].decode("utf-8", "replace").strip()
            v6 = ipaddress.IPv6Address(body)
            if v6.exploded.lower() in local_addrs:
                raise ValueError("direct exit (local address)")
            return v6.compressed
        finally:
            try:
                s.close()
            except Exception:
                pass

    # These domains are matched by the Proxy ruleset, so the connection goes
    # through Mihomo and echoes the node's IPv6 exit. icanhazip is excluded:
    # it is NOT in the proxy ruleset, falls into the catch-all group and often
    # goes DIRECT (echoing the NAS's own address).
    services = ("api64.ipify.org", "api6.ipify.org")
    try:
        with _cf.ThreadPoolExecutor(max_workers=len(services)) as ex:
            futs = [ex.submit(_try_v6, u) for u in services]
            for fut in _cf.as_completed(futs):
                try:
                    v = fut.result()
                    if v:
                        return v
                except Exception:
                    continue
    except Exception:
        pass
    try:
        out = subprocess.run(["/usr/sbin/ip", "-6", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            m = re.search(r"\binet6\s+([0-9a-fA-F:]+)/\d+", line)
            if m:
                addr = m.group(1)
                if ":" in addr and not addr.startswith("fe80"):
                    return addr
    except Exception:
        pass
    return ""

def _get_ipinfo_uncached():
    """Query the public exit IP via external APIs (best effort).
    Requests all candidate services concurrently, first success wins; each
    service is given a short 3s timeout so a slow/unreachable service cannot
    stall the page for seconds.
    Field set matches the IP-info card:
    ip / asn(自治域) / isp(服务商) / organization(组织) / location(位置) / timezone(时区)."""
    import urllib.request as _ureq
    import ssl
    import concurrent.futures as _cf
    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE

    def _fetch(url, timeout=3, use_ctx=False):
        req = _ureq.Request(url, headers={"User-Agent": "curl/8"}, method="GET")
        kw = {"timeout": timeout}
        if url.startswith("https"):
            kw["context"] = _ctx
        with _ureq.urlopen(req, **kw) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _service1():
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

    def _service2():
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

    def _service3():
        d = _fetch(
            "http://ip-api.com/json/?lang=zh-CN&fields=status,query,country,countryCode,regionName,city,isp,as,org,timezone")
        if d.get("status") != "success":
            raise ValueError("ip-api failed")
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

    def _service4():
        d = _fetch("https://ipinfo.io/json")
        return {
            "ip": d.get("ip") or "-",
            "country": d.get("country") or "",
            "countryCode": (d.get("country") or "").upper(),
            "region": d.get("region") or "",
            "city": d.get("city") or "",
            "isp": (d.get("org") or "").split(" ", 1)[-1],
            "organization": d.get("org") or "",
            "asn": (d.get("org") or "").split(" ", 1)[0].lstrip("AS"),
            "asn_organization": d.get("org") or "",
            "timezone": d.get("timezone") or "",
            "latitude": d.get("loc", "").split(",")[0] if d.get("loc") else None,
            "longitude": d.get("loc", "").split(",")[1] if d.get("loc") else None,
        }

    services = [_service1, _service2, _service3, _service4]
    with _cf.ThreadPoolExecutor(max_workers=len(services)) as ex:
        futs = {ex.submit(s): i for i, s in enumerate(services)}
        for fut in _cf.as_completed(futs):
            try:
                data = fut.result()
                if data and data.get("ip") and data["ip"] != "-":
                    return data
            except Exception:
                continue
    # All services failed - return error object so frontend can show message
    return {"ip": "-", "error": "无法获取 IP 信息，请检查网络连接"}

def get_system_info():
    """系统信息,带 10s 缓存避免每次 sleep(0.4) 采样 CPU。"""
    now = time.time()
    if _SYSINFO_CACHE["data"] is not None and now - _SYSINFO_CACHE["t"] < 10:
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

    def _send_text(self, text, content_type="text/plain"):
        """直接发送内存中的文本内容（不落盘）。"""
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
        Always sends a response; returns nothing meaningful.
        Read-only GET JSON endpoints (proxies / group / providers / rules / traffic…)
        are cached for 3s so dashboard page loads don't re-hit mihomo each time."""
        # 只读 GET 短缓存：命中直接回，避免订阅/首页反复请求拖慢
        cache_ttl = 3.0
        cacheable = (method == "GET" and
                     sub_path.split("?", 1)[0].rstrip("/") in
                     ("proxies", "group", "rules", "connections", "traffic",
                      "configs", "providers/proxies", "providers/rules") or
                     sub_path.startswith("providers/proxies/"))
        ckey = method + ":" + sub_path
        with _PROXY_GET_LOCK:
            cached = _PROXY_GET_CACHE.get(ckey)
            if cacheable and cached and time.time() - cached[2] < cache_ttl:
                ct, body, ts = cached
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
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
                            # 节点列表（/proxies、/group）展开图标耗时且前端用
                            # /api/group-icons 单独取组图标，故此二端点不再做图标展开，
                            # 避免每次请求重复 base64（曾导致策略组页加载 >10s）。
                            base_path = sub_path.split("?", 1)[0].rstrip("/")
                            if base_path not in ("proxies", "group"):
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
                    if cacheable:
                        ctype = resp.headers.get("Content-Type", "application/json")
                        with _PROXY_GET_LOCK:
                            _PROXY_GET_CACHE[ckey] = (ctype, data, time.time())
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
                # metacubexd 的 config.js 动态返回同源后端配置（不读磁盘、不写磁盘），
                # 面板升级完全替换目录后依然生效。
                if sub == "metacubexd" and rel == "config.js":
                    return self._send_text(_metacubexd_config_content(), "application/javascript")
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
            tun_enabled = _tun_enabled_from_config()
            ipv6_available = _global_ipv6_available()
            return self._send_json({"running": running, "pid": pid,
                                    "control_port": MIHOMO_CTRL_PORT,
                                    "remembered_on": _read_service_state(),
                                    "tun_enabled": tun_enabled,
                                    "ipv6_available": ipv6_available,
                                    "ipv6_udp_blocked": bool(running and ipv6_available and not tun_enabled)})
        if path == "/api/config":
            return self._send_json({"config": read_config()})
        if path == "/api/proxy-providers":
            yaml_text = read_config()
            providers = extract_proxy_providers(yaml_text)
            # 合并订阅元数据（官网 home 等响应头信息）
            meta = load_sub_meta()
            for name, m in meta.items():
                if name in providers and isinstance(providers[name], dict) and m.get("home"):
                    providers[name]["home"] = m["home"]
            return self._send_json({"providers": providers})
        if path == "/api/providers-live":
            # 订阅页只需要节点数/更新时间/类型，不要把完整 8MB 节点列表发给浏览器。
            try:
                import urllib.request as _ureq
                req = _ureq.Request(
                    f"http://{MIHOMO_CTRL_HOST}:{MIHOMO_CTRL_PORT}/providers/proxies",
                    headers={"Accept": "application/json"})
                with _ureq.urlopen(req, timeout=10) as resp:
                    raw = json.loads(resp.read())
                compact = {}
                for name, item in (raw.get("providers") or {}).items():
                    si = item.get("subscriptionInfo") or {}
                    entry = {
                        "count": len(item.get("proxies") or []),
                        "updatedAt": item.get("updatedAt", ""),
                        "vehicleType": item.get("vehicleType", "")
                    }
                    # 流量用量 + 到期（subscription-userinfo 响应头，mihomo 已解析）
                    if si:
                        entry["subInfo"] = {
                            "upload": si.get("Upload", 0) or 0,
                            "download": si.get("Download", 0) or 0,
                            "total": si.get("Total", 0) or 0,
                            "expire": si.get("Expire", 0) or 0
                        }
                    compact[name] = entry
                return self._send_json({"providers": compact})
            except Exception as e:
                return self._send_json({"providers": {}, "error": str(e)}, 502)
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
            # 解锁测试：默认测试项列表（Pending）
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
                d = _github_latest("huiikeung/fnsoar")
                latest = d.get("tag_name", "").lstrip("v")
                cur = _get_app_version()
                has = bool(latest) and cur not in ("", "unknown") and latest != cur
                return self._send_json({"success": True, "latest": latest, "current": cur, "url": d.get("html_url", ""), "has_update": has})
            except Exception as e:
                msg = str(e)
                if "404" in msg:
                    msg = "GitHub 仓库 huiikeung/fnsoar 不存在或为私有仓库，无法公开检测更新。请将仓库设为公开，或配置 MIHOMO_GITHUB_TOKEN 后重试。"
                return self._send_json({"success": False, "error": msg}, 500)
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
                # persist the user's choice; restored next time the panel daemon starts
                _write_service_state(enable)
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

                # TUN 不是普通热配置：它会创建/删除虚拟网卡并修改系统路由。
                # 服务运行中时立即重启 Mihomo，使配置和实际内核状态保持一致。
                running, _ = is_running()
                if running:
                    stopped = stop_service()
                    if not stopped.get("success"):
                        return self._send_json({"success": False,
                                                "error": "TUN 配置已保存，但停止旧服务失败: " + str(stopped.get("error", ""))}, 500)
                    started = start_service()
                    if not started.get("success"):
                        return self._send_json({"success": False,
                                                "error": "TUN 配置已保存，但重启服务失败: " + str(started.get("error", ""))}, 500)
                    return self._send_json({"success": True,
                                            "message": "TUN 已" + ("开启" if enable else "关闭") + "，服务已重启"})
                return self._send_json({"success": True,
                                        "message": "TUN 已" + ("开启" if enable else "关闭") + "，下次启动服务时生效"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/update-geo":
            try:
                import subprocess
                cmd = [_real_engine_bin(), "-d", TRIM_PKGVAR, "update", "geodata"]
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
                home = (data.get("home") or "").strip()
                old_name = (data.get("old_name") or "").strip()
                # 重命名：先按旧名称删除原块，再按新名称写入（一次重启生效）
                if old_name and old_name != name.strip():
                    del_result = edit_provider_delete(old_name)
                    if not del_result.get("success") and "未找到" not in del_result.get("error", ""):
                        return self._send_json(del_result, 400)
                result = edit_provider_add(name, url, interval, ptype)
                if not result.get("success"):
                    return self._send_json(result, 400)
                # 持久化官网地址：用户填了就存（覆盖自动探测），清空则删除
                final_name = (name or "").strip()
                if final_name:
                    all_meta = load_sub_meta()
                    if home:
                        entry = all_meta.get(final_name, {})
                        entry["home"] = home
                        all_meta[final_name] = entry
                    elif "home" in data:  # 明确的空值 → 用户清空官网
                        entry = all_meta.get(final_name, {})
                        entry.pop("home", None)
                        if entry:
                            all_meta[final_name] = entry
                        else:
                            all_meta.pop(final_name, None)
                    save_sub_meta(all_meta)
                queued = request_provider_restart()
                result["restarting"] = queued
                result["message"] = result.get("message", "订阅已保存") + ("，内核正在后台重启" if queued else "，内核已在重启队列中")
                return self._send_json(result)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/providers/delete":
            try:
                data = json.loads(body) if body else {}
                result = edit_provider_delete(data.get("name", ""))
                if not result.get("success"):
                    return self._send_json(result, 400)
                queued = request_provider_restart()
                result["restarting"] = queued
                result["message"] = result.get("message", "订阅已删除") + ("，内核正在后台重启" if queued else "，内核已在重启队列中")
                return self._send_json(result)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/providers/probe-name":
            try:
                data = json.loads(body) if body else {}
                url = data.get("url", "")
                name = data.get("name", "")  # 可选：关联的订阅名，用于持久化官网
                if not url or not url.startswith(("http://", "https://")):
                    return self._send_json({"success": False, "error": "无效的订阅链接"}, 400)
                meta = probe_subscription_meta(url)
                meta["name"] = meta.get("name") or fallback_provider_name(url)
                meta["success"] = True
                # 持久化官网地址（前端「刷新名称」时也能补全 home）
                if name and meta.get("home"):
                    all_meta = load_sub_meta()
                    entry = all_meta.get(name, {})
                    entry["home"] = meta["home"]
                    all_meta[name] = entry
                    save_sub_meta(all_meta)
                return self._send_json(meta)
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

def _auto_restore_service():
    """Restore the last saved engine state when the panel daemon starts."""
    if not _read_service_state():
        log("service.state=off, engine stays stopped")
        return
    log("service.state=on, restoring engine")
    try:
        running, _ = is_running()
        if running:
            return
        res = start_service()
        if res.get("success"):
            log("engine restored on startup")
        else:
            log("engine restore failed: " + str(res.get("error", "")))
    except Exception as e:
        log("engine restore error: " + str(e))

def main():
    log(f"Starting fnSoar Admin Server")
    log(f"  HTTP port : {ADMIN_PORT}")
    log(f"  Unix sock : {SOCKET_PATH}")
    log(f"  Config    : {CONFIG_FILE}")

    # 确保 config.yaml 存在（重装/首装后自动初始化），否则引擎无法加载配置
    ensure_config_initialized()

    # Start Unix socket server (for fnOS iframe integration) — non-fatal if it fails
    start_unix_socket_server()



    # Start HTTP server (for direct browser access)
    httpd = ThreadedHTTPServer(("0.0.0.0", ADMIN_PORT), AdminHandler)
    log(f"HTTP admin panel: http://0.0.0.0:{ADMIN_PORT}")

    # Restore the last saved engine state (on/off) in the background so the
    # HTTP panel becomes available immediately.
    threading.Thread(target=_auto_restore_service, daemon=True).start()

    # Graceful shutdown: stop the engine first, then exit. The fnOS daemon is
    # this admin process, so stopping the app must not orphan the engine.
    # NOTE: httpd.shutdown() must NOT be called from the main thread — the
    # handler runs in the same thread that is blocked in serve_forever(), so
    # a direct call deadlocks forever and the process ignores SIGTERM. That
    # left orphaned admin servers holding port 9099 ("端口占用" on the next
    # install). Run shutdown in a helper thread and force-exit.
    def shutdown_handler(sig, frame):
        log("Shutting down: stopping engine first...")
        try:
            stop_service()
        except Exception as e:
            log("stop engine on shutdown failed: " + str(e))
        log("Shutting down admin server...")
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
