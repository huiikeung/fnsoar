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
ADMIN_PORT    = int(os.environ.get("ADMIN_PORT", "9099"))
SOCKET_PATH   = os.environ.get("MIHOMO_GATEWAY_SOCK",
                                f"/vol1/@appcenter/{TRIM_APPNAME}/fnsoar.sock")

CONFIG_FILE   = f"{TRIM_PKGVAR}/config.yaml"
# ── 全局扩展（对齐 Clash Verge Rev）：合并模板 + 全局脚本 ──────────────
_CHANGELOG_CACHE = {"t": 0, "d": None}
_CORE_LATEST_CACHE = {"t": 0, "d": None}   # 内核最新版长效缓存（6h，扛 GitHub 限流）
_GH_CACHE_FILE = f"{TRIM_PKGVAR}/gh-cache.json"   # GitHub 查询的持久化「最后一次成功」


def _gh_cache_get(key, max_age):
    """持久化缓存读取：重启/限流/断网后仍能给出最近一次成功的结果。"""
    try:
        with open(_GH_CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        ent = (d or {}).get(key) or {}
        if ent.get("t") and time.time() - ent["t"] < max_age:
            return ent.get("d")
    except Exception:
        pass
    return None


def _gh_cache_put(key, data):
    try:
        d = {}
        if os.path.exists(_GH_CACHE_FILE):
            with open(_GH_CACHE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
        d[key] = {"t": int(time.time()), "d": data}
        with open(_GH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass
PROFILE_EXT_FILE = f"{TRIM_PKGVAR}/profile-ext.json"
# 应用设置接管的字段：全局扩展（合并/脚本）改不动这些，最终以应用设置为准
APP_MANAGED_KEYS = (
    "mode", "log-level", "tcp-concurrent", "find-process-mode",
    "external-controller", "external-ui", "external-ui-url", "secret",
    "geo-auto-update", "geo-update-interval",
    "mixed-port", "port", "socks-port", "redir-port", "tproxy-port",
    "bind-address", "allow-lan", "ipv6", "unified-delay", "host-transparent",
    "profile", "geodata-mode", "geodata-loader", "global-ua",
    "keep-alive-interval", "authentication",
)
APP_MANAGED_SECTIONS = ("tun", "dns", "sniffer", "geox-url", "x-fnsoar", "listeners")
DEFAULT_MERGE_CONFIG = """# 全局扩展覆写模板（对所有订阅生效，深合并进 config.yaml）
# 语义：字典递归合并；列表整体覆盖。取消注释或自行增删后保存生效。

# 前置规则集：中国网站直连（Loyalsoldier 社区列表，按天更新）
prepend-rule-providers:
  china_sites:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/direct.txt"
    path: ./ruleset/china_sites.yaml
    interval: 86400

# 前置规则（置于规则列表最前，优先匹配）
prepend-rules:
  # 中国网站直连
  - RULE-SET,china_sites,DIRECT
  # AI 镜像站点直连示例（国内可直连的 AI 镜像，按需取消注释）
  # - DOMAIN-SUFFIX,wzw.pp.ua,DIRECT
  # - DOMAIN-SUFFIX,runanytime.hxi.me,DIRECT
  # - DOMAIN-SUFFIX,fu520.top,DIRECT
"""
DEFAULT_GLOBAL_SCRIPT = """// Define main function (script entry)

function main(config, profileName) {
  return config;
}
"""
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
AGG_BAK_FILE = f"{TRIM_PKGVAR}/config.aggregated.bak"   # 聚合态的 config.yaml 备份


def _aggregate_state():
    """读取聚合模式状态：{aggregate: bool, active_sub: str}。默认聚合开。"""
    try:
        with open(SUB_META_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    agg = d.get("__aggregate__") if isinstance(d, dict) else None
    if not isinstance(agg, dict):
        agg = {"aggregate": True, "active_sub": ""}
    agg.setdefault("aggregate", True)
    agg.setdefault("active_sub", "")
    return agg


def _save_aggregate_state(state):
    meta = load_sub_meta()
    meta["__aggregate__"] = {
        "aggregate": bool(state.get("aggregate", True)),
        "active_sub": (state.get("active_sub") or "").strip(),
    }
    save_sub_meta(meta)


def _repair_proxy_refs(doc):
    """剔除/替换对不存在代理的引用。

    订阅节点过滤会把「公告/到期」类节点从 payload 删掉，但订阅配置内的
    策略组与规则仍引用它们 —— mihomo 直接 fatal
    (proxy group[x]: 'name' not found)。独立使用订阅配置前必须修复。
    """
    if not isinstance(doc, dict):
        return doc
    named = {"DIRECT", "REJECT"}
    for p in (doc.get("proxies") or []):
        if isinstance(p, dict) and p.get("name"):
            named.add(p["name"])
    named |= set((doc.get("proxy-providers") or {}).keys())
    for g in (doc.get("proxy-groups") or []):
        if isinstance(g, dict) and g.get("name"):
            named.add(g["name"])
    # 策略组引用
    for g in (doc.get("proxy-groups") or []):
        if not isinstance(g, dict):
            continue
        refs = g.get("proxies")
        if isinstance(refs, list):
            g["proxies"] = [r for r in refs if r in named] or ["DIRECT"]
        # include-all / exclude-filter 等字段保持原样
    # 规则目标（最后一段是策略名的）
    _RULE_PREFIX = ("MATCH", "RULE-SET", "GEOSITE", "GEOIP", "DOMAIN", "DOMAIN-SUFFIX",
                    "DOMAIN-KEYWORD", "IP-CIDR", "IP-CIDR6", "SRC-IP-CIDR", "DST-PORT",
                    "SRC-PORT", "PROCESS-NAME", "IN-NAME", "IN-TYPE", "IN-USER", "NETWORK",
                    "AND", "OR", "NOT", "SUB-RULE", "SCRIPT")
    fixed_rules = []
    for r in (doc.get("rules") or []):
        parts = r.split(",") if isinstance(r, str) else (r if isinstance(r, list) else None)
        if not parts or len(parts) < 2:
            fixed_rules.append(r)
            continue
        target = str(parts[-1]).strip()
        if target not in named and not target.startswith(_RULE_PREFIX):
            parts[-1] = "DIRECT"
            fixed_rules.append(",".join(str(x) for x in parts) if isinstance(r, str) else parts)
        else:
            fixed_rules.append(r)
    doc["rules"] = fixed_rules
    return doc


def _engine_config_path():
    return CONFIG_FILE


def _restart_engine_async():
    """异步重启引擎（engine-start 重启整条服务链）。"""
    import subprocess as _sp
    cli = "/usr/local/bin/appcenter-cli"
    cmd = (f"cd {TRIM_APPDEST} && {cli} stop fnnas.fnsoar && sleep 2 "
           f"&& {cli} start fnnas.fnsoar")
    _sp.Popen(["sh", "-c", "sleep 1 && " + cmd + " >/dev/null 2>&1 &"],
              start_new_session=True)
MIHOMO_BIN    = f"{TRIM_APPDEST}/bin/mihomo"
# The engine is a managed child of the panel daemon. fnOS tracks the admin
# server (bin/mihomo wrapper) as the app daemon; the engine is launched via
# bin/engine-start which applies host-transparent rules and drops privileges.
ENGINE_START  = f"{TRIM_APPDEST}/bin/engine-start"
ADMIN_DIR     = os.path.dirname(os.path.abspath(__file__))


def _resolve_ui_dir():
    """Locate the admin panel static assets (index.html / ui.html / ...).

    Installed layout: they ship next to this file (app/admin/).
    Source-tree layout (2025-09 repo reorg): the Python service lives in
    backend/admin/ while the static UI lives in frontend/admin/.
    """
    if os.path.exists(os.path.join(ADMIN_DIR, "index.html")):
        return ADMIN_DIR
    _base = os.path.dirname(ADMIN_DIR)
    for _up in range(1, 4):
        _cand = os.path.abspath(os.path.join(_base, *([".."] * _up), "frontend", "admin"))
        if os.path.exists(os.path.join(_cand, "index.html")):
            return _cand
    return ADMIN_DIR


ADMIN_UI_DIR  = _resolve_ui_dir()
DASHBOARD_DIR = f"{TRIM_PKGVAR}/dashboard"
# ── 应用图标切换（软件图标）：图标存数据目录，应用到 fnOS 网关 ui/images ──
ICON_STORE_DIR = f"{TRIM_PKGVAR}/icons"
ICON_META_FILE = f"{ICON_STORE_DIR}/current.json"
APP_ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "images")
HOST_TRANSPARENT_SCRIPT = os.path.join(ADMIN_DIR, "host_transparent.sh")
HOST_TRANSPARENT_PORT = int(os.environ.get("MIHOMO_REDIR_PORT", "7892"))
_PROVIDER_RESTART_LOCK = threading.Lock()
_PROVIDER_RESTARTING = False
_SUB_VALIDATION_CACHE = {}
_SUB_VALIDATION_CACHE_LOCK = threading.Lock()
_SUB_VALIDATION_CACHE_TTL = 120
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

def _tail_file(path, n=200):
    """高效读取文件末尾 n 行（避免 readlines() 读整个大文件）。"""
    with open(path, "rb") as f:
        BUFSIZE = 65536
        buf = b""
        f.seek(0, 2)
        pos = f.tell()
        while pos > 0 and buf.count(b"\n") <= n:
            read_size = min(BUFSIZE, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size) + buf
        lines = buf.decode("utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])

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
    4. source-tree fallback: walk up from the admin dir looking for
       app.json / manifest, incl. the fnpack/ packaging dir
    Returns "unknown" if none is found."""
    env_ver = os.environ.get("TRIM_APPVER", "").strip()
    if env_ver:
        return env_ver
    candidates = [
        os.path.join(TRIM_APPDEST, "VERSION"),
        os.path.join(TRIM_APPDEST, "app.json"),
        os.path.join(TRIM_APPDEST, "manifest"),
    ]
    # Source-tree fallback (2025-09 reorg): admin_server.py now lives in
    # backend/admin/, packaging metadata in fnpack/ — search both upward.
    _admin_parent = os.path.dirname(ADMIN_DIR)
    for _up in range(0, 4):
        _root = os.path.abspath(os.path.join(_admin_parent, *([".."] * _up))) if _up else _admin_parent
        candidates += [
            os.path.join(_root, "app.json"),
            os.path.join(_root, "manifest"),
            os.path.join(_root, "fnpack", "app.json"),
            os.path.join(_root, "fnpack", "manifest"),
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

def _github_fetch_json(url, timeout=15):
    """GitHub API 拉取：本地 mihomo 代理优先 → 直连兜底，各重试一次。

    GitHub 直连在境内不稳定（实测约半数 TLS EOF），而本机代理端口
    通常可达；内核/应用版本检测与更新日志都走这里，避免加载时赶上
    坏窗口就静默失败（徽章不出现）。
    """
    import urllib.request as _ureq
    token = os.environ.get("MIHOMO_GITHUB_TOKEN", "")
    headers = {"User-Agent": "ClashMini/1.0", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _fetch(use_proxy):
        req = _ureq.Request(url, headers=headers)
        if use_proxy:
            proxy = os.environ.get("MIHOMO_PROXY_ADDR", "http://127.0.0.1:7890")
            opener = _ureq.build_opener(_ureq.ProxyHandler({"http": proxy, "https": proxy}))
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        with _ureq.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    last = None
    for use_proxy in (True, False):
        for _try in range(2):
            try:
                return _fetch(use_proxy)
            except _ureq.HTTPError:
                raise        # 服务端已明确响应（403 限流/404 私有等），重试只会加速耗尽配额
            except Exception as e:
                last = e     # 仅网络层错误（超时/连接失败）才重试
    raise last

def _github_fetch_text(url, timeout=15):
    """GitHub 文本拉取（与 _github_fetch_json 同策略：本地代理优先、直连兜底）。"""
    import urllib.request as _ureq
    token = os.environ.get("MIHOMO_GITHUB_TOKEN", "")
    headers = {"User-Agent": "ClashMini/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _fetch(use_proxy):
        req = _ureq.Request(url, headers=headers)
        if use_proxy:
            proxy = os.environ.get("MIHOMO_PROXY_ADDR", "http://127.0.0.1:7890")
            opener = _ureq.build_opener(_ureq.ProxyHandler({"http": proxy, "https": proxy}))
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        with _ureq.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    last = None
    for use_proxy in (True, False):
        for _try in range(2):
            try:
                return _fetch(use_proxy)
            except _ureq.HTTPError:
                raise
            except Exception as e:
                last = e
    raise last


def _github_latest_via_atom(repo):
    """经 releases.atom 取最新版本号——该端点不走 REST API 的匿名限流
    （60 次/小时，代理出口 IP 共享时极易耗尽）。"""
    xml = _github_fetch_text(f"https://github.com/{repo}/releases.atom")
    # atom 按时间排序且含预发布（如 mihomo 的 "Prerelease-Alpha" 排在正式版前），
    # 取第一条符合版本号格式的 entry 标题
    for m in re.finditer(r"<entry>.*?<title>([^<]+)</title>", xml, re.S):
        title = m.group(1).strip()
        if re.match(r"^v?\d+(\.\d+){1,}", title):
            return title
    raise RuntimeError("atom 无正式版本 entry")

def _github_latest(repo):
    """Query GitHub release 'latest' using the default GitHub API address
    (https://api.github.com). An optional MIHOMO_GITHUB_TOKEN is attached when
    available (raises the rate limit and enables private-repo access)."""
    gh_api = os.environ.get("MIHOMO_GITHUB_API", "https://api.github.com").rstrip("/")
    try:
        return _github_fetch_json(f"{gh_api}/repos/{repo}/releases/latest")
    except Exception:
        # REST 限流/不可达：atom 源取版本号（无资产列表，资产由调用方按仓库规则拼）
        ver = _github_latest_via_atom(repo)
        return {"tag_name": ver, "assets": [], "atom_fallback": True}

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
    if not url and d.get("atom_fallback"):
        # atom 没有资产列表：按仓库既定命名规则拼下载地址
        tag = (d.get("tag_name") or "").strip()
        base = f"https://github.com/{meta['repo']}/releases/download/{tag}/"
        for cand in (f"dist-{latest}.zip", f"{latest}.zip", f"metacubexd-{latest}.tgz", f"{latest}.tgz"):
            if meta["asset_match"](cand):
                url = base + cand
                break
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


def _parse_changelog_md(md_text):
    """解析本地 CHANGELOG.md → releases 列表（与 GitHub 结构一致：version/date/body）。"""
    out = []
    blocks = re.split(r'(?m)^##\s+', md_text)
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        first_nl = b.find('\n')
        head_txt = b if first_nl < 0 else b[:first_nl]
        body = '' if first_nl < 0 else b[first_nl + 1:].strip()
        m = re.match(r'v?([0-9][0-9.]*)\s*[·\-\s]*(\d{4}-\d{2}-\d{2})?', head_txt)
        if not m:
            continue
        out.append({"version": m.group(1), "date": m.group(2) or "", "body": body, "url": ""})
    return out[:12]

def _read_profile_ext():
    """读取全局扩展配置（合并模板 + 脚本）。文件缺失时返回默认模板。"""
    try:
        with open(PROFILE_EXT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        d = {}
    # 默认空：未配置时不改动用户配置；模板仅作为「重置为默认值」的初始内容
    return {
        "merge_config": d.get("merge_config", ""),
        "script": d.get("script", ""),
    }

def _save_profile_ext(data):
    try:
        os.makedirs(os.path.dirname(PROFILE_EXT_FILE), exist_ok=True)
        tmp = PROFILE_EXT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"merge_config": data.get("merge_config", ""),
                       "script": data.get("script", "")}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROFILE_EXT_FILE)
        return True
    except Exception:
        return False

def _deep_merge(base, patch):
    """递归合并：dict 深合并，其余类型（list/标量）直接覆盖。"""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _deep_merge(base[k], v) if k in base else v
        return out
    return patch

def _find_node_bin():
    for cand in ("/var/apps/nodejs_v24/target/bin/node", "/var/apps/nodejs_v22/target/bin/node",
                 "/usr/local/bin/node", "/usr/bin/node"):
        if os.path.exists(cand):
            return cand
    return None

def _run_profile_script(config_text, script):
    """执行全局扩展脚本（签名同 Clash Verge：main(config, profileName)）。
    node 不可用或脚本异常时原样返回，不影响保存。"""
    node = _find_node_bin()
    if not node:
        print("[profile-ext] 未找到 node，跳过全局扩展脚本", file=sys.stderr)
        return config_text
    import json as _json
    try:
        cfg_obj = yaml.safe_load(config_text) or {}
    except Exception:
        return config_text
    wrapper = script + """
;process.stdout.write(JSON.stringify((typeof main === 'function' ? main(JSON.parse(require('fs').readFileSync(0,'utf8')), process.argv[2] || '') : JSON.parse(require('fs').readFileSync(0,'utf8')))));"""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
            tf.write(wrapper)
            js_path = tf.name
        r = subprocess.run([node, js_path, "fnsoar"], input=_json.dumps(cfg_obj),
                           capture_output=True, text=True, timeout=10)
        os.unlink(js_path)
        if r.returncode != 0:
            print("[profile-ext] 脚本执行失败: " + (r.stderr or "")[:300], file=sys.stderr)
            return config_text
        out = _json.loads(r.stdout)
        if out == cfg_obj:
            return config_text  # 无变化：保持原文，避免 YAML 重排
        return yaml.safe_dump(out, allow_unicode=True, sort_keys=False)
    except Exception as e:
        print("[profile-ext] 脚本异常: " + str(e)[:300], file=sys.stderr)
        return config_text

def _apply_profile_ext(cfg_text):
    """配置写库前应用全局扩展（执行顺序与弹窗声明一致）：
    订阅原文 + 应用设置（已在 cfg_text 中）→ 全局扩展配置（深合并）
    → 全局扩展脚本（node）→ 应用设置回写（设置字段恢复为扩展前的值）。
    未配置扩展时原文返回；任一步无实质变化不触发 YAML 重排。"""
    ext = _read_profile_ext()
    merge = (ext.get("merge_config") or "").strip()
    script = (ext.get("script") or "").strip()
    if not merge and not script:
        return cfg_text
    text = cfg_text
    # 1) 快照「应用设置接管」字段（扩展前的值即应用设置）
    snap = {}
    try:
        base = yaml.safe_load(text) or {}
        if isinstance(base, dict):
            for k in APP_MANAGED_KEYS:
                if k in base:
                    snap[k] = base[k]
            for s in APP_MANAGED_SECTIONS:
                if s in base:
                    snap[s] = base[s]
    except Exception:
        snap = {}
    # 2) 全局扩展配置：深合并
    if merge:
        try:
            base = yaml.safe_load(text) or {}
            patch = yaml.safe_load(merge) or {}
            if isinstance(base, dict) and isinstance(patch, dict):
                merged = _deep_merge(base, patch)
                if merged != base:
                    text = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)
        except Exception as e:
            print("[profile-ext] 合并模板解析失败: " + str(e)[:300], file=sys.stderr)
    # 3) 全局扩展脚本
    if script:
        text = _run_profile_script(text, ext.get("script") or "")
    # 4) 应用设置回写：设置字段恢复为扩展前快照（扩展改不动）
    if snap:
        try:
            final = yaml.safe_load(text) or {}
            if isinstance(final, dict) and any(final.get(k) != v for k, v in snap.items()):
                final.update(snap)
                text = yaml.safe_dump(final, allow_unicode=True, sort_keys=False)
        except Exception as e:
            print("[profile-ext] 应用设置回写失败: " + str(e)[:300], file=sys.stderr)
    return text

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
    url = f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/configs"
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


# ── 系统代理（代理环境变量托管，参考 Clash-for-fnos 实现） ─────────────────
# fnOS 没有桌面意义上的"系统代理"，这里的系统代理 = 往 /etc/environment、
# /etc/profile、/etc/bash.bashrc 写入带标记的托管块（HTTP_PROXY 等），
# 对读取环境变量的新进程/新登录会话生效；关闭时只移除本应用的管理块。
PROXY_BLOCK_BEGIN = "# BEGIN FNSOAR MANAGED PROXY"
PROXY_BLOCK_END   = "# END FNSOAR MANAGED PROXY"
PROXY_TARGETS = (
    ("environment", "/etc/environment", False),
    ("profile",     "/etc/profile",     True),
    ("bashrc",      "/etc/bash.bashrc", True),
)
SYSTEM_PROXY_SETTINGS_FILE = f"{TRIM_PKGVAR}/system-proxy.json"
PROXY_BACKUP_DIR = f"{TRIM_PKGVAR}/backups/proxy-environment"

# ── 内核设置（运行方式 / 延迟测试参数） ────────────────────────────────
CORE_SETTINGS_FILE = f"{TRIM_PKGVAR}/core-settings.json"


ICON_OPTIONS = (
    ("default", "默认（fnSoar）"),
    ("fnsoar2", "备选一"),
    ("fnsoar3", "备选二"),
    ("classic", "经典图标"),
)

def _app_icon_options():
    out = []
    try:
        for iid, name in ICON_OPTIONS:
            f = os.path.join(ICON_STORE_DIR, iid + ".png")
            if os.path.exists(f):
                out.append({"id": iid, "name": name, "has_icon": True})
    except Exception:
        pass
    return out

def _app_icon_selected():
    try:
        with open(ICON_META_FILE, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("selected") or "default")
    except Exception:
        return "default"

def _apply_app_icon(icon_id):
    """把所选图标应用到 fnOS 网关读取的 ui/images（256/64）。"""
    src = os.path.join(ICON_STORE_DIR, icon_id + ".png")
    if not os.path.exists(src):
        raise RuntimeError("图标文件不存在: " + icon_id)
    dst256 = os.path.join(APP_ICON_DIR, "icon_256.png")
    dst64 = os.path.join(APP_ICON_DIR, "icon_64.png")
    os.makedirs(APP_ICON_DIR, exist_ok=True)
    shutil.copyfile(src, dst256)
    # 同步面板自身 logo（favicon.png：左侧栏顶部图标 + 浏览器标签页图标）
    try:
        shutil.copyfile(src, os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.png"))
    except Exception:
        pass
    try:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGBA")
            im.resize((64, 64), Image.LANCZOS).save(dst64)
    except Exception:
        shutil.copyfile(src, dst64)
    with open(ICON_META_FILE, "w", encoding="utf-8") as f:
        json.dump({"selected": icon_id}, f)
    return True

def _default_core_settings():
    return {
        "healthcheck_url": "https://www.gstatic.com/generate_204",
        "healthcheck_timeout": 3000,
    }


def _read_core_settings():
    st = _default_core_settings()
    try:
        with open(CORE_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    if isinstance(data.get("healthcheck_url"), str) and data["healthcheck_url"].strip():
        st["healthcheck_url"] = data["healthcheck_url"].strip()
    try:
        st["healthcheck_timeout"] = max(1000, min(30000, int(data.get("healthcheck_timeout", st["healthcheck_timeout"]))))
    except Exception:
        pass
    return st


def _write_core_settings(settings):
    try:
        os.makedirs(os.path.dirname(CORE_SETTINGS_FILE), exist_ok=True)
        tmp = CORE_SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CORE_SETTINGS_FILE)
    except Exception:
        pass


GEO_FILE_MAP = (("geoip", "GeoIP", "geoip.dat"),
                ("geosite", "GeoSite", "geosite.dat"),
                ("mmdb", "Country MMDB", "Country.mmdb"),
                ("asn", "ASN MMDB", "ASN.mmdb"))
GEO_MAX_BYTES = 200 * 1024 * 1024


def _geo_files_payload():
    files = []
    for key, label, fname in GEO_FILE_MAP:
        fpath = os.path.join(TRIM_PKGVAR, fname)
        entry = {"key": key, "label": label, "filename": fname,
                 "exists": False, "size": 0, "mtime": ""}
        if os.path.exists(fpath):
            st = os.stat(fpath)
            entry["exists"] = True
            entry["size"] = st.st_size
            entry["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        files.append(entry)
    return files


def _geox_urls():
    """config.yaml geox-url 段：key -> 下载地址。"""
    try:
        cfg = yaml.safe_load(read_config()) or {}
        geox = cfg.get("geox-url") or {}
        out = {}
        for k, _l, _f in GEO_FILE_MAP:
            u = str(geox.get(k, "") or "").strip()
            if u.startswith(("http://", "https://")):
                out[k] = u
        return out
    except Exception:
        return {}


def _download_geo_file(key, url):
    """下载单个 GEO 文件到数据目录（原子写入）。"""
    import urllib.request as _ureq
    fname = dict((k, f) for k, _l, f in GEO_FILE_MAP)[key]
    req = _ureq.Request(url, headers={"User-Agent": "fnSoar/1.0"})
    with _ureq.urlopen(req, timeout=180) as resp:
        blob = resp.read(GEO_MAX_BYTES + 1)
    if len(blob) > GEO_MAX_BYTES:
        raise RuntimeError("文件超过 200MB 限制")
    dest = os.path.join(TRIM_PKGVAR, fname)
    tmp = dest + ".download"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, dest)
    try:
        os.chmod(dest, 0o644)
    except Exception:
        pass
    # 属主与既有 GEO 文件保持一致（引擎以降权 UID 运行，需可读）
    try:
        import pwd as _pwd, grp as _grp
        os.chown(dest, _pwd.getpwnam("fnsoar").pw_uid, _grp.getgrnam("fnsoar").gr_gid)
    except Exception:
        pass
    return fname


def _ctrl_endpoint():
    """面板→引擎 API 的 (host, port)（由包装器环境变量提供）。"""
    return (MIHOMO_CTRL_HOST, MIHOMO_CTRL_PORT)
DEFAULT_NO_PROXY = "localhost,127.0.0.1,::1"


def _default_proxy_settings():
    return {
        "enabled": False,
        "explicit": False,          # 用户是否明确设置过（未明确则启动时自动关闭，防误启）
        "follow_mixed_port": True,  # 端口跟随 mihomo mixed-port
        "port": 7890,
        "no_proxy": DEFAULT_NO_PROXY,
        "targets": {"environment": True, "profile": True, "bashrc": True},
    }


def _read_proxy_settings():
    settings = _default_proxy_settings()
    try:
        with open(SYSTEM_PROXY_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    for k in ("enabled", "explicit", "follow_mixed_port", "no_proxy"):
        if k in data:
            settings[k] = data[k]
    if isinstance(data.get("targets"), dict):
        for k in ("environment", "profile", "bashrc"):
            if k in data["targets"]:
                settings["targets"][k] = bool(data["targets"][k])
    try:
        settings["port"] = int(data.get("port", settings["port"]))
    except Exception:
        settings["port"] = 7890
    if not (1 <= settings["port"] <= 65535):
        settings["port"] = 7890
    if not isinstance(settings["no_proxy"], str):
        settings["no_proxy"] = DEFAULT_NO_PROXY
    return settings


def _sanitize_no_proxy(value):
    value = (value or "").strip()
    if len(value) > 2048 or any(c in value for c in "\r\n\x00\"'"):
        return None, "NO_PROXY 格式无效"
    return value, None


def _proxy_block_text(settings, shell):
    """Render the managed proxy block. shell=True → export 前缀（profile/bashrc）。"""
    port = settings["port"]
    http_url = f"http://127.0.0.1:{port}"
    socks_url = f"socks5h://127.0.0.1:{port}"
    pairs = [
        ("HTTP_PROXY", http_url), ("HTTPS_PROXY", http_url),
        ("ALL_PROXY", socks_url), ("NO_PROXY", settings["no_proxy"]),
        ("http_proxy", http_url), ("https_proxy", http_url),
        ("all_proxy", socks_url), ("no_proxy", settings["no_proxy"]),
    ]
    lines = [PROXY_BLOCK_BEGIN]
    for key, val in pairs:
        lines.append(f'export {key}="{val}"' if shell else f'{key}="{val}"')
    lines.append(PROXY_BLOCK_END)
    return "\n".join(lines)


def _strip_proxy_block(raw):
    """Remove our managed block only. Refuse on nested/orphan/unclosed markers."""
    active = False
    kept = []
    for chunk in raw.splitlines(keepends=True):
        marker = chunk.strip()
        if marker == PROXY_BLOCK_BEGIN:
            if active:
                return None, "检测到嵌套的系统代理管理块，拒绝自动覆盖"
            active = True
            continue
        if marker == PROXY_BLOCK_END:
            if not active:
                return None, "检测到孤立的系统代理管理块结束标记，拒绝自动覆盖"
            active = False
            continue
        if not active:
            kept.append(chunk)
    if active:
        return None, "系统代理管理块未闭合，拒绝自动覆盖"
    return "".join(kept), None


def _insert_proxy_block(raw, block):
    clean, err = _strip_proxy_block(raw)
    if err:
        return None, err
    clean = clean.rstrip("\n")
    if clean:
        clean += "\n\n"
    return clean + block + "\n", None


def _atomic_write_path(path, text):
    mode = 0o644
    try:
        mode = os.stat(path).st_mode & 0o777
    except Exception:
        pass
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".fnsoar.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _backup_proxy_target(path):
    """备份前保留一份原始文件（仅在有内容时）。"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return
        os.makedirs(PROXY_BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = path.strip("/").replace("/", "_") + "-" + stamp
        shutil.copy2(path, os.path.join(PROXY_BACKUP_DIR, name))
    except Exception:
        pass


def _apply_proxy_settings(settings):
    """把托管块写入/移出三个目标文件。返回 (changed_paths, error)。"""
    changed = []
    for key, path, shell in PROXY_TARGETS:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except FileNotFoundError:
            raw = ""
        except Exception as e:
            return changed, f"读取 {path} 失败: {e}"
        if settings["enabled"] and settings["targets"].get(key):
            nxt, err = _insert_proxy_block(raw, _proxy_block_text(settings, shell))
        else:
            nxt, err = _strip_proxy_block(raw)
        if err:
            return changed, err
        if nxt == raw:
            continue
        _backup_proxy_target(path)
        try:
            _atomic_write_path(path, nxt)
        except Exception as e:
            return changed, f"写入 {path} 失败: {e}"
        changed.append(path)
    return changed, None


def _parse_proxy_env_lines(raw):
    out = []
    rx = re.compile(r'(?i)^(?:export\s+)?(https?_proxy|all_proxy|no_proxy)\s*=\s*(.+)$')
    for i, line in enumerate(raw.split("\n")):
        m = rx.match(line.strip())
        if not m:
            continue
        value = m.group(2).strip().strip("\"'")
        out.append({"key": m.group(1), "value": value, "line": i + 1})
    return out


def _proxy_status_data(settings=None):
    if settings is None:
        settings = _read_proxy_settings()
    files = []
    for key, path, shell in PROXY_TARGETS:
        entry = {"key": key, "path": path, "shell": shell, "exists": False, "variables": []}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                entry["exists"] = True
                entry["variables"] = _parse_proxy_env_lines(f.read())
        except Exception:
            pass
        files.append(entry)
    return {"enabled": settings["enabled"], "settings": settings, "files": files}


def _write_proxy_settings(settings, changed):
    try:
        os.makedirs(os.path.dirname(SYSTEM_PROXY_SETTINGS_FILE), exist_ok=True)
        tmp = SYSTEM_PROXY_SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SYSTEM_PROXY_SETTINGS_FILE)
    except Exception:
        pass
    status = _proxy_status_data(settings)
    status["changed"] = changed
    return status


def update_proxy_settings(body):
    """API 入口：更新并应用系统代理设置。返回 (status, error)。"""
    settings = _read_proxy_settings()
    if "enabled" in body:
        settings["enabled"] = bool(body["enabled"])
        settings["explicit"] = True
    if "follow_mixed_port" in body:
        settings["follow_mixed_port"] = bool(body["follow_mixed_port"])
    if "port" in body:
        try:
            settings["port"] = int(body["port"])
        except Exception:
            return None, "代理端口无效"
    if not (1 <= settings["port"] <= 65535):
        return None, "代理端口无效"
    if "no_proxy" in body:
        clean, err = _sanitize_no_proxy(body["no_proxy"])
        if err:
            return None, err
        settings["no_proxy"] = clean
    if isinstance(body.get("targets"), dict):
        for k in ("environment", "profile", "bashrc"):
            if k in body["targets"]:
                settings["targets"][k] = bool(body["targets"][k])
    # 跟随 mixed-port 时，端口始终以当前 engine 配置为准
    if settings["follow_mixed_port"]:
        settings["port"] = _mixed_port_from_config()
    changed, err = _apply_proxy_settings(settings)
    if err:
        return None, err
    return _write_proxy_settings(settings, changed), None


def reconcile_proxy_on_start():
    """启动对账：历史上可能因保存其它网络设置而意外生成 enabled 的托管块，
    未明确开启过的（explicit=false）一律关闭，仅移除本应用管理块。"""
    settings = _read_proxy_settings()
    if settings["enabled"] and not settings["explicit"]:
        settings["enabled"] = False
        changed, err = _apply_proxy_settings(settings)
        if not err:
            _write_proxy_settings(settings, changed)


# ── TUN 设置（虚拟网卡模式，读取/保存 tun: 段全部参数） ────────────────────
_TUN_DEFAULTS = {
    "enable": False, "device": "tun", "stack": "mixed", "mtu": 1500,
    "dns-hijack": [], "auto-route": True, "auto-redirect": True,
    "auto-detect-interface": True, "strict-route": False,
    "route-exclude-address": [],
}
_TUN_SCALAR_KEYS = ("device", "stack", "mtu")
_TUN_BOOL_KEYS = ("auto-route", "auto-redirect", "auto-detect-interface", "strict-route")
_TUN_LIST_KEYS = ("dns-hijack", "route-exclude-address")
_TUN_ALLOWED = set(_TUN_DEFAULTS)


def _read_tun_settings():
    """当前 config.yaml 的 tun: 段（套默认值）。"""
    out = dict(_TUN_DEFAULTS)
    try:
        cfg = yaml.safe_load(read_config()) or {}
        tun = cfg.get("tun") or {}
        for k in _TUN_DEFAULTS:
            if k in tun and tun[k] is not None:
                out[k] = tun[k]
    except Exception:
        pass
    return out


def _format_tun_yaml_value(key, value):
    if key in _TUN_BOOL_KEYS:
        return "true" if value else "false"
    if key in _TUN_LIST_KEYS:
        items = [str(v) for v in (value or []) if str(v).strip()]
        if not items:
            return "[]"
        return "\n" + "".join(f'    - "{v}"\n' for v in items)
    return str(value)


def set_tun_settings_in_config(patch):
    """行级合并 tun 设置（保留文件其余字节与注释）。返回 (ok, msg)。"""
    if not isinstance(patch, dict):
        return False, "参数错误"
    clean = {}
    for k, v in patch.items():
        if k not in _TUN_ALLOWED:
            continue
        if k in _TUN_BOOL_KEYS:
            clean[k] = bool(v)
        elif k in _TUN_LIST_KEYS:
            if not isinstance(v, list):
                return False, f"{k} 必须是数组"
            items = [str(x) for x in v if str(x).strip()]
            if any(any(c in x for c in "\r\n\"'") for x in items):
                return False, f"{k} 含非法字符"
            clean[k] = items
        elif k == "mtu":
            try:
                mtu = int(v)
            except Exception:
                return False, "MTU 必须是整数"
            if not (1280 <= mtu <= 65535):
                return False, "MTU 必须在 1280–65535 之间"
            clean[k] = mtu
        elif k == "stack":
            if v not in ("mixed", "system", "gvisor"):
                return False, "stack 仅支持 mixed/system/gvisor"
            clean[k] = v
        else:
            clean[k] = str(v)
    if not clean:
        return True, "ok"
    # Mihomo 的 TUN 初始化要求 auto-redirect 必须伴随 auto-route：
    # 单独开启 auto-redirect（auto-route:false）会让 sing_tun 走错误路径，
    # 收尾时 nil 解引用直接 panic——引擎启动即崩（页面全空白）。
    # 按“合并后的最终值”判断，只改其中一个开关也要拦。
    eff = _read_tun_settings()
    eff.update(clean)
    if eff.get("auto-redirect") and not eff.get("auto-route"):
        return False, ("自动重定向(auto-redirect)依赖自动路由(auto-route)："
                       "请先开启「自动路由」，或关闭「自动重定向」")
    txt = read_config()
    if not txt.strip():
        return False, "config 为空"
    new_txt = _merge_tun_section(txt, clean)
    if new_txt is None:
        return False, "config.yaml 中未找到 tun: 段"
    if write_config(new_txt):
        return True, "ok"
    return False, "写入失败"


def _merge_tun_section(txt, patch):
    """在 tun: 段内行级替换/插入 patch 中的键；列表键整块替换。"""
    lines = txt.splitlines(keepends=True)
    tun_idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^tun:\s*$", ln):
            tun_idx = i
            break
    if tun_idx is None:
        return None
    indent0 = len(lines[tun_idx]) - len(lines[tun_idx].lstrip())
    pad = " " * (indent0 + 2)
    # 段结束行（第一个缩进 <= indent0 的非空行）
    end = len(lines)
    for j in range(tun_idx + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        if len(ln) - len(ln.lstrip()) <= indent0:
            end = j
            break
    for key, value in patch.items():
        rendered = _format_tun_yaml_value(key, value)
        replaced = False
        j = tun_idx + 1
        while j < end:
            ln = lines[j]
            if re.match(rf"^{re.escape(pad)}{re.escape(key)}:\s*", ln):
                if key in _TUN_LIST_KEYS:
                    # 列表键：连同后续列表项整块替换（空列表写成 []）
                    k = j + 1
                    while k < end and re.match(rf"^{re.escape(pad)}\s*-\s", lines[k]):
                        k += 1
                    if rendered == "[]":
                        lines[j:k] = [f"{pad}{key}: []\n"]
                    else:
                        lines[j:k] = [f"{pad}{key}:{rendered}"]
                    end += (j + 1) - k
                else:
                    lines[j] = f"{pad}{key}: {rendered}\n"
                replaced = True
                break
            j += 1
        if not replaced:
            lines.insert(tun_idx + 1, f"{pad}{key}: {rendered}\n")
            end += 1
    return "".join(lines)


def _mixed_port_from_config():
    """从 config.yaml 读取 mixed-port（系统代理端口跟随用）。"""
    try:
        cfg = yaml.safe_load(read_config()) or {}
        port = int(cfg.get("mixed-port", 7890))
        return port if 1 <= port <= 65535 else 7890
    except Exception:
        return 7890

def _foreign_engine_listener():
    """控制器端口有监听，但本应用引擎进程不存在。

    9090 是多款 Mihomo 面板的默认控制器端口（clash-for-fnos 等同样
    默认占用它）。端口被外来内核占用时若谎报“运行中”，启动会被
    “服务已在运行”挡死，而 API 代理只会拿到外来内核的 401（前端页面
    整页空白）。调用方应把这视为“fnSoar 引擎未运行 + 端口被占”。"""
    if _port_free(_ctrl_endpoint()[1]):
        return False
    return not _find_engine_pids()


def is_running():
    """Return the real engine state, not only the wrapper PID file.
    fnOS can briefly leave a stale/missing PID during reinstall or wrapper
    handoff while mihomo is already listening; the UI must still show ON.
    NOTE: since v1.0.68 the PID file tracks the ADMIN daemon (which is
    always alive while the panel is open), so engine status must never be
    derived from it.
    权威判据是本应用自己的引擎进程（bin/mihomo-{amd64,arm64}.real，由
    engine-start 启动）；端口在听但进程不存在属于外来内核占用，见
    _foreign_engine_listener()，此时必须报告未运行。"""
    pids = _find_engine_pids()
    if pids:
        return True, pids[0]
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
            if _port_free(_ctrl_endpoint()[1]):
                break
            time.sleep(0.5)
        # 端口被外来内核占用时不要硬启：引擎会因 bind 失败秒退，白等 20s
        # 后只换来一个没有信息量的“启动失败”。
        if not _port_free(_ctrl_endpoint()[1]) and not _find_engine_pids():
            return {"success": False,
                    "error": (f"控制器端口 {_ctrl_endpoint()[1]} 被其他程序占用"
                              "（常见于其他 Mihomo 面板/应用，如 clash-for-fnos）。"
                              "请先停止占用该端口的应用，再启动 fnSoar 引擎")}
        # TUN 组合预检：Mihomo 要求 auto-redirect 必须伴随 auto-route，
        # 非法组合会让 sing_tun 初始化走错误路径并 panic（引擎启动即崩）。
        tun_pre = _read_tun_settings()
        if (tun_pre.get("enable") and tun_pre.get("auto-redirect")
                and not tun_pre.get("auto-route")):
            return {"success": False,
                    "error": ("TUN 配置无效：「自动重定向(auto-redirect)」需要同时开启"
                              "「自动路由(auto-route)」。请先在 TUN 设置中修正后再启动服务")}
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
        return {"success": False,
                "error": ("引擎启动失败：进程已退出（多为端口被占、配置非法或"
                          f"TUN 初始化失败）。请查看日志 {TRIM_PKGVAR}/{TRIM_APPNAME}.log")}
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
            if _port_free(_ctrl_endpoint()[1]):
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

_SUB_UA_OVERRIDE = None

# 订阅拉取通道记忆（对齐 clash-verge-rev 的 PrfOption.self_proxy 持久化语义）：
# 每条 URL 记住上次成功通道（direct/proxy），更新时优先走它，避免被墙域名反复直连干等。
_SUB_CHANNELS_FILE = f"{TRIM_PKGVAR}/sub-channels.json"


def _sub_channel_get(url):
    try:
        with open(_SUB_CHANNELS_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get(url)
    except Exception:
        return None


def _sub_channel_set(url, channel):
    try:
        data = {}
        if os.path.exists(_SUB_CHANNELS_FILE):
            with open(_SUB_CHANNELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        data[url] = channel
        tmp = _SUB_CHANNELS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _SUB_CHANNELS_FILE)
    except Exception as e:
        log(f"save sub channel failed: {e}")


def _sub_user_agent():
    """订阅请求 UA。Clash Meta 格式可让机场返回完整 YAML 和名称响应头。"""
    return _SUB_UA_OVERRIDE or f"clash.meta/{_get_core_version_cached()}"


def _provider_header_lines():
    """proxy-provider 的 header 配置块：钉死内核下载订阅时的 User-Agent。"""
    return (
        "    header:\n"
        "      User-Agent:\n"
        f"        - \"{_sub_user_agent()}\"\n"
    )


def _provider_block(name, url, interval, ptype="http"):
    block = f"  {name}:\n"
    block += f"    type: {ptype}\n"
    block += f"    url: \"{url}\"\n"
    block += f"    interval: {int(interval)}\n"
    block += _provider_header_lines()
    block += "    health-check:\n"
    block += "      enable: true\n"
    block += "      url: https://www.gstatic.com/generate_204\n"
    block += "      interval: 180\n"
    return block + "\n"  # 与其他 provider 之间保留一行空行


_PROVIDER_BLOCK_RE = None


def _build_subfetch_url(orig_url):
    import base64 as _b64
    b64u = _b64.urlsafe_b64encode(orig_url.encode()).decode().rstrip("=")
    return f"http://127.0.0.1:{ADMIN_PORT}/subfetch?u={b64u}"


def _ensure_provider_headers():
    """启动迁移：为所有 http 型 proxy-provider 补钉白名单 UA 的 header 块。
    幂等（已含 header 的跳过）；仅改写 config.yaml 文本，不触发引擎动作。"""
    global _PROVIDER_BLOCK_RE
    try:
        import re as _re
        if _PROVIDER_BLOCK_RE is None:
            _PROVIDER_BLOCK_RE = _re.compile(
                r'(?ms)^(  (?P<key>[^\s:#][^:\n]*):\n)'
                r'(?P<body>(?:[ \t]+[^\n]*\n)+)')
        hl = _provider_header_lines()
        content = read_config()

        def _repl(m):
            body = m.group("body")
            if not _re.match(r'\s*type:\s*http\b', body):
                return m.group(0)
            if _re.search(r'^\s*header:', body, _re.M):
                return m.group(0)
            mm = _re.search(r'[ \t]+health-check:\n', body)
            if mm:
                body2 = body[:mm.start()] + hl + body[mm.start():]
            else:
                body2 = body + hl
            return m.group(1) + body2

        new_content = _PROVIDER_BLOCK_RE.sub(_repl, content)
        if new_content != content:
            # 先确保 YAML 可解析再落盘，防止手写配置被迁移弄坏
            import yaml as _yaml
            doc = _yaml.safe_load(new_content)
            assert isinstance(doc, dict) and isinstance(doc.get("proxy-providers"), dict), \
                "header migration produced invalid proxy-providers"
            write_config(new_content)
            log("provider UA headers injected (migration)")
    except Exception as e:
        log(f"WARNING: ensure provider headers failed: {e}")

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

def _parse_userinfo(hd):
    """解析订阅的 subscription-userinfo 头（含 x-amz-meta- 等前缀变体）。
    返回 {"upload":int,"download":int,"total":int,"expire":int}；无则 {}。"""
    import re as _re
    raw = None
    for k, v in (hd or {}).items():
        if "subscription-userinfo" in k and (
                k == "subscription-userinfo" or k.endswith("-subscription-userinfo")):
            raw = v
            break
    if not raw:
        return {}
    out = {}
    for part in str(raw).split(";"):
        if "=" in part:
            k2, _, v2 = part.partition("=")
            k2, v2 = k2.strip().lower(), v2.strip()
            try:
                out[k2] = int(float(v2)) if v2 else 0
            except ValueError:
                out[k2] = 0
    return {
        "upload": int(out.get("upload", 0)),
        "download": int(out.get("download", 0)),
        "total": int(out.get("total", 0)),
        "expire": int(out.get("expire", 0)),
    }


def _sub_meta_from_headers(hd):
    """从订阅响应头解析元数据（name/updateInterval/home），供探测与验证共用。"""
    meta = {}
    for k in ("profile-title", "subscription-title"):
        if hd.get(k) and hd[k].strip():
            meta["name"] = _sanitize_provider_name(hd[k])
            break
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
    ui = hd.get("profile-update-interval", "")
    if ui:
        try:
            hrs = int(str(ui).strip())
            if hrs > 0:
                meta["updateInterval"] = hrs * 3600
        except Exception:
            pass
    home = hd.get("profile-web-page-url", "")
    if home and home.strip():
        meta["home"] = home.strip()
    return meta


def _sub_name_from_yaml(text):
    """YAML 正文里的 profile.name（很多机场放在文件末尾）。"""
    text = text.lstrip("\ufeff")
    try:
        doc = yaml.safe_load(text) or {}
        prof = doc.get("profile") or {}
        if isinstance(prof, dict) and prof.get("name"):
            return _sanitize_provider_name(str(prof["name"]))
    except Exception:
        m = re.search(r'(?m)^profile:\s*\n\s*name:\s*["\']?([^"\'\n]+)', text)
        if m and m.group(1).strip():
            return _sanitize_provider_name(m.group(1).strip())
    return ""


_NODE_LINK_RE = re.compile(r'\b(?:vmess|vless|trojan|ss|ssr|hysteria2?|tuic|snell)://')

def _subscription_filter_rules():
    """从主 config.yaml 读取用户可编辑的订阅信息节点过滤规则。"""
    try:
        cfg = yaml.safe_load(read_config()) or {}
        opts = ((cfg.get("x-fnsoar") or {}).get("subscription-filter") or {})
        if not isinstance(opts, dict) or not bool(opts.get("enabled", False)):
            return [], []
        keywords = [str(x).strip().lower() for x in (opts.get("exclude-name-keywords") or [])
                    if str(x).strip()]
        regexes = []
        for expr in (opts.get("exclude-name-regex") or []):
            try:
                regexes.append(re.compile(str(expr), re.I))
            except re.error as e:
                log(f"WARNING: invalid subscription filter regex '{expr}': {e}")
        return keywords, regexes
    except Exception as e:
        log(f"WARNING: load subscription filter config failed: {e}")
        return [], []


def _filter_subscription_info_nodes(body):
    """按 config.yaml 中的规则删除流量、到期时间等信息节点。"""
    try:
        keywords, regexes = _subscription_filter_rules()
        if not keywords and not regexes:
            return body, 0
        text = body.decode("utf-8", "ignore") if isinstance(body, (bytes, bytearray)) else str(body)
        doc = yaml.safe_load(text)
        proxies = (doc or {}).get("proxies") if isinstance(doc, dict) else None
        if not isinstance(proxies, list):
            return body, 0
        kept = []
        removed = 0
        for proxy in proxies:
            name = str(proxy.get("name", "")) if isinstance(proxy, dict) else ""
            lowered = name.lower()
            matched = any(keyword in lowered for keyword in keywords)
            if not matched:
                matched = any(regex.search(name) for regex in regexes)
            if name and matched:
                removed += 1
                continue
            kept.append(proxy)
        if not removed:
            return body, 0
        doc["proxies"] = kept
        rendered = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                                  default_flow_style=False, width=4096, indent=2)
        return rendered.encode("utf-8"), removed
    except Exception:
        return body, 0


def _looks_like_subscription(text):
    """判断内容是否像一份可用订阅。返回 (ok, node_count)。"""
    t = (text or "").strip()
    if not t:
        return False, 0
    # a) 纯 base64 订阅体
    compact = re.sub(r'\s+', '', t)
    if len(compact) > 64 and re.fullmatch(r'[A-Za-z0-9+/=_-]+', compact):
        import base64 as _b64
        for pad in ("", "=", "=="):
            try:
                dec = _b64.b64decode(compact + pad, validate=False).decode("utf-8", "ignore")
            except Exception:
                dec = ""
            if dec and _NODE_LINK_RE.search(dec):
                return True, len(_NODE_LINK_RE.findall(dec))
    # b) Clash YAML：带 proxies 列表 / provider 配置 / 完整配置骨架
    try:
        doc = yaml.safe_load(t)
        if isinstance(doc, dict):
            px = doc.get("proxies")
            if isinstance(px, list) and px:
                return True, len(px)
            if doc.get("proxy-providers") or doc.get("mode") in ("rule", "global", "direct"):
                return True, 0
    except Exception:
        pass
    # c) 明文节点链接列表
    n = len(_NODE_LINK_RE.findall(t))
    if n > 0:
        return True, n
    return False, 0


def _get_core_version_cached():
    """获取 Mihomo 版本；引擎关闭时从随包二进制读取，首次订阅也使用真实 UA。"""
    global _CORE_VER_CACHE
    if _CORE_VER_CACHE:
        return _CORE_VER_CACHE
    try:
        with urllib.request.urlopen(
                f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/version", timeout=1) as r:
            v = (json.loads(r.read().decode()) or {}).get("version") or ""
        if v:
            _CORE_VER_CACHE = v
            return _CORE_VER_CACHE
    except Exception:
        pass
    try:
        out = subprocess.check_output([MIHOMO_BIN, "-v"], stderr=subprocess.STDOUT,
                                      text=True, timeout=3)
        match = re.search(r"\bv?([0-9]+(?:\.[0-9]+){2})\b", out)
        if match:
            _CORE_VER_CACHE = "v" + match.group(1)
            return _CORE_VER_CACHE
    except Exception:
        pass
    return "v1.19.30"


_CORE_VER_CACHE = ""



PROVIDERS_DIR_KEY = "_fnsoar_sub_store"


def _providers_dir():
    import os
    d = os.path.join(TRIM_PKGVAR, "providers")
    os.makedirs(d, exist_ok=True)
    return d


def _sub_file_path(name):
    import re as _re2
    safe = _re2.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name)[:60] or "sub"
    return f"{_providers_dir()}/{safe}.yaml"


class _IPv4Only:
    """作用域内强制仅IPv4出站(防v6直连泄露)，退出即还原"""
    def __enter__(self):
        import socket as _s
        self._orig_cc = _s.create_connection
        def _cc_v4(address, *a, **kw):
            host, port = address[0], address[1]
            infos = _s.getaddrinfo(host, port, _s.AF_INET, _s.SOCK_STREAM)
            return self._orig_cc((infos[0][4][0], port), *a, **kw)
        _s.create_connection = _cc_v4
        return self
    def __exit__(self, *exc):
        import socket as _s2
        _s2.create_connection = self._orig_cc
        return False


def _download_sub_validated(name_hint, url, timeout=25):
    """verge语义的完整订阅获取: 下载→必须是有效clash/b64且含节点→
    否则按UA阶梯(clash.meta→clash-verge)重试。失败抛最后异常。"""
    import yaml as _yv
    import time as _t3
    last_err = None
    def _is_net_eof(e):
        s = str(e).lower()
        return ("eof" in s or "ssl" in s or "connection reset" in s
                or "timed out" in s or "timeout" in s)
    # 默认 UA 已是 clash.meta/{core-version}，无需再用相同 UA 重复下载。
    uas = [None]
    for i6, ua in enumerate(uas):
        try:
            if ua is None:
                t_out = timeout if i6 == 0 else min(timeout, 12)
            else:
                t_out = min(timeout, 12)
            if ua is None:
                body, hd = _download_sub(url, timeout=t_out)
            else:
                old_ua = _SUB_UA_OVERRIDE
                globals()["_SUB_UA_OVERRIDE"] = ua
                try:
                    body, hd = _download_sub(url, timeout=t_out)
                finally:
                    globals()["_SUB_UA_OVERRIDE"] = old_ua
        except Exception as e0:
            last_err = e0
            if _is_net_eof(e0):
                raise                    # TLS被掐=网络层问题,换UA无意义立即止损
            continue                     # 内容层错误才值得下一跳UA
        try:
            txt = body.decode("utf-8", "ignore")
            doc = _yv.safe_load(txt)
            n_p = len((doc or {}).get("proxies") or [])
            ok_shape = isinstance(doc, dict) and (isinstance(doc.get("proxies"), list) or bool(doc.get("proxy-providers")))
            if not ok_shape:
                last_err = RuntimeError("content missing proxies list"); continue
            if not n_p:
                last_err = RuntimeError("proxies empty"); continue
            body, removed = _filter_subscription_info_nodes(body)
            if removed:
                log(f"provider '{name_hint}' filtered {removed} info-only nodes")
            return body, hd
        except Exception as e1:
            last_err = e1; continue
    raise last_err


_SUB_PROXY_URL = "http://127.0.0.1:7890"   # 本机 mihomo mixed-port 代理出口(与clash-verge同源)


def _subscription_request_error(exc):
    """提取机场服务端返回的 JSON/text 错误，避免只显示笼统的 HTTP 403。"""
    reason = str(exc) or exc.__class__.__name__
    try:
        raw = exc.read(4096).decode("utf-8", "ignore").strip()
        if raw:
            try:
                payload = json.loads(raw)
                detail = payload.get("message") or payload.get("error")
            except Exception:
                detail = raw
            if detail:
                reason += f" ({detail})"
    except Exception:
        pass
    return reason


class _SubFetchError(RuntimeError):
    """订阅拉取失败。server_responded=True 表示服务器有明确响应（如 403 token 无效），
    与网络不可达（超时/拒连）区分开，便于给出正确的处理建议。"""

    def __init__(self, message, server_responded=False):
        super().__init__(message)
        self.server_responded = server_responded


def _open_sub_with_proxy(url, timeout, ua):
    """获取订阅，对齐 clash-verge-rev 的通道选择语义：
    记住该订阅上次成功通道并优先使用（相当于 Verge 持久化的 self_proxy 选项）；
    未知链接先直连（限时12秒），失败再走本机 mixed-port。直连超时不再直接放弃，
    因为被墙域名只能经代理获取。返回 (body, headers, via_proxy)。"""
    import urllib.request as _urq
    import urllib.error as _ule
    headers = {"User-Agent": ua or _sub_user_agent()}
    remembered = _sub_channel_get(url)
    if remembered in ("direct", "proxy"):
        order = [remembered] + [c for c in ("direct", "proxy") if c != remembered]
    else:
        order = ["direct", "proxy"]
    attempts = []
    server_responded = False
    for i6, channel in enumerate(order):
        t_out = min(timeout, 12) if i6 == 0 else min(timeout, 6)
        try:
            req = _urq.Request(url, headers=headers)
            if channel == "proxy":
                proxy = _urq.ProxyHandler({"http": _SUB_PROXY_URL, "https": _SUB_PROXY_URL})
                opener = _urq.build_opener(proxy)
                resp = opener.open(req, timeout=t_out)
            else:
                resp = _urq.urlopen(req, timeout=t_out, context=_SSL_CTX)
            with resp:
                body = resp.read(8 * 1024 * 1024)
                hd = {k.lower(): v for k, v in resp.headers.items()}
            _sub_channel_set(url, channel)
            return body, hd, channel == "proxy"
        except Exception as e:
            attempts.append(channel + ":" + _subscription_request_error(e))
            if isinstance(e, _ule.HTTPError):
                # 直连收到 HTTP 错误 = 源站真实响应；代理通道的 502/503/504
                # 是 mihomo 网关自身的错误，属于网络失败而非服务器拒绝。
                if channel == "direct" or e.code not in (502, 503, 504):
                    server_responded = True
    raise _SubFetchError("; ".join(attempts), server_responded)


def _download_sub(url, timeout=20):
    """面板侧订阅下载器(对齐clash-verge prfitem.rs::from_url语义):
    自带UA、读subscription-userinfo头、cap 8MB、优先走本机mihomo代理, 失败回退直连。"""
    import urllib.request as _urq
    body, hd, used_proxy = _open_sub_with_proxy(url, timeout, _SUB_UA_OVERRIDE or _sub_user_agent())
    # BOM剥离(verge同款处理)
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    return body, hd


def _refresh_all_payloads(initial=True):
    """对齐verge自动更新:启动时及周期性把各订阅载荷重抓落盘。
    - 仅处理file型且meta中有url的订阅
    - 静默容错、错峰下载(间隔3s)、失败保留旧载荷"""
    import os as _os2, time as _t2, threading as _th2
    def _worker():
        # 启动预热前先等HTTP面就绪日志出现(内部仅为排序输出)
        try:
            meta_store = load_sub_meta()
            pps = {k: t for k, t in _yaml_provider_types().items()
                   if t == "file"}                    # 仅现存file型订阅
            names = [k for k in pps
                     if (meta_store.get(k) or {}).get("url", "").startswith("http")]
        except Exception:
            return
        for name in names:
            dest = _sub_file_path(name)
            if initial and _os2.path.exists(dest) and _os2.path.getsize(dest) > 4096:
                continue                      # 已有健康载荷,首启不重复拉
            try:
                body, hd = _download_sub_validated(
                    name, (load_sub_meta().get(name) or {}).get("url"))
                tmp = dest + ".tmp"
                open(tmp, "wb").write(body)
                _os2.replace(tmp, dest)
                log(f"payload refreshed '{name}' ({len(body)}B)")
                ui = None
                for k2, v2 in hd.items():
                    if k2.endswith("subscription-userinfo"):
                        ui = _parse_userinfo({k2: v2}); break
                if ui is not None:
                    ms = load_sub_meta()
                    ent = ms.setdefault(name, {}); ent["userInfo"] = ui
                    save_sub_meta(ms)
            except Exception as e2:
                log(f"WARNING: auto-refresh '{name}': {e2}")
            _t2.sleep(3)
    _th2.Thread(target=_worker, daemon=True).start()


def _yaml_provider_types():
    """返回 {name: 'file'|'http'} 映射,读当前配置"""
    import yaml as _yv
    try:
        d = _yv.safe_load(read_config()) or {}
        return {k: str((v or {}).get("type")) for k, v in (d.get('proxy-providers') or {}).items()}
    except Exception:
        return {}


def _render_providers_text(pps):
    """把 proxy-providers dict 渲染为嵌套在 proxy-providers: 下的 YAML 块(缩进2)"""
    import yaml as _yr
    if not pps:
        return ""
    out = _yr.safe_dump(pps, allow_unicode=True, sort_keys=False,
                        default_flow_style=False, width=4096, indent=2)
    # 每行加 2 空格缩进, 使其成为 proxy-providers 的子键
    indented = "\n".join(("  " + ln) if ln.strip() else ln for ln in out.splitlines())
    return indented


def _yaml_doc_save(doc):
    """落盘：保留 config.yaml 原有注释/排版，仅替换 proxy-providers 段。
    避免 safe_dump 全量重写冲掉用户的手写注释与分段样式。"""
    pps = (doc.get('proxy-providers') or {})
    # 结构校验（沿用原有保险）
    import yaml as _ys
    assert isinstance(pps, dict)
    raw = read_config()
    if not raw.strip():
        _yaml_doc_save_dump(doc)
        return
    providers_text = _render_providers_text(pps)
    new_raw = _patch_proxy_providers_section(raw, providers_text)
    if new_raw is None:
        # 找不到 proxy-providers 段：回退到原有 safe_dump（保底）
        _yaml_doc_save_dump(doc)
        return
    chk = _ys.safe_load(new_raw) or {}
    assert isinstance(chk.get('proxy-providers', {}) or {}, dict)
    write_config(new_raw)


def _yaml_doc_save_dump(doc):
    """原 safe_dump 全量落盘(仅在无文本可patch时回退)。"""
    import yaml as _ys
    out = _ys.safe_dump(doc, allow_unicode=True, sort_keys=False,
                        default_flow_style=False, width=4096, indent=2)
    chk = _ys.safe_load(out) or {}
    assert isinstance(chk.get("proxy-providers", {}) or {}, dict)
    write_config(out)


def _patch_proxy_providers_section(raw, providers_text):
    """在原 config 文本中定位顶层 proxy-providers: 段, 整段替换为其渲染文本。
    其余字节(注释/分段/其它区块)原样保留。找不到返回 None。
    段结束：遇到下一个顶格行(含顶格注释)即停, 以保留其后注释与区块。"""
    import re as _re
    lines = raw.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if _re.match(r'^proxy-providers:\s*$', ln):
            start = i
            break
    if start is None:
        return None
    # 段结束: 第一个顶格非空行(缩进为0), 无论是否注释, 都视为下一段开始
    end = len(lines)
    for j in range(start + 1, len(lines)):
        l = lines[j]
        if l.strip() and l[:1] not in (' ', '\t'):
            end = j
            break
    if providers_text:
        block = "proxy-providers:\n" + providers_text
        if not providers_text.endswith("\n"):
            block += "\n"
    else:
        block = "proxy-providers: {}\n"
    new_lines = lines[:start] + [block] + lines[end:]
    return "".join(new_lines)


def _rename_provider_side(old, new):
    """file架构下重命名订阅：yaml键名/payload文件/sub-meta三处同步"""
    import os as _osR
    import yaml as _yq
    doc = _yq.safe_load(read_config()) or {}
    pps = doc.get('proxy-providers') or {}
    if old in pps and new not in pps:
        pps[new] = pps.pop(old)
        _yaml_doc_save(doc)
    po, pn2 = _sub_file_path(old), _sub_file_path(new)
    if _osR.path.exists(po) and not _osR.path.exists(pn2):
        _osR.replace(po, pn2)
    ms = load_sub_meta()
    if old in ms and new not in ms:
        ms[new] = ms.pop(old)
        save_sub_meta(ms)


def _upsert_file_provider_block(name, health_check=None):
    """把file型块写入doc(存在则仅确保形状正确)，返回变更bool"""
    import yaml as _yu
    doc = _yu.safe_load(read_config()) or {}
    pps = doc.setdefault('proxy-providers', {})
    dest_rel = "providers/" + _sub_file_path(name).split('/')[-1]
    nb = {'type': 'file', 'path': dest_rel}
    hc = health_check or {
        'enable': True,
        'url': 'https://www.gstatic.com/generate_204',
        'interval': 180}
    nb['health-check'] = hc
    prev = pps.get(name)
    if prev != nb:
        pps[name] = nb
        _yaml_doc_save(doc)
        return True
    return False


def edit_provider_exists(name):
    """检查配置中是否存在该订阅块"""
    return name in (_yaml_provider_types() or {})


def _cleanup_provider_assets(name):
    """删除订阅后同步清理payload文件与sub-meta条目"""
    import os as _osC
    try:
        f = _sub_file_path(name)
        if _osC.path.exists(f):
            _osC.remove(f)
        ms = load_sub_meta()
        if name in ms:
            ms.pop(name)
            save_sub_meta(ms)
    except Exception as e:
        log(f"WARNING: cleanup provider assets '{name}': {e}")


def _migrate_to_file_providers():
    """把 http 型 proxy-provider 转为本地文件型(对齐verge架构):
    1) 首次转换时下载一份载荷落地 2) 改写块为 type:file
    3) 原URL持久化进 sub_meta 以便后续更新幂等安全"""
    import os as _os
    try:
        import yaml as _yaml
        cfg_text = read_config()
        doc = _yaml.safe_load(cfg_text) or {}
        pps = doc.get("proxy-providers")
        if not isinstance(pps, dict):
            return
        changed = False
        for name, pv in list(pps.items()):
            if not isinstance(pv, dict) or str(pv.get("type")) != "http":
                continue
            orig_url = pv.get("url", "")
            dest = _sub_file_path(name)
            need_seed = not (_os.path.exists(dest) and _os.path.getsize(dest) > 64)
            if need_seed and orig_url.startswith("http"):
                try:
                    body, _hd = _download_sub(orig_url)
                    open(dest, "wb").write(body)
                    log(f"provider '{name}' payload seeded ({len(body)}B)")
                except Exception as e:
                    log(f"WARNING: seed '{name}' failed: {e}; empty stub written")
                    if not _os.path.exists(dest):
                        open(dest, "w").write("proxies: []\n")
            elif not _os.path.exists(dest):
                open(dest, "w").write("proxies: []\n")
            nb = {"type": "file",
                  "path": f"providers/{_os.path.basename(dest)}"}
            if isinstance(pv.get("health-check"), dict):
                nb["health-check"] = pv["health-check"]
            pps[name] = nb
            changed = True
            # 持久化原始URL供后续更新(缺才补)
            meta_store = load_sub_meta()
            ent = meta_store.setdefault(name, {})
            if not ent.get("url"):
                ent["url"] = orig_url
                save_sub_meta(meta_store)
        if changed:
            # 用保留样式的落盘(仅改写 proxy-providers 段, 注释/分段/其它区块原样保留)
            _yaml_doc_save(doc)
            log(f"proxy-providers converted to local-file mode ({len(pps)} entries)")
    except Exception:
        import traceback as _tb2
        try:
            open("/tmp/panel_migration_err.txt", "w").write(_tb2.format_exc())
        except Exception:
            pass
        log("WARNING: migrate to file providers failed")


def _subscription_cache_get(url):
    now = time.time()
    with _SUB_VALIDATION_CACHE_LOCK:
        item = _SUB_VALIDATION_CACHE.get(url)
        if not item:
            return None
        if now - item.get("time", 0) > _SUB_VALIDATION_CACHE_TTL:
            _SUB_VALIDATION_CACHE.pop(url, None)
            return None
        return item


def _subscription_cache_put(url, result, body, headers):
    with _SUB_VALIDATION_CACHE_LOCK:
        _SUB_VALIDATION_CACHE[url] = {
            "time": time.time(), "result": result,
            "body": body, "headers": headers,
        }


def _subscription_cache_take(url):
    """保存成功前原子取走缓存，避免同一探测结果被重复消费。"""
    with _SUB_VALIDATION_CACHE_LOCK:
        item = _SUB_VALIDATION_CACHE.pop(url, None)
    if item and time.time() - item.get("time", 0) <= _SUB_VALIDATION_CACHE_TTL:
        return item
    return None


def validate_subscription(url, timeout=6, use_cache=True):
    """保存前验证订阅；成功结果与过滤后的载荷缓存 120 秒供保存直接复用。"""
    import urllib.request as _ureq
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "订阅链接必须以 http:// 或 https:// 开头"}
    if use_cache:
        cached = _subscription_cache_get(url)
        if cached:
            return dict(cached["result"])
    try:
        # 下载器内部已处理通道记忆、直连/代理回退，并保留服务端详细错误。
        data, hd, _up = _open_sub_with_proxy(url, timeout, _sub_user_agent())
    except _SubFetchError as e:
        if e.server_responded:
            # 服务器有明确响应（如 403 token is error）：链接被服务端拒绝，
            # 换代理或强制保存都无意义，引导用户重新复制有效订阅链接。
            return {"ok": False, "can_force": False,
                    "error": f"订阅服务器拒绝了请求（{e}）。通常是链接已失效，"
                             "或复制的是分享短链而不是 Clash 订阅链接；请到机场后台重新复制订阅链接"}
        return {"ok": False, "can_force": True,
                "error": f"无法访问订阅链接（{e}）；若该机场需经代理访问，首次添加可选择「仍要保存」"}
    except Exception as e:
        reason = str(e) or e.__class__.__name__
        hint = "；若该机场需经代理访问，首次添加可选择「仍要保存」"
        return {"ok": False, "error": f"无法访问订阅链接（{reason}）{hint}"}
    try:
        text = data.decode("utf-8", "ignore")
    except Exception:
        text = ""
    ok, nodes = _looks_like_subscription(text)
    if not ok:
        return {"ok": False,
                "error": "内容不是有效的订阅格式（需要 Clash YAML、base64 或节点链接列表）；若确认无误可选择「仍要保存」"}
    filtered_data, removed_info_nodes = _filter_subscription_info_nodes(data)
    nodes = max(0, nodes - removed_info_nodes)
    meta = _sub_meta_from_headers(hd)
    if not meta.get("name"):
        nm = _sub_name_from_yaml(text)
        if nm:
            meta["name"] = nm
    meta["userInfo"] = _parse_userinfo(hd)

    # 请求使用 Clash Verge Rev 默认 UA。一次成功响应同时用于名称、元数据、验证和载荷；
    # 不再为了名称切换 UA 重复下载整份订阅，避免添加过程成倍变慢。
    # 对齐 Clash Verge Rev：响应头/YAML 没有名称时，使用 URL 最后一段作为 profile name。
    # 这样正文没有 profile.name、且服务端没有 Content-Disposition 时仍会有稳定名称。
    if not meta.get("name"):
        meta["name"] = fallback_provider_name(url)
    result = {"ok": True, "meta": meta, "nodes": nodes}
    _subscription_cache_put(url, result, filtered_data, hd)
    return dict(result)


def probe_subscription_meta(url, timeout=7):
    """使用与保存订阅一致的 UA、直连/代理兜底探测名称和元数据。"""
    meta = {}
    try:
        body, hd, _via_proxy = _open_sub_with_proxy(url, timeout, _sub_user_agent())
        meta = _sub_meta_from_headers(hd)
        if not meta.get("name"):
            nm = _sub_name_from_yaml(body.decode("utf-8", "ignore"))
            if nm:
                meta["name"] = nm
        meta["userInfo"] = _parse_userinfo(hd)
    except Exception as e:
        log(f"probe subscription failed (ignored): {url}: {e}")
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
    """按 Clash Verge Rev 的 from_url 规则兜底：URL 最后一段优先，最后才用 Remote File。"""
    try:
        from urllib.parse import urlparse, unquote as _unquote
        parsed = urlparse(url)
        path = (parsed.path or "").strip("/")
        last = _unquote(path.rsplit("/", 1)[-1]) if path else ""
        # 上游直接使用 URL 最后一段，即使它是 token/hash；仅过滤明显的泛用路径名。
        generic = {"sub", "clash", "link", "api", "subscribe", "get", "download", "upload", "feed"}
        if last and last.lower() not in generic:
            return _sanitize_provider_name(last)
    except Exception:
        pass
    return "Remote File"

def edit_provider_add(name, url, interval=3600, ptype="http", pre_meta=None):
    """Insert/replace a proxy-provider entry in config.yaml text. No restart.
    pre_meta: 验证阶段已取得的元数据（保存路径全程只拉一次订阅）。
      - 非 None（常规保存）：不再发起任何网络请求，写盘立即返回。
      - None（强制保存，跳过验证）：名称留空时内联探测；已命名则后台线程补探。
    名称缺省时自动探测订阅真实名称（对齐 clash-verge-rev），探测失败回退域名。"""
    def _persist_meta(m, key):
        try:
            ui = m.get("userInfo")
            if isinstance(ui, dict) and (ui.get("total") or ui.get("upload") or ui.get("download")):
                all_meta = load_sub_meta()
                entry = all_meta.get(key, {}) if isinstance(all_meta, dict) else {}
                entry["userInfo"] = {k: int(ui.get(k, 0) or 0)
                                     for k in ("upload", "download", "total", "expire")}
                all_meta[key] = entry
                save_sub_meta(all_meta)
            if m.get("home") or m.get("updateInterval"):
                all_meta = load_sub_meta()
                entry = all_meta.get(key, {}) if isinstance(all_meta, dict) else {}
                if m.get("home"):
                    entry["home"] = m["home"]
                if m.get("updateInterval"):
                    entry["updateInterval"] = m["updateInterval"]
                if entry:
                    all_meta[key] = entry
                    save_sub_meta(all_meta)
        except Exception:
            pass

    def _bg_probe_persist(key, url):
        """强制保存后的补充探测：合并官网/更新间隔到 sub_meta，并回写 interval。"""
        try:
            m = probe_subscription_meta(url, timeout=6)
            if not m:
                return
            _persist_meta(m, key)
            ui = m.get("updateInterval")
            if isinstance(ui, int) and ui > 0:
                content2 = read_config()
                pat = (r"(?m)^(  " + re.escape(key) +
                       r":\n(?:    [^\n]*\n)*?    interval:\s*)\d+")
                old_line = re.search(pat, content2)
                if old_line:
                    write_config(content2[:old_line.start()] +
                                 old_line.group(1) + str(ui) +
                                 content2[old_line.end():])
        except Exception:
            pass

    named_by_user = bool(name and name.strip())
    meta = dict(pre_meta) if pre_meta else {}
    if not named_by_user and not meta.get("name"):
        # 常规保存时验证已带回 name；走到这里说明是强制保存或无验证调用
        meta.update(probe_subscription_meta(url, timeout=4))
    name = (meta.get("name") or name or "").strip() or fallback_provider_name(url)
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
    # 订阅响应头下发的更新间隔优先（仅在未显式传有效间隔时）
    if meta.get("updateInterval") and meta["updateInterval"] > 0 and interval == 3600:
        interval = int(meta["updateInterval"])
    # 持久化订阅元数据（官网 home / 更新间隔），供订阅卡片显示「官网」跳转
    if meta.get("home") or meta.get("updateInterval"):
        _persist_meta(meta, key)

    block = _provider_block(key, url, interval, ptype)
    content = read_config()
    # 1) proxy-providers: {}  → 展开为块
    m = re.search(r'^proxy-providers:\s*\{\}\s*$', content, re.M)
    if m:
        content = content[:m.start()] + "proxy-providers:\n" + block + content[m.end():]
        write_config(content)
        if pre_meta is None and named_by_user:
            threading.Thread(target=_bg_probe_persist, args=(key, url), daemon=True).start()
        return {"success": True, "message": f"已添加订阅「{key}」"}
    # 2) 已有 proxy-providers: 块 → 替换同名或追加
    m = re.search(r'^proxy-providers:\s*$', content, re.M)
    if m:
        prefix = content[:m.start()]
        suffix = content[m.start():]
        head, rest, found = _strip_provider_block(suffix, key)
        if found:
            write_config(prefix + "proxy-providers:\n" + head + block + rest)
            if pre_meta is None and named_by_user:
                threading.Thread(target=_bg_probe_persist, args=(key, url), daemon=True).start()
            return {"success": True, "message": f"已更新订阅「{key}」"}
        head2, rest2, _ = _strip_provider_block(suffix, "\x00never")
        write_config(prefix + "proxy-providers:\n" + head2 + block + rest2)
        if pre_meta is None and named_by_user:
            threading.Thread(target=_bg_probe_persist, args=(key, url), daemon=True).start()
        return {"success": True, "message": f"已添加订阅「{key}」"}
    # 3) 完全不存在 → 追加到文件末尾
    content = content.rstrip("\n") + "\n\nproxy-providers:\n" + block
    write_config(content)
    if pre_meta is None and named_by_user:
        threading.Thread(target=_bg_probe_persist, args=(key, url), daemon=True).start()
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

    def _api_update_provider(self):
        """对齐 clash-verge:面板下载→落盘→通知内核重载(file型)"""
        qs = parse_qs(urlparse(self.path).query)
        name = (qs.get("name") or [""])[0]
        if not name:
            return self._send_json({"success": False, "error": "缺少订阅名称"}, 400)
        meta_store = load_sub_meta()
        raw_url = (meta_store.get(name) or {}).get("url", "")
        import base64 as _b64u
        url = raw_url
        if "/subfetch?u=" in raw_url:
            p4 = raw_url.split("u=", 1)[1]
            url = _b64u.urlsafe_b64decode(p4 + "=" * (-len(p4) % 4)).decode()
            meta_store.setdefault(name, {})["url"] = url
            save_sub_meta(meta_store)
        if not url.startswith("http"):
            return self._send_json({"success": False,
                "error": f"未找到 '{name}' 的源URL, 请删除后重新添加"}, 400)
        try:
            body, hd = _download_sub_validated(name, url)
            open(_sub_file_path(name), "wb").write(body)
        except Exception as e:
            return self._send_json({"success": False,
                "error": f"下载失败: {e}"}, 502)
        ui = None
        for k5, v5 in hd.items():
            if k5.endswith("subscription-userinfo"):
                ui = _parse_userinfo({k5: v5}); break
        if ui is not None:
            ent = meta_store.setdefault(name, {}); ent["userInfo"] = ui
            save_sub_meta(meta_store)
        try:
            import urllib.request as _uq3
            rq3 = _uq3.Request(
                f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/providers/proxies/{quote(name)}",
                method="PUT")
            urllib.request.urlopen(rq3, timeout=20).read(64)
            note = "已同步内核"
        except Exception as e6:
            note = "已落盘, 内核重载待启动生效" + (f"; {e6}" if str(e6) else "")
        return self._send_json({"success": True, "name": name,
                                "size": len(body), "note": note})

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
            url = f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/{sub_path}"
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
                # 401/403 且本应用引擎不在运行：9090 上监听的是别的应用的
                # Mihomo（fnSoar 的 config.yaml 从不写 secret，自己的引擎不会
                # 要求鉴权）。原样透传只会让前端整页空白，改成明确错误。
                if e.code in (401, 403) and not _find_engine_pids():
                    self._send_json({
                        "error": "引擎控制器鉴权失败",
                        "detail": (f"{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]} 上监听的 "
                                   "Mihomo 不属于 fnSoar（端口可能被其他应用占用，"
                                   "如 clash-for-fnos）；fnSoar 引擎当前未运行。"),
                    }, 503)
                    return
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
                (_ctrl_endpoint()[0], _ctrl_endpoint()[1]), timeout=10)
            req = (f"GET /{sub} HTTP/1.1\r\n"
                   f"Host: {_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}\r\n"
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
        if path == "/api/aggregate":
            st = _aggregate_state()
            return self._send_json({"success": True, "aggregate": st["aggregate"],
                                    "active_sub": st["active_sub"]})
        if path.startswith("/api/sub-file"):
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name", [""])[0] or "").strip()
            fp = _sub_file_path(name)
            if not name or not os.path.exists(fp):
                return self._send_json({"success": False, "error": "订阅文件不存在"}, 404)
            return self._send_json({"success": True, "name": name, "path": fp,
                                    "content": open(fp, "r", encoding="utf-8").read()})
        if path == "/api/config":
            return self._send_json({"config": read_config()})
        if path == "/api/profile-ext":
            ext = _read_profile_ext()
            return self._send_json({"ok": True, "merge": ext["merge_config"],
                                    "script": ext["script"],
                                    "defaults": {"merge": DEFAULT_MERGE_CONFIG,
                                                 "script": DEFAULT_GLOBAL_SCRIPT}})
        if path == "/api/proxy-providers":
            yaml_text = read_config()
            providers = extract_proxy_providers(yaml_text)
            # 合并订阅元数据（官网 home 等响应头信息）
            meta = load_sub_meta()
            for name, pv in providers.items():
                if not isinstance(pv, dict):
                    continue
                m = meta.get(name) or {}
                if m.get("home"):
                    pv["home"] = m["home"]
                if str(pv.get("type")) == "file":
                    # file架构下原始URL存于sub-meta，此处回填给前端编辑框使用
                    if m.get("url"):
                        pv["url"] = m["url"]
                    iv = m.get("interval")
                    try:
                        pv["interval"] = int(iv) if iv else 3600
                    except Exception:
                        pv["interval"] = 3600
            return self._send_json({"providers": providers})
        if path == "/api/providers-live":
            # 订阅页只需要节点数/更新时间/类型，不要把完整 8MB 节点列表发给浏览器。
            try:
                import urllib.request as _ureq
                req = _ureq.Request(
                    f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/providers/proxies",
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
                        # 引擎本次未带回流量信息时，用保存时验证所得的 userinfo 兜底，
                    # 避免「刚重启/机场偶发不下发」导致卡片误判为自制订阅(∞)
                    if "subInfo" not in entry:
                        stored = ((load_sub_meta().get(name) or {}).get("userInfo"))
                        if isinstance(stored, dict) and stored:
                            entry["subInfo"] = {
                                "upload": int(stored.get("upload", 0) or 0),
                                "download": int(stored.get("download", 0) or 0),
                                "total": int(stored.get("total", 0) or 0),
                                "expire": int(stored.get("expire", 0) or 0),
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
            return self._api_update_provider()

        if path == "/api/logs":
            lines = 200
            try:
                content = _tail_file(LOG_FILE, lines)
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
        if path == "/api/app/icons":
            return self._send_json({"ok": True, "options": _app_icon_options(),
                                    "selected": _app_icon_selected()})
        if path.startswith("/api/app/icons/"):
            # 图标预览图（PNG 流）
            icon_id = path[len("/api/app/icons/"):].split("?")[0]
            if icon_id.endswith(".png"):
                icon_id = icon_id[:-4]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", icon_id or ""):
                return self._send_json({"success": False, "error": "非法图标 ID"}, 400)
            f = os.path.join(ICON_STORE_DIR, icon_id + ".png")
            if not os.path.exists(f):
                return self._send_json({"success": False, "error": "图标不存在"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            with open(f, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            return
        if path == "/api/unlock/items":
            # 解锁测试：默认测试项列表（Pending）
            from media_unlock import default_unlock_items
            return self._send_json(default_unlock_items())
        if path == "/api/tun":
            # 真实 TUN 状态：读 config.yaml 的 tun.enable（mihomo GET /configs 返回
            # 的是静态快照，动态 PUT 不生效，故以配置文件为准）；
            # settings 附带 tun: 段全部参数，供设置弹窗使用。
            try:
                settings = _read_tun_settings()
                return self._send_json({"enable": bool(settings.get("enable", False)),
                                        "settings": settings})
            except Exception:
                return self._send_json({"enable": False, "settings": dict(_TUN_DEFAULTS)})
        if path == "/api/system-proxy":
            # 系统代理（代理环境变量托管）状态
            try:
                return self._send_json(_proxy_status_data())
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/mode":
            # 真实代理模式：优先读引擎实时 mode（热切换后立即生效），
            # 引擎不可用时回退到 config.yaml 的 mode。
            try:
                import urllib.request as _ureq
                url = f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/configs"
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
        if path == "/api/core-status":
            # 内核状态：运行状态、版本、控制器端口占用（引擎未跑但端口被占=冲突）
            try:
                running, pid = is_running()
                core_ver = "unknown"
                try:
                    import urllib.request as _ureq
                    url = f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/version"
                    with _ureq.urlopen(_ureq.Request(url, headers={"Accept": "application/json"}), timeout=2) as resp:
                        core_ver = json.loads(resp.read()).get("version", "unknown")
                except Exception:
                    pass
                _ch, _cp = _ctrl_endpoint()
                port_conflict = (not running) and (not _port_free(_cp))
                _cst = _read_core_settings()
                _secret_set = False
                try:
                    _cfg = yaml.safe_load(read_config()) or {}
                    _secret_set = bool(str(_cfg.get("secret", "")).strip())
                except Exception:
                    pass
                return self._send_json({
                    "success": True,
                    "running": running,
                    "pid": pid,
                    "core_version": core_ver,
                    "app_version": _get_app_version(),
                    "controller": f"{_ch}:{_cp}",
                    "controller_port": _cp,
                    "port_conflict": port_conflict,
                    "secret_set": _secret_set,
                    "binary_path": os.path.join(TRIM_APPDEST, "bin", "mihomo"),
                    "config_path": CONFIG_FILE,
                    "healthcheck": {"url": _cst.get("healthcheck_url"),
                                    "timeout": _cst.get("healthcheck_timeout")},
                })
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/core-settings":
            try:
                st = _read_core_settings()
                ch, cp = _ctrl_endpoint()
                data = dict(st)
                data["controller"] = f"{ch}:{cp}"
                return self._send_json({"success": True, "settings": data})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/geo-status":
            # 数据目录 GEO 文件清单（大小/修改时间/是否存在）
            try:
                return self._send_json({"success": True, "files": _geo_files_payload(),
                                        "data_dir": TRIM_PKGVAR})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/version":
            try:
                import urllib.request as _ureq
                core_ver = "unknown"
                try:
                    url = f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/version"
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
                out = {"success": True, "latest": latest, "current": cur,
                       "url": d.get("html_url", ""), "has_update": has}
                _gh_cache_put("app-update", dict(out))
                return self._send_json(out)
            except Exception as e:
                cached = _gh_cache_get("app-update", 7 * 24 * 3600)
                if cached:
                    return self._send_json(dict(cached, stale=True))
                msg = str(e)
                if "404" in msg:
                    msg = "GitHub 仓库 huiikeung/fnsoar 不存在或为私有仓库，无法公开检测更新。请将仓库设为公开，或配置 MIHOMO_GITHUB_TOKEN 后重试。"
                return self._send_json({"success": False, "error": msg}, 500)
        if path == "/api/changelog":
            try:
                now = time.time()
                if _CHANGELOG_CACHE["t"] and now - _CHANGELOG_CACHE["t"] < 600:
                    return self._send_json(_CHANGELOG_CACHE["d"])
                import urllib.request as _cr
                gh_api = os.environ.get("MIHOMO_GITHUB_API", "https://api.github.com").rstrip("/")
                token = os.environ.get("MIHOMO_GITHUB_TOKEN", "")
                url = f"{gh_api}/repos/huiikeung/fnsoar/releases?per_page=8"
                headers = {"User-Agent": "ClashMini/1.0", "Accept": "application/vnd.github+json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                try:
                    arr = _github_fetch_json(url)
                except Exception:
                    # GitHub 不可达（仓库未公开/限流/网络受限）：回退本地 CHANGELOG.md
                    arr = None
                if arr is None:
                    md_path = os.path.join(TRIM_APPDEST, "admin", "CHANGELOG.md")
                    if os.path.exists(md_path):
                        try:
                            with open(md_path, "r", encoding="utf-8") as f:
                                md_text = f.read()
                            local = _parse_changelog_md(md_text)
                            if local:
                                out = {"ok": True, "releases": local, "local": True}
                                _CHANGELOG_CACHE.update({"t": now, "d": out})
                                return self._send_json(out)
                        except Exception:
                            pass
                    raise RuntimeError("GitHub 连接失败，且未找到本地更新日志")
                releases = []
                for it in (arr if isinstance(arr, list) else []):
                    releases.append({
                        "version": (it.get("tag_name") or "").lstrip("v"),
                        "date": (it.get("published_at") or "")[:10],
                        "body": it.get("body") or "",
                        "url": it.get("html_url") or "",
                    })
                out = {"ok": True, "releases": releases}
                _CHANGELOG_CACHE.update({"t": now, "d": out})
                return self._send_json(out)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/core-latest":
            try:
                now_ct = time.time()
                if _CORE_LATEST_CACHE["t"] and now_ct - _CORE_LATEST_CACHE["t"] < 6 * 3600:
                    return self._send_json(_CORE_LATEST_CACHE["d"])
                _persisted = _gh_cache_get("core-latest", 7 * 24 * 3600)
                d = None
                try:
                    d = _github_fetch_json("https://api.github.com/repos/MetaCubeX/mihomo/releases/latest")
                except Exception:
                    # API 限流/不可达：atom 源兜底（不受 REST 匿名限流约束）
                    try:
                        ver = _github_latest_via_atom("MetaCubeX/mihomo")   # 形如 v1.19.31
                        asset = f"mihomo-linux-amd64-compatible-{ver}.gz"
                        d = {"tag_name": ver, "assets": [
                            {"name": asset,
                             "browser_download_url": f"https://github.com/MetaCubeX/mihomo/releases/download/{ver}/{asset}"}]}
                    except Exception:
                        # 都失败：依次降级到内存缓存 → 持久化缓存（重启前的成功结果）
                        if _CORE_LATEST_CACHE["t"]:
                            return self._send_json(dict(_CORE_LATEST_CACHE["d"], stale=True))
                        if _persisted:
                            return self._send_json(dict(_persisted, stale=True))
                        raise
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
                out = {"success": True, "version": ver, "url": dl}
                _CORE_LATEST_CACHE.update({"t": now_ct, "d": dict(out)})
                _gh_cache_put("core-latest", dict(out))
                return self._send_json(out)
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
                        ent = {"name": name, "current": current or "", "latest": latest,
                               "url": url, "has_update": has}
                        _gh_cache_put("dashboard-" + name, dict(ent))
                        result.append(ent)
                    except Exception as e:
                        # 降级：持久化缓存里的上次成功结果（附 stale 标记）
                        cached = _gh_cache_get("dashboard-" + name, 30 * 24 * 3600)
                        if cached:
                            result.append(dict(cached, stale=True))
                        else:
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
            return self._send_file(f"{ADMIN_UI_DIR}/index.html", "text/html")
        if path == "/ui" or path == "/ui/" or path.startswith("/ui/"):
            # Web UI: redirect to admin panel root
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path.endswith(".html"):
            return self._send_file(f"{ADMIN_UI_DIR}{path}", "text/html")
        if path.endswith(".css"):
            return self._send_file(f"{ADMIN_UI_DIR}{path}", "text/css")
        if path.endswith(".js"):
            return self._send_file(f"{ADMIN_UI_DIR}{path}", "application/javascript")
        if path.endswith(".png"):
            return self._send_file(f"{ADMIN_UI_DIR}{path}", "image/png")
        if path.endswith(".svg"):
            return self._send_file(f"{ADMIN_UI_DIR}{path}", "image/svg+xml")
        self.send_error(404)

    def do_POST(self):
        """POST 端点：服务开关 / TUN / GEO 更新 / 内核更新 / 订阅管理 / 模式 / 配置保存等。"""
        path = self._strip_gateway_prefix(urlparse(self.path).path)
        body = self._read_body()
        if path == "/api/unlock/check":
            try:
                from media_unlock import check_media_unlock
                items = check_media_unlock()
                return self._send_json({"success": True, "items": items})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
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
                result = start_service() if enable else stop_service()
                if not result:
                    return self._send_json({"success": False, "error": "操作失败"}, 500)
                if isinstance(result, dict) and not result.get("success", True):
                    return self._send_json({"success": False, "error": result.get("error", "操作失败")}, 500)
                if isinstance(result, dict) and result.get("skipped"):
                    # 操作被跳过（如 skipped）：透传原因，不改变持久化的开关状态
                    return self._send_json({"success": True, "skipped": True,
                                            "message": result.get("message", "操作被跳过")})
                # persist the user's choice; restored next time the panel daemon starts
                _write_service_state(enable)
                msg = result.get("message") if isinstance(result, dict) else None
                return self._send_json({"success": True,
                                        "message": msg or ("服务已" + ("启动" if enable else "停止"))})
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
        if path == "/api/tun-settings":
            # 保存 TUN（虚拟网卡）参数：stack/device/mtu/自动路由/自动重定向/
            # 出口网卡检测/DNS 劫持/严格路由/排除网段。TUN 已开启且服务运行中时
            # 立即重启引擎生效；否则仅保存，下次启动时生效。
            try:
                data = json.loads(body) if body else {}
                patch = data.get("settings") or {}
            except Exception:
                return self._send_json({"success": False, "error": "参数错误"}, 400)
            try:
                ok, msg = set_tun_settings_in_config(patch)
                if not ok:
                    return self._send_json({"success": False, "error": msg}, 400)
                settings = _read_tun_settings()
                running, _ = is_running()
                restarted = False
                if running and bool(settings.get("enable")):
                    stopped = stop_service()
                    if not stopped.get("success"):
                        return self._send_json({"success": False,
                                                "error": "TUN 设置已保存，但停止旧服务失败: " + str(stopped.get("error", ""))}, 500)
                    started = start_service()
                    if not started.get("success"):
                        return self._send_json({"success": False,
                                                "error": "TUN 设置已保存，但重启服务失败: " + str(started.get("error", ""))}, 500)
                    restarted = True
                return self._send_json({
                    "success": True,
                    "settings": settings,
                    "restarted": restarted,
                    "message": "TUN 设置已保存" + ("，服务已重启" if restarted else "，开启 TUN 后生效")})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/app/icon":
            try:
                data = json.loads(body) if body else {}
            except Exception:
                return self._send_json({"success": False, "error": "参数错误"}, 400)
            icon_id = str(data.get("iconId") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", icon_id):
                return self._send_json({"success": False, "error": "非法图标 ID"}, 400)
            if icon_id != "default" and not os.path.exists(os.path.join(ICON_STORE_DIR, icon_id + ".png")):
                return self._send_json({"success": False, "error": "图标不存在"}, 400)
            try:
                _apply_app_icon(icon_id)
                return self._send_json({"success": True, "selected": icon_id,
                                        "message": "软件图标已切换；刷新 fnOS 桌面并重新打开窗口后生效"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/system-proxy":
            # 系统代理（代理环境变量托管）：开启/关闭与设置
            try:
                data = json.loads(body) if body else {}
            except Exception:
                return self._send_json({"success": False, "error": "参数错误"}, 400)
            try:
                status, err = update_proxy_settings(data)
                if err:
                    return self._send_json({"success": False, "error": err}, 400)
                status["success"] = True
                status["message"] = "系统代理已" + ("开启" if status.get("enabled") else "关闭")
                return self._send_json(status)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/core-settings":
            try:
                data = json.loads(body) if body else {}
            except Exception:
                return self._send_json({"success": False, "error": "参数错误"}, 400)
            try:
                st = _read_core_settings()
                if "healthcheck_url" in data:
                    u = str(data["healthcheck_url"] or "").strip()
                    if not u.startswith(("http://", "https://")) or len(u) > 500:
                        return self._send_json({"success": False, "error": "延迟测试 URL 无效"}, 400)
                    if any(c in u for c in "\r\n\x00 \"'"):
                        return self._send_json({"success": False, "error": "延迟测试 URL 含非法字符"}, 400)
                    st["healthcheck_url"] = u
                if "healthcheck_timeout" in data:
                    try:
                        st["healthcheck_timeout"] = max(1000, min(30000, int(data["healthcheck_timeout"])))
                    except Exception:
                        return self._send_json({"success": False, "error": "超时必须是毫秒数"}, 400)
                _write_core_settings(st)
                ch, cp = _ctrl_endpoint()
                data2 = dict(st)
                data2["controller"] = f"{ch}:{cp}"
                return self._send_json({"success": True, "settings": data2,
                                        "message": "内核设置已保存"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/geo-download":
            # 按 config.yaml geox-url 直接下载 GEO 文件。
            # mihomo update geodata 在引擎运行时会因入站端口占用而中止、不执行下载，
            # 因此由面板自行完成下载；支持单文件（key）或全部（不传 key）。
            try:
                data = json.loads(body) if body else {}
            except Exception:
                return self._send_json({"success": False, "error": "参数错误"}, 400)
            try:
                only = data.get("key")
                urls = _geox_urls()
                jobs = [(k, f) for k, _l, f in GEO_FILE_MAP if not only or k == only]
                if only and not jobs:
                    return self._send_json({"success": False, "error": "未知的 GEO 类型"}, 400)
                downloaded, failed = [], []
                for key, fname in jobs:
                    url = urls.get(key, "")
                    if not url:
                        failed.append({"key": key, "error": "未配置下载地址"})
                        continue
                    try:
                        _download_geo_file(key, url)
                        downloaded.append(key)
                    except Exception as e:
                        failed.append({"key": key, "error": str(e)})
                msg = ("已更新: " + ", ".join(downloaded)) if downloaded else "下载失败"
                if downloaded and failed:
                    msg += "（部分失败: " + ", ".join(x["key"] for x in failed) + "）"
                return self._send_json({
                    "success": bool(downloaded) and not failed,
                    "partial": bool(downloaded) and bool(failed),
                    "downloaded": downloaded, "failed": failed,
                    "message": msg,
                    "files": _geo_files_payload(),
                })
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
                cmd = f"cd /vol1/@appcenter/fnnas.fnsoar && {cli} stop fnnas.fnsoar && sleep 2 && {cli} start fnnas.fnsoar"
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
                force = bool(data.get("force"))
                manual_name = bool(data.get("manual_name"))
                # 保存前验证订阅（可达 + 内容格式）；强制保存跳过该步。
                # 验证结果复用为元数据来源，整条保存路径只拉一次订阅。
                pre_meta = None
                cached_download = None
                node_hint = ""
                if not force:
                    v = validate_subscription(url, timeout=7)
                    if not v.get("ok"):
                        return self._send_json({"success": False,
                                                "error": (v.get("error") or "订阅验证未通过"),
                                                "can_force": bool(v.get("can_force", True))}, 400)
                    pre_meta = v.get("meta") or {}
                    cached_download = _subscription_cache_take(url)
                    n = v.get("nodes") or 0
                    node_hint = f"，发现 {n} 个节点" if n else ""
                cur_types = _yaml_provider_types()
                final_name = (name or "").strip()
                old_is_file = bool(old_name) and cur_types.get(old_name) == "file"
                tgt_is_file = old_is_file or cur_types.get(final_name) == "file"
                if tgt_is_file:
                    # ── file架构下的编辑(重命名/换链/改间隔) ──
                    if old_is_file and old_name != final_name:
                        _rename_provider_side(old_name, final_name)
                    elif not edit_provider_exists(final_name):
                        return self._send_json({"success": False,
                            "error": f"订阅「{final_name}」不存在"}, 400)
                    ms2 = load_sub_meta()
                    ent = ms2.setdefault(final_name, {})
                    url_changed = bool(url) and url.strip() != ent.get("url", "")
                    ent["url"] = url.strip() or ent.get("url", "")
                    ent["interval"] = int(interval) if interval else 3600
                    if home:
                        ent["home"] = home
                    save_sub_meta(ms2)
                    dest = _sub_file_path(final_name)
                    import os as _osE
                    stale = (not _osE.path.exists(dest)) or _osE.path.getsize(dest) < 4096
                    if url_changed or stale:
                        try:
                            if cached_download:
                                body = cached_download["body"]
                                hd7 = cached_download["headers"]
                            else:
                                body, hd7 = _download_sub_validated(final_name, ent["url"])
                            tmpf = dest + ".tmp"
                            open(tmpf, "wb").write(body)
                            os.replace(tmpf, dest)
                        except Exception as e8:
                            return self._send_json({"success": False,
                                "error": f"载荷刷新失败: {e8}"}, 502)
                        for k9, v9 in (hd7 or {}).items():
                            if k9.endswith("subscription-userinfo"):
                                ui9 = _parse_userinfo({k9: v9})
                                if ui9 is not None:
                                    ent["userInfo"] = ui9
                                    save_sub_meta(ms2)
                                break
                    _upsert_file_provider_block(final_name)
                    try:
                        import urllib.request as _uqF
                        rqF = _uqF.Request(
                            f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/providers/proxies/{quote(final_name)}",
                            method="PUT")
                        urllib.request.urlopen(rqF, timeout=15).read(16)
                    except Exception as e10:
                        log(f"reload after edit '{final_name}': {e10}")
                    return self._send_json({"success": True,
                        "message": f"订阅已更新{node_hint}"})
                # 统一走file架构新增:载荷落地+块形态file
                # 新增订阅优先采用服务端 Content-Disposition/profile-title 名称。
                # 前端可能填入域名/URL兜底值，不能覆盖已经识别出的真实订阅名称；
                # 编辑已有订阅的重命名在上面的 file 分支处理，不受这里影响。
                detected_name = ((pre_meta or {}).get("name") or "").strip()
                eff_name = name if (manual_name and name) else (detected_name or name)
                if not eff_name:
                    # 与 Clash Verge Rev 一致：没有服务端名称时使用 URL 最后一段。
                    eff_name = fallback_provider_name(url)
                body_c, hd_c = None, None
                try:
                    if cached_download:
                        body_c = cached_download["body"]
                        hd_c = cached_download["headers"]
                    else:
                        body_c, hd_c = _download_sub_validated(eff_name, url)
                    open(_sub_file_path(eff_name), "wb").write(body_c)
                except Exception as e11:
                    return self._send_json({"success": False,
                        "error": f"订阅下载失败: {e11}", "can_force": False}, 400)
                pre_meta = pre_meta or {}
                _upsert_file_provider_block(eff_name)
                all_meta3 = load_sub_meta()
                e12 = all_meta3.setdefault(eff_name, {})
                e12.update({"url": url, "interval": int(interval) if interval else 3600})
                if (pre_meta.get("home")) or home:
                    e12["home"] = home or (pre_meta.get("home") or "")
                if pre_meta.get("userInfo"):
                    e12["userInfo"] = {k13: int(pre_meta["userInfo"].get(k13, 0) or 0)
                                       for k13 in ("upload","download","total","expire")}
                save_sub_meta(all_meta3)
                try:
                    import urllib.request as _uqG
                    rqG = _uqG.Request(
                        f"http://{_ctrl_endpoint()[0]}:{_ctrl_endpoint()[1]}/providers/proxies/{quote(eff_name)}",
                        method="PUT")
                    urllib.request.urlopen(rqG, timeout=15).read(16)
                except Exception as e14:
                    log(f"reload after create '{eff_name}': {e14}")
                result = {"success": True, "message": f"订阅已保存",
                          "name": eff_name}
                if node_hint:
                    result["message"] += node_hint
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
                _cleanup_provider_assets(data.get("name", ""))
                queued = request_provider_restart()
                result["restarting"] = queued
                result["message"] = result.get("message", "订阅已删除") + ("，内核正在后台重启" if queued else "，内核已在重启队列中")
                return self._send_json(result)
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/providers/upload":
            # 本地配置文件直达：body 为文件内容（JSON 包 {name, content}），
            # 落盘为 file 型 provider，与远程订阅同等参与聚合。
            try:
                data = json.loads(body) if body else {}
                name = (data.get("name") or "").strip()
                content = data.get("content") or ""
                interval = int(data.get("interval") or 0)
                if not name:
                    return self._send_json({"success": False, "error": "缺少订阅名称"}, 400)
                if not content.strip():
                    return self._send_json({"success": False, "error": "配置文件内容为空"}, 400)
                # 内容校验：Clash 配置(base64/yaml) 或 节点列表
                import yaml as _yu
                try:
                    doc = _yu.safe_load(content)
                except Exception:
                    doc = None
                ok = False
                if isinstance(doc, dict) and (doc.get("proxies") or doc.get("proxy-providers")):
                    ok = True
                else:
                    # 节点列表判定：base64(含 :port 段) 或 yaml proxies 列表
                    import base64 as _b64u
                    raw = content.strip()
                    is_b64_nodes = False
                    try:
                        dec = _b64u.b64decode(raw + "=" * (-len(raw) % 4), validate=False).decode("utf-8", "ignore")
                        is_b64_nodes = ("://" in dec) and ("," in dec or "\n" in dec)
                    except Exception:
                        pass
                    ok = is_b64_nodes or bool(re.search(r"^\s*-\s*name:", content, re.M))
                if not ok:
                    return self._send_json({"success": False,
                                            "error": "无法识别的配置文件（既非 Clash 配置也非节点列表）"}, 400)
                dest = _sub_file_path(name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(content)
                ms = load_sub_meta()
                ent = ms.setdefault(name, {})
                ent["type"] = "file"
                ent["url"] = ""
                ent["interval"] = interval or 0
                save_sub_meta(ms)
                # 触发一次订阅侧刷新（file 型无需下载，仅重建 provider 索引）
                try:
                    request_provider_restart()
                except Exception:
                    pass
                return self._send_json({"success": True, "name": name,
                                        "message": f"本地配置「{name}」已导入"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path.startswith("/api/sub-file"):
            try:
                if path.startswith("/api/sub-file/raw"):
                    q = parse_qs(urlparse(self.path).query)
                    name = (q.get("name", [""])[0] or "").strip()
                    fp = _sub_file_path(name)
                    if not name or not os.path.exists(fp):
                        return self._send_json({"success": False, "error": "订阅文件不存在"}, 404)
                    return self._send_file(fp, "application/x-yaml")
                data = json.loads(body) if body else {}
                if self.command == "GET":
                    q = parse_qs(urlparse(self.path).query)
                    name = (q.get("name", [""])[0] or "").strip()
                    fp = _sub_file_path(name)
                    if not name or not os.path.exists(fp):
                        return self._send_json({"success": False, "error": "订阅文件不存在"}, 404)
                    return self._send_json({"success": True, "name": name,
                                            "path": fp,
                                            "content": open(fp, "r", encoding="utf-8").read()})
                name = (data.get("name") or "").strip()
                content = data.get("content") or ""
                if not name:
                    return self._send_json({"success": False, "error": "缺少订阅名称"}, 400)
                import yaml as _ysf
                try:
                    _ysf.safe_load(content)
                except Exception as e:
                    return self._send_json({"success": False, "error": f"YAML 语法错误: {e}"}, 400)
                fp = _sub_file_path(name)
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(content)
                try:
                    request_provider_restart()
                except Exception:
                    pass
                return self._send_json({"success": True, "message": f"订阅「{name}」文件已保存"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/aggregate":
            try:
                st = _aggregate_state()
                return self._send_json({"success": True, "aggregate": st["aggregate"],
                                        "active_sub": st["active_sub"]})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/aggregate/set":
            try:
                data = json.loads(body) if body else {}
                active = (data.get("active_sub") or "").strip()
                st = _aggregate_state()
                if active:
                    st["active_sub"] = active
                # 仅设置活跃订阅（不带 aggregate 键）：不动运行配置
                if "aggregate" not in data:
                    _save_aggregate_state(st)
                    return self._send_json({"success": True,
                                            "message": f"已选择订阅「{st['active_sub']}」",
                                            "active_sub": st["active_sub"]})
                want_agg = bool(data.get("aggregate", True))
                # ── 切到「关闭聚合」：把活跃订阅的配置提升为引擎运行配置 ──
                if not want_agg:
                    sub_name = st["active_sub"]
                    if not sub_name:
                        return self._send_json({"success": False,
                                                "error": "请先选择要使用的订阅（卡片菜单 → 使用）"}, 400)
                    src = _sub_file_path(sub_name)
                    # 节点过滤会把「公告/到期」类节点从 payload 里剔除，但配置内的
                    # 策略组仍引用它们 —— 独立使用时 mihomo 会 fatal。
                    # 优先用过滤前的原始 payload（导入时留的 .before-info-filter.bak）。
                    bak = src + ".before-info-filter.bak"
                    if os.path.exists(bak):
                        src = bak
                    if not os.path.exists(src):
                        return self._send_json({"success": False,
                                                "error": f"订阅「{sub_name}」没有本地配置文件"}, 400)
                    sub_text = open(src, "r", encoding="utf-8").read()
                    import yaml as _yv2
                    try:
                        doc = _yv2.safe_load(sub_text)
                    except Exception as e:
                        return self._send_json({"success": False, "error": f"订阅文件不是有效 YAML: {e}"}, 400)
                    if not isinstance(doc, dict) or not (doc.get("proxies") or doc.get("proxy-providers")):
                        return self._send_json({"success": False,
                                                "error": "该订阅文件不是完整 Clash 配置（缺少 proxies/proxy-providers）"}, 400)
                    # 备份聚合态配置（仅当当前就是聚合态时）
                    if st["aggregate"]:
                        try:
                            import shutil as _sh
                            _sh.copyfile(_engine_config_path(), AGG_BAK_FILE)
                        except Exception:
                            pass
                    # 强制作正控制器端口：订阅自带的 external-controller 会让引擎
                    # 脱离面板管理（面板固定连 MIHOMO_CTRL_PORT）；并修复节点过滤
                    # 造成的悬空组/规则引用
                    _doc = _yv2.safe_load(sub_text)
                    if isinstance(_doc, dict):
                        _doc["external-controller"] = f"127.0.0.1:{MIHOMO_CTRL_PORT}"
                        _repair_proxy_refs(_doc)
                        sub_text = _yv2.safe_dump(_doc, allow_unicode=True, sort_keys=False)
                    write_config(sub_text)
                    _save_aggregate_state({"aggregate": False, "active_sub": sub_name})
                    _restart_engine_async()
                    return self._send_json({"success": True, "message": f"已切换到订阅「{sub_name}」的独立配置，引擎重启中…"})
                # ── 切回「聚合」：还原备份的聚合配置 ──
                if os.path.exists(AGG_BAK_FILE):
                    try:
                        import shutil as _sh2
                        _sh2.copyfile(AGG_BAK_FILE, _engine_config_path())
                    except Exception as e:
                        return self._send_json({"success": False, "error": f"还原聚合配置失败: {e}"}, 500)
                else:
                    return self._send_json({"success": False,
                                            "error": "未找到聚合配置备份，请重新导入订阅或恢复默认"}, 400)
                _save_aggregate_state({"aggregate": True, "active_sub": st["active_sub"]})
                _restart_engine_async()
                return self._send_json({"success": True, "message": "已恢复聚合配置，引擎重启中…"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/providers/probe-name":
            try:
                data = json.loads(body) if body else {}
                url = data.get("url", "")
                name = data.get("name", "")  # 可选：关联的订阅名，用于持久化官网
                if not url or not url.startswith(("http://", "https://")):
                    return self._send_json({"success": False, "error": "无效的订阅链接"}, 400)
                # 名称探测必须先确认链接能返回有效订阅；不能在 403/token 无效/超时后
                # 把 URL 尾段冒充成“识别到的名称”。有效订阅无标题时才按 Verge 规则兜底。
                checked = validate_subscription(url, timeout=7)
                if not checked.get("ok"):
                    return self._send_json({"success": False,
                                            "error": checked.get("error") or "订阅验证未通过"}, 400)
                meta = checked.get("meta") or {}
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
        if path == "/api/profile-ext":
            try:
                data = json.loads(body) if body else {}
                ok = _save_profile_ext(data)
                return self._send_json({"success": ok, "message": "全局扩展已保存" if ok else "保存失败"})
            except Exception as e:
                return self._send_json({"success": False, "error": str(e)}, 500)
        if path == "/api/config":
            try:
                data = json.loads(body) if body else {}
                cfg_text = data.get("config", "")
                cfg_text = _apply_profile_ext(cfg_text)  # 全局扩展：合并模板 + 脚本
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
                    # 区分「段落不存在」与「内容无变化」：后者是合法 no-op，不应报错
                    if not _re.search(pattern, text, _re.MULTILINE | _re.DOTALL):
                        # 段落不存在：追加到文件末尾（如首次创建 listeners/隧道配置）
                        new_text = text.rstrip("\n") + "\n\n" + replacement
                    else:
                        return self._send_json({"success": True, "message": f"{section} 无变化"})
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
            return self._api_update_provider()

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
    _migrate_to_file_providers()

    # 系统代理启动对账：未明确开启过的托管块一律关闭（仅移除本应用管理块）
    try:
        reconcile_proxy_on_start()
    except Exception as e:
        log(f"proxy reconcile skipped: {e}")

    # Start Unix socket server (for fnOS iframe integration) — non-fatal if it fails
    start_unix_socket_server()



    # Start HTTP server (for direct browser access)
    httpd = ThreadedHTTPServer(("0.0.0.0", ADMIN_PORT), AdminHandler)
    log(f"HTTP admin panel: http://0.0.0.0:{ADMIN_PORT}")

    threading.Thread(target=_refresh_all_payloads, kwargs={'initial': True}, daemon=True).start()

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
