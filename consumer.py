import os
import time
import json
import shutil
import subprocess
import requests

# ---------------------------------------------------------
# Configuration & Environment Variables
# ---------------------------------------------------------
GITHUB_PAT = os.getenv("GITHUB_PAT")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # Format: username/repo-name
RC_URL = os.getenv("RC_URL").rstrip('/')
RC_USER_ID = os.getenv("RC_USER_ID")
RC_TOKEN = os.getenv("RC_TOKEN")

# تایمرها بر اساس نیاز شما (به ثانیه)
ACTION_TRIGGER_INTERVAL = 600  # ۱۰ دقیقه بازه اصلی
POLLING_INTERVAL = 30         # چک کردن بافر هر ۳۰ ثانیه
ACTION_TIMEOUT = 600          # اگر بعد از ۱۰ دقیقه دیتایی نیامد، تلاش مجدد

REPO_URL = f"https://oauth2:{GITHUB_PAT}@github.com/{GITHUB_REPO}.git"
WORK_DIR = "/app/temp_workspace/repo"

# ---------------------------------------------------------
# GitHub Action Trigger Function
# ---------------------------------------------------------
def trigger_github_action():
    """Triggers the GitHub Action via Repository Dispatch."""
    print(f"\n[ {time.strftime('%H:%M:%S')} ] Sending Trigger to GitHub Actions...")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {GITHUB_PAT}"
    }
    payload = {"event_type": "trigger-scrape"} # این باید با yml یکی باشد
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 204:
            print("  [OK] GitHub Action triggered successfully.")
            return True
        else:
            print(f"  [ERROR] Failed to trigger: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"  [ERROR] Connection error during trigger: {e}")
        return False

# ---------------------------------------------------------
# Rocket.Chat API Helper Functions (بدون تغییر)
# ---------------------------------------------------------
def get_rc_headers():
    return {"X-Auth-Token": RC_TOKEN, "X-User-Id": RC_USER_ID}

def format_channel_name(name):
    return name.lower().replace("_", "-")

def ensure_channel_exists(channel_name):
    formatted_name = format_channel_name(channel_name)
    headers = get_rc_headers()
    info_url = f"{RC_URL}/api/v1/channels.info?roomName={formatted_name}"
    response = requests.get(info_url, headers=headers)
    if response.status_code == 200:
        return response.json().get('channel', {}).get('_id')
    
    create_url = f"{RC_URL}/api/v1/channels.create"
    create_res = requests.post(create_url, headers=headers, json={"name": formatted_name})
    if create_res.status_code == 200:
        return create_res.json().get('channel', {}).get('_id')
    raise Exception(f"Failed to create channel {formatted_name}: {create_res.text}")

def send_to_rocketchat(room_id, message_data, media_dir):
    headers = get_rc_headers()
    text = message_data.get("content", {}).get("text", "")
    media_info = message_data.get("media")

    if media_info and media_info.get("relative_path"):
        file_name = os.path.basename(media_info["relative_path"])
        file_path = os.path.join(media_dir, file_name)
        if not os.path.exists(file_path): return

        upload_url = f"{RC_URL}/api/v1/rooms.media/{room_id}"
        with open(file_path, 'rb') as f:
            # تشخیص نوع فایل برای پشتیبانی از وویس و موزیک
            files = {'file': (file_name, f, 'application/octet-stream')}
            res_upload = requests.post(upload_url, headers=headers, files=files)
            if res_upload.status_code != 200: return
            file_id = res_upload.json().get("file", {}).get("_id")

        confirm_url = f"{RC_URL}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}"
        requests.post(confirm_url, headers=headers, json={"msg": text, "fileName": file_name})
    else:
        if not text.strip(): return
        requests.post(f"{RC_URL}/api/v1/chat.postMessage", headers=headers, json={"roomId": room_id, "text": text})

# ---------------------------------------------------------
# Git & Process Logic
# ---------------------------------------------------------
def run_git_command(command, cwd=WORK_DIR):
    result = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True)
    if result.returncode != 0:
        raise Exception(f"Git command failed: {result.stderr}")
    return result.stdout

def check_and_process():
    """Clones the repo and checks for lock.txt. Returns True if processed, False if no data."""
    if os.path.exists(WORK_DIR): shutil.rmtree(WORK_DIR)
    run_git_command(f"git clone {REPO_URL} {WORK_DIR}", cwd="/app")
    
    lock_file = os.path.join(WORK_DIR, "lock.txt")
    if not os.path.exists(lock_file):
        return False

    print("  [DATA] Lock file found! Processing messages...")
    data_dir = os.path.join(WORK_DIR, "data")
    media_dir = os.path.join(WORK_DIR, "media")

    for filename in sorted(os.listdir(data_dir)):
        if not filename.endswith(".json"): continue
        with open(os.path.join(data_dir, filename), 'r', encoding='utf-8') as f:
            data = json.load(f)
            channel_name = data.get("scrape_metadata", {}).get("channel_username")
            messages = data.get("messages", [])
            if not channel_name or not messages: continue
            
            room_id = ensure_channel_exists(channel_name)
            for msg in messages:
                send_to_rocketchat(room_id, msg, media_dir)
                time.sleep(0.5)

    # Reset Repository
    print("  [CLEANUP] Resetting repository...")
    run_git_command("git config user.email 'consumer@system.local'")
    run_git_command("git config user.name 'Consumer Bot'")
    run_git_command("git checkout --orphan temp_branch")
    run_git_command("git rm -rf .")
    with open(os.path.join(WORK_DIR, "README.md"), "w") as f:
        f.write("# Buffer Repository\nFlushed by Consumer.")
    run_git_command("git add README.md")
    run_git_command("git commit -m 'System: Flush'")
    run_git_command("git push -f origin temp_branch:main")
    return True

# ---------------------------------------------------------
# Main Scheduler Loop
# ---------------------------------------------------------
if __name__ == "__main__":
    print("Rocket.Chat Active Consumer Service Started.")
    
    while True:
        # ۱. فراخوانی اکشن گیت‌هاب
        trigger_github_action()
        trigger_time = time.time()
        data_received = False

        # ۲. حلقه انتظار برای دریافت فایل (چک کردن هر ۳۰ ثانیه)
        print(f"Waiting for Engine to upload data (Polling every {POLLING_INTERVAL}s)...")
        
        while True:
            current_time = time.time()
            
            # چک کردن ریپازیتوری
            try:
                if check_and_process():
                    print("Cycle completed successfully.")
                    data_received = True
                    break # خروج از حلقه انتظار و رفتن به استراحت ۱۰ دقیقه‌ای
            except Exception as e:
                print(f"  [ERROR] Error during polling: {e}")

            # بررسی تایم‌اوت (اگر ۱۰ دقیقه گذشت و خبری نشد)
            if (current_time - trigger_time) > ACTION_TIMEOUT:
                print("  [TIMEOUT] No data received within 10 minutes. Re-triggering Action...")
                trigger_github_action()
                trigger_time = time.time() # ریست کردن زمان شروع تایم‌اوت

            time.sleep(POLLING_INTERVAL)

        # ۳. استراحت بعد از اتمام موفقیت‌آمیز یک چرخه
        print(f"Sleeping for {ACTION_TRIGGER_INTERVAL}s before next cycle...")
        time.sleep(ACTION_TRIGGER_INTERVAL)
