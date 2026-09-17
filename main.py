import urllib.request
import xml.etree.ElementTree as ET
import json
import os
import ssl
from datetime import datetime, timezone, timedelta

# Pulls your hidden webhook from GitHub Secrets
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

# The top internship tracker RSS Feeds
FEED_URLS = [
    "https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/commits/main.atom",
    "https://github.com/SimplifyJobs/Summer2027-Internships/commits/dev.atom"
]

# Target keywords
KEYWORDS = ["security", "cloud", "linux", "network", "infrastructure", "sre", "devops", "systems"]

def fetch_and_notify():
    if not WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK secret is missing or empty! Check your GitHub Repository Secrets.")
        return

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    # Looking back 30 days for this test run
    now = datetime.now(timezone.utc)
    time_threshold = now - timedelta(minutes=15).
    
    seen_links = set()
    alerts_sent = 0

    for feed_url in FEED_URLS:
        repo_name = "Zshah101" if "zshah101" in feed_url else "Simplify / Pitt CSC"
        print(f"🔍 Checking feed for {repo_name}...")
        
        try:
            req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            response = urllib.request.urlopen(req, context=ctx)
            xml_data = response.read()
            
            root = ET.fromstring(xml_data)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            entries = root.findall('atom:entry', ns)
            print(f"   Found {len(entries)} total entries in {repo_name}.")
            
            for entry in entries:
                title = entry.find('atom:title', ns).text or ""
                link = entry.find('atom:link', ns).attrib.get('href', '')
                updated_str = entry.find('atom:updated', ns).text or ""
                
                # Search full commit content where actual job text lives
                content_elem = entry.find('atom:content', ns)
                content_text = content_elem.text if content_elem is not None else ""
                
                searchable_text = f"{title} {content_text}".lower()
                
                try:
                    updated_time = datetime.fromisoformat(updated_str.replace('Z', '+00:00'))
                except ValueError:
                    continue
                    
                if updated_time > time_threshold:
                    matched_keywords = [kw for kw in KEYWORDS if kw in searchable_text]
                    if matched_keywords and link not in seen_links:
                        seen_links.add(link)
                        print(f"   🎯 Match found in {repo_name} for keyword: '{matched_keywords[0]}'")
                        send_discord_alert(title, link, repo_name, matched_keywords)
                        alerts_sent += 1
                        
        except Exception as e:
            print(f"❌ Failed to process feed {repo_name}: {e}")

    print(f"✅ Finished. Total alerts sent to Discord: {alerts_sent}")

def send_discord_alert(title, link, repo_name, keywords):
    payload = {
        "content": f"🚨 **NEW ROLE DETECTED ({repo_name})**\n\n**Keywords Matched:** `{', '.join(keywords)}` \n**Commit:** {title}\n**Link:** {link}"
    }
    
    # Custom User-Agent header so Discord doesn't block Python
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    
    req = urllib.request.Request(WEBHOOK_URL, data=json.dumps(payload).encode('utf-8'), headers=headers)
    
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"   🚀 Discord alert sent! Status code: {resp.status}")
    except Exception as e:
        print(f"❌ Failed to send alert to Discord: {e}")

if __name__ == "__main__":
    fetch_and_notify()
