import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")
STATE_FILE = Path("seen_commits.json")
MAX_SEEN = 500

FEEDS = {
    "Zshah101": "https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/commits/main.atom",
    "Simplify / Pitt CSC": "https://github.com/SimplifyJobs/Summer2027-Internships/commits/dev.atom",
}

KEYWORDS = ["security", "cloud", "linux", "network", "infrastructure", "sre", "devops", "systems"]

# \b + keyword + \w* => "network" matches "networking", but "sre" won't match inside other words
KEYWORD_RE = re.compile(r"\b(" + "|".join(map(re.escape, KEYWORDS)) + r")\w*", re.IGNORECASE)
URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')
TAG_RE = re.compile(r"<[^>]+>")
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

IGNORED_DOMAINS = ("github.com", "githubusercontent.com", "shields.io", "w3.org", "schema.org")
IMAGE_EXTS = (".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp")
HEADERS = {"User-Agent": "internship-watcher/1.0"}


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_seen() -> list[str]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return []


def save_seen(seen: list[str]) -> None:
    STATE_FILE.write_text(json.dumps(seen[-MAX_SEEN:], indent=0))


def new_commits(feed_url: str, seen: set[str]) -> list[tuple[str, str, str]]:
    root = ET.fromstring(http_get(feed_url))
    commits = []
    for entry in root.findall("a:entry", ATOM_NS):
        cid = entry.findtext("a:id", default="", namespaces=ATOM_NS)
        link_el = entry.find("a:link", ATOM_NS)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        title = (entry.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
        if cid and link and cid not in seen:
            commits.append((cid, link, title))
    return list(reversed(commits))  # oldest first so alerts arrive in order


def added_markdown_lines(commit_link: str) -> list[str]:
    """Return lines added in this commit, but only from .md files (skips listings.json etc.)."""
    patch = http_get(commit_link + ".patch")
    lines, in_md = [], False
    for raw in patch.splitlines():
        if raw.startswith("diff --git"):
            in_md = raw.lower().endswith(".md")
        elif in_md and raw.startswith("+") and not raw.startswith("+++"):
            lines.append(raw[1:])
    return lines


def extract_links(line: str) -> list[str]:
    links = []
    for url in URL_RE.findall(line):
        url = url.rstrip(".,;")
        low = url.lower()
        if any(d in low for d in IGNORED_DOMAINS) or low.endswith(IMAGE_EXTS):
            continue
        if url not in links:
            links.append(url)
    return links


def clean_text(line: str) -> str:
    text = URL_RE.sub("", line)
    text = unescape(TAG_RE.sub(" ", text))
    text = re.sub(r"[|*\[\]()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:200]


def find_matches(lines: list[str]) -> list[dict]:
    matches = []
    for line in lines:
        text = clean_text(line)
        kws = sorted({m.group(1).lower() for m in KEYWORD_RE.finditer(text)})
        if kws:
            matches.append({"text": text, "keywords": kws, "links": extract_links(line)})
    return matches


def post_discord(content: str) -> bool:
    body = json.dumps({"content": content[:2000]}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=body, headers={**HEADERS, "Content-Type": "application/json"}
    )
    for _ in range(3):
        try:
            urllib.request.urlopen(req, timeout=30)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After", 2)))
                continue
            print(f"Discord error {e.code}: {e.read()[:200]!r}")
            return False
        except Exception as e:
            print(f"Discord error: {e}")
            return False
    return False


def format_alert(repo: str, commit_link: str, match: dict) -> str:
    if match["links"]:
        links = "\n".join(f"👉 **Apply:** {u}" for u in match["links"][:3])
    else:
        links = f"👉 **View diff:** <{commit_link}>"
    return (
        f"🚨 **New internship ({repo})**\n"
        f"{match['text']}\n"
        f"**Matched:** `{', '.join(match['keywords'])}`\n"
        f"{links}"
    )


def main() -> None:
    if not WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK is not set")

    seen_list = load_seen()
    seen = set(seen_list)
    sent = 0

    for repo, feed_url in FEEDS.items():
        try:
            commits = new_commits(feed_url, seen)
        except Exception as e:
            print(f"Feed failed ({repo}): {e}")
            continue

        for cid, link, title in commits:
            seen.add(cid)
            seen_list.append(cid)
            try:
                matches = find_matches(added_markdown_lines(link))
            except Exception as e:
                print(f"Patch failed ({link}): {e}")
                continue
            print(f"[{repo}] {title!r}: {len(matches)} match(es)")
            for m in matches:
                if post_discord(format_alert(repo, link, m)):
                    sent += 1
                time.sleep(1)  # stay under Discord webhook rate limits

    save_seen(seen_list)
    print(f"Sent {sent} alert(s); tracking {len(seen_list)} commits")


if __name__ == "__main__":
    main()
