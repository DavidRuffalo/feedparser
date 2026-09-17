#!/usr/bin/env python3
"""
Internship watcher v2
- Reads structured aggregator data (Simplify listings.json, vanshb03 listings.json, zshah101 CSV)
- Polls target companies' ATS boards directly (Greenhouse / Lever / Ashby / Workday) — upstream of the aggregators
- Dedupes by normalized job URL (+ company|title), persisted in state.json
- Scores each posting; title matches / priority companies alert instantly, weaker matches go to a digest
- Rich Discord embeds, optional role ping, daily heartbeat, throttled error alerts

Stdlib only. Python 3.10+.

Usage:
  python watcher.py                 # normal run
  python watcher.py --check-targets # verify every ATS target returns data (no Discord, no state change)
  python watcher.py --dry-run       # print what would be sent, don't post or save
  python watcher.py --seed          # mark everything currently listed as seen, alert nothing
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

NOW = datetime.now(timezone.utc)
UA = {"User-Agent": "internship-watcher/2.0 (personal job-alert bot)"}

# --------------------------------------------------------------------------- defaults

DEFAULT_CONFIG: dict = {
    # weight = points when the keyword appears in the TITLE. Appearing only in category/skills/team = 1 point.
    "keywords": {
        "security": 3, "cyber": 3, "cloud": 3, "linux": 3, "network": 3,
        "infrastructure": 3, "sre": 3, "site reliability": 3, "devops": 3,
        "system": 2, "platform": 2,
    },
    # regex; a posting must match one of these in the title to count as an internship
    "require_title": r"\b(intern|internship|co-?op)\b",
    # regex list; any match in the title drops the posting
    "exclude_title": [r"\bsenior\b", r"\bph\.?d\b", r"\bmba\b", r"\bdirector\b", r"\bmanager\b"],
    # keep postings whose stated term matches one of these; postings with no stated term are kept
    "allowed_terms": ["Summer 2027"],
    # any internship from these companies alerts even without a keyword hit (targets below are priority automatically)
    "priority_companies": [],
    "priority_bonus": 3,
    "instant_min_score": 3,
    "digest_every_hours": 3,
    "digest_flush_at": 12,
    "heartbeat_every_hours": 24,
    "error_alert_every_hours": 6,
    "stale_hours": 72,              # postings older than this never ping instantly; they go to the digest
    "us_only": True,                # drop postings whose location is clearly outside the United States
    "location_unknown_ok": True,    # keep postings with no location / bare "Remote" / unrecognised city
    "first_run_hours": 48,          # on the very first run, only alert on postings newer than this
    "seen_ttl_days": 120,
    "discord_role_id": "",          # optional: numeric role ID to @mention so your phone buzzes
    "aggregators": [
        {"name": "simplify", "type": "simplify_json", "repo": "SimplifyJobs/Summer2027-Internships", "branches": ["dev", "main"]},
        {"name": "vanshb03", "type": "simplify_json", "repo": "vanshb03/Summer2027-Internships", "branches": ["dev", "main"]},
        {"name": "zshah101", "type": "zshah_csv",
         "url": "https://raw.githubusercontent.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/main/data/internships.csv"},
    ],
    "targets": [],
    # Auto-discover ATS boards from the job URLs in the aggregator data, and poll them directly.
    "auto_targets": {
        "enabled": True,
        "ats": ["greenhouse", "lever", "ashby"],   # add "workday" if you accept slower runs
        "max_boards": 800,
        "workers": 24,                              # concurrency for Greenhouse / Lever / Ashby
        "workday_workers": 6,                       # concurrency inside the selected Workday shard
        "workday_shards": 5,                        # rotate auto-discovered Workday boards across runs
        "workday_delay": 0.5,                       # seconds between Workday page requests
        "exclude": [],                              # board tokens to skip, e.g. ["andurilindustries"]
    },
}

# --------------------------------------------------------------------------- model


@dataclass
class Listing:
    source: str
    company: str
    title: str
    url: str
    location: str = ""
    posted: Optional[datetime] = None
    term: str = ""
    extra: str = ""                       # category / skills / team — secondary match text
    flags: list = field(default_factory=list)
    score: int = 0
    matched: list = field(default_factory=list)
    priority: bool = False

    def to_json(self) -> dict:
        d = self.__dict__.copy()
        d["posted"] = self.posted.isoformat() if self.posted else None
        return d

    @staticmethod
    def from_json(d: dict) -> "Listing":
        d = dict(d)
        d["posted"] = parse_dt(d.get("posted"))
        return Listing(**d)


# --------------------------------------------------------------------------- helpers


def http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 60, retries: int = 3) -> str:
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})},
                                 method="POST" if data else "GET")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = e.headers.get("Retry-After")
                time.sleep(min(float(wait) if wait and wait.isdigit() else 2.0 * (attempt + 1), 15))
                continue
            raise
    raise RuntimeError("unreachable")


def get_json(url: str, **kw):
    return json.loads(http(url, **kw))


def parse_dt(v) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        if v > 1e12:          # milliseconds
            v /= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    if re.search(r"today|just posted|hours? ago|minutes? ago", s, re.I):
        return NOW
    if re.search(r"yesterday", s, re.I):
        return NOW - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*days?\s*ago", s, re.I)
    if m:
        return NOW - timedelta(days=int(m.group(1)))
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


TRACKING_PARAM = re.compile(r"^(utm_|ref$|source$|src$|gh_src$|lever-|ashby_|rx_|mkt_tok$)", re.I)


def norm_url(u: str) -> str:
    p = urllib.parse.urlsplit(u.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)]
    return urllib.parse.urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), p.path.rstrip("/"),
                                    urllib.parse.urlencode(q), ""))


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def age_hours(d: Optional[datetime]) -> Optional[float]:
    return None if d is None else (NOW - d).total_seconds() / 3600


# --------------------------------------------------------------------------- aggregator sources


def src_simplify_json(a: dict) -> list[Listing]:
    last_err: Exception | None = None
    data = None
    for br in a.get("branches", ["dev", "main"]):
        url = f"https://raw.githubusercontent.com/{a['repo']}/{br}/.github/scripts/listings.json"
        try:
            data = get_json(url)
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code != 404:
                raise
    if data is None:
        raise last_err or RuntimeError("no listings.json found")

    out = []
    for j in data:
        if not j.get("active", True) or not j.get("is_visible", True):
            continue
        url = j.get("url") or ""
        if not url:
            continue
        flags = []
        sp = (j.get("sponsorship") or "").strip()
        if sp and sp.lower() not in ("offers sponsorship", "other"):
            flags.append(sp)
        out.append(Listing(
            source=a["name"], company=j.get("company_name", ""), title=j.get("title", ""), url=url,
            location="; ".join(j.get("locations") or []), posted=parse_dt(j.get("date_posted")),
            term=", ".join(j.get("terms") or []),
            extra=" ".join([j.get("category") or "", " ".join(j.get("degrees") or [])]), flags=flags,
        ))
    return out


ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
URL_RE = re.compile(r"^https?://", re.I)


def src_zshah_csv(a: dict) -> list[Listing]:
    rows = [r for r in csv.reader(io.StringIO(http(a["url"]))) if r]
    if not rows:
        return []

    has_header = not any(URL_RE.match(c) for c in rows[0])
    header = [h.strip().lower() for h in rows[0]] if has_header else []
    body = rows[1:] if has_header else rows

    def col(*cands) -> Optional[int]:
        for c in cands:
            for i, h in enumerate(header):
                if c in h:
                    return i
        return None

    idx = {
        "company": col("company") if has_header else 0,
        "title": col("role", "title", "position") if has_header else 1,
        "cycle": col("cycle", "term", "season") if has_header else 2,
        "category": col("category") if has_header else 3,
        "location": col("location") if has_header else 4,
        "sponsor": col("sponsor", "visa", "citizen") if has_header else 5,
        "skills": col("skill", "stack", "tech") if has_header else None,
        "posted": col("posted", "published") if has_header else None,
        "url": col("url", "apply", "link") if has_header else None,
    }

    def cell(r: list[str], key: str) -> str:
        i = idx.get(key)
        return r[i].strip() if i is not None and i < len(r) else ""

    out = []
    for r in body:
        url = cell(r, "url") or next((c for c in r if URL_RE.match(c)), "")
        if not url:
            continue
        posted_raw = cell(r, "posted") or next((c for c in r if ISO_RE.match(c)), "")
        sp = cell(r, "sponsor").lower()
        flags = [sp] if sp and sp not in ("unknown", "sponsors", "") else []
        out.append(Listing(
            source=a["name"], company=cell(r, "company"), title=cell(r, "title"), url=url,
            location=cell(r, "location"), posted=parse_dt(posted_raw), term=cell(r, "cycle"),
            extra=" ".join([cell(r, "category"), cell(r, "skills")]), flags=flags,
        ))
    if not out:
        raise RuntimeError(f"CSV parsed but produced 0 listings (header={header[:12]})")
    return out


# --------------------------------------------------------------------------- direct ATS sources


def src_greenhouse(t: dict) -> list[Listing]:
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{t['board']}/jobs")
    return [
        Listing(source=f"greenhouse:{t['board']}", company=t["company"], title=j.get("title", ""),
                url=j.get("absolute_url", ""), location=(j.get("location") or {}).get("name", ""),
                posted=parse_dt(j.get("first_published") or j.get("updated_at")))
        for j in data.get("jobs", []) if j.get("absolute_url")
    ]


def src_lever(t: dict) -> list[Listing]:
    data = get_json(f"https://api.lever.co/v0/postings/{t['site']}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        url = j.get("hostedUrl") or j.get("applyUrl") or ""
        if not url:
            continue
        out.append(Listing(
            source=f"lever:{t['site']}", company=t["company"], title=j.get("text", ""), url=url,
            location=cats.get("location", "") or "", posted=parse_dt(j.get("createdAt")),
            extra=" ".join(str(cats.get(k, "") or "") for k in ("team", "department", "commitment")),
        ))
    return out


def src_ashby(t: dict) -> list[Listing]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{t['board']}")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        if not url:
            continue
        out.append(Listing(
            source=f"ashby:{t['board']}", company=t["company"], title=j.get("title", ""), url=url,
            location=j.get("location", "") or "", posted=parse_dt(j.get("publishedAt")),
            extra=" ".join(str(j.get(k, "") or "") for k in ("department", "team", "employmentType")),
        ))
    return out


_WD_MULTI = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def workday_resolve_location(l: Listing) -> None:
    """Workday lists multi-site postings as '3 Locations'. Fetch the detail record to get real locations/country."""
    if not l.source.startswith("workday:") or not _WD_MULTI.match(l.location or ""):
        return
    try:
        p = urllib.parse.urlsplit(l.url)
        parts = [x for x in p.path.split("/") if x]
        segs = [x for x in parts if not _LOCALE.match(x)]        # drop en-US
        site, path = segs[0], "/" + "/".join(segs[1:])
        tenant = p.netloc.split(".")[0]
        info = get_json(f"{p.scheme}://{p.netloc}/wday/cxs/{tenant}/{site}{path}",
                        headers={"Accept": "application/json"}, timeout=20, retries=1).get("jobPostingInfo") or {}
        locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
        country = ((info.get("country") or {}).get("descriptor") or "")
        text = "; ".join(x for x in locs if x)
        if country:
            text = f"{text} ({country})" if text else country
        if text:
            l.location = text
    except Exception:
        pass                                                      # keep "N Locations"; filter treats it as unknown


def src_workday(t: dict) -> list[Listing]:
    host = f"https://{t['tenant']}.{t.get('wd', 'wd5')}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{t['tenant']}/{t['site']}/jobs"
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    out, offset, page = [], 0, 20
    while offset < int(t.get("max", 100)):
        body = json.dumps({"appliedFacets": {}, "limit": page, "offset": offset,
                           "searchText": t.get("search", "intern")}).encode()
        data = get_json(api, data=body, headers=hdrs)
        posts = data.get("jobPostings") or []
        for j in posts:
            path = j.get("externalPath") or ""
            if not path:
                continue
            out.append(Listing(
                source=f"workday:{t['tenant']}", company=t["company"], title=j.get("title", ""),
                url=f"{host}/en-US/{t['site']}{path}", location=j.get("locationsText", "") or "",
                posted=parse_dt(j.get("postedOn", "")), extra=" ".join(j.get("bulletFields") or []),
            ))
        if len(posts) < page:
            break
        offset += page
        time.sleep(float(t.get("delay", 0.5)))
    return out


ATS: dict[str, Callable[[dict], list[Listing]]] = {
    "greenhouse": src_greenhouse, "lever": src_lever, "ashby": src_ashby, "workday": src_workday,
}
AGG: dict[str, Callable[[dict], list[Listing]]] = {
    "simplify_json": src_simplify_json, "zshah_csv": src_zshah_csv,
}


def target_label(t: dict) -> str:
    ident = t.get("tenant") if t["ats"] == "workday" else (t.get("board") or t.get("site"))
    return f"{t['ats']}:{ident}"



# --------------------------------------------------------------------------- location (US filter)

_US_STATES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|"
              "OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
_US_STATE_NAMES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
                   "illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
                   "missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|"
                   "oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|"
                   "washington|west virginia|wisconsin|wyoming|puerto rico")
US_RE = re.compile(
    r"\b(united states|u\.s\.a?\.?|usa|us)\b"                   # explicit country
    r"|\b(" + _US_STATE_NAMES + r")\b"                          # full state names
    r"|(?:,|-|\s)\s*(" + _US_STATES + r")\b(?![a-z])"           # ", TX" / "-Ohio-" style / "US CA"
    r"|\b\d{5}(?:-\d{4})?\b"                                    # zip code
    r"|\b(nyc|sf bay|bay area|silicon valley|remote\s*[-–(]?\s*us)\b",
    re.I)
_CA_PROV = "ON|BC|QC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU"
FOREIGN_RE = re.compile(
    r"\b(canada|canadian|toronto|vancouver|montreal|montr[ée]al|ottawa|calgary|waterloo|ontario|quebec|british columbia|alberta"
    r"|united kingdom|uk|u\.k\.|england|london|manchester|cambridge uk|edinburgh|scotland|ireland|dublin|cork|galway|waterford|limerick"
    r"|germany|berlin|munich|frankfurt|hamburg|france|paris|netherlands|amsterdam|belgium|brussels|switzerland|zurich|z[üu]rich|geneva"
    r"|sweden|stockholm|norway|oslo|denmark|copenhagen|finland|helsinki|poland|warsaw|krak[óo]w|czech|prague|austria|vienna"
    r"|spain|madrid|barcelona|portugal|lisbon|italy|milan|rome|greece|athens|romania|bucharest|hungary|budapest|estonia|tallinn"
    r"|india|bangalore|bengaluru|hyderabad|mumbai|pune|chennai|delhi|gurgaon|gurugram|noida|kolkata"
    r"|singapore|japan|tokyo|osaka|korea|seoul|taiwan|taipei|china|shanghai|beijing|shenzhen|hangzhou|hong kong|vietnam|hanoi|ho chi minh"
    r"|philippines|manila|indonesia|jakarta|malaysia|kuala lumpur|thailand|bangkok"
    r"|australia|sydney|melbourne au|brisbane|new zealand|auckland|wellington"
    r"|israel|tel aviv|herzliya|haifa|uae|dubai|abu dhabi|saudi|riyadh|qatar|doha|turkey|istanbul|egypt|cairo|nigeria|lagos|kenya|nairobi|south africa|cape town|johannesburg"
    r"|brazil|s[ãa]o paulo|mexico|ciudad de m[ée]xico|mexico city|guadalajara|monterrey|argentina|buenos aires|chile|santiago|colombia|bogot[áa]|peru|lima"
    r"|emea|apac|latam)\b"
    r"|,\s*(" + _CA_PROV + r")\b(?![a-z])",
    re.I)


def is_us_location(location: str) -> Optional[bool]:
    """True = clearly US, False = clearly outside the US, None = can't tell (blank, 'Remote', unknown city)."""
    if not location or not location.strip():
        return None
    # any segment that is US → US (multi-location roles usually list several)
    segs = re.split(r"[;|/]|\s+\+\d+\s+more|\band\b", location)
    us = any(US_RE.search(seg) for seg in segs)
    foreign = any(FOREIGN_RE.search(seg) for seg in segs)
    if us:
        return True
    if foreign:
        return False
    return None

# --------------------------------------------------------------------------- filtering & scoring

TERM_IN_TITLE = re.compile(r"\b(summer|fall|autumn|spring|winter)\s*'?(20\d\d|\d\d)\b", re.I)


def compile_rules(cfg: dict) -> dict:
    return {
        "require": re.compile(cfg["require_title"], re.I),
        "exclude": [re.compile(p, re.I) for p in cfg["exclude_title"]],
        "kw": {kw: re.compile(r"\b" + re.escape(kw) + (r"\w*" if len(kw) >= 5 else r"\b"), re.I) for kw in cfg["keywords"]},
        "allowed_terms": [a.lower() for a in cfg["allowed_terms"]],
        "priority": {norm_text(c) for c in cfg["priority_companies"]} | {norm_text(t["company"]) for t in cfg["targets"]},
    }


def evaluate(l: Listing, cfg: dict, rx: dict) -> Optional[str]:
    """Returns 'instant', 'digest', or None (drop)."""
    if not rx["require"].search(l.title):
        return None
    if any(r.search(l.title) for r in rx["exclude"]):
        return None

    if cfg.get("us_only", True):
        us = is_us_location(l.location)
        if us is None:
            us = is_us_location(l.title)          # titles often carry "- Cincinnati OH" or "(Dublin, Ireland)"
        if us is False or (us is None and not cfg.get("location_unknown_ok", True)):
            return None

    term_text = (l.term or "").lower()
    if not term_text:
        m = TERM_IN_TITLE.search(l.title)
        term_text = m.group(0).lower() if m else ""
    if rx["allowed_terms"] and term_text and not any(a in term_text for a in rx["allowed_terms"]):
        return None

    secondary = f"{l.extra} {l.location}"
    score, matched, title_hit = 0, [], False
    for kw, w in cfg["keywords"].items():
        pat = rx["kw"][kw]
        if pat.search(l.title):
            matched.append(kw); score += w; title_hit = True
        elif pat.search(secondary):
            matched.append(kw + "*"); score += 1

    l.priority = norm_text(l.company) in rx["priority"]
    if l.priority:
        score += cfg["priority_bonus"]
    age = age_hours(l.posted)
    if age is not None and age < 24:
        score += 1
    if not any(l.source == a["name"] for a in cfg["aggregators"]):
        score += 1                           # came straight from the company's ATS

    any_intern_ok = l.priority and cfg.get("priority_any_intern", False)
    if not matched and not any_intern_ok:
        return None
    l.score, l.matched = score, matched
    instant = (title_hit or any_intern_ok) and score >= cfg["instant_min_score"]
    if instant and age is not None and age > cfg.get("stale_hours", 72):
        instant = False                      # old posting: everyone has seen it; don't buzz the phone
    return "instant" if instant else "digest"


# --------------------------------------------------------------------------- discord


class Discord:
    def __init__(self, webhook: str, role_id: str = "", dry_run: bool = False):
        self.webhook, self.role_id, self.dry_run = webhook, role_id, dry_run

    def _post(self, payload: dict) -> bool:
        if self.dry_run:
            print("DRY-RUN discord payload:", json.dumps(payload, indent=1)[:1500])
            return True
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.webhook, data=body,
                                     headers={**UA, "Content-Type": "application/json"})
        for _ in range(4):
            try:
                urllib.request.urlopen(req, timeout=30)
                time.sleep(0.6)
                return True
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(float(e.headers.get("Retry-After", 2)) + 0.5)
                    continue
                print(f"Discord HTTP {e.code}: {e.read()[:300]!r}")
                return False
            except Exception as e:
                print(f"Discord error: {e}")
                return False
        return False

    def send(self, content: str, embeds: list[dict], ping: bool = False) -> int:
        mention = f"<@&{self.role_id}> " if (ping and self.role_id) else ""
        sent = 0
        for i in range(0, len(embeds), 5):
            payload = {
                "content": (mention + content) if i == 0 else "",
                "embeds": embeds[i:i + 5],
                "allowed_mentions": {"parse": [], "roles": [self.role_id] if (ping and self.role_id) else []},
            }
            if self._post(payload):
                sent += len(payload["embeds"])
        return sent

    def text(self, content: str) -> bool:
        return self._post({"content": content[:2000], "allowed_mentions": {"parse": []}})


def embed_for(l: Listing) -> dict:
    color = 0xE74C3C if l.priority else (0xF39C12 if l.score >= 5 else 0x3498DB)
    fields = [{"name": "Location", "value": (l.location or "—")[:1000], "inline": True}]
    if l.term:
        fields.append({"name": "Term", "value": l.term[:200], "inline": True})
    posted = l.posted.strftime("%b %d, %H:%M UTC") if l.posted else "unknown"
    if l.posted:
        h = age_hours(l.posted)
        posted += f" ({h:.0f}h ago)" if h < 48 else f" ({h / 24:.0f}d ago)"
    fields.append({"name": "Posted", "value": posted, "inline": True})
    if l.flags:
        fields.append({"name": "Flags", "value": ", ".join(l.flags)[:1000], "inline": False})
    why = ", ".join(l.matched) or "priority company"
    return {
        "title": f"{l.company} — {l.title}"[:256], "url": l.url, "color": color, "fields": fields,
        "footer": {"text": f"{l.source} · score {l.score} · {why}"[:2048]},
    }


# --------------------------------------------------------------------------- state


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"WARNING: {path} unreadable, starting fresh")
    return default


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(path, {}))
    return cfg


def load_state(path: Path) -> dict:
    s = load_json(path, {})
    s.setdefault("seen", {})
    s.setdefault("pending_digest", [])
    s.setdefault("last_digest", None)
    s.setdefault("last_heartbeat", None)
    s.setdefault("error_alerts", {})
    s.setdefault("runs", 0)
    return s


def save_state(path: Path, state: dict) -> None:
    """Write state atomically so a reboot cannot leave a half-written JSON file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=0)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def hours_since(iso: Optional[str]) -> float:
    d = parse_dt(iso)
    return float("inf") if d is None else (NOW - d).total_seconds() / 3600



# --------------------------------------------------------------------------- auto-discovery of ATS boards

_LOCALE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
_SLUG = re.compile(r"^[A-Za-z0-9._-]{2,80}$")


def board_from_url(url: str) -> Optional[dict]:
    """Recognise a Greenhouse / Lever / Ashby / Workday job URL and return a target dict (without company)."""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host, parts = p.netloc.lower(), [x for x in p.path.split("/") if x]
    qs = dict(urllib.parse.parse_qsl(p.query))

    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        if parts and parts[0] == "embed":
            tok = qs.get("for")
        else:
            tok = parts[0] if parts else None
        return {"ats": "greenhouse", "board": tok.lower()} if tok and _SLUG.match(tok) else None

    if host == "jobs.lever.co":
        return {"ats": "lever", "site": parts[0].lower()} if parts and _SLUG.match(parts[0]) else None

    if host == "jobs.ashbyhq.com":
        return {"ats": "ashby", "board": parts[0].lower()} if parts and _SLUG.match(parts[0]) else None

    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m:
        segs = [x for x in parts if not _LOCALE.match(x)]
        if segs and segs[0] != "job" and _SLUG.match(segs[0]):
            return {"ats": "workday", "tenant": m.group(1), "wd": m.group(2), "site": segs[0], "search": "intern", "max": 200}
    return None


def board_key(t: dict) -> str:
    return target_label(t)


def discover_boards(listings: list[Listing], cfg: dict, state: dict) -> list[dict]:
    """Merge boards found in this run's aggregator URLs with those remembered in state; return auto targets."""
    ac = cfg["auto_targets"]
    if not ac.get("enabled"):
        return []
    allowed = set(ac.get("ats", []))
    manual = {board_key(t) for t in cfg["targets"]}
    exclude = {x.lower() for x in ac.get("exclude", [])}
    known: dict[str, dict] = state.setdefault("boards", {})
    now_iso = NOW.isoformat()

    for l in listings:
        t = board_from_url(l.url)
        if not t or t["ats"] not in allowed:
            continue
        k = board_key(t)
        tok = (t.get("board") or t.get("site") or t.get("tenant") or "").lower()
        if k in manual or tok in exclude:
            continue
        rec = known.get(k)
        if rec is None:
            known[k] = {**t, "company": l.company or tok, "first_seen": now_iso, "last_seen": now_iso}
            state.setdefault("_new_boards", []).append(k)
        else:
            rec["last_seen"] = now_iso
            if l.company and (not rec.get("company") or rec["company"] == tok):
                rec["company"] = l.company

    # newest-seen first, cap the count; skip boards that have failed 3+ times (retry those once a day)
    boards = sorted(known.values(), key=lambda r: r.get("last_seen", ""), reverse=True)
    out = []
    for b in boards:
        if b["ats"] not in allowed or board_key(b) in manual:
            continue
        if b.get("fails", 0) >= 3 and hours_since(b.get("last_fail")) < 24:
            continue
        if b["ats"] == "workday":
            b = {**b, "delay": float(ac.get("workday_delay", 0.5))}
        out.append(b)
    return out[: int(ac.get("max_boards", 800))]


def poll_boards(targets: list[dict], workers: int) -> tuple[list[Listing], dict[str, int], dict[str, str]]:
    listings, counts, errors = [], {}, {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(ATS[t["ats"]], t): t for t in targets}
        for f in as_completed(futs):
            label = target_label(futs[f])
            try:
                got = f.result()
                counts[label] = len(got)
                listings += got
            except Exception as e:
                errors[label] = f"{type(e).__name__}: {e}"
    return listings, counts, errors


def select_workday_shard(boards: list[dict], state: dict, requested_shards: int) -> tuple[list[dict], int, int]:
    """Select one stable Workday shard and advance the persisted cursor."""
    if not boards:
        return [], 0, 1
    shard_count = max(1, min(int(requested_shards), len(boards)))
    shard_index = int(state.get("workday_shard_cursor", 0)) % shard_count
    selected = [
        board for board in boards
        if zlib.crc32(board_key(board).encode("utf-8")) % shard_count == shard_index
    ]
    state["workday_shard_cursor"] = (shard_index + 1) % shard_count
    return selected, shard_index, shard_count

# --------------------------------------------------------------------------- main


def collect(cfg: dict, state: dict) -> tuple[list[Listing], dict[str, int], dict[str, str], dict]:
    """Returns listings, counts, errors (aggregators + manual targets), and auto-board stats."""
    listings, counts, errors = [], {}, {}
    for a in cfg["aggregators"]:
        try:
            got = AGG[a["type"]](a)
            counts[a["name"]] = len(got)
            listings += got
        except Exception as e:
            errors[a["name"]] = f"{type(e).__name__}: {e}"

    workers = int(cfg["auto_targets"].get("workers", 24))
    m_list, m_counts, m_errors = poll_boards(cfg["targets"], workers)
    listings += m_list; counts.update(m_counts); errors.update(m_errors)

    auto_cfg = cfg["auto_targets"]
    auto = discover_boards(listings, cfg, state)
    fast = [b for b in auto if b["ats"] != "workday"]
    slow = [b for b in auto if b["ats"] == "workday"]
    slow_selected, shard_index, shard_count = select_workday_shard(
        slow, state, int(auto_cfg.get("workday_shards", 5))
    )

    fast_started = time.monotonic()
    a_list, a_counts, a_errors = poll_boards(fast, workers)
    fast_seconds = time.monotonic() - fast_started

    workday_started = time.monotonic()
    w_list, w_counts, w_errors = poll_boards(
        slow_selected, int(auto_cfg.get("workday_workers", 6))
    )
    workday_seconds = time.monotonic() - workday_started
    a_list += w_list; a_counts.update(w_counts); a_errors.update(w_errors)
    listings += a_list

    known = state.get("boards", {})
    for k in a_counts:
        if k in known:
            known[k]["fails"] = 0
    for k, msg in a_errors.items():
        if k in known and re.search(r"HTTP Error (404|410|422)", msg):
            known[k]["fails"] = known[k].get("fails", 0) + 1
            known[k]["last_fail"] = NOW.isoformat()

    auto_stats = {
        "boards": len(fast) + len(slow_selected),
        "known_boards": len(auto),
        "ok": len(a_counts),
        "failed": len(a_errors),
        "postings": sum(a_counts.values()),
        "errors": a_errors,
        "fast_seconds": fast_seconds,
        "workday_seconds": workday_seconds,
        "workday_polled": len(slow_selected),
        "workday_known": len(slow),
        "workday_shard": shard_index + 1,
        "workday_shards": shard_count,
    }
    return listings, counts, errors, auto_stats


def check_targets(cfg: dict) -> int:
    bad = 0
    intern_re = re.compile(cfg["require_title"], re.I)
    for t in cfg["targets"]:
        label = f"{t['company']} [{target_label(t)}]"
        try:
            got = ATS[t["ats"]](t)
            interns = [l.title for l in got if intern_re.search(l.title)]
            print(f"OK   {label}: {len(got)} postings, {len(interns)} intern/co-op")
            for title in interns[:3]:
                print(f"       - {title}")
        except Exception as e:
            bad += 1
            print(f"FAIL {label}: {type(e).__name__}: {e}")
    print(f"\n{len(cfg['targets']) - bad}/{len(cfg['targets'])} targets OK")
    return bad


def main() -> None:
    run_started = time.monotonic()
    app_dir = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(app_dir / "config.json"))
    ap.add_argument("--state", default=str(app_dir / "state.json"))
    ap.add_argument("--dry-run", action="store_true", help="print instead of posting; don't save state")
    ap.add_argument("--seed", action="store_true", help="mark all current postings seen, alert nothing")
    ap.add_argument("--check-targets", action="store_true", help="verify ATS targets respond")
    ap.add_argument("--list-boards", action="store_true", help="print auto-discovered boards from state and exit")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    if args.check_targets:
        sys.exit(1 if check_targets(cfg) else 0)
    if args.list_boards:
        boards = load_state(Path(args.state)).get("boards", {})
        for k, b in sorted(boards.items(), key=lambda kv: kv[1].get("company", "").lower()):
            print(f"{b.get('company','?'):40} {k}")
        print(f"\n{len(boards)} boards discovered")
        return

    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    if not webhook and not args.dry_run:
        sys.exit("DISCORD_WEBHOOK is not set")
    role_id = cfg.get("discord_role_id") or os.environ.get("DISCORD_ROLE_ID", "")
    dc = Discord(webhook, role_id, dry_run=args.dry_run)

    state = load_state(Path(args.state))
    seen: dict[str, str] = state["seen"]
    first_run = not seen
    rx = compile_rules(cfg)
    now_iso = NOW.isoformat()

    listings, counts, errors, auto = collect(cfg, state)
    for name, n in counts.items():
        if n == 0:
            errors[name] = "returned 0 postings (schema change or empty board?)"

    instant, digest, evaluated = [], [], 0
    for l in listings:
        if not rx["require"].search(l.title):
            continue                      # not an internship title; cheap to re-check, don't store
        k_url = norm_url(l.url)
        k_ct = "ct:" + norm_text(l.company) + "|" + norm_text(l.title)
        if k_url in seen or k_ct in seen:
            continue
        seen[k_url] = now_iso
        seen[k_ct] = now_iso
        evaluated += 1
        workday_resolve_location(l)
        verdict = evaluate(l, cfg, rx)
        if verdict is None:
            continue
        if args.seed:
            continue
        newly_discovered = l.source in state.get("_new_boards", [])
        if (first_run or newly_discovered) and (age_hours(l.posted) is None or age_hours(l.posted) > cfg["first_run_hours"]):
            continue                      # board is new to us: only alert on genuinely fresh postings
        (instant if verdict == "instant" else digest).append(l)

    # cross-source dedupe within this run (same URL from two aggregators)
    def dedupe(ls: list[Listing]) -> list[Listing]:
        out, keys = [], set()
        for l in sorted(ls, key=lambda x: -x.score):
            k = norm_url(l.url)
            if k not in keys:
                keys.add(k); out.append(l)
        return out

    instant, digest = dedupe(instant), dedupe(digest)
    sent = 0
    if instant:
        header = f"🚨 **{len(instant)} new internship{'s' if len(instant) != 1 else ''} matching your filters**"
        sent += dc.send(header, [embed_for(l) for l in instant], ping=True)

    state["pending_digest"] += [l.to_json() for l in digest]
    pending = state["pending_digest"]
    if pending and (hours_since(state["last_digest"]) >= cfg["digest_every_hours"] or len(pending) >= cfg["digest_flush_at"]):
        ls = dedupe([Listing.from_json(d) for d in pending])
        dc.send(f"📋 **Digest — {len(ls)} weaker match{'es' if len(ls) != 1 else ''}** (keyword in category/skills only)",
                [embed_for(l) for l in ls[:25]], ping=False)
        state["pending_digest"], state["last_digest"] = [], now_iso

    for name, msg in errors.items():
        if hours_since(state["error_alerts"].get(name)) >= cfg["error_alert_every_hours"]:
            dc.text(f"⚠️ Source **{name}** failed: `{msg[:300]}`")
            state["error_alerts"][name] = now_iso
        print(f"ERROR {name}: {msg}")

    if hours_since(state["last_heartbeat"]) >= cfg["heartbeat_every_hours"] and not first_run:
        summary = ", ".join(f"{k} {v}" for k, v in counts.items())
        dc.text(f"💓 Heartbeat — {len(counts)} sources OK, {len(errors)} failing · "
                f"auto-boards {auto['ok']}/{auto['boards']} polled OK ({auto['postings']} postings) · "
                f"Workday shard {auto['workday_shard']}/{auto['workday_shards']} "
                f"({auto['workday_polled']}/{auto['workday_known']} boards) · tracking {len(seen)//2} postings\n`{summary}`"[:2000])
        state["last_heartbeat"] = now_iso

    # prune old seen keys
    cutoff = NOW - timedelta(days=cfg["seen_ttl_days"])
    state["seen"] = {k: v for k, v in seen.items() if (parse_dt(v) or NOW) > cutoff}
    state["runs"] += 1
    state.pop("_new_boards", None)
    if not args.dry_run:
        save_state(Path(args.state), state)

    mode = "SEED" if args.seed else ("FIRST RUN" if first_run else "run")
    duration = time.monotonic() - run_started
    print(f"[{mode}] fetched {len(listings)} · auto-boards {auto['ok']}/{auto['boards']} polled ok "
          f"({auto['known_boards']} known) · fast ATS {auto['fast_seconds']:.1f}s · "
          f"Workday shard {auto['workday_shard']}/{auto['workday_shards']} "
          f"{auto['workday_polled']}/{auto['workday_known']} boards in {auto['workday_seconds']:.1f}s · "
          f"new {evaluated} · instant {len(instant)} · digest {len(digest)} · sent {sent} · errors {len(errors)} · "
          f"tracking {len(state['seen'])//2} · duration {duration:.1f}s")
    for label, msg in list(auto["errors"].items())[:15]:
        print(f"  auto-board failed {label}: {msg[:120]}")


if __name__ == "__main__":
    main()
