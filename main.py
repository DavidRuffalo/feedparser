import urllib.request
import xml.etree.ElementTree as ET
import json
import os
import ssl
import re
from datetime import datetime, timezone, timedelta

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

FEED_URLS = [
    "https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/commits/main.atom",
    "https://github.com/SimplifyJobs/Summer2027-Internships/commits/dev.atom"
]

# Real target roles
KEYWORDS = ["security", "cloud", "linux", "network", "infrastructure", "sre", "devops", "systems"]

def extract_job_links(html_content):
    """Finds direct job application links inside the commit text, ignoring GitHub system links."""
    raw_urls = re.findall(r'https?://[^\s<>"\'\)]+', html_content)
    job_urls = []
    
    ignored_domains = [
        "github.com", "githubusercontent.com", "w3.org", "schema.org", 
        "shields.io", "actions", "api.github"
    ]
    
    for url in raw_urls:
        if not any(domain in url.lower() for domain in ignored_domains):
            # Clean trailing punctuation
            clean_url = url.rstrip('.,;')
            if clean_url not in job_urls:
                job_urls.append(clean_url)
                
    return job_urls

def fetch_and_notify():
    if not WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK secret is missing!")
        return

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    # Live mode: checks commits from the last 15 minutes
    now = datetime.now(timezone.utc)
    time_threshold = now - timedelta(days=30)
    
    seen_links = set()

    for feed_url in FEED_URLS:
        repo_name = "Zshah101" if "zshah101" in feed_url else "Simplify / Pitt CSC"
        
        try:
            req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            response = urllib.request.urlopen(req, context=ctx)
            xml_data = response.read()
            
            root = ET.fromstring(xml_data)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            entries = root.findall('atom:entry', ns)
            
            for entry in entries:
                title = entry.find('atom:title', ns).text or ""
                commit_link = entry.find('atom:link', ns).attrib.get('href', '')
                updated_str = entry.find('atom:updated', ns).text or ""
                
                content_elem = entry.find('atom:content', ns)
                content_text = content_elem.text if content_elem is not None else ""
                
                searchable_text = f"{title} {content_text}".lower()
                
                try:
                    updated_time = datetime.fromisoformat(updated_str.replace('Z', '+00:00'))
                except ValueError:
                    continue
                    
                if updated_time > time_threshold:
                    matched_keywords = [kw for kw in KEYWORDS if kw in searchable_text]
                    
                    if matched_keywords and commit_link not in seen_links:
                        seen_links.add(commit_link)
                        
                        # Extract actual application URLs from commit body
                        direct_apply_links = extract_job_links(content_text)
                        
                        send_discord_alert(title, commit_link, direct_apply_links, repo_name, matched_keywords)
                        
        except Exception as e:
            print(f"❌ Failed to process feed {repo_name}: {e}")

def send_discord_alert(title, commit_link, direct_links, repo_name, keywords):
    # Format direct apply links
    if direct_links:
        formatted_apply_links = "\n".join([f"👉 **Apply Here:** {url}" for url in direct_links[:3]])
    else:
        formatted_apply_links = f"👉 **View Diff on GitHub:** {commit_link}"

    payload = {
        "content": (
            f"🚨 **NEW INTERNSHIP DETECTED ({repo_name})**\n\n"
            f"**Matched Keywords:** `{', '.join(keywords)}` \n"
            f"{formatted_apply_links}\n\n"
            f"*(Commit Details: <{commit_link}>)*"
        )
    }
    
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    
    req = urllib.request.Request(WEBHOOK_URL, data=json.dumps(payload).encode('utf-8'), headers=headers)
    
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"❌ Failed to send alert: {e}")

if __name__ == "__main__":
    fetch_and_notify()
