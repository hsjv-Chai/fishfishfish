"""HTTP-only adapters. No stored passwords, no redirects carrying credentials."""
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import base64
import json
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import private_file
from .strategy import Candle, HOUR, closed_candles


class APIError(Exception):
    def __init__(self, code=0, retry_after=0, message="request failed"):
        super().__init__(f"{message} (status={code})")
        self.code = code
        self.retry_after = retry_after


class SchemaError(APIError):
    def __init__(self, message="unexpected response schema"):
        super().__init__(0, 0, message)


def tls_context():
    # The python.org macOS build may lack its own certificate bundle.
    cafile = os.environ.get("SSL_CERT_FILE")
    if not cafile and not ssl.get_default_verify_paths().cafile and Path("/etc/ssl/cert.pem").exists():
        cafile = "/etc/ssl/cert.pem"
    return ssl.create_default_context(cafile=cafile)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTP:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context()))

    def request(self, url, body=None, headers=None, raw=False, timeout=10):
        headers = {"Accept": "application/json", "User-Agent": "fishfishfish/0.1", **(headers or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                data = response.read(12_000_000)
                if len(data) >= 12_000_000:
                    raise SchemaError("response too large")
                result = data.decode() if raw else json.loads(data)
                return result, response.headers
        except urllib.error.HTTPError as e:
            retry = e.headers.get("Retry-After", "0")
            try:
                retry = min(86400, max(0, int(retry)))
            except ValueError:
                retry = 60
            raise APIError(e.code, retry) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0) from None
        except (UnicodeError, json.JSONDecodeError):
            raise SchemaError() from None


def number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SchemaError()
    if value < 0 or (positive and value == 0):
        raise SchemaError()
    return value


def fish_units(value):
    value = number(value)
    scaled = value * 10000
    if abs(scaled - round(scaled)) > max(1e-6, abs(scaled) * 1e-14):
        raise SchemaError("fish precision changed")
    return round(scaled)


class ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.scripts.append(data)


def parse_positions_page(html):
    """Read only serialized TradePanel props; never execute page JavaScript.

    RSC is an internal, version-sensitive interface: an unknown shape fails closed.
    """
    parser = ScriptParser()
    parser.feed(html)
    chunks = []
    decoder = json.JSONDecoder()
    for script in parser.scripts:
        for m in re.finditer(r"self\.__next_f\.push\(", script):
            try:
                value, _ = decoder.raw_decode(script[m.end():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, list) and len(value) == 2 and value[0] == 1 and isinstance(value[1], str):
                chunks.append(value[1])
    candidates = []

    def visit(obj):
        if isinstance(obj, dict):
            if {"positions", "balance", "feeRate", "minStake", "initialQuotes"} <= obj.keys():
                candidates.append(obj)
            for x in obj.values():
                visit(x)
        elif isinstance(obj, list):
            for x in obj:
                visit(x)

    for line in "".join(chunks).splitlines():
        _, _, payload = line.partition(":")
        if payload.startswith(("[", "{")):
            try:
                visit(json.loads(payload))
            except json.JSONDecodeError:
                continue
    if len(candidates) != 1:
        raise SchemaError("TradePanel snapshot unavailable; cannot reconcile positions")
    props = candidates[0]
    if props["feeRate"] != .0002 or props["minStake"] != 1 or not isinstance(props["positions"], list):
        raise SchemaError("site trading rules changed")
    seen = set()
    for p in props["positions"]:
        if not isinstance(p, dict) or not {"id", "symbol", "stake", "entryPrice", "leverage"} <= p.keys():
            raise SchemaError()
        if not isinstance(p["id"], str) or not p["id"] or p["id"] in seen:
            raise SchemaError()
        seen.add(p["id"])
        fish_units(p["stake"])
        number(p["entryPrice"], True)
        if p["symbol"] not in {"BTCUSDT", "ETHUSDT"} or p["leverage"] not in (1, 2, 3, 5, 10, 20, 100):
            raise SchemaError()
    return {"balance": fish_units(props["balance"]), "positions": props["positions"]}


@dataclass
class Quote:
    price: float
    age_ms: int
    stale: bool
    received_at: int

    def fresh(self, now):
        return not self.stale and 0 <= now - self.received_at + self.age_ms <= 40000


class Site:
    def __init__(self, config, http=None):
        self.cfg = config
        self.http = http or HTTP()

    def session(self):
        obj = json.loads(private_file(self.cfg.session_file).read_text())
        token = obj.get("token", "")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
            raise ValueError("invalid session file")
        return obj

    def call(self, path, body=None, raw=False):
        session = self.session()
        try:
            data, _ = self.http.request(self.cfg.site_url + path, body, {
                "Cookie": "raricy_session=" + session["token"],
                "Origin": self.cfg.site_url, "Referer": self.cfg.site_url + "/fish/trade",
            }, raw=raw)
        except APIError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise APIError(401, message="session redirected; login required") from None
            raise
        if not raw and (not isinstance(data, dict) or data.get("code") != 200):
            raise SchemaError()
        return data

    def expiry(self):
        return int(self.session()["expires_at"]) * 1000

    def quote(self, now):
        data = self.call("/api/fish/trade/quote")
        if data.get("fee_rate") != .0002 or data.get("min_stake") != 1:
            raise SchemaError("site trading rules changed")
        if data.get("ok") is not True:
            raise APIError(503)
        for q in data.get("quotes", []):
            if q.get("symbol") == "BTCUSDT":
                if not isinstance(q.get("stale"), bool):
                    raise SchemaError()
                return Quote(number(q["price"], True), int(number(q["age_ms"])), q["stale"], now)
        raise SchemaError()

    def candles(self, interval):
        data = self.call("/api/fish/trade/candles?symbol=BTCUSDT&interval=" + interval)
        if data.get("ok") is not True or data.get("symbol") != "BTCUSDT":
            raise SchemaError()
        return [Candle.row(row) for row in data["candles"]]

    def snapshot(self, since_id=0):
        page = parse_positions_page(self.call("/fish/trade", raw=True))
        ledger = {}
        expected_total = None
        # Fetch through the previous checkpoint; never silently truncate.
        for n in range(1, 102):
            data = self.call(f"/api/fish/transactions?per_page=100&page={n}")
            if expected_total is None:
                expected_total = data.get("total")
            if data.get("total") != expected_total:
                raise SchemaError("ledger changed during pagination; retry snapshot")
            rows = data.get("transactions")
            if not isinstance(rows, list):
                raise SchemaError()
            for row in rows:
                if not {"id", "amount", "type", "reference_id"} <= row.keys():
                    raise SchemaError()
                if not isinstance(row["id"], int) or not isinstance(row["amount"], (float, int)) or not math.isfinite(row["amount"]):
                    raise SchemaError()
                ledger[row["id"]] = row
            if any(row["id"] <= since_id for row in rows) or not data.get("has_next"):
                break
        else:
            raise SchemaError("ledger too long for verified snapshot")
        after = self.call("/api/fish/balance?per_page=1")
        balance = fish_units(after.get("balance"))
        newest = max(ledger, default=0)
        after_newest = max((r["id"] for r in after.get("transactions", [])), default=0)
        if balance != page["balance"] or after_newest != newest or after.get("total") != expected_total:
            raise SchemaError("account changed during snapshot; retry")
        page["transactions"] = sorted(ledger.values(), key=lambda r: r["id"])
        if since_id == 0 and len(ledger) != expected_total:
            raise SchemaError("incomplete initial ledger pagination")
        page["newest_id"] = newest
        return page

    def buy(self, order):
        data = self.call("/api/fish/trade/buy", dict(symbol="BTCUSDT", amount=order["stake"] / 10000,
                         leverage=1, idempotency_key=order["key"]))
        p = data.get("position", {})
        if (p.get("symbol") != "BTCUSDT" or p.get("leverage") != 1 or
                fish_units(p.get("stake")) != order["stake"] or not isinstance(p.get("id"), str)):
            raise SchemaError("buy receipt mismatch")
        number(p.get("entry_price"), True)
        return data

    def sell(self, position_id):
        data = self.call("/api/fish/trade/sell", {"position_id": position_id})
        if data.get("position_id") != position_id or data.get("symbol") != "BTCUSDT":
            raise SchemaError("sell receipt mismatch")
        fish_units(data.get("payout"))
        number(data.get("exit_price"), True)
        return data


class Market:
    def __init__(self, base="https://data-api.binance.vision", http=None):
        self.base, self.http = base, http or HTTP()

    def candles(self, interval, now):
        width = HOUR if interval == "1h" else 4 * HOUR
        query = urllib.parse.urlencode(dict(symbol="BTCUSDT", interval=interval, limit=1000,
                                            endTime=(now // width) * width - 1))
        rows, _ = self.http.request(self.base + "/api/v3/klines?" + query)
        return closed_candles([Candle.row(r) for r in rows], width, now)

    def price(self, now):
        rows, _ = self.http.request(self.base + "/api/v3/aggTrades?symbol=BTCUSDT&limit=1", timeout=5)
        if not isinstance(rows, list) or not rows:
            raise SchemaError()
        price, stamp = float(rows[-1]["p"]), int(rows[-1]["T"])
        number(price, True)
        actual_now = int(time.time() * 1000)
        if not -5000 <= actual_now - stamp <= 15000:
            raise SchemaError("upstream trade timestamp stale or clock skew")
        return price, stamp


def login(config, username, password):
    from http.cookies import SimpleCookie
    data, headers = HTTP().request(config.site_url + "/api/auth/login", {"username": username, "password": password})
    if data.get("code") != 200:
        raise APIError(401)
    jar = SimpleCookie()
    for header in headers.get_all("Set-Cookie", []):
        jar.load(header)
    if "raricy_session" not in jar:
        raise SchemaError("login did not return a session")
    token = jar["raricy_session"].value
    # Decode exp only for scheduling; identity and signature are verified by the website.
    payload = token.split(".")[1]
    exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    path = Path(config.session_file)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".new")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump({"token": token, "expires_at": exp}, file)
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    return exp
