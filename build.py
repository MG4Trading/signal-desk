#!/usr/bin/env python3
"""Signal Desk updater.

Runs about every 10 minutes on GitHub Actions (see loop.sh). Gathers news, markets, weather and
Environment Canada warnings, writes site/data.json, and sends push alerts
through ntfy.sh. Standard library only.
"""
import json, os, re, shutil, sys, time, html
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "site")
PREV = os.path.join(ROOT, "prev")
TZ = ZoneInfo("America/Toronto")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
NOW = time.time()
DRY = os.environ.get("SD_DRY_RUN") == "1"
PER_ANGLE = 8

cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, timeout=20, data=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


prev_data = load_json(os.path.join(PREV, "data.json"), {})
state = load_json(os.path.join(PREV, "state.json"), {})
state.setdefault("seen", [])
state.setdefault("ec_seen", [])
state.setdefault("wx_seen", [])

# ---------------------------------------------------------------- news
def feed_url(topic, f):
    gl = (f[2] or topic.get("gl") or "CA").upper()
    hl = f[3] or topic.get("hl") or "en"
    when = f[4] if len(f) > 4 and f[4] else "7d"
    lang = "en-" + gl if hl == "en" else hl
    q = urllib.parse.quote(f[1] + " when:" + when)
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={gl}:{hl}"


def tag(block, name):
    m = re.search(r"<" + name + r"[^>]*>([\s\S]*?)</" + name + ">", block)
    if not m:
        return ""
    s = m.group(1)
    s = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", s)
    return html.unescape(s).strip()


def fetch_feed(topic, f):
    last = None
    for attempt in range(2):
        try:
            xml = get(feed_url(topic, f))
            if "<rss" not in xml:
                raise ValueError("not a feed")
            break
        except Exception as e:  # retry once after a short pause
            last = e
            time.sleep(1.5)
    else:
        raise last
    must = re.compile(f[5], re.I) if len(f) > 5 and f[5] else None
    notre = re.compile(f[6], re.I) if len(f) > 6 and f[6] else None
    topic_not = re.compile(topic["exclude"], re.I) if topic.get("exclude") else None
    out = []
    for block in re.findall(r"<item>([\s\S]*?)</item>", xml):
        source = tag(block, "source")
        title = tag(block, "title")
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)]
        link = tag(block, "link")
        try:
            ts = parsedate_to_datetime(tag(block, "pubDate")).timestamp()
        except Exception:
            ts = 0
        if not title or not link.startswith("https://"):
            continue
        hay = title + " " + source
        if must and not must.search(title):
            continue
        if notre and notre.search(hay):
            continue
        if topic_not and topic_not.search(hay):
            continue
        out.append({"a": f[0], "t": title, "l": link, "s": source, "ts": int(ts)})
    return out


def gather_news():
    jobs = [(t, f) for t in cfg["topics"] for f in t["feeds"]]
    headlines = {t["id"]: [] for t in cfg["topics"]}
    failed = []
    prev_h = prev_data.get("headlines", {})
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [(t, f, ex.submit(fetch_feed, t, f)) for t, f in jobs]
        for t, f, fu in futs:
            try:
                headlines[t["id"]].extend(fu.result())
            except Exception as e:
                failed.append(t["id"] + ":" + f[0])
                # keep the last good stories for this feed so nothing disappears
                headlines[t["id"]].extend([x for x in prev_h.get(t["id"], []) if x.get("a") == f[0]])
    for tid, items in headlines.items():
        items.sort(key=lambda x: -x["ts"])
        seen, keep, per = set(), [], {}
        for it in items:
            key = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", it["t"].lower()).strip()[:80]
            if key in seen or it["l"] in seen:
                continue
            seen.add(key); seen.add(it["l"])
            # keep the newest few per category so quieter ones (like Ward 7) are not crowded out
            if per.get(it["a"], 0) >= PER_ANGLE:
                continue
            per[it["a"]] = per.get(it["a"], 0) + 1
            keep.append(it)
        headlines[tid] = keep[:120]
    log(f"news: {sum(len(v) for v in headlines.values())} stories, {len(failed)} feeds failed")
    return headlines, failed

# ---------------------------------------------------------------- markets
def gather_quotes():
    def one(pair):
        label, sym = pair
        last = None
        for host in ("query1", "query2"):
            try:
                txt = get(f"https://{host}.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym) + "?range=1d&interval=15m",
                          headers={"Accept": "application/json,text/plain,*/*", "Referer": "https://finance.yahoo.com/"})
                r = json.loads(txt)["chart"]["result"][0]
                break
            except Exception as e:
                last = e
        else:
            raise last
        meta = r["meta"]
        closes = [c for c in (r["indicators"]["quote"][0].get("close") or []) if isinstance(c, (int, float))]
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        price = meta.get("regularMarketPrice")
        step = max(1, len(closes) // 40)
        return {"label": label, "price": price, "pct": ((price - prev) / prev * 100) if prev and price else 0,
                "spark": [round(c, 4) for c in closes[::step]]}
    prev_q = {q["label"]: q for q in prev_data.get("quotes", []) if q.get("price") is not None}
    out, errors = [], []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for pair, fu in [(p, ex.submit(one, p)) for p in cfg["quotes"]]:
            try:
                out.append(fu.result())
            except Exception as e:
                errors.append(f"{pair[0]}: {e}")
                out.append(prev_q.get(pair[0], {"label": pair[0], "price": None}))
    if errors:
        log("quotes errors:", "; ".join(errors[:4]))
    # currencies: fall back to a free daily exchange-rate source when live quotes are unavailable
    if any(q.get("price") is None for q in out if q["label"] in ("USD/CAD", "CAD/EGP", "USD/EGP")):
        try:
            rates = json.loads(get("https://open.er-api.com/v6/latest/USD"))["rates"]
            fx = {"USD/CAD": rates["CAD"], "USD/EGP": rates["EGP"], "CAD/EGP": rates["EGP"] / rates["CAD"]}
            for q in out:
                if q.get("price") is None and q["label"] in fx:
                    q.update({"price": round(fx[q["label"]], 4), "pct": 0, "spark": [], "note": "daily rate"})
        except Exception as e:
            log("fx fallback failed:", e)
    return out

# ---------------------------------------------------------------- weather
WMO = {
    0: ("Clear", "☀️"), 1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Cloudy", "☁️"),
    45: ("Fog", "🌫️"), 48: ("Freezing fog", "🌫️"), 51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"),
    55: ("Heavy drizzle", "🌧️"), 56: ("Freezing drizzle", "🧊"), 57: ("Freezing drizzle", "🧊"),
    61: ("Light rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"), 66: ("Freezing rain", "🧊"),
    67: ("Freezing rain", "🧊"), 71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"), 80: ("Showers", "🌦️"), 81: ("Showers", "🌧️"), 82: ("Heavy showers", "🌧️"),
    85: ("Snow showers", "🌨️"), 86: ("Heavy snow showers", "❄️"), 95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm, hail", "⛈️"), 99: ("Thunderstorm, hail", "⛈️"),
}


def wx_text(code):
    return WMO.get(int(code), ("—", "🌡️"))


def wear_advice(day):
    lo, hi = day["feels_min"], day["max"]
    wet, snow = day["pop"] >= 50 and day["precip"] >= 1, day["snow"] >= 0.5 or int(day["code"]) in (71, 73, 75, 77, 85, 86)
    ice = int(day["code"]) in (56, 57, 66, 67)
    base = (
        "Heavy parka, thermal base layer, warm hat, gloves and a face covering" if lo < -10 else
        "Winter coat, hat, gloves and a scarf" if lo < 0 else
        "Warm coat with a sweater underneath" if lo < 6 else
        "Sweater or fleece with a medium jacket" if lo < 12 else
        "Long sleeves and a light jacket" if hi < 18 else
        "T-shirt, with a light layer for the morning and evening" if hi < 25 else
        "Light, breathable clothes"
    )
    tips = [base]
    if snow: tips.append("winter boots")
    if ice: tips.append("boots with good grip: icy surfaces")
    if wet and not snow: tips.append("umbrella or rain jacket")
    if day["wind"] >= 35: tips.append("windproof outer layer")
    if day["uv"] >= 6: tips.append("sunscreen and sunglasses")
    if hi - lo >= 10 and lo < 18: tips.append("dress in layers: big temperature swing")
    return tips


def gather_weather():
    loc = cfg["location"]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m,relative_humidity_2m,precipitation"
           "&hourly=temperature_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m,uv_index"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_min,apparent_temperature_max,"
           "precipitation_probability_max,precipitation_sum,snowfall_sum,wind_speed_10m_max,wind_gusts_10m_max,uv_index_max,sunrise,sunset"
           "&timezone=America%%2FToronto&forecast_days=7") % (loc["lat"], loc["lon"])
    d = json.loads(get(url))
    c = d["current"]
    days = []
    dd = d["daily"]
    for i, date in enumerate(dd["time"]):
        day = {
            "date": date, "code": dd["weather_code"][i], "max": round(dd["temperature_2m_max"][i]),
            "min": round(dd["temperature_2m_min"][i]), "feels_min": round(dd["apparent_temperature_min"][i]),
            "feels_max": round(dd["apparent_temperature_max"][i]),
            "pop": dd["precipitation_probability_max"][i] or 0, "precip": round(dd["precipitation_sum"][i] or 0, 1),
            "snow": round(dd["snowfall_sum"][i] or 0, 1), "wind": round(dd["wind_speed_10m_max"][i] or 0),
            "gust": round(dd["wind_gusts_10m_max"][i] or 0), "uv": round(dd["uv_index_max"][i] or 0, 1),
            "sunrise": dd["sunrise"][i][-5:], "sunset": dd["sunset"][i][-5:],
        }
        day["label"], day["icon"] = wx_text(day["code"])
        day["wear"] = wear_advice(day)
        days.append(day)
    now_local = datetime.now(TZ).strftime("%Y-%m-%dT%H:00")
    h = d["hourly"]
    start = next((i for i, t in enumerate(h["time"]) if t >= now_local), 0)
    hours = []
    for i in range(start, min(start + 24, len(h["time"]))):
        lab, ic = wx_text(h["weather_code"][i])
        hours.append({"t": h["time"][i][-5:], "temp": round(h["temperature_2m"][i]), "pop": h["precipitation_probability"][i] or 0,
                      "icon": ic, "label": lab, "feels": round(h["apparent_temperature"][i]),
                      "wind": round(h["wind_speed_10m"][i] or 0), "uv": round(h["uv_index"][i] or 0)})
    lab, ic = wx_text(c["weather_code"])
    current = {"temp": round(c["temperature_2m"]), "feels": round(c["apparent_temperature"]), "label": lab, "icon": ic,
               "wind": round(c["wind_speed_10m"]), "gust": round(c.get("wind_gusts_10m") or 0),
               "humidity": c.get("relative_humidity_2m")}
    return {"current": current, "hours": hours, "days": days}


def gather_ec_alerts():
    b = cfg["location"]["alert_bbox"]
    url = ("https://api.weather.gc.ca/collections/weather-alerts/items?f=json&lang=en&limit=50&bbox=%s,%s,%s,%s" % tuple(b))
    d = json.loads(get(url, timeout=25))
    out, seen = [], set()
    now = datetime.now(timezone.utc)
    for f in d.get("features", []):
        p = f.get("properties", {})
        try:
            exp = datetime.fromisoformat(p.get("expiration_datetime", "").replace("Z", "+00:00"))
            if exp < now:
                continue
        except Exception:
            pass
        name = p.get("alert_short_name_en") or p.get("alert_name_en") or "Weather alert"
        key = (p.get("alert_code"), p.get("publication_datetime"))
        if name in seen:
            continue
        seen.add(name)
        text = (p.get("alert_text_en") or "").strip()
        first = text.split("\n\n")[0][:300]
        out.append({"id": str(f.get("id") or key), "name": name, "type": p.get("alert_type", ""),
                    "summary": first, "issued": p.get("publication_datetime"), "expires": p.get("expiration_datetime")})
    order = {"warning": 0, "watch": 1, "advisory": 2, "statement": 3}
    out.sort(key=lambda a: order.get(a["type"], 4))
    return out

# ---------------------------------------------------------------- homes for rent
def next_data(h):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    return json.loads(m.group(1)) if m else {}


def gather_rentals(topic):
    """3-bed rentals around Oakville from a public Kijiji search, refreshed about once an hour."""
    r = topic["rentals"]
    prev = prev_data.get("rentals") or {}
    if prev.get("at") and NOW - prev["at"] < 55 * 60 and prev.get("listings"):
        return prev
    b, out, seen, pages_ok = r["bbox"], [], set(), 0
    for p in range(1, r.get("pages", 3) + 1):
        url = r["search"].format(page="" if p == 1 else f"page-{p}/")
        try:
            ap = next_data(get(url, timeout=25)).get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
        except Exception as e:
            log("rentals page", p, e); break
        if not ap:
            break
        pages_ok += 1
        for k, v in ap.items():
            if not k.startswith("RealEstateListing:"):
                continue
            try:
                at = {a["canonicalName"]: a["canonicalValues"] for a in (v.get("attributes") or {}).get("all", [])}
                beds = float((at.get("numberbedrooms") or ["0"])[0])
                baths = float((at.get("numberbathrooms") or ["0"])[0]) / 10
                loc = v.get("location") or {}
                co = loc.get("coordinates") or {}
                lat, lon = co.get("latitude"), co.get("longitude")
                price = ((v.get("price") or {}).get("amount") or 0) / 100
            except Exception:
                continue
            u = v.get("url", "")
            if not u or u in seen or lat is None or lon is None:
                continue
            if not (r["beds_min"] <= beds <= r["beds_max"] and baths >= r["baths_min"]):
                continue
            if not (b[1] <= lat <= b[3] and b[0] <= lon <= b[2]):
                continue
            if r.get("not_places") and re.search(r["not_places"], (loc.get("name") or "") + " " + (loc.get("address") or ""), re.I):
                continue
            if not (1500 <= price <= 20000):
                continue
            seen.add(u)
            name = loc.get("name") or "Oakville"
            hood = re.sub(r"^Oakville\s*\(\w+\s*", "", name).rstrip(")") if "(" in name else ""
            try:
                listed = int(datetime.fromisoformat((v.get("activationDate") or v.get("sortingDate")).replace("Z", "+00:00")).timestamp())
            except Exception:
                listed = 0
            kind = ((at.get("unittype") or [""])[0] or "").replace("-", " ")
            out.append({"t": v.get("title", "").strip()[:90], "l": u, "p": round(price), "beds": beds, "baths": baths,
                        "kind": "" if kind.lower() == "not available" else kind, "hood": hood, "ts": listed,
                        "img": (v.get("imageUrls") or [""])[0]})
        time.sleep(1.2)
    if not pages_ok:
        log("rentals: no pages loaded, keeping last results")
        return prev or {"at": 0, "listings": [], "stats": None, "hist": []}
    out.sort(key=lambda x: -x["ts"])
    prices = sorted(x["p"] for x in out)
    stats = None
    if prices:
        mid = len(prices) // 2
        med = prices[mid] if len(prices) % 2 else round((prices[mid - 1] + prices[mid]) / 2)
        stats = {"median": med, "low": prices[0], "high": prices[-1], "count": len(prices)}
    hist = [h for h in (prev.get("hist") or []) if h[0] != datetime.now(TZ).strftime("%Y-%m-%d")]
    if stats:
        hist.append([datetime.now(TZ).strftime("%Y-%m-%d"), stats["median"], stats["count"]])
    log(f"rentals: {len(out)} matching listings from {pages_ok} pages")
    return {"at": int(NOW), "listings": out[:40], "stats": stats, "hist": hist[-180:],
            "filter": f"{r['beds_min']:g}+ bed, {r['baths_min']:g}+ bath, within about 8 km of Oakville"}

# ---------------------------------------------------------------- events
def gather_events(topic):
    """Upcoming Meetup events for Oakville, Mississauga and the GTA, refreshed about once an hour."""
    prev = prev_data.get("events") or {}
    if prev.get("at") and NOW - prev["at"] < 55 * 60 and prev.get("items"):
        prev["items"] = [e for e in prev["items"] if e["start"] > NOW - 3600]
        return prev
    notre = re.compile(topic.get("meetup_not") or r"^$", re.I)
    items, seen, ok = [], set(), 0

    def one(q):
        cat, loc, kw = q
        url = "https://www.meetup.com/find/?" + urllib.parse.urlencode({"location": loc, "source": "EVENTS", "keywords": kw})
        h = get(url, timeout=25)
        got = []
        for blk in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', h, re.S):
            try:
                d = json.loads(blk)
            except Exception:
                continue
            for e in (d if isinstance(d, list) else [d]):
                if isinstance(e, dict) and e.get("@type") == "Event":
                    got.append((cat, loc, e))
        return got

    with ThreadPoolExecutor(max_workers=3) as ex:
        results = []
        for q, fu in [(q, ex.submit(one, q)) for q in topic["meetup"]]:
            try:
                results.extend(fu.result()); ok += 1
            except Exception as e:
                log("meetup", q, e)
    for cat, loc, e in results:
        u = (e.get("url") or "").split("?")[0]
        name = (e.get("name") or "").replace("\n", " ").strip()
        org = e.get("organizer", {}).get("name", "") if isinstance(e.get("organizer"), dict) else ""
        if not u or not name or u in seen or notre.search(name + " " + org):
            continue
        try:
            start = int(datetime.fromisoformat(e["startDate"].replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        if start < NOW - 3600 or start > NOW + 60 * 86400:
            continue
        seen.add(u)
        online = "Online" in (e.get("eventAttendanceMode") or "")
        if online and topic.get("in_person_only"):
            continue
        where = ""
        l = e.get("location")
        if isinstance(l, dict):
            where = ((l.get("address") or {}) if isinstance(l.get("address"), dict) else {}).get("addressLocality") or ""
        items.append({"c": cat, "t": name[:110], "l": u, "start": start, "org": org[:60],
                      "where": "Online" if online else (where.strip().title() or loc.split("--")[-1]),
                      "area": loc.split("--")[-1]})
    if not ok:
        log("events: Meetup unavailable, keeping last results")
        prev["items"] = [e for e in prev.get("items", []) if e["start"] > NOW - 3600]
        return prev or {"at": 0, "items": []}
    items.sort(key=lambda x: x["start"])
    log(f"events: {len(items)} upcoming")
    return {"at": int(NOW), "items": items[:90]}

# ---------------------------------------------------------------- GO (for the morning briefing)
def next_trains(n=3):
    go = load_json(os.path.join(ROOT, "go.json"), None)
    if not go:
        return []
    now = datetime.now(TZ)
    dow = (now.weekday() + 1) % 7  # Sun=0
    mins = now.hour * 60 + now.minute
    key = ("we_" if dow in (0, 6) else "wk_") + "east"
    def tm(t):
        h, m_ = map(int, t.split(":")); v = h * 60 + m_
        return v + 1440 if v < 180 else v
    ok = lambda d: (not d) or (d == "MTh" and 1 <= dow <= 4) or (d == "F" and dow == 5) or (d == "Sa" and dow == 6) or (d == "Su" and dow == 0)
    return [t for t in go[key] if ok(t[3]) and tm(t[0]) >= mins][:n]


def fmt12(t):
    h, m = map(int, t.split(":")); h %= 24
    return f"{h % 12 or 12}:{m:02d} {'p.m.' if h >= 12 else 'a.m.'}"

# ---------------------------------------------------------------- push alerts
def push(title, message, tags=None, priority=3, click=None):
    topic = cfg["alerts"].get("ntfy_topic")
    if not topic:
        return
    body = {"topic": topic, "title": title[:250], "message": message[:3500], "priority": priority}
    if tags: body["tags"] = tags
    if click: body["click"] = click
    if DRY:
        log("PUSH (dry run):", json.dumps(body, ensure_ascii=False)[:400]); return
    try:
        get("https://ntfy.sh", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, timeout=15)
        log("pushed:", title)
    except Exception as e:
        log("push failed:", e)


def site_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def run_alerts(headlines, weather, ec):
    first_run = not state.get("initialized")
    seen = set(state["seen"])
    names = {t["id"]: t["name"] for t in cfg["topics"]}
    fresh = []
    for tid in ("oakville",):
        for it in headlines.get(tid, []):
            if it["a"] not in cfg["alerts"]["breaking_angles"] or it["l"] in seen:
                continue
            seen.add(it["l"])
            if NOW - it["ts"] < 2 * 3600:
                fresh.append((tid, it))
    if not first_run:
        for tid, it in fresh[: cfg["alerts"]["max_breaking_per_run"]]:
            tags = {"GO alerts": ["train"], "Accidents": ["rotating_light"], "Traffic": ["car"], "Road closures": ["construction"]}.get(it["a"], ["newspaper"])
            push(f"{names[tid]} · {it['a']}", f"{it['t']} ({it['s']})", tags=tags, priority=4, click=it["l"])
    state["seen"] = list(seen)[-800:]

    # Environment Canada warnings
    ec_seen = set(state["ec_seen"])
    for a in ec:
        if a["id"] in ec_seen:
            continue
        ec_seen.add(a["id"])
        if not first_run:
            pr = 5 if a["type"] == "warning" else 4
            push(f"⚠️ {a['name']} for Oakville", a["summary"], tags=["warning"], priority=pr,
                 click="https://weather.gc.ca/en/location/index.html?coords=43.467,-79.688")
    state["ec_seen"] = list(ec_seen)[-200:]

    # heads-up for heavy rain, snow or ice today or tomorrow
    if weather:
        wx_seen = set(state["wx_seen"])
        for d in weather["days"][:2]:
            reasons = []
            if d["precip"] >= 15 and d["pop"] >= 60: reasons.append(f"heavy rain, about {d['precip']:.0f} mm")
            if d["snow"] >= 5: reasons.append(f"snow, about {d['snow']:.0f} cm")
            if int(d["code"]) in (56, 57, 66, 67): reasons.append("freezing rain or drizzle")
            if d["gust"] >= 70: reasons.append(f"wind gusts near {d['gust']} km/h")
            key = d["date"] + ":" + ",".join(reasons)
            if reasons and key not in wx_seen:
                wx_seen.add(key)
                if not first_run:
                    when = "Today" if d is weather["days"][0] else "Tomorrow"
                    push(f"{when} in Oakville: " + reasons[0], "Expect " + "; ".join(reasons) + ". Wear: " + "; ".join(d["wear"]) + ".",
                         tags=["umbrella"], priority=4, click=site_url())
        state["wx_seen"] = list(wx_seen)[-60:]

    # morning briefing
    now = datetime.now(TZ)
    hh, mm = map(int, cfg["alerts"]["morning_briefing"].split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    if target <= now < target + timedelta(hours=2) and state.get("last_morning") != today:
        state["last_morning"] = today
        lines = []
        if weather:
            c, d = weather["current"], weather["days"][0]
            lines.append(f"{d['icon']} {d['label']}, now {c['temp']}° (feels {c['feels']}°). High {d['max']}°, low {d['min']}°, {d['pop']}% chance of rain.")
            lines.append("Wear: " + "; ".join(d["wear"]) + ".")
        if ec:
            lines.append("⚠️ " + ", ".join(a["name"] for a in ec[:2]))
        trains = next_trains(3)
        if trains:
            lines.append("Next GO to Union: " + ", ".join(fmt12(t[0]) for t in trains))
        top = [it for it in headlines.get("oakville", []) if NOW - it["ts"] < 24 * 3600][:2]
        top += [it for it in headlines.get("canada", []) if NOW - it["ts"] < 24 * 3600][:1]
        for it in top:
            lines.append("• " + it["t"])
        push("Good morning, Oakville · " + now.strftime("%a %b %-d"), "\n".join(lines), tags=["sunrise"], priority=3, click=site_url())
    state["initialized"] = True

# ---------------------------------------------------------------- icons
def make_icons():
    if all(os.path.exists(os.path.join(ROOT, n)) for n in ("icon-192.png", "icon-512.png", "apple-touch-icon.png")):
        return
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        log("Pillow not installed; skipping icons"); return
    fonts = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for n, name in ((192, "icon-192.png"), (512, "icon-512.png"), (180, "apple-touch-icon.png")):
        im = Image.new("RGB", (n, n), "#0B7A84"); d = ImageDraw.Draw(im)
        cx, cy = n * 0.5, n * 0.6
        for r in (0.16, 0.28, 0.40):
            R = n * r; d.arc([cx - R, cy - R, cx + R, cy + R], 215, 325, fill="#FFFFFF", width=max(2, int(n * 0.045)))
        d.ellipse([cx - n * 0.05, cy - n * 0.05, cx + n * 0.05, cy + n * 0.05], fill="#FFFFFF")
        try:
            f = ImageFont.truetype(fonts[0], int(n * 0.13)); d.text((n * 0.5, n * 0.84), "SD", fill="#D9EEF0", font=f, anchor="mm")
        except Exception:
            pass
        im.save(os.path.join(ROOT, name))


# ---------------------------------------------------------------- main
def main():
    make_icons()
    t0 = time.time()
    headlines, failed = gather_news()
    try:
        quotes = gather_quotes()
    except Exception as e:
        log("quotes failed", e); quotes = prev_data.get("quotes", [])
    try:
        weather = gather_weather()
    except Exception as e:
        log("weather failed", e); weather = prev_data.get("weather")
    try:
        ec = gather_ec_alerts()
    except Exception as e:
        log("EC alerts failed", e); ec = prev_data.get("warnings", [])
    rentals = events = None
    for t in cfg["topics"]:
        if t.get("rentals"):
            try:
                rentals = gather_rentals(t)
            except Exception as e:
                log("rentals failed", e); rentals = prev_data.get("rentals")
        if t.get("meetup"):
            try:
                events = gather_events(t)
            except Exception as e:
                log("events failed", e); events = prev_data.get("events")
    data = {
        "generated": int(time.time()),
        "interval": int(os.environ.get("SD_INTERVAL", "600")),
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "topics": [{"id": t["id"], "name": t["name"], "links": t.get("links", [])} for t in cfg["topics"]],
        "headlines": headlines, "quotes": quotes, "weather": weather, "warnings": ec,
        "rentals": rentals, "events": events,
        "failed": failed, "location": cfg["location"]["name"],
    }
    run_alerts(headlines, weather, ec)
    os.makedirs(SITE, exist_ok=True)
    with open(os.path.join(SITE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(SITE, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    page = open(os.path.join(ROOT, "page-template.html"), encoding="utf-8").read()
    page = (page.replace("__CSS__", open(os.path.join(ROOT, "style.css"), encoding="utf-8").read())
                .replace("__GO__", open(os.path.join(ROOT, "go.json"), encoding="utf-8").read().strip())
                .replace("__TOPIC__", cfg["alerts"].get("ntfy_topic", "")))
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    for name in ("manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png", "apple-touch-icon.png"):
        src = os.path.join(ROOT, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(SITE, name))
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    log(f"done in {time.time() - t0:.1f}s, data.json {os.path.getsize(os.path.join(SITE, 'data.json')) // 1024} KB")


if __name__ == "__main__":
    main()
