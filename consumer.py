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
GITHUB_REPO = os.getenv("GITHUB_REPO") # Format: username/repo-name
RC_URL = os.getenv("RC_URL").rstrip('/') # e.g., https://chat.mycompany.com
RC_USER_ID = os.getenv("RC_USER_ID")
RC_TOKEN = os.getenv("RC_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 900)) # Default: 15 minutes

REPO_URL = f"https://oauth2:{GITHUB_PAT}@github.com/{GITHUB_REPO}.git"
WORK_DIR = "/app/temp_workspace/repo"

# ---------------------------------------------------------
# Rocket.Chat API Helper Functions
# ---------------------------------------------------------
def get_rc_headers():
    return {
        "X-Auth-Token": RC_TOKEN,
        "X-User-Id": RC_USER_ID
    }

def format_channel_name(name):
    """Rocket.Chat channel names must be lowercase and alphanumeric."""
    return name.lower().replace("_", "-")

def ensure_channel_exists(channel_name):
    """Creates the channel if it doesn't exist and returns the Room ID."""
    formatted_name = format_channel_name(channel_name)
    headers = get_rc_headers()
    
    # 1. Check if channel exists and get its ID
    info_url = f"{RC_URL}/api/v1/channels.info?roomName={formatted_name}"
    response = requests.get(info_url, headers=headers)
    
    if response.status_code == 200:
        return response.json().get('channel', {}).get('_id')
    
    # 2. If it doesn't exist, create it
    create_url = f"{RC_URL}/api/v1/channels.create"
    payload = {"name": formatted_name}
    create_res = requests.post(create_url, headers=headers, json=payload)
    
    if create_res.status_code == 200:
        return create_res.json().get('channel', {}).get('_id')
    else:
        raise Exception(f"Failed to create channel {formatted_name}: {create_res.text}")

def send_to_rocketchat(room_id, message_data, media_dir):
    """Two-step upload process discovered from Browser Inspect."""
    headers = get_rc_headers()
    text = message_data.get("content", {}).get("text", "")
    media_info = message_data.get("media")

    if media_info and media_info.get("relative_path"):
        file_name = os.path.basename(media_info["relative_path"])
        file_path = os.path.join(media_dir, file_name)
        
        # --- STEP 1: UPLOAD ---
        # URL: /api/v1/rooms.media/:rid
        upload_url = f"{RC_URL}/api/v1/rooms.media/{room_id}"
        print(f"  --> [Step 1] Uploading file to: {upload_url}")
        
        with open(file_path, 'rb') as f:
            files = {'file': (file_name, f, 'application/octet-stream')}
            res_upload = requests.post(upload_url, headers=headers, files=files)
            
            if res_upload.status_code != 200:
                print(f"Upload failed: {res_upload.text}")
                res_upload.raise_for_status()
            
            file_id = res_upload.json().get("file", {}).get("_id")
            print(f"  [OK] File uploaded. ID: {file_id}")

        # --- STEP 2: CONFIRM ---
        # URL: /api/v1/rooms.mediaConfirm/:rid/:fileId
        confirm_url = f"{RC_URL}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}"
        print(f"  --> [Step 2] Confirming post to: {confirm_url}")
        
        confirm_payload = {
            "msg": text,
            "fileName": file_name
        }
        
        res_confirm = requests.post(confirm_url, headers=headers, json=confirm_payload)
        
        if res_confirm.status_code != 200:
            print(f"Confirmation failed: {res_confirm.text}")
            res_confirm.raise_for_status()
            
        print(f"  [OK] Media and text posted successfully.")

    else:
        # ارسال متن ساده (بدون تغییر)
        if not text.strip(): return
        post_url = f"{RC_URL}/api/v1/chat.postMessage"
        res = requests.post(post_url, headers=headers, json={"roomId": room_id, "text": text})
        res.raise_for_status()
        print(f"  [OK] Text message posted.")

# ---------------------------------------------------------
# Core Consumer Logic
# ---------------------------------------------------------
def run_git_command(command, cwd=WORK_DIR):
    """Helper to run git commands securely."""
    result = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True)
    if result.returncode != 0:
        raise Exception(f"Git command failed: {result.stderr}")
    return result.stdout

def process_buffer():
    print(f"\n--- [ {time.strftime('%Y-%m-%d %H:%M:%S')} ] Waking up to check buffer ---")
    
    # 1. Clean workspace
    if os.path.exists(WORK_DIR):
        shutil.rmtree(WORK_DIR)
        
    # 2. Clone the repository
    print("Cloning the buffer repository...")
    run_git_command(f"git clone {REPO_URL} {WORK_DIR}", cwd="/app")
    
    # 3. Check for Lock file
    lock_file = os.path.join(WORK_DIR, "lock.txt")
    if not os.path.exists(lock_file):
        print("No lock.txt found. Engine is still working or no new data. Sleeping.")
        return

    print("Lock file found. Processing data...")
    
    data_dir = os.path.join(WORK_DIR, "data")
    media_dir = os.path.join(WORK_DIR, "media")
    
    if not os.path.exists(data_dir):
        print("Data directory is missing, but lock exists. Corrupt state?")
        return

    # 4. Process each JSON file
    for filename in sorted(os.listdir(data_dir)):
        if not filename.endswith(".json"):
            continue
            
        file_path = os.path.join(data_dir, filename)
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        channel_name = data.get("scrape_metadata", {}).get("channel_username")
        messages = data.get("messages", [])
        
        if not channel_name or not messages:
            continue
            
        print(f"Processing {len(messages)} messages for channel: {channel_name}")
        
        try:
            # Ensure channel exists and get room ID
            room_id = ensure_channel_exists(channel_name)
            
            for msg in messages:
                send_to_rocketchat(room_id, msg, media_dir)
                time.sleep(1) # Rate limiting protection for Rocket.Chat
                
        except Exception as e:
            print(f"ERROR processing channel {channel_name}: {e}")
            print("Aborting current cycle. Will retry next time. Repository WILL NOT be reset.")
            return # Critical exit: Do not reset repo if pushing to RC fails!

    # 5. All data sent successfully -> Reset the repository
    print("All data successfully pushed to Rocket.Chat. Resetting repository history...")
    
    # Git setup inside the temporary clone
    run_git_command("git config user.email 'consumer@system.local'")
    run_git_command("git config user.name 'Consumer Bot'")
    
    # Create an orphan branch (no history)
    run_git_command("git checkout --orphan temp_branch")
    
    # Remove all files (including .git/ tracking for them)
    run_git_command("git rm -rf .")
    
    # Create a fresh README
    with open(os.path.join(WORK_DIR, "README.md"), "w") as f:
        f.write("# Buffer Repository\nThis repository is automatically flushed by the consumer bot.")
        
    # Commit and Force Push
    run_git_command("git add README.md")
    run_git_command("git commit -m 'System: Buffer flush and history reset'")
    run_git_command("git push -f origin temp_branch:main")
    
    print("Repository successfully reset. Volume is back to 0 bytes.")

# ---------------------------------------------------------
# Main Loop
# ---------------------------------------------------------
if __name__ == "__main__":
    print("Rocket.Chat Consumer Service Started.")
    while True:
        try:
            process_buffer()
        except Exception as e:
            print(f"System Error: {e}")
            
        print(f"Sleeping for {CHECK_INTERVAL} seconds...")
        time.sleep(CHECK_INTERVAL)

