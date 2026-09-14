"""波动率异动预警智能体 · 命令行入口（四通道）。

用法
  python agent.py                          # 本地网页服务（原功能不变：默认 8002，PORT 环境变量优先）
  python agent.py web                      # 同上
  python agent.py BTCUSDT                  # 预警信号单：多平台公开行情（与网页同源同引擎）
  python agent.py BTCUSDT --live           # 各大平台合约公开K线（自动选路）
  python agent.py BTCUSDT --skill          # 官方 CLI（binance-cli request）直取
  python agent.py BTCUSDT --official       # 官方开源数据仓库归档K线（T+1，免代理）
  python agent.py "用官方归档看看 BTCUSDT"   # 自然语言：自动选通道

说明
  · 判定引擎与网页是同一段代码（sources.py 的 calculate 是纯函数：各通道把K线直接喂给
    引擎原函数，一行不改；网页版则由 analyze 走同一入口）。
  · 命令行不做演示数据降级：取不到数就如实报错退出（网页版保留演示降级并明确标注）。
  · 不构成投资建议。
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import sources

# ---------------------------------------------------------------- 通道标识
CHANNEL_LABELS = {
    "market": "多平台公开行情（Hyperliquid → OKX → Bybit 三连，与网页同源同引擎）",
    "fapi": "各大平台合约公开K线（自动选路：直连 / 本机代理端口）",
    "cli": "官方 CLI（binance-cli request）直取合约公开K线",
    "archive": "各大平台官方开源数据仓库（1h K线月度 + 日度归档，T+1）",
}

ARCHIVE_BASE = "https://data.binance.vision"
FACE_PREFIXES = ["", "1000", "10000", "100000"]  # 官方归档的面值币前缀（SHIBUSDT 实际是 1000SHIBUSDT）
NEED = 720          # 引擎的 30 天基准口径需要 720 根 1h K线
VPN_PORTS = [7897, 7890, 10809, 2080, 1080, 8888]  # 本机常见代理端口（合约接口直连失败时逐个试）
UA = {"User-Agent": "bolu-yidong-agent/1.0", "Accept": "application/json"}

# 直连 opener（空代理 = 绕过系统代理设置）与系统默认 opener（读环境变量代理）双保险
OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OPENER_SYSTEM = urllib.request.build_opener()


def say(msg=""):
    sys.stderr.write(str(msg) + "\n")


# ------------------------------------------------------------ 自然语言入口

KNOWN_BASES = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
               "DOT", "LTC", "TRX", "TON", "PEPE", "SHIB", "WIF", "OP", "ARB", "NEAR"]


def channel_from_intent(text):
    """自然语言 → 通道。识别不出就返回 None（静默走默认），绝不乱猜。"""
    t = text.lower()
    if any(k in t for k in ["官方技能", "官方 cli", "官方cli", "binance-cli", "skill"]):
        return "skill"
    if any(k in t for k in ["官方开源", "开源数据", "归档", "历史数据", "t+1", "免代理"]):
        return "official"
    if any(k in t for k in ["实时", "最新", "强制刷新", "立刻", "刚刚"]):
        return "live"
    return None


def symbol_from_intent(text):
    """从一句话里找合约交易对：先找明写的 XXXUSDT，再认常见币种名。找不到返回 None。"""
    m = re.search(r"([A-Z0-9]{2,12}USDT)", text.upper())
    if m:
        return m.group(1)
    up = text.upper()
    for base in KNOWN_BASES:
        if re.search(rf"\b{base}\b", up):
            return base + "USDT"
    return None


# ------------------------------------------------------------ 各大平台合约接口（--live 用）

def fapi_get(path, timeout=12):
    """合约公开K线：先直连，失败逐个试本机常见代理端口。
    成功返回 (数据, 路径描述)；全部失败抛 RuntimeError（如实报错，绝不降级）。"""
    last = None
    for proxy in [None] + [f"http://127.0.0.1:{p}" for p in VPN_PORTS]:
        try:
            req = urllib.request.Request("https://fapi.binance.com" + path, headers=UA)
            handler = urllib.request.ProxyHandler(
                {} if proxy is None else {"http": proxy, "https": proxy})
            with urllib.request.build_opener(handler).open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), (
                    "直连" if proxy is None else f"本机代理端口 {proxy.rsplit(':', 1)[-1]}")
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(
        f"各大平台合约接口不可达（直连与常见本机代理端口 {','.join(map(str, VPN_PORTS))} 均已尝试）：{last}")


def _klines_result(data, min_bars=100):
    """校验K线响应形态并直接喂给引擎原函数（list 布局索引 2/3/4 = high/low/close）。"""
    if not isinstance(data, list) or len(data) < min_bars:
        n = len(data) if isinstance(data, list) else type(data).__name__
        raise RuntimeError(f"合约接口返回K线数量异常：{n}（至少需要 {min_bars} 根）")
    return sources.calculate(data)


def collect_live(symbol):
    data, via_desc = fapi_get(f"/fapi/v1/klines?symbol={symbol}&interval=1h&limit=1000")
    result = _klines_result(data)
    meta = {"via": "fapi-route", "label": f"{CHANNEL_LABELS['fapi']}（{via_desc}）",
            "notes": [f"取回 {len(data)} 根 1h K线，直接喂给引擎原函数",
                      "各大平台合约行情与多平台现货口径略有差异，波动率数值会有小幅不同，趋势一致"]}
    return result, meta


# ------------------------------------------------------------ 默认通道（与网页同源）

def collect_market(symbol):
    """多平台三连（Hyperliquid → OKX → Bybit）：直接调用引擎原函数。
    注意绕开 analyze() —— 它内含演示数据降级，命令行不做降级，取不到就报错。"""
    rows, src = sources.fetch_market_candles(symbol)
    result = sources.calculate(rows)
    meta = {"via": "market-route", "label": f"{CHANNEL_LABELS['market']} · 本次实际来源：{src}",
            "notes": ["与网页版同源同引擎：同一批K线喂给同一个 calculate 函数，数值完全一致",
                      "45 秒缓存只存在于网页版；命令行每次现取"]}
    return result, meta


# ------------------------------------------------------------ 官方 CLI 通道

def cli_get(url):
    """官方 CLI 的 request 子命令。CLI 不在 PATH 就如实抛错，由上层回退。"""
    cli = shutil.which("binance-cli")
    if not cli:
        raise FileNotFoundError("本机未安装官方 CLI（binance-cli）")
    proc = subprocess.run([cli, "request", "GET", url], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(f"官方 CLI 请求失败：{(proc.stderr or proc.stdout or '').strip()[:200]}")
    return json.loads(proc.stdout)


def collect_skill(symbol):
    """官方 CLI 直取合约公开K线。CLI 失败 → 如实回退到自动选路，并标注 requestedSource。"""
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit=1000"
    try:
        data = cli_get(url)
        if not isinstance(data, list) or len(data) < 100:
            n = len(data) if isinstance(data, list) else type(data).__name__
            raise RuntimeError(f"官方 CLI 返回K线数量异常：{n}")
    except Exception as exc:
        say(f"（官方 CLI 不可用：{exc}）")
        say("（如实回退：改走合约公开K线自动选路，通道标识会写明真实路径）")
        try:
            result, meta = collect_live(symbol)
        except Exception as exc2:
            raise RuntimeError(f"官方 CLI 与合约公开接口均不可用：{exc2}") from exc2
        meta["requestedSource"] = "skill"
        meta["notes"].insert(0, "本次请求的是官方 CLI 通道，但 CLI 不可用，已如实回退到自动选路并标注")
        return result, meta
    result = sources.calculate(data)
    meta = {"via": "binance-cli", "label": CHANNEL_LABELS["cli"],
            "notes": [f"本次经官方 CLI（binance-cli request）直取合约公开K线，{len(data)} 根全部成功"]}
    return result, meta


# ------------------------------------------------------------ 官方归档通道（T+1，免代理）

def urllib_req(url, method="GET"):
    return urllib.request.Request(url, headers=UA, method=method)


def _archive_open(req, timeout):
    """归档下载：先直连，失败走系统默认（环境变量代理）。"""
    last = None
    for opener in (OPENER_DIRECT, OPENER_SYSTEM):
        try:
            return opener.open(req, timeout=timeout)
        except Exception as exc:
            last = exc
    raise last


def _head_exists(url):
    try:
        _archive_open(urllib_req(url, method="HEAD"), 15).close()
        return True
    except Exception:
        return False


def _fetch_zip_rows(url):
    """下载归档 zip，返回 [(时间戳毫秒, CSV行list), ...]，自动兼容毫秒时间戳与 ISO 两种 open_time 格式。"""
    with _archive_open(urllib_req(url), 30) as resp:
        zf = zipfile.ZipFile(io.BytesIO(resp.read()))
        text = zf.read(zf.namelist()[0]).decode("utf-8")
    rows = []
    for line in text.strip().splitlines():
        cells = line.split(",")
        if cells[0].strip().strip('"').lower() in ("open_time", "opentime"):  # 表头（新归档带表头）
            continue
        rows.append((_parse_open_time(cells[0]), cells))
    return rows


def _parse_open_time(raw):
    raw = raw.strip().strip('"')
    if raw.isdigit():
        return int(raw)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return int(datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析 open_time：{raw}")


def collect_official(symbol):
    """官方开源数据仓库：月度 + 日度 1h K线拼到 ≥720 根，直接喂给引擎原函数。"""
    today = datetime.now(timezone.utc).date()
    notes = []

    # 1) 锚定日期：从昨天往前找第一个「1h 日度K线归档存在」的日子（T+1），顺带探测面值币前缀
    anchor, actual, used_prefix = None, symbol, None
    for back in range(1, 8):
        d = today - timedelta(days=back)
        for prefix in FACE_PREFIXES:
            candidate = prefix + symbol
            url = (f"{ARCHIVE_BASE}/data/futures/um/daily/klines/{candidate}/1h/"
                   f"{candidate}-1h-{d.isoformat()}.zip")
            if _head_exists(url):
                anchor, actual, used_prefix = d, candidate, prefix
                break
        if anchor:
            break
    if not anchor:
        raise RuntimeError(
            f"官方归档里找不到 {symbol} 的 1h 日度K线文件（含面值币前缀都试过），请确认合约交易对名称（如 BTCUSDT）")
    if used_prefix:
        notes.append(f"官方归档使用面值币代码：{symbol} 对应 {actual}，已自动对应")

    # 2) 日度归档：锚定日往前收到当月 1 号，连续缺 3 个文件就停（更早的缺口由月度补齐）
    daily_rows, used_days, missing_days = [], [], []
    d, misses = anchor, 0
    while True:
        url = (f"{ARCHIVE_BASE}/data/futures/um/daily/klines/{actual}/1h/"
               f"{actual}-1h-{d.isoformat()}.zip")
        try:
            daily_rows.extend(_fetch_zip_rows(url))
            used_days.append(d.isoformat())
            misses = 0
        except Exception:
            missing_days.append(d.isoformat())
            misses += 1
            if misses >= 3:
                break
        if d.day == 1:
            break
        d -= timedelta(days=1)

    # 3) 月度归档：从锚定月的上一个整月往前，直到凑够 720 根（最多试 4 个月）
    monthly_rows, used_months, missing_months = [], [], []
    y, m = anchor.year, anchor.month - 1
    if m == 0:
        y, m = y - 1, 12
    need = NEED - len(daily_rows)
    tries = 0
    while need > 0 and tries < 4:
        tries += 1
        url = (f"{ARCHIVE_BASE}/data/futures/um/monthly/klines/{actual}/1h/"
               f"{actual}-1h-{y:04d}-{m:02d}.zip")
        try:
            batch = _fetch_zip_rows(url)
            monthly_rows.extend(batch)
            used_months.append(f"{y:04d}-{m:02d}")
            need -= len(batch)
        except Exception:
            missing_months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12

    # 4) 合并去重（日度覆盖重叠的月度行）→ 按时间排序 → 直接喂引擎原函数
    merged = {}
    for ts, cells in monthly_rows + daily_rows:
        merged[ts] = cells
    ordered = [merged[ts] for ts in sorted(merged)]
    if len(ordered) < 200:
        raise RuntimeError(
            f"官方归档拼出的K线太少（{len(ordered)} 根，至少需要 200 根才能算波动率），如实报错")
    result = sources.calculate(ordered)

    # 5) 诚实说明
    notes.insert(0, f"归档是 T+1：本信号单截至 {anchor.isoformat()} 收盘口径（1h 整点K线），不是此刻实时值")
    if len(ordered) < NEED:
        notes.append(f"归档拼出 {len(ordered)} 根（不足 720 根），30 天基准按现有样本计算，口径略有放宽")
    else:
        notes.append(f"归档拼出 {len(ordered)} 根 1h K线，满足 30 天基准口径")
    if missing_days:
        notes.append(f"日度K线缺档：{', '.join(missing_days[:4])}{'…' if len(missing_days) > 4 else ''}，如实跳过")
    if missing_months:
        notes.append(f"月度K线缺档：{', '.join(missing_months)}，如实跳过")

    meta = {"via": "official-archive",
            "label": f"{CHANNEL_LABELS['archive']}（截至 {anchor.isoformat()}）",
            "archive": {"anchor": anchor.isoformat(), "daysUsed": used_days,
                        "missingDays": missing_days, "monthsUsed": used_months,
                        "missingMonths": missing_months, "candles": len(ordered),
                        "actualSymbol": actual if actual != symbol else None},
            "notes": notes}
    return result, meta


# ------------------------------------------------------------ 预警信号单输出（第 8 套：气象预警体）

LEVEL_DOTS = {"平静": "●", "正常": "●●", "活跃": "●●●", "剧烈": "●●●●"}


def alert_reasons(r):
    """如实列出是哪条线触发的等级（而不是笼统说「超阈值」）。"""
    reasons = []
    if r["level"] == "剧烈":
        if r["ratio"] >= 1.8:
            reasons.append(f"近7天波动率放大达基准的 {r['ratio']} 倍（预警线 1.8 倍）")
        if r["hv7"] >= 120:
            reasons.append(f"近7天年化波动率 {r['hv7']}%（预警线 120%）")
    elif r["level"] == "活跃":
        if r["ratio"] >= 1.3:
            reasons.append(f"近7天波动率放大达基准的 {r['ratio']} 倍（活跃线 1.3 倍）")
        if r["hv7"] >= 70:
            reasons.append(f"近7天年化波动率 {r['hv7']}%（活跃线 70%）")
    elif r["level"] == "正常":
        reasons.append(f"近7天年化波动率 {r['hv7']}%（达到正常线 35%，未到活跃线 70%）")
    else:
        reasons.append(f"近7天年化波动率 {r['hv7']}%（低于正常线 35%）")
    return reasons


def source_of(meta):
    """通道标识里的 source 字段：requestedSource（如实回退标注）优先，其余按 via 映射。"""
    if meta.get("requestedSource"):
        return meta["requestedSource"]
    return {"binance-cli": "skill", "official-archive": "official",
            "fapi-route": "live", "market-route": "realtime"}.get(meta["via"], "realtime")


def print_report(symbol, result, meta, generated_at):
    line = "─" * 58
    burst = "  ⚠ 波动率爆发" if result["alert"] else ""
    say("")
    say(f"  {line}")
    say(f"   波动率异动预警 · 预警信号单    {generated_at[:16].replace('T', ' ')} UTC")
    say(f"  {line}")
    say(f"   观测站    {meta['label']}")
    say(f"   观测目标  {symbol}")
    say(f"   预警等级  {LEVEL_DOTS[result['level']]} {result['level']}{burst}")
    say(f"  {line}")
    say("   观测数据")
    say(f"   · 近7天年化波动率   {result['hv7']:>8.2f}%   （近30天 {result['hv30']:.2f}%）")
    say(f"   · 相对基准放大      {result['ratio']:>8.2f} 倍  （基准 {result['baseline']:.2f}%）")
    say(f"   · ATR（14小时）     {result['atr']:>8.3f}%")
    curve = result.get("curve") or []
    if curve:
        say(f"   · 逐日波动曲线      {' → '.join(str(x) for x in curve[-7:])}")
    say("   预警依据")
    for reason in alert_reasons(result):
        say(f"   · {reason}")
    say("   防灾提示（别过度解读）")
    say("   · 预警说的是「运动速度」不是方向：波动率升高不等于要看跌，也不等于要看涨")
    say("   · 波动率爆发时段杠杆和止损更容易被异常波动扫到：重检杠杆、仓位与止损设置")
    for n in meta.get("notes", []):
        say(f"   · {n}")
    say("   · 研判仅供观察与研究，不构成投资建议")
    say("")


def build_payload(symbol, result, meta, generated_at):
    return {
        "source": source_of(meta),
        "via": meta["via"],
        "dataSource": meta["label"],
        "generatedAt": generated_at,
        "symbol": symbol,
        "result": result,          # 引擎原样输出（hv7/hv30/baseline/ratio/atr/curve/level/alert），一行未改
        "archive": meta.get("archive"),
        "requestedSource": meta.get("requestedSource"),
        "honesty": meta.get("notes", []) + [
            "研判仅供观察与研究，不构成投资建议",
        ],
    }


# ---------------------------------------------------------------- 入口

def run_web(host, port):
    """网页服务：与改造前完全一致（复用 sources.Handler；PORT 环境变量优先由调用方保证）。"""
    from sources import Handler
    print(f"Volatility agent: http://0.0.0.0:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="波动率异动预警智能体（四通道：多平台行情 / 合约实时 / 官方CLI / 官方归档；不带参数起网页）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例：python agent.py BTCUSDT --official   |   python agent.py "用官方归档看看 ETHUSDT"   |   python agent.py --port 8002（网页）')
    parser.add_argument("target", nargs="?", default=None,
                        help="留空或 web（启动网页服务）/ 合约交易对如 BTCUSDT / 一句自然语言")
    parser.add_argument("--host", default="0.0.0.0", help="网页模式监听地址")
    parser.add_argument("--port", type=int, default=8002, help="网页模式监听端口（PORT 环境变量优先）")
    parser.add_argument("--live", action="store_true", help="各大平台合约公开K线通道（自动选路）")
    parser.add_argument("--skill", action="store_true", help="官方 CLI（binance-cli）通道")
    parser.add_argument("--official", action="store_true", help="官方开源数据仓库通道（T+1，免代理）")
    parser.add_argument("--json", action="store_true", help="stdout 只输出纯 JSON，进度与信号单走 stderr")
    args = parser.parse_args()

    # 不带参数或 web → 网页服务（部署环境用：PORT 环境变量优先于 --port，与改造前一致）
    if args.target is None or args.target.strip().lower() == "web":
        port = int(os.environ.get("PORT", str(args.port)))
        run_web(args.host, port)
        return 0

    explicit = [name for name, flag in (("--live", args.live), ("--skill", args.skill),
                                        ("--official", args.official)) if flag]
    if len(explicit) > 1:
        say(f"参数错误：{', '.join(explicit)} 只能选一个通道（用 --help 查看用法）")
        return 2

    target = args.target.strip()
    channel = explicit[0].lstrip("-") if explicit else None  # live / skill / official
    symbol = None
    nl_note = None
    if re.fullmatch(r"[A-Za-z0-9]{2,16}", target):
        # 纯字母数字：当合约交易对用。BTC → BTCUSDT；写错的币名会原样传给接口、由接口如实报错，
        # 绝不悄悄替换成 BTCUSDT（否则用户会把自己输错的币当成 BTC 的结论）
        symbol = target.upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
    else:
        channel = channel or channel_from_intent(target)
        symbol = symbol_from_intent(target)
        if symbol is None:
            symbol = "BTCUSDT"
            nl_note = "一句话里没认出币种，按默认 BTCUSDT 观测"

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if not args.json:
        say(f"波动率异动预警 · 通道：{channel or 'market（默认，与网页同源）'}")
        if nl_note:
            say(f"（{nl_note}）")

    try:
        if channel == "official":
            result, meta = collect_official(symbol)
        elif channel == "skill":
            result, meta = collect_skill(symbol)
        elif channel == "live":
            result, meta = collect_live(symbol)
        else:
            result, meta = collect_market(symbol)
    except Exception as exc:
        say(f"[失败] {type(exc).__name__}: {exc}")
        return 1

    payload = build_payload(symbol, result, meta, generated_at)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0
    print_report(symbol, result, meta, generated_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
