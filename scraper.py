import os
import sys
import time
import json
import hashlib
import requests
from bs4 import BeautifulSoup

# تنظیمات اولیه
CHANNEL = os.getenv("CHANNEL_USERNAME")
LAST_ID = int(os.getenv("LAST_MESSAGE_ID", "0"))
BASE_URL = f"https://t.me/s/{CHANNEL}"

# 1. بررسی وضعیت قفل
if os.path.exists("lock.txt"):
    print("Lock file exists. Consumer has not processed the data yet. Exiting.")
    sys.exit(0)

# ساخت پوشه‌های مورد نیاز
os.makedirs("data", exist_ok=True)
os.makedirs("media", exist_ok=True)

def download_and_hash(url):
    """دانلود مدیا و ذخیره با نام هش شده"""
    try:
        response = requests.get(url, stream=True, timeout=10)
        response.raise_for_status()
        
        sha256 = hashlib.sha256()
        media_data = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            sha256.update(chunk)
            media_data.extend(chunk)
            
        file_hash = sha256.hexdigest()
        
        # تشخیص پسوند (بسیار ساده‌شده - برای پروداکشن از mimetypes استفاده کنید)
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

# 2. خزش صفحه
print(f"Scraping {BASE_URL} for messages after ID: {LAST_ID}...")
html = requests.get(BASE_URL).text
soup = BeautifulSoup(html, "html.parser")
messages_html = soup.find_all("div", class_="tgme_widget_message")

parsed_messages = []
highest_id = LAST_ID

for msg in messages_html:
    # استخراج ID پیام
    post_param = msg.get("data-post", "")
    if not post_param:
        continue
    msg_id = int(post_param.split("/")[-1])
    
    if msg_id <= LAST_ID:
        continue

    # استخراج متن
    text_div = msg.find("div", class_="tgme_widget_message_text")
    msg_text = text_div.get_text(separator="\n") if text_div else ""
    msg_html = str(text_div) if text_div else ""

    # استخراج مدیا (عکس به عنوان نمونه)
    media_info = None
    photo_wrap = msg.find("a", class_="tgme_widget_message_photo_wrap")
    if photo_wrap:
        # آدرس عکس در استایل بک‌گراند قرار دارد
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

# 3. ذخیره‌سازی داده‌ها در صورت وجود پیام جدید
if not parsed_messages:
    print("No new messages found.")
    sys.exit(0)

timestamp = int(time.time())
json_filename = f"data/{timestamp}_{CHANNEL}.json"

output_data = {
    "scrape_metadata": {
        "channel_username": CHANNEL,
        "scrape_timestamp": timestamp,
        "messages_count": len(parsed_messages)
    },
    "messages": parsed_messages
}

with open(json_filename, "w", encoding="utf-8") as f:
    json.dump(output_data, f, ensure_ascii=False, indent=2)

# 4. ایجاد فایل قفل
with open("lock.txt", "w") as f:
    f.write("LOCKED")

# پاس دادن ID جدید به GitHub Actions
with open("new_id.txt", "w") as f:
    f.write(str(highest_id))

print(f"Successfully processed {len(parsed_messages)} messages. Max ID: {highest_id}")
