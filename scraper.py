"""Eswatini tender watcher: fetches each portal in sources.json, merges results
into tenders.json (new / still open / expired / removed). Run: python scraper.py"""
import hashlib, json, re, sys, datetime as dt
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dparse

ROOT = Path(__file__).parent
OUT = ROOT / "tenders.json"
KEYWORDS = re.compile(r"tender|rfp|rfq|eoi|bid|proposal|expression of interest|supply|provision|construction", re.I)
DATE_RE = re.compile(r"\b(\d{1,2}[\s/-]\w{3,9}[\s/-]\d{4}|\d{4}-\d{2}-\d{2}|\w{3,9} \d{1,2}, \d{4})\b")
HEADERS = {"User-Agent": "EswatiniTenderWatcher/1.0 (public tender monitoring)"}
now = lambda: dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

def find_date(text):
    for m in DATE_RE.findall(text or ""):
        try: return dparse.parse(m, dayfirst=True).date().isoformat()
        except Exception: pass
    return None

def parse_page(html, base, source):
    """Heuristic parser: table rows first, then keyword links. Tune per portal if needed."""
    soup, found = BeautifulSoup(html, "html.parser"), []
    for tr in soup.select("table tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        a = tr.find("a", href=True)
        if len(cells) < 2 or not a: continue
        title = max(cells, key=len)
        if not KEYWORDS.search(title) and len(title) < 25: continue
        found.append(dict(title=title, url=urljoin(base, a["href"]), closing=find_date(" ".join(cells[1:])), source=source))
    if not found:
        for a in soup.find_all("a", href=True):
            t = a.get_text(" ", strip=True)
            if len(t) > 20 and KEYWORDS.search(t):
                ctx = a.parent.get_text(" ", strip=True)
                found.append(dict(title=t, url=urljoin(base, a["href"]), closing=find_date(ctx), source=source))
    return found

def main():
    old = json.loads(OUT.read_text()) if OUT.exists() else {"tenders": {}}
    db, seen, log = old["tenders"], set(), []
    for s in json.loads((ROOT / "sources.json").read_text()):
        if not s.get("enabled"): continue
        try:
            r = requests.get(s["url"], headers=HEADERS, timeout=30); r.raise_for_status()
            items = parse_page(r.text, s["url"], s["name"])
            log.append(f'{s["name"]}: {len(items)} items')
        except Exception as e:
            log.append(f'{s["name"]}: FAILED ({e})'); continue
        for it in items:
            tid = hashlib.sha1((it["url"] + it["title"]).encode()).hexdigest()[:12]
            seen.add(tid)
            rec = db.get(tid, {"id": tid, "first_seen": now()})
            rec.update(it, last_seen=now()); db[tid] = rec
    today = dt.date.today().isoformat()
    for tid, rec in db.items():
        rec["is_new"] = rec["first_seen"][:10] == today
        if rec.get("closing") and rec["closing"] < today: rec["status"] = "expired"
        elif tid in seen: rec["status"] = "open"
        else: rec["status"] = "removed"
    OUT.write_text(json.dumps({"updated": now(), "log": log, "tenders": db}, indent=1))
    print("\n".join(log))

if __name__ == "__main__":
    sys.exit(main())
