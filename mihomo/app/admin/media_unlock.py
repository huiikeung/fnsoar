#!/usr/bin/env python3
"""
Media streaming unlock checker (ports clash-verge-rev's `clash-verge-media-unlock`).

Checks whether the current exit IP can access each streaming service, mirroring
the exact logic / status strings of the Rust crate:

  Netflix, Disney+, Prime Video, Spotify, TikTok, YouTube Premium, Bahamut Anime,
  Bilibili CN, Bilibili HK/MC/TW, ChatGPT iOS/Web, Claude, Gemini

Each item is a dict: {"name", "status", "region" (optional), "check_time"}.
A "Pending" item has check_time=None.
"""
import re
import ssl
import json
import urllib.request
import urllib.error
import urllib.parse
from http.cookiejar import CookieJar
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
WIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
TIMEOUT = 30


class Resp:
    """Minimal HTTP response wrapper (status, final_url, headers, body)."""
    __slots__ = ("status", "url", "headers", "text")

    def __init__(self, status, url, headers, text):
        self.status = status
        self.url = url
        self.headers = headers
        self.text = text


def _make_opener(jar=None, ua=MAC_UA):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


_OPENER = _make_opener(None, MAC_UA)


def _request(method, url, headers=None, body=None, form=None, timeout=TIMEOUT,
             ua=MAC_UA, jar=None):
    """Perform a request. body is raw bytes/str; form is a dict->urlencoded.
    Returns a Resp, or None on error."""
    all_headers = {"User-Agent": ua}
    if headers:
        all_headers.update(headers)
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    opener = _make_opener(jar, ua) if jar is not None else _OPENER
    try:
        resp = opener.open(req, timeout=timeout)
        text = resp.read().decode("utf-8", "replace")
        return Resp(resp.status, resp.geturl(), resp.headers, text)
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return Resp(e.code, getattr(e, "url", url) or url, e.headers, text)
    except Exception:
        return None


# ── helpers / item factory ───────────────────────────────────────────────
_ALPHA3_TO_ALPHA2 = {
    "CHN": "CN", "RUS": "RU", "BLR": "BY", "CUB": "CU", "IRN": "IR",
    "PRK": "KP", "SYR": "SY", "HKG": "HK", "MAC": "MO", "USA": "US",
    "GBR": "GB", "JPN": "JP", "AUS": "AU", "DEL": "DE", "CAN": "CA",
    "FRA": "FR", "KOR": "KR", "TWN": "TW", "SGP": "SG", "BRA": "BR",
    "IND": "IN", "DEU": "DE", "ITA": "IT", "ESP": "ES", "NZL": "NZ",
}


def region_label(country_code):
    """Build '🇺🇸US' style label. Accepts alpha-2 or alpha-3. Falls back to raw code."""
    code = (country_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2,3}", code):
        return code
    a2 = code
    if len(code) == 3:
        a2 = _ALPHA3_TO_ALPHA2.get(code, "")
    if len(a2) == 2:
        try:
            emoji = (chr(0x1F1E6 + ord(a2[0]) - ord("A"))
                     + chr(0x1F1E6 + ord(a2[1]) - ord("A")))
            return f"{emoji}{a2}"
        except Exception:
            pass
    return code


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def checked(name, status, region=None):
    return {"name": name, "status": status,
            "region": region, "check_time": _now()}


def checked_region(name, status, country_code):
    return checked(name, status, region_label(country_code))


def pending(name):
    return {"name": name, "status": "Pending", "region": None, "check_time": None}


DEFAULT_UNLOCK_ITEM_NAMES = [
    "哔哩哔哩大陆", "哔哩哔哩港澳台", "ChatGPT iOS", "ChatGPT Web", "Claude",
    "Gemini", "YouTube Premium", "Bahamut Anime", "Netflix", "Disney+",
    "Prime Video", "Spotify", "TikTok",
]


def default_unlock_items():
    return [pending(n) for n in DEFAULT_UNLOCK_ITEM_NAMES]


# ── Bilibili ─────────────────────────────────────────────────────────────
def _check_bilibili(url, name):
    r = _request("GET", url)
    if r is None:
        return checked(name, "Failed")
    try:
        body = json.loads(r.text)
        code = body.get("code")
        status = "Yes" if code == 0 else ("No" if code == -10403 else "Failed")
    except Exception:
        status = "Failed"
    return checked(name, status)


def check_bilibili_china_mainland():
    return _check_bilibili(
        "https://api.bilibili.com/pgc/player/web/playurl?avid=82846771&qn=0"
        "&type=&otype=json&ep_id=307247&fourk=1&fnver=0&fnval=16&module=bangumi",
        "哔哩哔哩大陆")


def check_bilibili_hk_mc_tw():
    return _check_bilibili(
        "https://api.bilibili.com/pgc/player/web/playurl?avid=18281381"
        "&cid=29892777&qn=0&type=&otype=json&ep_id=183799&fourk=1"
        "&fnver=0&fnval=16&module=bangumi",
        "哔哩哔哩港澳台")


# ── ChatGPT ──────────────────────────────────────────────────────────────
def check_chatgpt_combined():
    results = []
    # region from CF trace
    region = None
    r = _request("GET", "https://chat.openai.com/cdn-cgi/trace")
    if r is not None:
        loc = None
        for line in r.text.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                if key.strip() == "loc":
                    loc = value.strip()
                    break
        if loc:
            region = region_label(loc)
    # iOS
    ios_status = "Failed"
    r = _request("GET", "https://ios.chat.openai.com/")
    if r is not None:
        low = r.text.lower()
        if "you may be connected to a disallowed isp" in low:
            ios_status = "Disallowed ISP"
        elif "request is not allowed. please try again later." in low:
            ios_status = "Yes"
        elif "sorry, you have been blocked" in low:
            ios_status = "Blocked"
        else:
            ios_status = "Failed"
    else:
        ios_status = "Failed"
    # Web
    web_status = "Failed"
    r = _request("GET", "https://api.openai.com/compliance/cookie_requirements")
    if r is not None:
        if "unsupported_country" in r.text.lower():
            web_status = "Unsupported Country/Region"
        else:
            web_status = "Yes"
    results.append(checked("ChatGPT iOS", ios_status, region))
    results.append(checked("ChatGPT Web", web_status, region))
    return results


# ── Claude ───────────────────────────────────────────────────────────────
CLAUDE_BLOCKED = {"AF", "BY", "CN", "CU", "HK", "IR", "KP", "MO", "RU", "SY"}


def check_claude():
    r = _request("GET", "https://claude.ai/cdn-cgi/trace")
    if r is None:
        return checked("Claude", "Failed")
    code = None
    for line in r.text.splitlines():
        if line.startswith("loc="):
            code = line[4:].strip().upper()
            break
    if not code:
        return checked("Claude", "Failed")
    status = "No" if code in CLAUDE_BLOCKED else "Yes"
    return checked_region("Claude", status, code)


# ── Gemini ───────────────────────────────────────────────────────────────
GEMINI_BLOCKED = {"CHN", "RUS", "BLR", "CUB", "IRN", "PRK", "SYR", "HKG", "MAC"}
GEMINI_MARKER = ',2,1,200,"'


def check_gemini():
    r = _request("GET", "https://gemini.google.com")
    if r is None:
        return checked("Gemini", "Failed")
    body = r.text
    idx = body.find(GEMINI_MARKER)
    code = None
    if idx >= 0:
        start = idx + len(GEMINI_MARKER)
        chunk = body[start:start + 3]
        if chunk.isascii() and chunk.isupper() and len(chunk) == 3:
            code = chunk
    if not code:
        return checked("Gemini", "Failed")
    status = "No" if code in GEMINI_BLOCKED else "Yes"
    return checked_region("Gemini", status, code)


# ── YouTube Premium ──────────────────────────────────────────────────────
def check_youtube_premium():
    r = _request("GET", "https://www.youtube.com/premium?hl=en")
    if r is None:
        return checked("YouTube Premium", "Failed")
    body = r.text
    low = body.lower()
    region = None
    patterns = [
        r'id=["\']country-code["\'][^>]*>\s*([A-Za-z]{2,3})\s*<',
        r'"GL"\s*:\s*"([A-Za-z]{2})"',
        r'"countryCode"\s*:\s*"([A-Za-z]{2})"',
        r'"country_code"\s*:\s*"([A-Za-z]{2})"',
    ]
    for pat in patterns:
        m = re.search(pat, body)
        if m:
            region = region_label(m.group(1).strip().upper())
            break
    if ("youtube premium is not available in your country" in low
            or "premium is not available in your country" in low
            or "premium is not available in your region" in low):
        status = "No"
    elif 200 <= r.status < 300 and (
            "youtube premium" in low or "ad-free" in low
            or '"browseid":"spunlimited"' in low):
        status = "Yes"
    else:
        status = "Failed"
    return checked("YouTube Premium", status, region)


# ── Bahamut Anime ────────────────────────────────────────────────────────
def check_bahamut_anime():
    jar = CookieJar()
    r = _request("GET", "https://ani.gamer.com.tw/ajax/getdeviceid.php",
                 ua=WIN_UA, jar=jar)
    device_id = ""
    if r is not None:
        m = re.search(r'"deviceid"\s*:\s*"([^"]+)"', r.text)
        if m:
            device_id = m.group(1)
    if not device_id:
        return checked("Bahamut Anime", "Failed")
    url = ("https://ani.gamer.com.tw/ajax/token.php?adID=89422&sn=37783"
           "&device=" + urllib.parse.quote(device_id))
    r = _request("GET", url, ua=WIN_UA, jar=jar)
    if r is None or "animeSn" not in r.text:
        return checked("Bahamut Anime", "No")
    region = None
    r = _request("GET", "https://ani.gamer.com.tw/", ua=WIN_UA, jar=jar)
    if r is not None:
        m = re.search(r'data-geo="([^"]+)"', r.text)
        if m:
            region = region_label(m.group(1))
    return checked("Bahamut Anime", "Yes", region)


# ── Netflix ──────────────────────────────────────────────────────────────
def check_netflix():
    def netflix_item(status, region=None):
        return checked("Netflix", status, region)

    # CDN probe
    cdn = check_netflix_cdn()
    if cdn["status"] == "Yes":
        return cdn

    urls = ["https://www.netflix.com/title/81280792",
            "https://www.netflix.com/title/70143836"]
    resp1 = _request("GET", urls[0])
    resp2 = _request("GET", urls[1])
    if resp1 is None or resp2 is None:
        return netflix_item("Failed")
    status1, status2 = resp1.status, resp2.status
    if status1 == 404 and status2 == 404:
        return netflix_item("Originals Only")
    if status1 == 403 or status2 == 403:
        return netflix_item("No")
    if status1 in (200, 301) or status2 in (200, 301):
        r = _request("GET", "https://www.netflix.com/title/80018499")
        if r is None:
            return netflix_item("Yes (但无法获取区域)")
        location = (r.headers.get("Location") or r.headers.get("location") or "")
        if location:
            parts = location.split("/")
            if len(parts) >= 4:
                region_code = parts[3].split("-")[0]
                return checked_region("Netflix", "Yes", region_code)
        return checked_region("Netflix", "Yes", "us")
    return netflix_item(f"Failed (状态码: {status1}_{status2}")


def check_netflix_cdn():
    url = ("https://api.fast.com/netflix/speedtest/v2?https=true"
           "&token=YXNkZmFzZGxmbnNkYWZoYXNkZmhrYWxm&urlCount=5")
    r = _request("GET", url)
    if r is None:
        return checked("Netflix", "Failed (CDN API)")
    if r.status == 403:
        return checked("Netflix", "No (IP Banned By Netflix)")
    try:
        data = json.loads(r.text)
        targets = data.get("targets") or []
        if targets:
            country = (targets[0].get("location") or {}).get("country")
            if country:
                return checked_region("Netflix", "Yes", country)
        return checked("Netflix", "Unknown")
    except Exception:
        return checked("Netflix", "Failed (解析错误)")


# ── Disney+ ──────────────────────────────────────────────────────────────
DISNEY_AUTH = ("Bearer ZGlzbmV5JmJyb3dzZXImMS4wLjA."
               "Cu56AgSfBTDag5NiRA81oLHkDZfu5L3CKadnefEAY84")


def _disney_main_page_region():
    r = _request("GET", "https://www.disneyplus.com/")
    if r is None:
        return None
    m = re.search(r'region"\s*:\s*"([^"]+)"', r.text)
    if not m:
        return None
    region_code = m.group(1)
    return checked("Disney+", "Yes",
                   "{} (from main page)".format(region_label(region_code)))


def check_disney_plus():
    def disney_item(status, region=None):
        return checked("Disney+", status, region)

    auth = {"authorization": DISNEY_AUTH}
    device_body = {
        "deviceFamily": "browser", "applicationRuntime": "chrome",
        "deviceProfile": "windows", "attributes": {},
    }
    r = _request("POST", "https://disney.api.edge.bamgrid.com/devices",
                 headers=dict(auth, **{"Content-Type": "application/json; charset=UTF-8"}),
                 body=json.dumps(device_body))
    if r is None:
        return disney_item("Failed (Network Connection)")
    if r.status == 403:
        return disney_item("No (IP Banned By Disney+)")
    m = re.search(r'"assertion"\s*:\s*"([^"]+)"', r.text)
    assertion = m.group(1) if m else None
    if not assertion:
        return disney_item("Failed (Error: Cannot extract assertion)")

    token_form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "latitude": "0", "longitude": "0", "platform": "browser",
        "subject_token": assertion,
        "subject_token_type": "urn:bamtech:params:oauth:token-type:device",
    }
    r = _request("POST", "https://disney.api.edge.bamgrid.com/token",
                 headers=auth, form=token_form)
    if r is None:
        return disney_item("Failed (Network Connection)")
    token_status = r.status
    token_text = r.text
    if "forbidden-location" in token_text or "403 ERROR" in token_text:
        return disney_item("No (IP Banned By Disney+)")
    refresh_token = None
    try:
        refresh_token = json.loads(token_text).get("refresh_token")
    except Exception:
        refresh_token = None
    if not refresh_token:
        m = re.search(r'"refresh_token"\s*:\s*"([^"]+)"', token_text)
        refresh_token = m.group(1) if m else None
    if not refresh_token:
        return disney_item(
            "Failed (Error: Cannot extract refresh token, status: {}, "
            "response: {})".format(token_status, token_text[:100] + "..."))

    graphql_payload = (
        '{{"query":"mutation refreshToken($input: RefreshTokenInput!) '
        '{{ refreshToken(refreshToken: $input) {{ activeSession '
        '{{ sessionId }} }} }}","variables":{{"input":{{"refreshToken":'
        '"{}"}}}}}}'.format(refresh_token.replace("\\", "\\\\").replace('"', '\\"')))
    g = _request("POST", "https://disney.api.edge.bamgrid.com/graph/v1/device/graphql",
                 headers=dict(auth, **{"Content-Type": "application/json"}),
                 body=graphql_payload)

    preview = _request("GET", "https://disneyplus.com")
    is_unavailable = True
    if preview is not None:
        u = preview.url or ""
        is_unavailable = ("preview" in u) or ("unavailable" in u)

    g_status = g.status if g is not None else 0
    g_text = g.text if g is not None else ""
    if not g_text or g_status >= 400:
        main = _disney_main_page_region()
        if main:
            return main
        if not g_text:
            return disney_item("Failed (GraphQL error: empty response, status: {})".format(g_status))
        return disney_item("Failed (GraphQL error: {}, status: {})".format(
            g_text[:50] + "...", g_status))

    m = re.search(r'"countryCode"\s*:\s*"([^"]+)"', g_text)
    region_code = m.group(1) if m else None
    m2 = re.search(r'"inSupportedLocation"\s*:\s*(false|true)', g_text)
    in_supported = (m2.group(1) == "true") if m2 else None

    if not region_code:
        main = _disney_main_page_region()
        if main:
            return main
        return disney_item("No")

    if region_code.upper() == "JP":
        return checked_region("Disney+", "Yes", region_code)

    if is_unavailable:
        return disney_item("No")

    if in_supported is False:
        return disney_item("Soon",
                           "{}（即将上线）".format(region_label(region_code)))
    if in_supported is True:
        return checked_region("Disney+", "Yes", region_code)
    return disney_item("Failed (Error: Unknown region status for {})".format(region_code))


# ── Prime Video ──────────────────────────────────────────────────────────
def check_prime_video():
    def pv(status, region=None):
        return checked("Prime Video", status, region)
    r = _request("GET", "https://www.primevideo.com")
    if r is None:
        return pv("Failed (Network Connection)")
    body = r.text
    is_blocked = "isServiceRestricted" in body
    m = re.search(r'"currentTerritory":"([^"]+)"', body)
    region_code = m.group(1) if m else None
    if is_blocked:
        return pv("No (Service Not Available)")
    if region_code:
        return checked_region("Prime Video", "Yes", region_code)
    if not is_blocked:
        return pv("Failed (Error: PAGE ERROR)")
    return pv("Failed (Error: Unknown Region)")


# ── Spotify ──────────────────────────────────────────────────────────────
def check_spotify():
    url = ("https://www.spotify.com/api/content/v1/country-selector"
           "?platform=web&format=json")
    r = _request("GET", url)
    if r is None:
        return checked("Spotify", "Failed")
    region = None
    final_url = r.url or ""
    try:
        path = urllib.parse.urlparse(final_url).path
        segments = [s for s in path.split("/") if s]
        if segments:
            first = segments[0]
            if first and first != "api":
                region = region_label(first.split("-")[0].upper())
    except Exception:
        pass
    if not region:
        m = re.search(r'"countryCode":"([^"]+)"', r.text)
        if m and m.group(1):
            region = region_label(m.group(1).upper())
    status_c = r.status
    low = r.text.lower()
    if status_c == 403 or status_c == 451:
        status = "No"
    elif not (200 <= status_c < 300):
        status = "Failed"
    elif "not available in your country" in low:
        status = "No"
    else:
        status = "Yes"
    return checked("Spotify", status, region)


# ── TikTok ───────────────────────────────────────────────────────────────
def _tiktok_status(status, body):
    if status == 403 or status == 451:
        return "No"
    if not (200 <= status < 300):
        return "Failed"
    low = body.lower()
    if ("access denied" in low or "not available in your region" in low
            or "tiktok is not available" in low):
        return "No"
    return "Yes"


def _tiktok_region(body):
    m = re.search(r'"region"\s*:\s*"([a-zA-Z-]+)"', body)
    if not m:
        return None
    raw = m.group(1)
    code = raw.split("-")[0].upper()
    return region_label(code) if code else None


def check_tiktok():
    status = "Failed"
    region = None
    r = _request("GET", "https://www.tiktok.com/cdn-cgi/trace")
    if r is not None:
        status = _tiktok_status(r.status, r.text)
        region = _tiktok_region(r.text)
    if region is None or status == "Failed":
        r2 = _request("GET", "https://www.tiktok.com/")
        if r2 is not None:
            fallback_status = _tiktok_status(r2.status, r2.text)
            fallback_region = _tiktok_region(r2.text)
            if status != "No":
                status = fallback_status
            if region is None:
                region = fallback_region
    return checked("TikTok", status, region)


# ── Summary ──────────────────────────────────────────────────────────────
_CHECKERS = [
    lambda: [check_bilibili_china_mainland()],
    lambda: [check_bilibili_hk_mc_tw()],
    check_chatgpt_combined,
    lambda: [check_claude()],
    lambda: [check_gemini()],
    lambda: [check_youtube_premium()],
    lambda: [check_bahamut_anime()],
    lambda: [check_netflix()],
    lambda: [check_disney_plus()],
    lambda: [check_prime_video()],
    lambda: [check_spotify()],
    lambda: [check_tiktok()],
]


def check_media_unlock():
    """Run all unlock checks concurrently. Each checker may return 1+ items."""
    results = []
    with ThreadPoolExecutor(max_workers=len(_CHECKERS)) as ex:
        futures = [ex.submit(fn) for fn in _CHECKERS]
        for fut in futures:
            try:
                items = fut.result()
                if items:
                    results.extend(items)
            except Exception:
                continue
    return results


if __name__ == "__main__":
    for it in check_media_unlock():
        print(it)