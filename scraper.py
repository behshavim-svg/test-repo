import os
import sys
import time
import json
import hashlib
import requests
import mimetypes
from bs4 import BeautifulSoup

# تنظیمات مسیر بافر
BUFFER_DIR = "../buffer"

# ۱. بررسی فایل قفل برای جلوگیری از تداخل
lock_file_path = os.path.join(BUFFER_DIR, "lock.txt")
if os.path.exists(lock_file_path):
    print("Lock file exists. Consumer is busy. Exiting.")
    sys.exit(0)

# بارگذاری وضعیت آخرین IDها
channels_state_raw = os.getenv("CHANNELS_STATE", "{}")
try:
    channels_state = json.loads(channels_state_raw)
except json.JSONDecodeError:
    print("Invalid JSON in CHANNELS_STATE.")
    sys.exit(1)

# ایجاد پوشه‌های مورد نیاز
os.makedirs(os.path.join(BUFFER_DIR, "data"), exist_ok=True)
os.makedirs(os.path.join(BUFFER_DIR, "media"), exist_ok=True)

def download_and_hash(url):
    """دانلود رسانه و ذخیره با نام هش شده برای جلوگیری از تکرار"""
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        sha256 = hashlib.sha256()
        media_data = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            sha256.update(chunk)
            media_data.extend(chunk)
            
        file_hash = sha256.hexdigest()
        
        # تشخیص پسوند
        if ".mp4" in url or "video" in url: ext = ".mp4"
        elif ".ogg" in url or ".mp3" in url or "audio" in url: ext = ".ogg"
        elif ".jpg" in url or ".jpeg" in url or "image" in url: ext = ".jpg"
        else:
            content_type = response.headers.get('content-type', '')
            ext = mimetypes.guess_extension(content_type) or ".file"
            
        file_name = f"{file_hash}{ext}"
        file_path = os.path.join(BUFFER_DIR, "media", file_name)
        
        if not os.path.exists(file_path):
            with open(file_path, "wb") as f:
                f.write(media_data)
                
        return {"file_hash_sha256": file_hash, "relative_path": f"media/{file_name}"}
    except Exception as e:
        print(f"Media Download Failed: {e}")
        return None

def extract_message_data(msg, msg_id):
    """استخراج دقیق متن اصلی و تفکیک آن از متن ریپلای"""
    
    # الف) استخراج متن ریپلای (در صورت وجود)
    reply_div = msg.find("div", class_="js-message_reply_text")
    reply_prefix = ""
    if reply_div:
        # پاکسازی متن ریپلای برای نمایش در یک خط
        reply_content = reply_div.get_text().strip().replace('\n', ' ')
        reply_prefix = f"🔄 **در پاسخ به:** «{reply_content}»\n\n"

    # ب) استخراج متن اصلی پیام (با استفاده از کلاس دقیق)
    # استفاده از js-message_text به جای کلاس عمومی برای جلوگیری از تداخل با ریپلای
    main_text_div = msg.find("div", class_="js-message_text")
    msg_text = ""
    msg_html = ""
    
    if main_text_div:
        msg_html = str(main_text_div)
        # کپی برای تغییرات بدون آسیب به HTML اصلی
        temp_soup = BeautifulSoup(msg_html, "html.parser")
        target_div = temp_soup.find("div")
        
        # تبدیل <br> به اینتر واقعی
        for br in target_div.find_all("br"):
            br.replace_with("\n")
            
        # استخراج متن با حفظ ساختار اموجی‌ها (بدون اضافه کردن فاصله توسط BS4)
        msg_text = target_div.get_text(separator="").strip()

    # ترکیب نهایی متن (ریپلای + متن اصلی)
    full_text = f"{reply_prefix}{msg_text}".strip()

    # ج) استخراج مدیا (عکس، ویدیو، صوت)
    media_info = None
    
    # تست عکس
    photo_wrap = msg.find("a", class_="tgme_widget_message_photo_wrap")
    if photo_wrap:
        style = photo_wrap.get("style", "")
        if "url('" in style:
            img_url = style.split("url('")[1].split("')")[0]
            media_info = download_and_hash(img_url)

    # تست ویدیو
    if not media_info:
        video_tag = msg.find("video", class_="tgme_widget_message_video")
        if video_tag and video_tag.get("src"):
            media_info = download_and_hash(video_tag.get("src"))

    # تست صوت یا ویدیو مسیج
    if not media_info:
        audio_tag = msg.find("audio")
        if audio_tag and audio_tag.get("src"):
            media_info = download_and_hash(audio_tag.get("src"))
        else:
            round_video = msg.find("video", class_="tgme_widget_message_roundvideo")
            if round_video and round_video.get("src"):
                media_info = download_and_hash(round_video.get("src"))

    return {
        "message_id": msg_id,
        "content": {"text": full_text, "html": msg_html},
        "media": media_info
    }

# --- شروع فرآیند اصلی اسکراپ ---
total_new_messages_scraped = 0
run_timestamp = int(time.time())

for channel, last_id in channels_state.items():
    print(f"\n--- Scraping {channel} | Last ID: {last_id} ---")
    
    current_url = f"https://t.me/s/{channel}"
    channel_messages = []
    highest_id = last_id
    page_count = 0
    max_pages = 30

    while current_url and page_count < max_pages:
        try:
            html = requests.get(current_url, timeout=15).text
            page_count += 1
        except Exception as e:
            print(f"Fetch Error: {e}")
            break
            
        soup = BeautifulSoup(html, "html.parser")
        messages_html = soup.find_all("div", class_="tgme_widget_message")
        
        if not messages_html: break

        smallest_id_on_page = float('inf')
        page_parsed_messages = []

        for msg in messages_html:
            post_param = msg.get("data-post", "")
            if not post_param: continue
                
            msg_id = int(post_param.split("/")[-1])
            if msg_id < smallest_id_on_page:
                smallest_id_on_page = msg_id
                
            if msg_id <= last_id: continue

            parsed_msg = extract_message_data(msg, msg_id)
            page_parsed_messages.append(parsed_msg)
            
            if msg_id > highest_id:
                highest_id = msg_id

        channel_messages.extend(page_parsed_messages)

        if last_id == 0 or smallest_id_on_page <= last_id:
            break

        current_url = f"https://t.me/s/{channel}?before={smallest_id_on_page}"
        print(f"Moving to earlier messages (Before {smallest_id_on_page})...")
        time.sleep(1)

    # ذخیره نتایج
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
        print(f"Success: {len(channel_messages)} new messages for {channel}.")

# ایجاد قفل و آپدیت وضعیت
if total_new_messages_scraped > 0:
    with open(lock_file_path, "w") as f:
        f.write("LOCKED")

    with open("new_state.json", "w", encoding="utf-8") as f:
        json.dump(channels_state, f, ensure_ascii=False)
    print(f"\nDone. Total: {total_new_messages_scraped}")
else:
    print("\nNo new content found.")
