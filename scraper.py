"""Eswatini tender watcher. Reads every portal in sources.json and merges the results
into tenders.json (new / open / expired / removed).  Run: python scraper.py
Source types in sources.json:  "esppra" (ESPPRA list), "res" (RES tabbed tables),
"links" (generic: any tender-looking link or PDF on a page)."""
import hashlib, json, re, sys, time, datetime as dt
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from dateutil import parser as dparse

ROOT = Path(__file__).parent
OUT = ROOT / "tenders.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EswatiniTenderWatcher/1.2; public tender monitoring)"}
now = lambda: dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
LABELS = ("tender type", "procurement method", "submission deadline", "download")
KEYWORDS = re.compile(r"tender|rfp|rfq|rfi|eoi|\bbid|proposal|expressions? of interest|invitation|advert|"
                      r"supply|provision|construction|procurement|consultan|prequalif", re.I)
NOT_TENDER = re.compile(r"policy|privacy|slavery|cookie|newsletter|annual report|terms\s*(&|and)\s*conditions|"
                        r"trading (terms|conditions)|standard trading|data protection|code of conduct", re.I)
DATE_RE = re.compile(r"\b(\d{1,2}(?:st|nd|rd|th)?[\s/-]+\w{3,9}[\s/-]+\d{4}|\d{4}-\d{2}-\d{2})\b")

def to_iso(text):
    try:
        iso = bool(re.search(r"\d{4}-\d{1,2}-\d{1,2}", text))
        return dparse.parse(text, fuzzy=True, dayfirst=not iso).date().isoformat()
    except Exception:
        return None

def get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=40, **kw); r.raise_for_status(); return r

# ---------------------------------------------------------------- ESPPRA
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
        if not r["title"] or not r["url"]: continue
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
        out.append(dict(title=r["title"], url=r["url"],
            entity=plain[0] if plain else "", reference=plain[1] if len(plain) > 1 else "",
            method=field("procurement method"), type=field("tender type"),
            closing=to_iso(deadline), closing_text=deadline, source="ESPPRA (official)"))
    return out

def fetch_esppra(s):
    items, page = [], 1
    while page <= 20:
        r = get(s["url"], params={"Page": page, "txtKeyword": ""})
        got = parse_esppra(r.text, s["url"])
        if not got: break
        items += got
        if f"Page={page + 1}" not in r.text: break
        page += 1; time.sleep(1)
    return items

# ---------------------------------------------------------------- RES (tabbed tables)
def parse_res(html, s):
    soup, out = BeautifulSoup(html, "html.parser"), []
    base = s["url"].split("#")[0]
    panes = [(soup.find(id=i), k) for i, k in (("home", "Open Tender"), ("profile2", "Restricted Tender"))]
    panes = [(p, k) for p, k in panes if p] or [(soup, "Tender (see RES website)")]
    for pane, kind in panes:
        for tr in pane.select("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 3 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[-1]): continue
            name, ref, closing = cells[-3], cells[-2], cells[-1]
            a = tr.find("a", href=True)
            out.append(dict(title=name.title() if name.isupper() else name,
                url=urljoin(base, a["href"]) if a else f"{base}#{ref}",
                entity=s["name"], reference=ref, method="", type=kind,
                closing=to_iso(closing), closing_text=closing, source=s["name"]))
    return out

# ---------------------------------------------------------------- generic pages
def parse_links(html, s):
    """Any link that looks like a tender, or points to a PDF/Word file (policy/terms files are skipped).
    A link that just says 'Download' takes its title from the heading above it.
    Optional in sources.json: "include" / "exclude" regexes."""
    soup, seen, out = BeautifulSoup(html, "html.parser"), set(), []
    inc = re.compile(s["include"], re.I) if s.get("include") else None
    exc = re.compile(s["exclude"], re.I) if s.get("exclude") else None
    for a in soup.find_all("a", href=True):
        text, href = a.get_text(" ", strip=True), urljoin(s["url"], a["href"])
        if href in seen or href.startswith(("mailto:", "tel:", "javascript:")) or href.split("#")[0] == s["url"].split("#")[0]: continue
        is_doc = bool(re.search(r"\.(pdf|docx?|xlsx?)(\?|$)", href, re.I))
        ctx = " ".join((a.parent.get_text(" ", strip=True) if a.parent else "").split())
        if a.find_parent(["nav", "header", "footer"]): continue          # skip menus and footers
        if is_doc and (len(text) < 16 or re.match(r"(preview|download|view|read more|click here|more)\b", text, re.I)):
            h = a.find_previous(re.compile(r"^h[1-6]$"))
            title = (h.get_text(" ", strip=True) if h else "") or ctx[:160]
            if not title or title.lower() in ("tenders", "open tenders", "procurement notices"):
                title = Path(href.split("?")[0]).stem.replace("%20", " ").replace("_", " ").replace("-", " ")
        else:
            title = text
        if NOT_TENDER.search(title) or NOT_TENDER.search(href): continue
        m = DATE_RE.search(ctx)
        if not (is_doc or (KEYWORDS.search(title) and (len(title.split()) >= 4 or m))): continue
        if inc and not inc.search(title): continue
        if exc and exc.search(title): continue
        seen.add(href)
        up = re.search(r"/uploads/(\d{4})/(\d{2})/", href)
        out.append(dict(title=title, url=href, entity=s["name"], reference="", method="", type="",
            closing=to_iso(m.group(1)) if m else None, closing_text=m.group(1) if m else "",
            posted=f"{up.group(1)}-{up.group(2)}" if up else "", source=s["name"]))
    return out

def fetch(s):
    t = s.get("type", "links")
    if t == "esppra": return fetch_esppra(s)
    html = get(s["url"].split("#")[0]).text
    return parse_res(html, s) if t == "res" else parse_links(html, s)

# ---------------------------------------------------------------- main
def main():
    old = json.loads(OUT.read_text()) if OUT.exists() else {"tenders": {}}
    db, seen, log = old["tenders"], set(), []
    for s in json.loads((ROOT / "sources.json").read_text()):
        if not s.get("enabled"): continue
        try:
            items = fetch(s)
            log.append(f'{s["name"]}: {len(items)} tenders' if items else f'{s["name"]}: no tenders listed right now')
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
