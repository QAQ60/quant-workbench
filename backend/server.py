"""
雨霖量化 - 量化选股工作台后端（真实数据版）
数据源：东方财富 push2 免费接口（全市场 A 股实时行情，覆盖选股所需全部字段）
字段映射：f2=最新价 f3=涨跌幅 f6=成交额 f8=换手率 f10=量比 f12=代码 f14=名称 f20=总市值
"""
import os, json, time, math, urllib.request, concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", "8080"))
WEB_DIR = os.environ.get("WEB_DIR", os.path.join(os.path.dirname(__file__), "..", "web"))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
EM_BASE = "https://push2.eastmoney.com/api/qt/clist/get"
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"  # 沪深A股（深主板/创业板/沪主板/科创板）
FIELDS = "f12,f14,f2,f3,f6,f8,f10,f20"

_cache = {"data": [], "ts": 0, "ttl": 30}  # 30秒缓存，避免频繁请求

def fetch_page(pn):
    url = f"{EM_BASE}?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs={FS}&fields={FIELDS}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    return d.get("data", {}).get("diff", []) or []

def load_market():
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < _cache["ttl"]:
        return _cache["data"]
    # 先拿总数确定页数
    first = fetch_page(1)
    # 并发拉全部页（约56页）
    allrows = list(first)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(fetch_page, pn) for pn in range(2, 58)]
        for f in futures:
            try:
                rows = f.result()
                if rows:
                    allrows.extend(rows)
            except Exception:
                pass
    # 去重（按代码）
    seen = set(); uniq = []
    for x in allrows:
        c = x.get("f12")
        if c and c not in seen:
            seen.add(c); uniq.append(x)
    _cache["data"] = uniq; _cache["ts"] = now
    return uniq

def num(v):
    return v if isinstance(v, (int, float)) else 0

def market_overview():
    rows = load_market()
    up = sum(1 for x in rows if num(x["f3"]) > 0)
    dn = sum(1 for x in rows if num(x["f3"]) < 0)
    fl = sum(1 for x in rows if num(x["f3"]) == 0)
    zt = sum(1 for x in rows if num(x["f3"]) >= 9.8)
    dt = sum(1 for x in rows if num(x["f3"]) <= -9.8)
    amt = sum(num(x["f6"]) for x in rows) / 1e8
    return {
        "up": up, "down": dn, "flat": fl, "zt": zt, "dt": dt, "total": len(rows),
        "amount": f"{amt:.0f}亿",
        "indices": [
            {"name": "上证指数", "val": "—", "chg": "实时", "down": False},
        ],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

def select_stocks(strategy="lin_gongzi"):
    """林公子弱转强：涨幅2-5% + 量比≥1.3 + 换手>3.5% + 市值≥60亿 + 排除ST"""
    rows = load_market()
    tradable = [x for x in rows if num(x["f2"]) > 0]
    non_st = [x for x in tradable if "ST" not in str(x["f14"]).upper()]
    hits = []
    for x in non_st:
        chg, lb, hs, mv = num(x["f3"]), num(x["f10"]), num(x["f8"]), num(x["f20"]) / 1e8
        if 2 <= chg <= 5 and lb >= 1.3 and hs > 3.5 and mv >= 60:
            hits.append({
                "code": x["f12"], "name": x["f14"], "price": num(x["f2"]),
                "chg": chg, "amount": f"{num(x['f6'])/1e8:.2f}亿",
                "turnover": hs, "vol_ratio": lb, "market_cap": f"{mv:.0f}亿",
                "ma20": "—", "rsi14": "—",  # 技术指标需历史K线，后续补
            })
    hits.sort(key=lambda x: -x["chg"])
    return {
        "strategy": strategy, "scanned": len(rows), "tradable": len(tradable),
        "excluded_st": len(tradable) - len(non_st), "prescreen": len(non_st),
        "hits": len(hits), "results": hits[:50],
        "desc": "标准N字弱转强：涨幅2%~5% + 量比≥1.3 + 换手>3.5% + 总市值≥60亿",
    }

def limit_up_alerts():
    """涨停预警：涨幅>5%未封板（炸板/临界冲板），按距涨停幅度排序"""
    rows = load_market()
    out = []
    for x in rows:
        chg = num(x["f3"])
        if 5 <= chg < 9.8:
            name = str(x["f14"])
            is_st = "ST" in name.upper()
            limit = 5 if is_st else (10 if not (str(x["f12"]).startswith("30") or str(x["f12"]).startswith("68")) else 20)
            gap = limit - chg
            if gap <= limit / 2:  # 距涨停不超过半幅
                board = "创业板" if str(x["f12"]).startswith("30") else ("科创板" if str(x["f12"]).startswith("68") else "主板")
                out.append({
                    "name": name, "code": x["f12"], "price": num(x["f2"]), "chg": chg,
                    "gap": round(gap, 2),
                    "type": "临界冲板" if gap <= 1.5 else "炸板待回封",
                    "meta": f"{board}·成交{num(x['f6'])/1e8:.2f}亿",
                })
    out.sort(key=lambda x: x["gap"])
    return out[:20]

def stock_list(kw=""):
    rows = load_market()
    if kw:
        rows = [x for x in rows if kw in str(x["f12"]) or kw in str(x["f14"])]
    return [{"code": x["f12"], "name": x["f14"], "price": num(x["f2"]), "chg": num(x["f3"])}
            for x in rows[:30]]

ROUTES = {
    "/api/market": lambda q: market_overview(),
    "/api/select": lambda q: select_stocks(q.get("strategy", ["lin_gongzi"])[0]),
    "/api/alerts": lambda q: limit_up_alerts(),
    "/api/stocks": lambda q: stock_list(q.get("kw", [""])[0]),
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"status": "ok", "datasource": "eastmoney", "cached": len(_cache["data"])})
        fn = ROUTES.get(u.path)
        if fn:
            try: return self._send(200, {"ok": True, "data": fn(parse_qs(u.query))})
            except Exception as e: return self._send(500, {"ok": False, "error": str(e)})
        # 静态文件（前端页面）
        if u.path in ("/", "/index.html"):
            p = os.path.join(WEB_DIR, "index.html")
            if os.path.exists(p):
                body = open(p, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
        self._send(404, {"error": "not found"})

if __name__ == "__main__":
    print(f"量化工作台启动 :{PORT} 数据源=东方财富实时行情")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
