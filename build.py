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
def _num(x):
    try:
        return float(str(x).replace(",", "").replace("%", "").replace("+", ""))
    except Exception:
        return None


def _iso(t):
    try:
        t = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", t)
        return int(datetime.fromisoformat(t).timestamp())
    except Exception:
        return None


def cnbc_quotes(symbols):
    url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols="
           + urllib.parse.quote("|".join(symbols)) + "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json")
    d = json.loads(get(url, headers={"Accept": "application/json"}))
    out = {}
    for q in d["FormattedQuoteResult"]["FormattedQuote"]:
        price = _num(q.get("last"))
        if price is None:
            continue
        out[q["symbol"]] = {"price": price, "pct": _num(q.get("change_pct")) or 0.0, "asof": _iso(q.get("last_time") or ""),
                            "name": q.get("name") or q["symbol"]}
    return out


def google_quote(sym):
    h = get("https://www.google.com/finance/quote/" + sym + "?hl=en")
    a, b = sym.split(":")
    m = re.search(r'\["' + re.escape(a) + r'","' + re.escape(b) + r'"\],"[^"]*",\d+,null,\[([-\d.]+),([-\d.]+),([-\d.]+)[^\]]*\],null,[-\d.]+,null,null,null,\[(\d+)\]', h)
    if not m:
        raise ValueError("price not found")
    return {"price": float(m.group(1)), "pct": float(m.group(3)), "asof": int(m.group(4))}


def gather_quotes():
    """Markets: CNBC's public quote feed, Google Finance for Tadawul. Each tile links to its source."""
    prev_q = {q["label"]: q for q in prev_data.get("quotes", []) if q.get("price") is not None}
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    cn_syms = sorted({x for _, src in cfg["quotes"] for x in re.findall(r"(?:cnbc:|ratio:|/)([^/]+)", src) if not src.startswith("google:")})
    try:
        cn = cnbc_quotes(cn_syms)
    except Exception as e:
        log("cnbc failed:", e); cn = {}
    out = []
    for label, src in cfg["quotes"]:
        q = None
        try:
            if src.startswith("cnbc:"):
                sym = src[5:]; x = cn.get(sym)
                if x:
                    q = dict(x, source="CNBC", url="https://www.cnbc.com/quotes/" + urllib.parse.quote(sym))
            elif src.startswith("ratio:"):
                a, b = src[6:].split("/"); xa, xb = cn.get(a), cn.get(b)
                if xa and xb:
                    pct = ((1 + xa["pct"] / 100) / (1 + xb["pct"] / 100) - 1) * 100
                    q = {"price": round(xa["price"] / xb["price"], 4), "pct": pct, "asof": min(xa["asof"] or 0, xb["asof"] or 0) or None,
                         "source": "CNBC (USD/EGP ÷ USD/CAD)", "url": "https://www.google.com/finance/quote/CAD-EGP"}
            elif src.startswith("google:"):
                sym = src[7:]
                q = dict(google_quote(sym), source="Google Finance", url="https://www.google.com/finance/quote/" + sym)
        except Exception as e:
            log("quote", label, e)
        old = prev_q.get(label)
        if not q:
            q = dict(old, stale=True) if old else {"price": None}
        else:
            spark = list(old.get("spark") or []) if old and old.get("day") == today else []
            if not spark or spark[-1] != q["price"]:
                spark.append(q["price"])
            q.update(spark=spark[-60:], day=today)
        q["label"] = label
        out.append(q)
    log("quotes:", sum(1 for q in out if q.get("price") is not None and not q.get("stale")), "fresh of", len(out))
    return out

# ---------------------------------------------------------------- market deep dive
def cnbc_chart(sym, rng):
    d = json.loads(get("https://ts-api.cnbc.com/harmony/app/charts/%s.json?symbol=%s" % (rng, urllib.parse.quote(sym)),
                       headers={"Accept": "application/json"}, timeout=25))
    out = []
    for b in (d.get("barData") or {}).get("priceBars") or []:
        try:
            out.append((int(b["tradeTimeinMills"]) // 1000, float(b["close"])))
        except Exception:
            pass
    return out


def google_hist(sym):
    h = get("https://www.google.com/finance/quote/" + sym + "?hl=en")
    i = h.find("key: 'ds:12'")
    blk = h[i:i + 20000] if i >= 0 else ""
    out = []
    for m in re.finditer(r'\[([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+),"(\d{4}-\d\d-\d\d)T', blk):
        out.append((int(datetime.strptime(m.group(5), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()), float(m.group(2))))
    return out


def _at(series, ts):
    v = None
    for t, c in series:
        if t <= ts:
            v = c
        else:
            break
    return v


def _rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        d = b - a; gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:-1], closes[n + 1:]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n; al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def analyse(daily, weekly, live):
    daily = sorted(daily); weekly = sorted(weekly)
    if not daily:
        return None
    now = int(time.time())
    last = live if live is not None else daily[-1][1]
    pct = lambda old: None if not old else round((last / old - 1) * 100, 2)
    year_start = int(datetime(datetime.now(TZ).year, 1, 1, tzinfo=TZ).timestamp())
    longer = weekly if weekly else daily
    rets = {"1W": pct(_at(daily, now - 7 * 86400)), "1M": pct(_at(daily, now - 30 * 86400)), "3M": pct(_at(daily, now - 91 * 86400)),
            "YTD": pct(_at(daily, year_start - 1)), "1Y": pct(_at(daily, now - 365 * 86400)),
            "3Y": pct(_at(longer, now - 3 * 365 * 86400)) if longer and longer[0][0] <= now - 3 * 365 * 86400 else None,
            "5Y": pct(_at(longer, now - 5 * 365 * 86400)) if longer and longer[0][0] <= now - 5 * 365 * 86400 + 14 * 86400 else None}
    closes = [c for _, c in daily]
    yr = [c for t, c in daily if t >= now - 365 * 86400] + [last]
    ma = lambda n: round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None
    ma50, ma200, rsi = ma(50), ma(200), _rsi(closes + [last])
    hi, lo = max(yr), min(yr)
    if ma50 and ma200:
        trend = "Uptrend" if last > ma50 > ma200 else "Downtrend" if last < ma50 < ma200 else "Mixed"
    elif ma50:
        trend = "Above 50-day" if last > ma50 else "Below 50-day"
    else:
        trend = None
    rnd = lambda v: float("%.6g" % v)
    return {"rets": rets, "ma50": ma50, "ma200": ma200, "rsi": None if rsi is None else round(rsi, 1),
            "hi52": rnd(hi), "lo52": rnd(lo), "from_hi": round((last / hi - 1) * 100, 2), "trend": trend,
            "daily": [[t // 86400, rnd(c)] for t, c in daily if t >= now - 400 * 86400],
            "weekly": [[t // 86400, rnd(c)] for t, c in weekly if t >= now - 5 * 366 * 86400]}


def gather_deep(quotes):
    """History, returns and trend signals per instrument; refreshed about once an hour."""
    prev = prev_data.get("deep") or {}
    live = {q["label"]: q.get("price") for q in quotes}
    if prev.get("at") and NOW - prev["at"] < 55 * 60 and prev.get("items"):
        return prev
    items = {}
    def one(label, src):
        if src.startswith("cnbc:"):
            sym = src[5:]
            return analyse(cnbc_chart(sym, "1Y"), cnbc_chart(sym, "5Y"), live.get(label))
        if src.startswith("ratio:"):
            a, b = src[6:].split("/")
            def ratio(rng):
                sa, sb = dict((t // 86400, c) for t, c in cnbc_chart(a, rng)), dict((t // 86400, c) for t, c in cnbc_chart(b, rng))
                return [(d * 86400, sa[d] / sb[d]) for d in sorted(set(sa) & set(sb)) if sb[d]]
            return analyse(ratio("1Y"), ratio("5Y"), live.get(label))
        if src.startswith("google:"):
            r = analyse(google_hist(src[7:]), [], live.get(label))
            if r:
                r["limited"] = True
            return r
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {label: ex.submit(one, label, src) for label, src in cfg.get("deep", {}).items()}
        for label, fu in futs.items():
            try:
                r = fu.result()
                if r:
                    items[label] = r
            except Exception as e:
                log("deep", label, e)
                if (prev.get("items") or {}).get(label):
                    items[label] = prev["items"][label]
    log(f"deep: {len(items)} instruments")
    return {"at": int(NOW), "items": items} if items else prev


def gather_rates():
    prev = prev_data.get("rates") or {}
    if prev.get("at") and NOW - prev["at"] < 55 * 60 and prev.get("items"):
        return prev
    items = []
    try:
        start = (datetime.now(TZ) - timedelta(days=730)).strftime("%Y-%m-%d")
        d = json.loads(get("https://www.bankofcanada.ca/valet/observations/V39079,V80691311,V80691335,BD.CDN.5YR.DQ.YLD/json?start_date=" + start))
        obs = d.get("observations", [])
        def series(k):
            return [(o["d"], float(o[k]["v"])) for o in obs if k in o and o[k].get("v") not in (None, "")]
        pol = series("V39079")
        if pol:
            last_d, last_v = pol[-1]
            chg = next(((d_, v) for d_, v in reversed(pol) if v != last_v), None)
            moved = None
            if chg:
                first_new = next(d_ for d_, v in pol if d_ > chg[0])
                moved = {"date": first_new, "by": round(last_v - chg[1], 2)}
            items.append({"k": "boc", "label": "Bank of Canada policy rate", "value": last_v, "asof": last_d, "moved": moved,
                          "source": "Bank of Canada", "url": "https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/"})
        for k, label, url in (("V80691311", "Prime rate (banks)", "https://www.bankofcanada.ca/rates/banking-and-financial-statistics/posted-interest-rates-offered-by-chartered-banks/"),
                              ("V80691335", "5-year fixed mortgage, posted", "https://www.bankofcanada.ca/rates/banking-and-financial-statistics/posted-interest-rates-offered-by-chartered-banks/"),
                              ("BD.CDN.5YR.DQ.YLD", "Government of Canada 5-year yield", "https://www.bankofcanada.ca/rates/interest-rates/canadian-bonds/")):
            sr = series(k)
            if sr:
                yr_ago = next((v for d_, v in sr if d_ >= (datetime.now(TZ) - timedelta(days=365)).strftime("%Y-%m-%d")), None)
                items.append({"k": k, "label": label, "value": sr[-1][1], "asof": sr[-1][0], "yoy": None if yr_ago is None else round(sr[-1][1] - yr_ago, 2),
                              "source": "Bank of Canada", "url": url})
    except Exception as e:
        log("boc rates failed:", e)
    try:
        cn = cnbc_quotes(["CA2Y", "CA10Y", "US2Y", "US10Y"])
        for sym, label in (("CA10Y", "Canada 10-year yield"), ("US2Y", "U.S. 2-year yield"), ("US10Y", "U.S. 10-year yield")):
            if sym in cn:
                items.append({"k": sym, "label": label, "value": cn[sym]["price"], "asof": cn[sym]["asof"], "source": "CNBC",
                              "url": "https://www.cnbc.com/quotes/" + sym})
    except Exception as e:
        log("bond yields failed:", e)
    if not items:
        return prev
    return {"at": int(NOW), "items": items}


def gather_housing():
    prev = prev_data.get("housing") or {}
    if prev.get("at") and NOW - prev["at"] < 6 * 3600 and prev.get("snlr"):
        return prev
    h = get("https://stats.crea.ca/en-CA/", timeout=25)
    t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)))
    out = {"at": int(NOW), "source": "CREA", "url": "https://stats.crea.ca/en-CA/"}
    m = re.search(r"sales-to-new listings ratio.{0,80}?([\d.]+)%", t, re.I)
    out["snlr"] = float(m.group(1)) if m else None
    m = re.search(r"([\d.]+) months of inventory", t, re.I); out["moi"] = float(m.group(1)) if m else None
    m = re.search(r"Home Price Index \(HPI\)[^.]*?(up|down) ([\d.]+)% year-over-year", t, re.I)
    out["hpi_yoy"] = (1 if m.group(1).lower() == "up" else -1) * float(m.group(2)) if m else None
    m = re.search(r"average sale price was (up|down) ([\d.]+)%", t, re.I)
    out["avg_yoy"] = (1 if m.group(1).lower() == "up" else -1) * float(m.group(2)) if m else None
    m = re.search(r"National Statistics (.{10,110}?) Monthly Housing Market Report", t); out["title"] = m.group(1).strip() if m else "CREA monthly housing report"
    m = re.search(r"Ottawa, ON (\w+ \d{1,2}, \d{4})", t); out["date"] = m.group(1) if m else None
    v = out["snlr"]
    out["verdict"] = None if v is None else ("Buyer's market" if v < 45 else "Seller's market" if v > 65 else "Balanced market")
    if not out["snlr"]:
        return prev or out
    return out

# ---------------------------------------------------------------- faith, kitchen, watch & listen (daily)
QURAN = "https://api.alquran.cloud/v1"
TAFSIR = "https://cdn.jsdelivr.net/gh/spa5k/tafsir_api@main/tafsir/"
HADITH = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1/editions/"


def gather_prayer():
    loc = cfg["location"]; out = []
    for off in (0, 1):
        d = datetime.now(TZ) + timedelta(days=off)
        j = json.loads(get(f"https://api.aladhan.com/v1/timings/{d.strftime('%d-%m-%Y')}?latitude={loc['lat']}&longitude={loc['lon']}&method=2&school=0"))["data"]
        t = j["timings"]; h = j["date"]["hijri"]
        out.append({"date": d.strftime("%Y-%m-%d"), "times": {k: t[k][:5] for k in ("Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha")},
                    "hijri_ar": f"{h['day']} {h['month']['ar']} {h['year']}", "hijri_en": f"{h['day']} {h['month']['en']} {h['year']} AH"})
    return {"days": out, "method": "ISNA, standard Asr", "source": "Aladhan", "url": "https://aladhan.com/prayer-times-api"}


def gather_faith(day_no, content):
    faith = {}
    ref = content["ayahs"][day_no % len(content["ayahs"])]
    a = json.loads(get(f"{QURAN}/ayah/{ref}/editions/quran-uthmani,en.sahih,ar.muyassar"))["data"]
    faith["ayah"] = {"ref": ref, "ar": a[0]["text"], "en": a[1]["text"], "tafsir_ar": a[2]["text"],
                     "surah_ar": a[0]["surah"]["name"], "surah_en": a[0]["surah"]["englishName"],
                     "url": "https://quran.com/" + ref.replace(":", "/")}
    # surah of the day: Al-Kahf on Fridays, otherwise a daily cycle through all 114
    n = 18 if datetime.now(TZ).weekday() == 4 else 114 - (day_no % 114)
    su = json.loads(get(f"{QURAN}/surah/{n}/editions/quran-uthmani,en.sahih"))["data"]
    ar_t = json.loads(get(f"{TAFSIR}ar-tafsir-muyassar/{n}.json", timeout=40))
    en_t = json.loads(get(f"{TAFSIR}en-tafsir-al-mukhtasar/{n}.json", timeout=40))
    ar_map = {x["ayah"]: x["text"] for x in ar_t}; en_map = {x["ayah"]: x["text"] for x in en_t}
    intro = ""
    first = ar_map.get(1, "")
    if "من مقاصد السورة" in first or "تسمية السورة" in first:
        parts = first.split("\n\n")
        keep = [p_ for p_ in parts if p_.strip()]
        # the intro ends where the tafsir of verse 1 begins (the last paragraph)
        intro = "\n\n".join(keep[:-1]).strip(); ar_map[1] = keep[-1] if keep else first
    faith["surah"] = {"n": n, "name_ar": su[0]["name"], "name_en": su[0]["englishName"], "meaning": su[0]["englishNameTranslation"],
                      "type": su[0]["revelationType"], "count": su[0]["numberOfAyahs"], "intro_ar": intro,
                      "ayahs": [{"n": x["numberInSurah"], "ar": x["text"], "en": su[1]["ayahs"][i]["text"],
                                 "t_ar": ar_map.get(x["numberInSurah"], ""), "t_en": en_map.get(x["numberInSurah"], "")} for i, x in enumerate(su[0]["ayahs"])],
                      "info_url": f"https://quran.com/surah/{n}/info", "read_url": f"https://quran.com/{n}",
                      "listen_url": f"https://quran.com/{n}?reciter=7"}
    # hadith: rotate the 42 of an-Nawawi and the 40 Hadith Qudsi, Arabic and English
    pool = [("nawawi", i) for i in range(1, 43)] + [("qudsi", i) for i in range(1, 41)]
    book, num = pool[day_no % len(pool)]
    ar = json.loads(get(f"{HADITH}ara-{book}/{num}.json"))["hadiths"][0]["text"]
    en = json.loads(get(f"{HADITH}eng-{book}/{num}.json"))["hadiths"][0]["text"]
    faith["hadith"] = {"book": {"nawawi": "الأربعون النووية · Forty Hadith of an-Nawawi", "qudsi": "الأحاديث القدسية · Forty Hadith Qudsi"}[book],
                       "num": num, "ar": ar.strip(), "en": en.strip(),
                       "url": f"https://sunnah.com/{'nawawi40' if book == 'nawawi' else 'qudsi40'}:{num}"}
    return faith


def gather_podcasts(country, n=40, max_min=30):
    feed = json.loads(get(f"https://rss.applemarketingtools.com/api/v2/{country}/podcasts/top/{n}/podcasts.json"))["feed"]["results"]
    ids = ",".join(r["id"] for r in feed)
    look = json.loads(get(f"https://itunes.apple.com/lookup?id={ids}&entity=podcastEpisode&limit=1&country={country}"))["results"]
    ep = {}
    for r in look:
        if r.get("wrapperType") == "podcastEpisode" and r.get("collectionId") and r.get("trackTimeMillis"):
            ep.setdefault(str(r["collectionId"]), r)
    out = []
    for i, r in enumerate(feed):
        e = ep.get(r["id"])
        if not e:
            continue
        mins = round(e["trackTimeMillis"] / 60000)
        if mins > max_min:
            continue
        out.append({"rank": i + 1, "name": r["name"], "by": r.get("artistName", ""), "genre": (r.get("genres") or [{}])[0].get("name", ""),
                    "url": r["url"], "img": r.get("artworkUrl100", ""), "ep": e.get("trackName", ""), "mins": mins,
                    "ep_url": e.get("trackViewUrl") or r["url"]})
    return out


def gather_movies(genre, n=15):
    d = json.loads(get(f"https://itunes.apple.com/ca/rss/topmovies/limit={n}/genre={genre}/json"))["feed"].get("entry", [])
    out = []
    for e in d:
        link = next((l["attributes"]["href"] for l in (e.get("link") if isinstance(e.get("link"), list) else [e.get("link")]) if l and l.get("attributes", {}).get("rel") == "alternate"), "")
        out.append({"t": e["im:name"]["label"], "year": (e.get("im:releaseDate", {}).get("label") or "")[:4], "url": link,
                    "genre": e.get("category", {}).get("attributes", {}).get("label", ""), "sum": (e.get("summary", {}).get("label") or "")[:220],
                    "img": (e.get("im:image") or [{}])[-1].get("label", "")})
    return out


def pick_foryou(out, prev):
    """Three new action, crime, thriller or adventure picks a day, never repeating what was suggested before."""
    pool = {}
    def add(lst, tag, bonus):
        for i, m in enumerate(lst or []):
            mid = (re.search(r"/id(\d+)", m.get("url") or "") or [None, m["t"]])[1]
            e = pool.setdefault(mid, dict(m, id=mid, tags=set(), score=0.0))
            e["tags"].add(tag); e["score"] += bonus + max(0, 15 - i) / 5
    add(out.get("movies_action"), "Action & adventure", 2)
    add(out.get("movies_thriller"), "Thriller", 2)
    for g, tag in ((4401, "Action & adventure"), (4416, "Thriller")):
        try:
            add([dict(m, t=m["t"]) for m in json_movies("us", g)], tag + " · U.S. chart", 1)
        except Exception as e:
            log("us chart", e)
    year = datetime.now(TZ).year
    for e in pool.values():
        if e.get("year") and e["year"].isdigit() and int(e["year"]) >= year - 1:
            e["score"] += 3; e["tags"].add("New release")
        if re.search(r"crime|heist|detective|murder|cartel|gang|mafia|cop|police|killer|agent|spy", e.get("sum", ""), re.I):
            e["score"] += 2; e["tags"].add("Crime")
        if re.search(r"disney|pixar|dreamworks|illumination|animated|animation|family film|kids|princess|musical|moana|pokemon|paw patrol", e.get("t", "") + " " + e.get("sum", ""), re.I):
            e["score"] -= 20
    shown = list((prev.get("foryou") or {}).get("shown") or [])
    today_ids = (prev.get("foryou") or {}).get("today_ids") if (prev.get("foryou") or {}).get("date") == out["date"] else None
    ranked = sorted(pool.values(), key=lambda e: -e["score"])
    picks = [e for e in ranked if e["id"] in (today_ids or [])] if today_ids else [e for e in ranked if e["id"] not in shown][:5]
    if len(picks) < 5:
        picks += ([e for e in ranked if e not in picks and e["id"] not in shown] + [e for e in ranked if e not in picks and e["id"] in shown])[:5 - len(picks)]
    ids = [e["id"] for e in picks]
    return {"date": out["date"], "today_ids": ids, "shown": (shown + [i for i in ids if i not in shown])[-300:],
            "picks": [{k: (sorted(v) if isinstance(v, set) else v) for k, v in e.items() if k != "score"} for e in picks]}


def json_movies(country, genre, n=25):
    d = json.loads(get(f"https://itunes.apple.com/{country}/rss/topmovies/limit={n}/genre={genre}/json"))["feed"].get("entry", [])
    out = []
    for e in d:
        link = next((l["attributes"]["href"] for l in (e.get("link") if isinstance(e.get("link"), list) else [e.get("link")]) if l and l.get("attributes", {}).get("rel") == "alternate"), "")
        out.append({"t": e["im:name"]["label"], "year": (e.get("im:releaseDate", {}).get("label") or "")[:4], "url": link.replace("/us/", "/ca/"),
                    "genre": e.get("category", {}).get("attributes", {}).get("label", ""), "sum": (e.get("summary", {}).get("label") or "")[:220],
                    "img": (e.get("im:image") or [{}])[-1].get("label", "")})
    return out


def gather_extras():
    prev = load_json(os.path.join(PREV, "extras.json"), {})
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if prev.get("date") == today and prev.get("faith") and prev.get("prayer") and len((prev.get("foryou") or {}).get("today_ids") or []) >= 5:
        prev["content"] = load_json(os.path.join(ROOT, "extras-content.json"), prev.get("content") or {})
        return prev
    content = load_json(os.path.join(ROOT, "extras-content.json"), {})
    day_no = (datetime.now(TZ).date() - datetime(2026, 1, 1).date()).days
    out = {"date": today, "generated": int(time.time()), "day_no": day_no}
    for key, fn in (("prayer", gather_prayer), ("faith", lambda: gather_faith(day_no, content)),
                    ("podcasts_ca", lambda: gather_podcasts("ca")), ("podcasts_eg", lambda: gather_podcasts("eg")),
                    ("movies_action", lambda: gather_movies(4401)), ("movies_thriller", lambda: gather_movies(4416))):
        try:
            out[key] = fn()
        except Exception as e:
            log("extras", key, e)
            if prev.get(key):
                out[key] = prev[key]
    try:
        out["foryou"] = pick_foryou(out, prev)
    except Exception as e:
        log("foryou", e); out["foryou"] = prev.get("foryou")
    try:
        feed = {"id": "egcinema", "gl": "EG", "hl": "ar", "feeds": []}
        out["egypt_cinema"] = fetch_feed(feed, ["سينما", "فيلم مصري جديد OR أفلام السينما المصرية OR إيرادات السينما", "EG", "ar", "7d"])[:8]
    except Exception as e:
        log("egypt cinema", e); out["egypt_cinema"] = prev.get("egypt_cinema", [])
    out["content"] = content
    log("extras:", ", ".join(k for k in out if k not in ("date", "generated", "day_no")))
    return out

# ---------------------------------------------------------------- businesses for sale
BIZ_INC = re.compile(r"business|franchise|restaurant|clinic|salon|caf[eé]|store|gas station|route|company|turn-?key|bakery|pizza|daycare|spa\b|gym|laundromat|car wash|e-?commerce", re.I)
BIZ_SALE = re.compile(r"for sale|sale\b|opportunit|established|take over|retiring|turn-?key|profitable", re.I)
BIZ_EXC = re.compile(r"rack|mezzanine|ladder|pallet|forklift|shelving|container|carpet|\bpos\b|cash register|mobile sign|equipment|free for buyers|find your perfect|wanted|we buy|i will buy|looking (?:to|for)|for rent|for lease|appliances|closing sale|must go|liquidat", re.I)
BIZ_TYPES = [("Food & restaurant", r"restaurant|caf[eé]|pizza|bakery|food|kitchen|grill|shawarma|coffee"), ("Franchise", r"franchise"),
             ("Health & beauty", r"clinic|salon|spa\b|massage|dental|physio|beauty|barber"), ("Retail", r"store|shop|retail|convenience|gas station"),
             ("Services", r"cleaning|rental|route|vending|daycare|school|repair|delivery|moving|landscap|sign"), ("Online", r"online|e-?commerce|website|amazon|shopify")]


def gather_biz():
    prev = prev_data.get("biz") or {}
    if prev.get("at") and NOW - prev["at"] < 55 * 60 and prev.get("items"):
        return prev
    searches = [("Oakville & Halton", "oakville-halton-region", "1700277", "business-for-sale"),
                ("Oakville & Halton", "oakville-halton-region", "1700277", "franchise-for-sale"),
                ("GTA", "gta-greater-toronto-area", "1700272", "business-for-sale"),
                ("GTA", "gta-greater-toronto-area", "1700272", "franchise-for-sale"),
                ("GTA", "gta-greater-toronto-area", "1700272", "restaurant-for-sale")]
    items, seen_url, seen_desc, ok = [], set(), {}, 0
    for area, slug, loc, kw in searches:
        url = f"https://www.kijiji.ca/b-other-business-industrial/{slug}/{kw}/k0c145l{loc}?sort=dateDesc"
        try:
            ap = next_data(get(url, timeout=25)).get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
            ok += 1
        except Exception as e:
            log("biz", kw, e); continue
        for k, v in ap.items():
            if not k.startswith("StandardListing:"):
                continue
            t = (v.get("title") or "").strip(); d = re.sub(r"\s+", " ", v.get("description") or "").strip(); u = v.get("url") or ""
            if not u or u in seen_url or BIZ_EXC.search(t) or not BIZ_INC.search(t + " " + d[:200]) or not BIZ_SALE.search(t + " " + d[:300]):
                continue
            seen_url.add(u)
            place = ((v.get("location") or {}).get("name") or "").replace(" / Halton Region", "").replace(" / Peel Region", "").replace(" / York Region", "").replace(" / Durham Region", "").replace("City of ", "")
            key = d[:90].lower()
            if key in seen_desc:
                if place and place not in seen_desc[key]["also"] and place != seen_desc[key]["place"]:
                    seen_desc[key]["also"].append(place)
                continue
            amt = (v.get("price") or {}).get("amount")
            price = round(amt / 100) if amt and amt >= 100000 else None
            try:
                listed = int(datetime.fromisoformat((v.get("activationDate") or v.get("sortingDate")).replace("Z", "+00:00")).timestamp())
            except Exception:
                listed = 0
            facts = []
            m = re.search(r"\$\s?([\d,.]+)\s?([kKmM])?\s*(?:down|down payment)", t + " " + d)
            if m: facts.append("Down payment $" + m.group(1) + (m.group(2) or "").upper())
            for lab, pat in (("Sales", r"(?:annual )?(?:sales|revenue)[^$.]{0,25}\$\s?([\d,.]+\s?[kKmM]?)"), ("Profit", r"(?:net )?(?:profit|income|cash ?flow|SDE|EBITDA)[^$.]{0,25}\$\s?([\d,.]+\s?[kKmM]?)"),
                             ("Rent", r"rent[^$.]{0,20}\$\s?([\d,.]+\s?[kKmM]?)"), ("Years", r"(\d{1,2}\+?\s?years)")):
                m = re.search(pat, d, re.I)
                if m: facts.append(lab + " " + (("$" + m.group(1)) if lab != "Years" else m.group(1)))
            kind = next((name for name, pat in BIZ_TYPES if re.search(pat, t + " " + d[:300], re.I)), "Other")
            it = {"t": t[:100], "d": d[:420], "l": u, "p": price, "place": place, "area": "Oakville & Halton" if re.search(r"oakville|burlington|milton|halton", place, re.I) else area,
                  "kind": kind, "facts": facts[:4], "ts": listed, "img": (v.get("imageUrls") or [""])[0], "also": []}
            seen_desc[key] = it; items.append(it)
        time.sleep(1)
    if not ok:
        return prev or {"at": 0, "items": []}
    items.sort(key=lambda x: -x["ts"])
    log(f"biz: {len(items)} businesses for sale")
    return {"at": int(NOW), "items": items[:60]}

# ---------------------------------------------------------------- gas prices
EXTRA = {}


def gather_gas(quotes):
    prev = prev_data.get("gas") or {}
    if prev.get("at") and NOW - prev["at"] < 55 * 60 and prev.get("days") and prev.get("avg"):
        return prev
    url = "https://gaswizard.ca/gas-prices/oakville/"
    h = get(url, timeout=25)
    days = []
    for blk in re.split(r"<li>", h)[1:]:
        m = re.search(r'daytext">(\w+)</span>\s*-\s*<span class="datetext">([^<]+)</span>', blk)
        pv = re.search(r'fuel-price-value">([\d.]+)</span>', blk)
        if not (m and pv):
            continue
        body = re.sub(r"<!--.*?-->", "", blk, flags=re.S)
        dm = re.search(r'price-direction (pd-[\w-]+)"', body)
        cm = re.search(r'price-text">([^<]*)<', body) or re.search(r'price-direction [\w-]+">([^<]*)<', body)
        chg = (cm.group(1).strip() if cm else "")
        days.append({"day": m.group(1), "date": m.group(2).strip(), "price": float(pv.group(1)),
                     "dir": {"pd-up": "up", "pd-down": "down"}.get(dm.group(1) if dm else "", "same"),
                     "chg": "" if chg in ("---", "") else chg})
        if len(days) >= 2:
            break
    m = re.search(r"Current Average Price\s*\$\s*([\d.]+)\s*\(Reported at:\s*([^)]+)\)", html.unescape(re.sub(r"<[^>]+>", " ", h)))
    avg = round(float(m.group(1)) * 100, 1) if m else None
    out = {"at": int(NOW), "days": days, "avg": avg, "reported": m.group(2).strip() if m else "", "url": url,
           "source": "Gas Wizard (Dan McTeague)"}
    # wholesale gasoline trend as a guide for the rest of the week
    try:
        rb = cnbc_chart("@RB.1", "1M")
        usdcad = next((q["price"] for q in quotes if q.get("label") == "USD/CAD" and q.get("price")), 1.38)
        if len(rb) >= 4:
            d3 = (rb[-1][1] - rb[-4][1]) * 100 / 3.78541 * usdcad
            out["wholesale"] = {"chg3": round(d3, 1), "last": rb[-1][1], "series": [[t // 86400, round(c, 4)] for t, c in rb[-22:]]}
            out["outlook"] = "up" if d3 >= 1.5 else "down" if d3 <= -1.5 else "steady"
    except Exception as e:
        log("rbob", e)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    hist = [x for x in (prev.get("hist") or []) if x[0] != today]
    if avg:
        hist.append([today, avg])
    out["hist"] = hist[-90:]
    return out if days or avg else prev


# ---------------------------------------------------------------- stock watchlist
def gather_watch():
    wl = cfg.get("watchlist") or []
    if not wl:
        return {"items": []}
    prev = prev_data.get("watch") or {}
    prev_deep = {w["sym"]: w.get("deep") for w in prev.get("items", []) if w.get("deep")}
    fresh_deep = not (prev.get("deep_at") and NOW - prev["deep_at"] < 55 * 60)
    try:
        cn = cnbc_quotes([w["sym"] for w in wl])
    except Exception as e:
        log("watch quotes", e); cn = {}
    prev_q = {w["sym"]: w for w in prev.get("items", [])}
    items = []
    def deep_for(w):
        return analyse(cnbc_chart(w["sym"], "1Y"), cnbc_chart(w["sym"], "5Y"), (cn.get(w["sym"]) or {}).get("price"))
    deeps = {}
    if fresh_deep:
        with ThreadPoolExecutor(max_workers=3) as ex:
            for w, fu in [(w, ex.submit(deep_for, w)) for w in wl]:
                try:
                    deeps[w["sym"]] = fu.result()
                except Exception as e:
                    log("watch deep", w["sym"], e)
    for w in wl:
        q = cn.get(w["sym"]) or {}
        old = prev_q.get(w["sym"]) or {}
        label = w["sym"].replace("-T", "").replace(".TO", "")
        items.append({"sym": w["sym"], "label": label, "name": w.get("name") or q.get("name") or old.get("name") or "",
                      "price": q.get("price", old.get("price")), "pct": q.get("pct", old.get("pct")), "asof": q.get("asof", old.get("asof")),
                      "stale": not q, "note": w.get("note", ""), "above": w.get("above"), "below": w.get("below"),
                      "tsx": w["sym"].endswith("-T"), "url": "https://www.cnbc.com/quotes/" + urllib.parse.quote(w["sym"]), "source": "CNBC",
                      "deep": deeps.get(w["sym"]) or prev_deep.get(w["sym"])})
    return {"items": items, "deep_at": int(NOW) if fresh_deep and deeps else prev.get("deep_at")}

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

    # watchlist price alerts, once a day per stock and direction
    ws = state.setdefault("watch_seen", [])
    for w in (EXTRA.get("watch") or {}).get("items", []):
        px = w.get("price")
        if px is None or w.get("stale"):
            continue
        for side, lvl in (("above", w.get("above")), ("below", w.get("below"))):
            if lvl and ((side == "above" and px >= lvl) or (side == "below" and px <= lvl)):
                key = f"{datetime.now(TZ).strftime('%Y-%m-%d')}:{w['sym']}:{side}"
                if key not in ws:
                    ws.append(key)
                    if not first_run:
                        push(f"{w['label']} is {side} {lvl:g}", f"{w['label']} ({w.get('name','')}) is at {px:g}, {'above' if side == 'above' else 'below'} your alert of {lvl:g}.",
                             tags=["chart_with_upwards_trend" if side == "above" else "chart_with_downwards_trend"], priority=4, click=w.get("url"))
    state["watch_seen"] = ws[-200:]

    # gas: push when the next-day forecast moves 2 cents or more (daytime only, once per forecast day)
    try:
        gd = (EXTRA.get("gas") or {}).get("days") or []
        hr = datetime.now(TZ).hour
        if len(gd) >= 2 and gd[0].get("price") and gd[1].get("price") and 7 <= hr < 22:
            diff = round(gd[0]["price"] - gd[1]["price"], 1)
            key = "gas:" + gd[0].get("date", "")
            if abs(diff) >= 2 and key not in ws:
                ws.append(key); state["watch_seen"] = ws[-200:]
                if not first_run:
                    up = diff > 0
                    push(f"Gas {'up' if up else 'down'} {abs(diff):g}\u00a2 {gd[0].get('day','')}",
                         f"Oakville regular: {gd[0]['price']:g}\u00a2/L on {gd[0].get('day','')} ({'+' if up else '-'}{abs(diff):g}\u00a2). "
                         + ("Fill up before it rises." if up else "Wait to fill up if you can."),
                         tags=["fuelpump"], priority=4, click="https://www.gasbuddy.com/gasprices/ontario/oakville")
    except Exception as e:
        log("gas alert", e)

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
        g = EXTRA.get("gas") or {}
        if g.get("days"):
            t0 = g["days"][0]
            lines.append(f"⛽ Gas {t0['day']}: {t0['price']:.1f}¢/L" + (f" ({t0['chg']})" if t0.get("chg") else " (no change)"))
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
    gas = watch = None
    try:
        gas = gather_gas(quotes)
    except Exception as e:
        log("gas failed", e); gas = prev_data.get("gas")
    try:
        watch = gather_watch()
    except Exception as e:
        log("watch failed", e); watch = prev_data.get("watch")
    EXTRA["gas"], EXTRA["watch"] = gas, watch
    deep = rates = housing = biz = None
    try:
        biz = gather_biz()
    except Exception as e:
        log("biz failed", e); biz = prev_data.get("biz")
    try:
        deep = gather_deep(quotes)
    except Exception as e:
        log("deep failed", e); deep = prev_data.get("deep")
    try:
        rates = gather_rates()
    except Exception as e:
        log("rates failed", e); rates = prev_data.get("rates")
    try:
        housing = gather_housing()
    except Exception as e:
        log("housing failed", e); housing = prev_data.get("housing")
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
        "topics": [{"id": t["id"], "name": t["name"], "links": t.get("links", []), "in_markets": bool(t.get("in_markets"))} for t in cfg["topics"]],
        "headlines": headlines, "quotes": quotes, "weather": weather, "warnings": ec,
        "rentals": rentals, "events": events, "deep": deep, "rates": rates, "housing": housing, "biz": biz, "gas": gas, "watch": watch,
        "failed": failed, "location": cfg["location"]["name"],
    }
    run_alerts(headlines, weather, ec)
    os.makedirs(SITE, exist_ok=True)
    with open(os.path.join(SITE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    try:
        extras = gather_extras()
        with open(os.path.join(SITE, "extras.json"), "w", encoding="utf-8") as f:
            json.dump(extras, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        log("extras failed", e)
        if os.path.exists(os.path.join(PREV, "extras.json")):
            shutil.copy(os.path.join(PREV, "extras.json"), os.path.join(SITE, "extras.json"))
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
