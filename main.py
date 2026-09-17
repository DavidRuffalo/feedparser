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

# Your target roles
KEYWORDS = ["e"]

def fetch_and_notify():
    # Bypass SSL verification issues
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    # Only look at commits from the last 15 minutes
    now = datetime.now(timezone.utc)
    time_threshold = now - timedelta(days=30)    
    # Track links we've already alerted about during this run to prevent duplicates
    seen_links = set()

    for feed_url in FEED_URLS:
        try:
            req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
            response = urllib.request.urlopen(req, context=ctx)
            xml_data = response.read()
            
            root = ET.fromstring(xml_data)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            
            for entry in root.findall('atom:entry', ns):
                title = entry.find('atom:title', ns).text
                link = entry.find('atom:link', ns).attrib['href']
                updated_str = entry.find('atom:updated', ns).text
                
                try:
                    # Format the timestamp
                    updated_time = datetime.fromisoformat(updated_str.replace('Z', '+00:00'))
                except ValueError:
                    continue
                    
                if updated_time > time_threshold:
                    # If the commit has a keyword and we haven't sent it yet, fire the alert!
                    if link not in seen_links and any(kw in title.lower() for kw in KEYWORDS):
                        seen_links.add(link)
                        send_discord_alert(title, link, feed_url)
                        
        except Exception as e:
            print(f"Failed to process feed {feed_url}: {e}")

def send_discord_alert(title, link, source_feed):
    if not WEBHOOK_URL:
        print("No Webhook URL found. Check your GitHub Secrets!")
        return
        
    # Quick formatting to show you which repository found the role
    repo_name = "Zshah101" if "zshah101" in source_feed else "Simplify / Pitt CSC"
        
    payload = {
        "content": f"🚨 **NEW ROLE DETECTED ({repo_name})**\n\n**Commit:** {title}\n**Link:** {link}"
    }
    
    req = urllib.request.Request(WEBHOOK_URL, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Failed to send alert to Discord: {e}")

if __name__ == "__main__":
    fetch_and_notify()
