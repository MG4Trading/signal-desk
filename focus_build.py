"""Focus app data: private calendar, target-job roles and brief actions.

Everything personal is encrypted with the passcode in the FOCUS_KEY secret before it is
published, so the public site only ever holds ciphertext. Without the secret nothing
personal is written at all.
"""
import base64, hashlib, html, json, os, re, shutil, time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")
ITER = 200000
TITLE_OK = re.compile(r"sales op|revenue op|revops|rev ops|sales strateg|sales planning|revenue planning|go[- ]to[- ]market|\bgtm\b|"
                      r"commercial|business operations|deal desk|partner|channel|alliances|strategy & planning|strategy and planning|"
                      r"sales enablement|business development|distribution|wholesal|market entry|quota|capacity|forecast", re.I)
TITLE_NO = re.compile(r"engineer|developer|intern\b|co-op|scientist|designer|counsel|nurse|actuar|accountant|payroll|technician|"
                      r"account executive|architect|marketing|recruit|customer success manager|solutions consultant|representative|coordinator", re.I)
PLACE_CA = re.compile(r"canada|toronto|ontario|\bON\b|oakville|mississauga|waterloo|kitchener|ottawa|montr[eé]al|qu[eé]bec|vancouver|burnaby|british columbia|calgary|alberta|\bCAN\b", re.I)
PLACE_OTHER = re.compile(r"\bUS\b|USA|United States|Mexico|Brazil|Colombia|Bogot|India|\bUK\b|London|Ireland|Dublin|Germany|Australia|Japan|Singapore|"
                         r"Netherlands|France|Spain|Poland|Philippines|Hong Kong|Vietnam|Argentina|California|Texas|New York|Massachusetts", re.I)


def place_ok(place):
    place = place or ""
    return bool(PLACE_CA.search(place)) and not (PLACE_OTHER.search(place) and not PLACE_CA.search(place))


def _key(passcode, salt):
    return hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, ITER, 32)


def encrypt(obj, passcode):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passcode, salt)).encrypt(iv, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(blob, passcode):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = lambda x: base64.b64decode(x)
    pt = AESGCM(_key(passcode, d(blob["salt"]))).decrypt(d(blob["iv"]), d(blob["ct"]), None)
    return json.loads(pt)


# ------------------------------------------------------------------ calendar
def calendar(ics_url, get, log):
    import icalendar, recurring_ical_events
    raw = get(ics_url, timeout=30)
    cal = icalendar.Calendar.from_ical(raw)
    today = datetime.now(TZ).date()
    start = datetime.combine(today - timedelta(days=1), datetime.min.time(), TZ)
    end = datetime.combine(today + timedelta(days=8), datetime.min.time(), TZ)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s, e = ev.get("DTSTART").dt, (ev.get("DTEND").dt if ev.get("DTEND") else None)
        title = str(ev.get("SUMMARY", "") or "").strip()
        all_day = not isinstance(s, datetime)
        if all_day:
            e = e or (s + timedelta(days=1))
            item = {"d": s.isoformat(), "d2": e.isoformat(), "t": title, "all": True}
        else:
            s = s.astimezone(TZ) if s.tzinfo else s.replace(tzinfo=TZ)
            e = (e.astimezone(TZ) if e.tzinfo else e.replace(tzinfo=TZ)) if isinstance(e, datetime) else s + timedelta(hours=1)
            item = {"s": s.isoformat(timespec="minutes"), "e": e.isoformat(timespec="minutes"), "t": title}
        loc = str(ev.get("LOCATION", "") or "").strip()
        if loc:
            item["loc"] = loc[:80]
        if title.startswith("🎯"):
            desc = str(ev.get("DESCRIPTION", "") or "")
            desc = re.sub(r"<br\s*/?>", "\n", desc, flags=re.I)
            item["desc"] = html.unescape(re.sub(r"<[^>]+>", "", desc)).strip()[:6000]
        out.append(item)
    out.sort(key=lambda x: x.get("s") or x.get("d"))
    log(f"focus: {len(out)} calendar events")
    return out


# ------------------------------------------------------------------ job boards
def _keep(title, place):
    return bool(TITLE_OK.search(title)) and not TITLE_NO.search(title) and place_ok(place)


def greenhouse(board, get):
    d = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", timeout=30))
    out = []
    for j in d.get("jobs", []):
        place = (j.get("location") or {}).get("name", "")
        if _keep(j["title"], place):
            out.append({"id": f"gh{j['id']}", "t": j["title"], "loc": place, "url": j["absolute_url"], "date": (j.get("updated_at") or "")[:10]})
    return out


def workday(host, tenant, site, queries, get):
    seen, out = set(), []
    for q in queries:
        for off in (0, 20):
            body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": off, "searchText": q}).encode()
            d = json.loads(get(f"https://{host}/wday/cxs/{tenant}/{site}/jobs", data=body, timeout=30,
                               headers={"Content-Type": "application/json", "Accept": "application/json"}))
            posts = d.get("jobPostings", [])
            for j in posts:
                path = j.get("externalPath", "")
                if path in seen:
                    continue
                seen.add(path)
                place = j.get("locationsText", "")
                multi = bool(re.match(r"\d+ Locations", place))
                if TITLE_OK.search(j["title"]) and not TITLE_NO.search(j["title"]) and (multi or place_ok(place)):
                    out.append({"id": "wd" + path.rsplit("_", 1)[-1], "t": j["title"], "loc": place, "url": f"https://{host}/en-US/{site}{path}", "date": j.get("postedOn", "")})
            if len(posts) < 20:
                break
    return out


def teradata(queries, get):
    q = ("query s($query:String,$first:Int){ searchJobs: searchJobPostings(query:$query, first:$first)"
         "{ results { nodes { key title postedOn workplaceType primaryPlace{ name } places{ nodes{ name } } } } } }")
    seen, out = set(), []
    for term in queries:
        d = json.loads(get("https://careers.teradata.com/graphql", timeout=30, headers={"Content-Type": "application/json"},
                           data=json.dumps({"query": q, "variables": {"query": term, "first": 40}}).encode()))
        for j in d["data"]["searchJobs"]["results"]["nodes"]:
            if j["key"] in seen:
                continue
            seen.add(j["key"])
            places = "; ".join(sorted({p["name"] for p in (j.get("places") or {}).get("nodes", [])})) or (j.get("primaryPlace") or {}).get("name", "")
            place = places + (" · Remote" if j.get("workplaceType") == "REMOTE" else "")
            if _keep(j["title"], place):
                out.append({"id": "td" + j["key"], "t": j["title"], "loc": place[:120], "url": f"https://careers.teradata.com/jobs/{j['key']}", "date": (j.get("postedOn") or "")[:10]})
    return out


def scan(src, get):
    kind = src.get("kind")
    if kind == "greenhouse":
        return greenhouse(src["board"], get)
    if kind == "workday":
        return workday(src["host"], src["tenant"], src["site"], src.get("queries", ["operations", "strategy"]), get)
    if kind == "teradata":
        return teradata(src.get("queries", ["operations", "revenue", "strategy"]), get)
    return []


def jobs(cfg, prev, cal, get, log, drops=None):
    now = time.time()
    prev_jobs = {c["key"]: c for c in (prev.get("jobs") or [])}
    fresh = not (prev.get("jobs_at") and now - prev["jobs_at"] < 55 * 60)
    briefs = [e for e in cal if e.get("t", "").startswith("🎯")]
    out = []
    for c in cfg.get("companies", []):
        p = prev_jobs.get(c["key"], {})
        roles, err = p.get("roles", []), ""
        if fresh and c.get("source"):
            try:
                roles = scan(c["source"], get)
            except Exception as e:
                err = str(e)[:120]
                log("focus jobs", c["key"], e)
        first = dict(p.get("first", {}))
        for r in roles:
            first.setdefault(r["id"], int(now))
            r["new"] = now - first[r["id"]] < 48 * 3600 and bool(p.get("first"))
        first = {k: v for k, v in first.items() if now - v < 120 * 86400}
        acts = [{"d": e.get("d") or (e.get("s") or "")[:10], "t": e["t"], "text": e.get("desc", "")}
                for e in briefs if c["name"].lower() in e["t"].lower()]
        acts += [{"d": x["d"], "t": x["t"], "text": x["text"]} for x in (drops or []) if x["co"].lower() == c["name"].lower()]
        out.append({"key": c["key"], "name": c["name"], "goal": c.get("goal", ""), "careers": c.get("careers", ""),
                    "roles": roles[:12], "first": first, "people": c.get("people", []), "search": c.get("search", []),
                    "actions": acts[-3:], "err": err})
    return out, (now if fresh else prev.get("jobs_at", now))


# ------------------------------------------------------------------ brief inbox
def inbox(root, log):
    """Daily briefs drop their 3 actions here, sealed with the inbox public key.
    Only the build (holding FOCUS_INBOX_SK) can open them."""
    sk = (os.environ.get("FOCUS_INBOX_SK") or "").strip()
    d = os.path.join(root, "focus_inbox")
    out = []
    if not sk or not os.path.isdir(d):
        return out
    from nacl.public import PrivateKey, SealedBox
    box = SealedBox(PrivateKey(base64.b64decode(sk)))
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json"):
            continue
        try:
            j = json.load(open(os.path.join(d, f), encoding="utf-8"))
            text = box.decrypt(base64.b64decode(j["box"])).decode("utf-8")
            co = j.get("co", "")
            out.append({"co": co, "d": j.get("d", ""), "t": "🎯 " + co + " · today's 3 actions", "text": text[:6000]})
        except Exception as e:
            log("focus inbox", f, e)
    return out


# ------------------------------------------------------------------ main hook
def run(root, site, prev_dir, get, log):
    src = os.path.join(root, "focus")
    dst = os.path.join(site, "focus")
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    os.makedirs(dst, exist_ok=True)
    key = (os.environ.get("FOCUS_KEY") or "").strip()
    ics = (os.environ.get("GCAL_ICS") or "").strip()
    try:
        cfg = json.loads(os.environ.get("FOCUS_CFG") or "{}")
    except Exception:
        cfg = {}
    status = {"key": bool(key), "cal": bool(ics), "jobs": bool(cfg.get("companies")), "at": int(time.time())}
    if not key:
        json.dump(status, open(os.path.join(dst, "status.json"), "w"))
        log("focus: no FOCUS_KEY, nothing personal published")
        return
    prev = {}
    try:
        blob = json.load(open(os.path.join(prev_dir, "focus", "data.enc")))
        prev = decrypt(blob, key)
    except Exception:
        pass
    data = {"generated": int(time.time())}
    try:
        data["cal"] = calendar(ics, get, log) if ics else []
        status["cal_ok"] = bool(ics)
    except Exception as e:
        log("focus calendar", e)
        data["cal"] = prev.get("cal", [])
        status["cal_ok"] = False
    try:
        data["jobs"], data["jobs_at"] = jobs(cfg, prev, data["cal"], get, log, inbox(root, log))
    except Exception as e:
        log("focus jobs", e)
        data["jobs"], data["jobs_at"] = prev.get("jobs", []), prev.get("jobs_at")
    json.dump(encrypt(data, key), open(os.path.join(dst, "data.enc"), "w"))
    json.dump(status, open(os.path.join(dst, "status.json"), "w"))
    log(f"focus: encrypted data written ({sum(len(c['roles']) for c in data['jobs'])} roles)")
