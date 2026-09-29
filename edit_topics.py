#!/usr/bin/env python3
"""Adds or removes a topic, or search words in a topic, from an issue sent by the app's Settings panel.

Issue title: "Signal Desk: add" or "Signal Desk: remove"
Issue body:
    Topic: Real estate
    Region: CA
    Language: en
    Keywords:
    - Oakville real estate
    - Halton housing market
"""
import json, os, re, sys

title = os.environ.get("ISSUE_TITLE", "").lower()
body = os.environ.get("ISSUE_BODY", "") or ""
cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
cfg = json.load(open(cfg_path, encoding="utf-8"))

fields, words, in_words = {}, [], False
for line in body.replace("\r", "").split("\n"):
    s = line.strip()
    if not s:
        continue
    if in_words and s.startswith(("-", "*", "•")):
        w = s.lstrip("-*• ").strip()
        if w:
            words.append(w[:120])
        continue
    m = re.match(r"^([A-Za-z]+(?: [A-Za-z]+)?)\s*:\s*(.*)$", s)
    if m:
        key, val = m.group(1).lower(), m.group(2).strip()
        in_words = key == "keywords"
        if in_words and val:
            words += [w.strip() for w in re.split(r"[;,\n]", val) if w.strip()]
        elif not in_words:
            fields[key] = val
words = list(dict.fromkeys(words))[:12]
name = re.sub(r"\s+", " ", fields.get("topic", "")).strip()[:40]
region = (fields.get("region") or "CA").upper()[:2]
lang = "ar" if (fields.get("language") or "").lower().startswith("ar") else "en"
when = fields.get("period") or "7d"
if not re.fullmatch(r"\d{1,2}[dh]", when):
    when = "7d"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:30] or "topic"


def done(summary, detail=""):
    print(summary)
    if detail:
        print("\n" + detail)
    print("\nChanges show up in the app within about 10 minutes.")
    sys.exit(0)


if title.startswith("signal desk: watch"):
    import urllib.request, urllib.parse
    sym = re.sub(r"[^A-Za-z0-9.\-=@]", "", fields.get("symbol", "")).upper()
    wl = cfg.setdefault("watchlist", [])
    cur = next((w for w in wl if w["sym"] == sym), None)
    num = lambda v: float(v.replace(",", "").replace("$", "")) if re.fullmatch(r"\$?[\d,]+(\.\d+)?", (v or "").strip()) else None
    if not sym:
        done("Nothing changed: the symbol was missing.")
    if "remove" in title:
        if not cur:
            done(f"Nothing changed: {sym} is not on the watchlist.")
        wl.remove(cur); json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        done(f"Removed {sym} from the watchlist")
    name = ""
    try:
        u = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=" + urllib.parse.quote(sym)
             + "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json")
        q = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read())["FormattedQuoteResult"]["FormattedQuote"][0]
        if not q.get("last"):
            done(f"Nothing changed: {sym} was not found. Use the ticker as it trades, for example AAPL, or add -T for Toronto shares, for example RY-T.")
        name = q.get("name") or ""
    except SystemExit:
        raise
    except Exception:
        pass
    entry = cur or {"sym": sym}
    entry.update({"name": fields.get("name") or entry.get("name") or name, "note": fields.get("note", entry.get("note", "")),
                  "above": num(fields.get("alert above", fields.get("above", ""))), "below": num(fields.get("alert below", fields.get("below", "")))})
    if not cur:
        wl.append(entry)
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    done(("Updated " if cur else "Added ") + sym + (" · " + entry["name"] if entry["name"] else ""),
         "\n".join(x for x in [f"- Alert above: {entry['above']}" if entry["above"] else "", f"- Alert below: {entry['below']}" if entry["below"] else ""] if x))

if not name:
    done("Nothing changed: the topic name was missing.")
topic = next((t for t in cfg["topics"] if t["id"] == slug(name) or t["name"].lower() == name.lower()), None)

if "remove" in title:
    if not topic:
        done(f"Nothing changed: there is no topic called {name}.")
    if words:
        lw = {w.lower() for w in words}
        before = len(topic["feeds"])
        topic["feeds"] = [f for f in topic["feeds"] if f[1].lower() not in lw and f[0].lower() not in lw]
        removed = before - len(topic["feeds"])
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        done(f"Removed {removed} search(es) from {topic['name']}", "\n".join("- " + w for w in words))
    cfg["topics"] = [t for t in cfg["topics"] if t is not topic]
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    done(f"Removed the {topic['name']} topic")

if not words:
    done("Nothing changed: add at least one search word or phrase.")
new = topic is None
if new:
    topic = {"id": slug(name), "name": name, "gl": region, "hl": lang, "feeds": []}
    cfg["topics"].append(topic)
have = {f[1].lower() for f in topic["feeds"]}
added = []
for w in words:
    if w.lower() in have:
        continue
    label = w if len(w) <= 24 else w[:22].rstrip() + "…"
    gl = "" if region == (topic.get("gl") or "CA").upper() else region
    hl = "" if lang == (topic.get("hl") or "en") else lang
    topic["feeds"].append([label, w, gl, hl, when])
    added.append(w)
json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
done(("Added the " + name + " topic" if new else f"Added {len(added)} search(es) to {topic['name']}"),
     "\n".join("- " + w for w in added) or "(those searches were already there)")
