"""Eswatini tender watcher: reads ESPPRA's tender list (all pages) and merges it
into tenders.json (new / open / expired / removed). Run: python scraper.py"""
import hashlib, json, re, sys, time, datetime as dt
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from dateutil import parser as dparse

ROOT = Path(__file__).parent
OUT = ROOT / "tenders.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EswatiniTenderWatcher/1.1; public tender monitoring)"}
now = lambda: dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
LABELS = ("tender type", "procurement method", "submission deadline", "download")

def to_iso(text):
    try: return dparse.parse(text, fuzzy=True, dayfirst=True).date().isoformat()
    except Exception: return None

def parse_esppra(html, base):
    """Each tender starts at an <h5> title and runs until the next <h5>."""
    soup, recs, cur = BeautifulSoup(html, "html.parser"), [], None
    for node in soup.descendants:
        if isinstance(node, Tag) and node.name == "h5":
            cur = {"title": node.get_text(" ", strip=True), "lines": [], "url": None}
            recs.append(cur)
        elif cur is not None:
            if isinstance(node, Tag) and node.name == "a" and node.get("href") \
               and "download" in node.get_text().lower():
                cur["url"] = urljoin(base, node["href"])
            elif isinstance(node, NavigableString) and node.strip():
                if not any(p.name == "a" for p in node.parents if isinstance(p, Tag)):
                    cur["lines"].append(" ".join(node.split()))
    out = []
    for r in recs:
        lines = [l for l in r["lines"] if l and l != r["title"]]
        if not r["title"] or not r["url"]: continue          # not a tender block
        def field(label):
            for i, l in enumerate(lines):
                if l.lower().startswith(label):
                    rest = l.split(":", 1)[1].strip() if ":" in l else ""
                    return rest or (lines[i + 1] if i + 1 < len(lines) else "")
            return ""
        plain = [l for l in lines if not l.lower().startswith(LABELS)
                 and l.lower() not in ("open tender", "request for tender", "request for proposal")
                 and not re.match(r"^(open|restricted|direct)\b.*tender$", l, re.I)
                 and not re.match(r"^request for ", l, re.I)]
        deadline = field("submission deadline")
        out.append(dict(
            title=r["title"], url=r["url"],
            entity=plain[0] if plain else "", reference=plain[1] if len(plain) > 1 else "",
            method=field("procurement method"), type=field("tender type"),
            closing=to_iso(deadline), closing_text=deadline, source="ESPPRA (official)"))
    return out

def fetch_esppra(s):
    items, page = [], 1
    while page <= 20:                                         # safety cap
        r = requests.get(s["url"], params={"Page": page, "txtKeyword": ""}, headers=HEADERS, timeout=40)
        r.raise_for_status()
        got = parse_esppra(r.text, s["url"])
        if not got: break
        items += got
        if f"Page={page + 1}" not in r.text: break
        page += 1; time.sleep(1)
    return items

def main():
    old = json.loads(OUT.read_text()) if OUT.exists() else {"tenders": {}}
    db, seen, log = old["tenders"], set(), []
    for s in json.loads((ROOT / "sources.json").read_text()):
        if not s.get("enabled"): continue
        try:
            items = fetch_esppra(s)
            log.append(f'{s["name"]}: {len(items)} tenders')
        except Exception as e:
            log.append(f'{s["name"]}: FAILED ({e})'); continue
        for it in items:
            tid = hashlib.sha1(it["url"].encode()).hexdigest()[:12]
            seen.add(tid)
            rec = db.get(tid, {"id": tid, "first_seen": now()})
            rec.update(it, last_seen=now()); db[tid] = rec
    today, cutoff = dt.date.today().isoformat(), (dt.date.today() - dt.timedelta(days=30)).isoformat()
    for tid in list(db):
        rec = db[tid]
        if tid not in seen and rec["last_seen"][:10] < cutoff: del db[tid]; continue
        rec["is_new"] = rec["first_seen"][:10] == today
        if rec.get("closing") and rec["closing"] < today: rec["status"] = "expired"
        elif tid in seen: rec["status"] = "open"
        else: rec["status"] = "removed"
    OUT.write_text(json.dumps({"updated": now(), "log": log, "tenders": db}, indent=1))
    print("\n".join(log))

if __name__ == "__main__":
    sys.exit(main())
