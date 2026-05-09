import os
import sys
import time
import json
import hashlib
import requests
from bs4 import BeautifulSoup

# 1. Check lock state to avoid race conditions
if os.path.exists("lock.txt"):
    print("Lock file exists. Consumer has not processed the data yet. Exiting.")
    sys.exit(0)

# Load the JSON string from GitHub Variables
channels_state_raw = os.getenv("CHANNELS_STATE", "{}")
try:
    channels_state = json.loads(channels_state_raw)
except json.JSONDecodeError:
    print("Invalid JSON format in CHANNELS_STATE variable.")
    sys.exit(1)

# Create necessary directories
os.makedirs("data", exist_ok=True)
os.makedirs("media", exist_ok=True)

def download_and_hash(url):
    """Download media file and save it with a SHA-256 hash name"""
    try:
        response = requests.get(url, stream=True, timeout=15)
        response.raise_for_status()
        
        sha256 = hashlib.sha256()
        media_data = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            sha256.update(chunk)
            media_data.extend(chunk)
            
        file_hash = sha256.hexdigest()
        ext = ".jpg" if ".jpg" in url else ".mp4" if ".mp4" in url else ".file"
        file_name = f"{file_hash}{ext}"
        file_path = os.path.join("media", file_name)
        
        if not os.path.exists(file_path):
            with open(file_path, "wb") as f:
                f.write(media_data)
                
        return {"file_hash_sha256": file_hash, "relative_path": f"media/{file_name}"}
    except Exception as e:
        print(f"Failed to download {url}: {e}")
        return None

total_new_messages_scraped = 0
run_timestamp = int(time.time())

# 2. Iterate over all channels defined in the state
for channel, last_id in channels_state.items():
    base_url = f"https://t.me/s/{channel}"
    print(f"\n--- Scraping {channel} after ID: {last_id} ---")
    
    try:
        html = requests.get(base_url, timeout=10).text
    except Exception as e:
        print(f"Failed to fetch {channel}: {e}")
        continue
        
    soup = BeautifulSoup(html, "html.parser")
    messages_html = soup.find_all("div", class_="tgme_widget_message")

    parsed_messages = []
    highest_id = last_id

    for msg in messages_html:
        post_param = msg.get("data-post", "")
        if not post_param:
            continue
            
        msg_id = int(post_param.split("/")[-1])
        if msg_id <= last_id:
            continue

        # Extract text content
        text_div = msg.find("div", class_="tgme_widget_message_text")
        msg_text = text_div.get_text(separator="\n") if text_div else ""
        msg_html = str(text_div) if text_div else ""

        # Extract media (e.g., photos)
        media_info = None
        photo_wrap = msg.find("a", class_="tgme_widget_message_photo_wrap")
        if photo_wrap:
            style = photo_wrap.get("style", "")
            start = style.find("url('") + 5
            end = style.find("')", start)
            if start > 4 and end > -1:
                img_url = style[start:end]
                media_info = download_and_hash(img_url)

        parsed_messages.append({
            "message_id": msg_id,
            "content": {"text": msg_text, "html": msg_html},
            "media": media_info
        })
        
        if msg_id > highest_id:
            highest_id = msg_id

    # 3. Save data if new messages exist for this specific channel
    if parsed_messages:
        json_filename = f"data/{run_timestamp}_{channel}.json"
        output_data = {
            "scrape_metadata": {
                "channel_username": channel,
                "scrape_timestamp": run_timestamp,
                "messages_count": len(parsed_messages)
            },
            "messages": parsed_messages
        }

        with open(json_filename, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
            
        # Update the state dictionary with the new highest ID
        channels_state[channel] = highest_id
        total_new_messages_scraped += len(parsed_messages)
        print(f"Saved {len(parsed_messages)} messages for {channel}. Max ID: {highest_id}")
    else:
        print(f"No new messages for {channel}.")

# 4. Finalize process if any data was collected across all channels
if total_new_messages_scraped > 0:
    # Create the synchronization lock file
    with open("lock.txt", "w") as f:
        f.write("LOCKED")

    # Save the updated state to a file so GitHub Actions can read it
    with open("new_state.json", "w", encoding="utf-8") as f:
        json.dump(channels_state, f, ensure_ascii=False)
        
    print(f"\nTotal new messages across all channels: {total_new_messages_scraped}")
else:
    print("\nNo new messages across any channels.")
