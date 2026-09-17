import urllib.request
import xml.etree.ElementTree as ET
import json
import os
import ssl
from datetime import datetime, timezone, timedelta

# Pulls your hidden webhook from GitHub Secrets
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")
# The Zshah101 Automated Tracker RSS Feed
FEED_URL = "https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/commits/main.atom"
# Your target roles
KEYWORDS = ["security", "cloud", "linux", "network", "infrastructure", "sre", "devops", "systems"]

def fetch_and_notify():
    # Bypass SSL verification issues
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    req = urllib.request.Request(FEED_URL, headers={'User-Agent': 'Mozilla/5.0'})
    response = urllib.request.urlopen(req, context=ctx)
    xml_data = response.read()
    
    root = ET.fromstring(xml_data)
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    
    # Only look at commits from the last 15 minutes to avoid spamming you with old roles
    now = datetime.now(timezone.utc)
    time_threshold = now - timedelta(minutes=15)

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
            # If the commit title has one of your keywords, fire the alert!
            if any(kw in title.lower() for kw in KEYWORDS):
                send_discord_alert(title, link)

def send_discord_alert(title, link):
    if not WEBHOOK_URL:
        print("No Webhook URL found. Check your GitHub Secrets!")
        return
        
    payload = {
        "content": f"🚨 **NEW ROLE DETECTED**\n\n**Commit:** {title}\n**Link:** {link}"
    }
    req = urllib.request.Request(WEBHOOK_URL, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req)

if __name__ == "__main__":
    fetch_and_notify()
