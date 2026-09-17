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


def http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 30) -> str:
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


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


# --------------------------------------------------------------------------- filtering & scoring

TERM_IN_TITLE = re.compile(r"\b(summer|fall|autumn|spring|winter)\s*'?(20\d\d|\d\d)\b", re.I)


def compile_rules(cfg: dict) -> dict:
    return {
        "require": re.compile(cfg["require_title"], re.I),
        "exclude": [re.compile(p, re.I) for p in cfg["exclude_title"]],
        "kw": {kw: re.compile(r"\b" + re.escape(kw) + r"\w*", re.I) for kw in cfg["keywords"]},
        "allowed_terms": [a.lower() for a in cfg["allowed_terms"]],
        "priority": {norm_text(c) for c in cfg["priority_companies"]} | {norm_text(t["company"]) for t in cfg["targets"]},
    }


def evaluate(l: Listing, cfg: dict, rx: dict) -> Optional[str]:
    """Returns 'instant', 'digest', or None (drop)."""
    if not rx["require"].search(l.title):
        return None
    if any(r.search(l.title) for r in rx["exclude"]):
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
    return "instant" if (title_hit or any_intern_ok) and score >= cfg["instant_min_score"] else "digest"


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


def hours_since(iso: Optional[str]) -> float:
    d = parse_dt(iso)
    return float("inf") if d is None else (NOW - d).total_seconds() / 3600


# --------------------------------------------------------------------------- main


def collect(cfg: dict) -> tuple[list[Listing], dict[str, int], dict[str, str]]:
    listings, counts, errors = [], {}, {}
    for a in cfg["aggregators"]:
        try:
            got = AGG[a["type"]](a)
            counts[a["name"]] = len(got)
            listings += got
        except Exception as e:
            errors[a["name"]] = f"{type(e).__name__}: {e}"
    for t in cfg["targets"]:
        label = target_label(t)
        try:
            got = ATS[t["ats"]](t)
            counts[label] = len(got)
            listings += got
        except Exception as e:
            errors[label] = f"{type(e).__name__}: {e}"
    return listings, counts, errors


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--dry-run", action="store_true", help="print instead of posting; don't save state")
    ap.add_argument("--seed", action="store_true", help="mark all current postings seen, alert nothing")
    ap.add_argument("--check-targets", action="store_true", help="verify ATS targets respond")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    if args.check_targets:
        sys.exit(1 if check_targets(cfg) else 0)

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

    listings, counts, errors = collect(cfg)
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
        verdict = evaluate(l, cfg, rx)
        if verdict is None:
            continue
        if args.seed:
            continue
        if first_run and (age_hours(l.posted) is None or age_hours(l.posted) > cfg["first_run_hours"]):
            continue
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
        dc.text(f"💓 Heartbeat — {len(counts)} sources OK, {len(errors)} failing · tracking {len(seen)//2} postings\n`{summary}`"[:2000])
        state["last_heartbeat"] = now_iso

    # prune old seen keys
    cutoff = NOW - timedelta(days=cfg["seen_ttl_days"])
    state["seen"] = {k: v for k, v in seen.items() if (parse_dt(v) or NOW) > cutoff}
    state["runs"] += 1
    if not args.dry_run:
        Path(args.state).write_text(json.dumps(state, indent=0))

    mode = "SEED" if args.seed else ("FIRST RUN" if first_run else "run")
    print(f"[{mode}] fetched {len(listings)} · new {evaluated} · instant {len(instant)} · digest {len(digest)} · "
          f"sent {sent} · errors {len(errors)} · tracking {len(state['seen'])//2}")


if __name__ == "__main__":
    main()
