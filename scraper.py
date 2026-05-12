import os
import sys
import time
import json
import hashlib
import requests
import mimetypes
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
    """Download any media file (Image, Video, Audio) and save it with SHA-256 hash name"""
    try:
        # Longer timeout for larger media files like videos or music
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        sha256 = hashlib.sha256()
        media_data = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            sha256.update(chunk)
            media_data.extend(chunk)
            
        file_hash = sha256.hexdigest()
        
        # Smart extension detection
        if ".mp4" in url or "video" in url:
            ext = ".mp4"
        elif ".ogg" in url or ".mp3" in url or "audio" in url:
            ext = ".ogg"  # Telegram voices are typically OGG Opus
        elif ".jpg" in url or ".jpeg" in url or "image" in url:
            ext = ".jpg"
        else:
            # Fallback to mimetypes detection if URL doesn't help
            content_type = response.headers.get('content-type', '')
            ext = mimetypes.guess_extension(content_type) or ".file"
            
        file_name = f"{file_hash}{ext}"
        file_path = os.path.join(BUFFER_DIR, "media", file_name)
        
        # Save file only if it does not already exist
        if not os.path.exists(file_path):
            with open(file_path, "wb") as f:
                f.write(media_data)
                
        return {"file_hash_sha256": file_hash, "relative_path": f"media/{file_name}"}
    except Exception as e:
        print(f"Failed to download media from {url}: {e}")
        return None

def extract_message_data(msg, msg_id):
    """Extract text (with emoji fix) and all media types from a message DOM element"""
    
    # --- Part 1: Text Extraction (Fixing Emoji Newlines) ---
    text_div = msg.find("div", class_="tgme_widget_message_text")
    msg_text = ""
    msg_html = ""
    
    if text_div:
        msg_html = str(text_div)
        # Convert <br> tags to actual newlines before stripping tags
        for br in text_div.find_all("br"):
            br.replace_with("\n")
        # Use empty separator to prevent BeautifulSoup from adding newlines around <i> tags (emojis)
        msg_text = text_div.get_text(separator="").strip()

    # --- Part 2: Multi-Media Extraction ---
    media_info = None
    
    # 1. Try Photo
    photo_wrap = msg.find("a", class_="tgme_widget_message_photo_wrap")
    if photo_wrap:
        style = photo_wrap.get("style", "")
        start = style.find("url('") + 5
        end = style.find("')", start)
        if start > 4 and end > -1:
            img_url = style[start:end]
            media_info = download_and_hash(img_url)

    # 2. Try Video (if no photo)
    if not media_info:
        video_tag = msg.find("video", class_="tgme_widget_message_video")
        if video_tag and video_tag.get("src"):
            media_info = download_and_hash(video_tag.get("src"))

    # 3. Try Audio/Voice (if no photo or video)
    if not media_info:
        # Standard Audio
        audio_tag = msg.find("audio")
        if audio_tag and audio_tag.get("src"):
            media_info = download_and_hash(audio_tag.get("src"))
        else:
            # Telegram Round Videos (often treated as voice/video mix)
            round_video = msg.find("video", class_="tgme_widget_message_roundvideo")
            if round_video and round_video.get("src"):
                media_info = download_and_hash(round_video.get("src"))

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
    max_pages = 30 # Safety limit (approx 600 messages max)

    # 3. Pagination loop
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
            if msg_id < smallest_id_on_page:
                smallest_id_on_page = msg_id
                
            if msg_id <= last_id:
                continue

            parsed_msg = extract_message_data(msg, msg_id)
            page_parsed_messages.append(parsed_msg)
            
            if msg_id > highest_id:
                highest_id = msg_id

        channel_messages.extend(page_parsed_messages)

        if last_id == 0:
            print(f"Cold start detected for {channel}. Processed first page only.")
            break

        if smallest_id_on_page > last_id:
            current_url = f"https://t.me/s/{channel}?before={smallest_id_on_page}"
            print(f"Gap detected. Loading older messages before ID: {smallest_id_on_page}...")
            time.sleep(1)
        else:
            break

    # 4. Save data to the buffer if new messages exist
    if channel_messages:
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
            
        channels_state[channel] = highest_id
        total_new_messages_scraped += len(channel_messages)
        print(f"Saved {len(channel_messages)} messages for {channel}. Max ID: {highest_id}")
    else:
        print(f"No new messages for {channel}.")

# 5. Finalize process
if total_new_messages_scraped > 0:
    with open(lock_file_path, "w") as f:
        f.write("LOCKED")

    with open("new_state.json", "w", encoding="utf-8") as f:
        json.dump(channels_state, f, ensure_ascii=False)
        
    print(f"\nTotal new messages across all channels: {total_new_messages_scraped}")
else:
    print("\nNo new messages across any channels.")
