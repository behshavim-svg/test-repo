import os
import sys
import time
import json
import hashlib
import requests
from bs4 import BeautifulSoup

# Define the path to the secondary (private) repository
# The GitHub Action workflow will clone the private data repo into this folder
BUFFER_DIR = "../buffer"

# 1. Check lock state in the buffer to avoid race conditions
lock_file_path = os.path.join(BUFFER_DIR, "lock.txt")
if os.path.exists(lock_file_path):
    print("Lock file exists in the buffer. Consumer has not processed the data yet. Exiting.")
    sys.exit(0)

# Load the JSON string from GitHub Variables (passed via environment)
channels_state_raw = os.getenv("CHANNELS_STATE", "{}")
try:
    channels_state = json.loads(channels_state_raw)
except json.JSONDecodeError:
    print("Invalid JSON format in CHANNELS_STATE variable.")
    sys.exit(1)

# Create necessary directories for storage inside the buffer repository
os.makedirs(os.path.join(BUFFER_DIR, "data"), exist_ok=True)
os.makedirs(os.path.join(BUFFER_DIR, "media"), exist_ok=True)

def download_and_hash(url):
    """Download media file (image or video) and save it to the buffer with a SHA-256 hash name"""
    try:
        # Added a longer timeout to accommodate larger video files
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        sha256 = hashlib.sha256()
        media_data = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            sha256.update(chunk)
            media_data.extend(chunk)
            
        file_hash = sha256.hexdigest()
        
        # Simple extension detection supporting mp4 for videos
        ext = ".jpg" if ".jpg" in url else ".mp4" if ".mp4" in url else ".file"
        file_name = f"{file_hash}{ext}"
        file_path = os.path.join(BUFFER_DIR, "media", file_name)
        
        # Save file only if it does not already exist
        if not os.path.exists(file_path):
            with open(file_path, "wb") as f:
                f.write(media_data)
                
        # Return relative path based on the root of the buffer repo
        return {"file_hash_sha256": file_hash, "relative_path": f"media/{file_name}"}
    except Exception as e:
        print(f"Failed to download {url}: {e}")
        return None

def extract_message_data(msg, msg_id):
    """Extract text, HTML, and media (images AND videos) from a single message DOM element"""
    
    # --- FIX 1: Extract text without breaking emojis ---
    text_div = msg.find("div", class_="tgme_widget_message_text")
    msg_text = ""
    msg_html = ""
    
    if text_div:
        msg_html = str(text_div)
        # Convert <br> tags to newlines manually
        for br in text_div.find_all("br"):
            br.replace_with("\n")
        # Extract text without adding newlines between inline elements like emojis
        msg_text = text_div.get_text(separator="").strip()

    # --- FIX 2: Extract both Photos and Videos ---
    media_info = None
    
    # Check for Photo first
    photo_wrap = msg.find("a", class_="tgme_widget_message_photo_wrap")
    if photo_wrap:
        style = photo_wrap.get("style", "")
        start = style.find("url('") + 5
        end = style.find("')", start)
        if start > 4 and end > -1:
            img_url = style[start:end]
            media_info = download_and_hash(img_url)
            
    # Check for Video if no photo was found
    if not media_info:
        video_tag = msg.find("video", class_="tgme_widget_message_video")
        if video_tag and video_tag.get("src"):
            vid_url = video_tag.get("src")
            media_info = download_and_hash(vid_url)

    return {
        "message_id": msg_id,
        "content": {"text": msg_text, "html": msg_html},
        "media": media_info
    }

total_new_messages_scraped = 0
run_timestamp = int(time.time())

# 2. Iterate over all channels defined in the state
for channel, last_id in channels_state.items():
    print(f"\n--- Scraping {channel} after ID: {last_id} ---")
    
    current_url = f"https://t.me/s/{channel}"
    channel_messages = []
    highest_id = last_id
    page_count = 0
    max_pages = 30 # Safety limit to prevent infinite loops (approx 600 messages max)

    # 3. Pagination loop for handling high volume of new messages
    while current_url and page_count < max_pages:
        try:
            html = requests.get(current_url, timeout=15).text
            page_count += 1
        except Exception as e:
            print(f"Failed to fetch {current_url}: {e}")
            break
            
        soup = BeautifulSoup(html, "html.parser")
        messages_html = soup.find_all("div", class_="tgme_widget_message")
        
        if not messages_html:
            break

        smallest_id_on_page = float('inf')
        page_parsed_messages = []

        for msg in messages_html:
            post_param = msg.get("data-post", "")
            if not post_param:
                continue
                
            msg_id = int(post_param.split("/")[-1])
            
            # Track the smallest ID to know if we need to load older pages
            if msg_id < smallest_id_on_page:
                smallest_id_on_page = msg_id
                
            # Skip messages that have already been read
            if msg_id <= last_id:
                continue

            parsed_msg = extract_message_data(msg, msg_id)
            page_parsed_messages.append(parsed_msg)
            
            # Track the highest ID processed overall
            if msg_id > highest_id:
                highest_id = msg_id

        channel_messages.extend(page_parsed_messages)

        # Boundary Condition: Cold Start
        if last_id == 0:
            print(f"Cold start detected for {channel}. Processed first page only.")
            break # Stop pagination immediately

        # Gap Detection: Check if there are unread messages older than this page
        if smallest_id_on_page > last_id:
            current_url = f"https://t.me/s/{channel}?before={smallest_id_on_page}"
            print(f"Gap detected. Loading older messages before ID: {smallest_id_on_page}...")
            time.sleep(1) # Be polite to Telegram servers to avoid rate limiting
        else:
            # We have successfully reached messages we've seen before
            break

    # 4. Save data to the buffer if new messages exist for this channel
    if channel_messages:
        # Sort messages chronologically (oldest to newest)
        channel_messages.sort(key=lambda x: x["message_id"])
        
        json_filename = os.path.join(BUFFER_DIR, "data", f"{run_timestamp}_{channel}.json")
        output_data = {
            "scrape_metadata": {
                "channel_username": channel,
                "scrape_timestamp": run_timestamp,
                "messages_count": len(channel_messages)
            },
            "messages": channel_messages
        }

        with open(json_filename, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
            
        # Update the state dictionary with the new highest ID
        channels_state[channel] = highest_id
        total_new_messages_scraped += len(channel_messages)
        print(f"Saved {len(channel_messages)} messages for {channel} in buffer. Max ID: {highest_id}")
    else:
        print(f"No new messages for {channel}.")

# 5. Finalize process if any data was collected across all channels
if total_new_messages_scraped > 0:
    # Create the synchronization lock file inside the buffer repository
    with open(lock_file_path, "w") as f:
        f.write("LOCKED")

    # Save the updated state to a file in the ENGINE repository (current dir)
    # so GitHub Actions can read it and update the repository variable
    with open("new_state.json", "w", encoding="utf-8") as f:
        json.dump(channels_state, f, ensure_ascii=False)
        
    print(f"\nTotal new messages across all channels: {total_new_messages_scraped}")
else:
    print("\nNo new messages across any channels.")
