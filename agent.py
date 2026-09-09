"""Volatility anomaly alert agent using overseas public market APIs."""
from __future__ import annotations
import argparse, json, math, os, statistics, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).parent
CACHE = {}
CACHE_TTL = 45

class DataError(Exception):
    pass

def request_json(base, path, params, provider):
    url = f"{base}{path}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "bolu-yidong-agent/2.0", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=15) as response:
            raw = response.read()
            if not raw:
                raise DataError(f"{provider} 返回了空响应")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DataError(f"{provider} 返回内容不是有效JSON") from exc
            return payload
    except HTTPError as exc:
        raise DataError(f"{provider} HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise DataError(f"无法连接 {provider} 公共接口") from exc

def request_json_post(url, body, provider):
    raw_body = json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=raw_body,
        headers={
            "User-Agent": "bolu-yidong-agent/3.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read()
            if not raw:
                raise DataError(f"{provider} 返回了空响应")
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DataError(f"{provider} 返回内容不是有效JSON") from exc
    except HTTPError as exc:
        raise DataError(f"{provider} HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise DataError(f"无法连接 {provider} 公共接口") from exc


def fetch_hyperliquid(symbol):
    coin = symbol[:-4]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 30 * 24 * 60 * 60 * 1000
    payload = request_json_post(
        "https://api.hyperliquid.xyz/info",
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": "1h",
                "startTime": start_ms,
                "endTime": end_ms,
            },
        },
        "Hyperliquid",
    )
    if not isinstance(payload, list) or len(payload) < 100:
        raise DataError("Hyperliquid 返回的K线数量不足")
    rows = sorted(payload, key=lambda item: int(item["t"]))
    return rows, "Hyperliquid perpetual live"


def fetch_okx(symbol):
    inst_id = f"{symbol[:-4]}-USDT-SWAP"
    base = "https://www.okx.com"
    rows = []
    after = None
    for _ in range(3):
        params = {"instId": inst_id, "bar": "1H", "limit": "300"}
        if after:
            params["after"] = str(after)
        payload = request_json(base, "/api/v5/market/candles", params, "OKX")
        if payload.get("code") != "0":
            raise DataError(f"OKX API错误: {payload.get('msg', '未知错误')}")
        batch = payload.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        oldest = min(int(item[0]) for item in batch)
        if after == oldest:
            break
        after = oldest
        if len(batch) < 300:
            break
    if len(rows) < 100:
        raise DataError("OKX 返回的K线数量不足")
    unique = {str(item[0]): item for item in rows}
    return [unique[key] for key in sorted(unique)], "OKX Swap live"

def fetch_bybit(symbol):
    payload = request_json(
        "https://api.bybit.com",
        "/v5/market/kline",
        {"category": "linear", "symbol": symbol, "interval": "60", "limit": "1000"},
        "Bybit",
    )
    if payload.get("retCode") != 0:
        raise DataError(f"Bybit API错误: {payload.get('retMsg', '未知错误')}")
    rows = (payload.get("result") or {}).get("list") or []
    if len(rows) < 100:
        raise DataError("Bybit 返回的K线数量不足")
    return list(reversed(rows)), "Bybit Linear live"

def fetch_market_candles(symbol):
    errors = []
    for fetcher in (fetch_hyperliquid, fetch_okx, fetch_bybit):
        try:
            return fetcher(symbol)
        except DataError as exc:
            errors.append(str(exc))
    raise DataError("海外行情接口暂时不可用：" + "；".join(errors))

def synthetic(symbol):
    seed = sum(ord(c) for c in symbol)
    base = 0.012 + (seed % 17) / 1000
    closes=[]; price=100 + seed % 80
    for i in range(720):
        wave = math.sin(i / 17 + seed) * base * .7
        shock = (math.sin(i / 5 + seed) * .25 + math.cos(i/31) * .15) * base
        ret = wave + shock
        price *= math.exp(ret)
        closes.append(price)
    return [{"open": closes[max(0,i-1)], "high": closes[i]*(1+base*.8), "low": closes[i]*(1-base*.8), "close": closes[i], "time": i} for i in range(720)]

def calculate(candles, interval_hours=1):
    if isinstance(candles[0], list):
        # OKX / Bybit: timestamp, open, high, low, close, volume, ...
        highs = [float(x[2]) for x in candles]; lows = [float(x[3]) for x in candles]; closes = [float(x[4]) for x in candles]
    elif isinstance(candles[0], dict) and "h" in candles[0]:
        # Hyperliquid: {t, T, o, c, h, l, v, n}
        highs = [float(x["h"]) for x in candles]; lows = [float(x["l"]) for x in candles]; closes = [float(x["c"]) for x in candles]
    else:
        highs = [float(x["high"]) for x in candles]; lows = [float(x["low"]) for x in candles]; closes = [float(x["close"]) for x in candles]
    returns = [math.log(closes[i]/closes[i-1]) for i in range(1,len(closes)) if closes[i-1] > 0]
    def annualized(rs): return statistics.stdev(rs) * math.sqrt(24*365) * 100 if len(rs)>1 else 0
    hv7 = annualized(returns[-168:])
    hv30 = annualized(returns[-720:])
    rolling = []
    step = 24
    for end in range(max(24,len(returns)-7*24), len(returns)+1, step):
        rolling.append(round(annualized(returns[max(0,end-24):end]), 2))
    trs=[]
    for i in range(1,len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) / closes[i-1] * 100)
    atr = statistics.mean(trs[-14:]) if trs else 0
    baseline = annualized(returns[-720:-24]) if len(returns)>48 else hv30
    ratio = hv7 / max(baseline, .01)
    if ratio >= 1.8 or hv7 >= 120: level, alert = "剧烈", True
    elif ratio >= 1.3 or hv7 >= 70: level, alert = "活跃", False
    elif hv7 >= 35: level, alert = "正常", False
    else: level, alert = "平静", False
    return {"hv7":round(hv7,2), "hv30":round(hv30,2), "baseline":round(baseline,2), "ratio":round(ratio,2), "atr":round(atr,3), "curve":rolling, "level":level, "alert":alert}

def analyze(symbol):
    symbol=symbol.upper().strip()
    if not symbol or not symbol.endswith("USDT") or len(symbol)<6: raise DataError("请输入 USDT 永续合约，例如 BTCUSDT")
    key=symbol
    now=time.time()
    if key in CACHE and now-CACHE[key][0] < CACHE_TTL: return CACHE[key][1]
    try:
        rows, source = fetch_market_candles(symbol)
        result=calculate(rows)
        demo=False
    except Exception as exc:
        rows=synthetic(symbol); result=calculate(rows); source="Illustrative demo mode"; demo=True
        result["notice"] = f"海外实时接口暂时不可达，当前显示演示数据（{type(exc).__name__}）。"
    result.update({"symbol":symbol,"source":source,"demo":demo,"updated":datetime.now(timezone.utc).isoformat()})
    CACHE[key]=(now,result)
    return result

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        parsed=urlparse(self.path)
        if parsed.path == "/api/analyze":
            symbol = parse_qs(parsed.query).get("symbol", ["BTCUSDT"])[0]
            try:
                payload = {"ok": True, "data": analyze(symbol)}
                code = 200
            except DataError as exc:
                payload = {"ok": False, "error": str(exc)}
                code = 400
            except Exception:
                payload = {"ok": False, "error": "服务器处理失败，请稍后重试"}
                code = 500
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path not in ("/", "/index.html"):
            body = json.dumps({"ok": False, "error": "页面不存在"}, ensure_ascii=False).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        file = ROOT / "static" / "index.html"
        try:
            body = file.read_bytes()
        except OSError:
            body = "<h1>网页文件缺失，请检查 static/index.html</h1>".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8002")))
    a = p.parse_args()
    port = int(os.environ.get("PORT", str(a.port)))
    print(f"Volatility agent: http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
if __name__=="__main__": main()
