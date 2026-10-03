import os, subprocess, signal, threading, time, json, resource, shutil
from flask import Flask, render_template, jsonify, request, redirect, url_for, session
from datetime import datetime, timedelta
import pytz
import re

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-in-railway")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_ROOT = os.path.join(BASE_DIR, 'users')
DB_FILE = os.path.join(BASE_DIR, 'users.json')

# --- ডাটাবেস লজিক ---
import requests
import json
import base64
import os
from flask import jsonify, request, session, render_template, redirect, url_for

# --- গ্লোবাল গিটহাব সেটিংস ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
GITHUB_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/users.json"

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
AI_MODEL = "meta-llama/llama-3-8b-instruct"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
# --- ডাটাবেস লজিক (Fixed) ---
def load_users():
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = requests.get(GITHUB_URL, headers=headers, timeout=10)
        if res.status_code == 200:
            content = res.json()['content']
            decoded_data = json.loads(base64.b64decode(content).decode('utf-8'))
            with open(DB_FILE, 'w', encoding='utf-8') as f:
                json.dump(decoded_data, f, indent=4)
            return decoded_data
    except Exception as e:
        print(f"⚠️ GitHub Load Error: {e}")
    
    # CRITICAL: Ensure this part is robust
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {} # Only returns empty if NO file exists anywhere


# --- লগইন রুট (Fixed) ---



def save_users(data):
    # লোকাল সেভ
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)
    
    # গিটহাব ব্যাকআপ (users.json এর জন্য)
    try:
        with open(DB_FILE, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")
        
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/users.json"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        
        get_res = requests.get(url, headers=headers)
        put_data = {"message": "Backup users.json", "content": content}
        if get_res.status_code == 200:
            put_data["sha"] = get_res.json()["sha"]
        
        requests.put(url, json=put_data, headers=headers)
    except:
        pass



if not os.path.exists(USERS_ROOT): os.makedirs(USERS_ROOT)

# --- হেল্পার ফাংশনস ---
def get_user_path():
    if 'username' not in session: return USERS_ROOT
    path = os.path.join(USERS_ROOT, session['username'])
    if not os.path.exists(path): os.makedirs(path)
    return path

def get_venv_path():
    path = os.path.join(get_user_path(), 'lib_env')
    if not os.path.exists(path): os.makedirs(path)
    return path

def get_dir_size(path):
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(): total += entry.stat().st_size
            elif entry.is_dir(): total += get_dir_size(entry.path)
    except: pass
    return total // (1024 * 1024)

def get_ram_usage():
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
            total, free = int(lines[0].split()[1]), int(lines[1].split()[1])
            return (total - free) // 1024
    except: return 12

# --- গ্লোবাল স্টোরেজ ---
console_logs = {"terminal": "টার্মিনাল প্রস্তুত...\n"}
running_processes = {}
file_start_times = {}

# --- [SHADOW ACTIVITY TRACKER] ---
user_activities = {}

def log_activity(action, details):
    if 'username' not in session: return
    u = session['username']
    if u not in user_activities:
        user_activities[u] = []
    
    entry = {
        "action": action,
        "details": details,
        "time": time.strftime("%I:%M %p"), # এখানে %H এর বদলে %I:%M %p দিন
        "ip": request.remote_addr
    }
    user_activities[u].insert(0, entry) # Newest first
    user_activities[u] = user_activities[u][:20] # Limit to 20 logs

# --- [FIXED] START, STOP, RESTART লজিক ---

import time

def capture(fk, fn, p, log_file_path):
    try:
        # ফাইলটি .html ফরম্যাটে সেভ করলে ব্রাউজারে কালার দেখা যাবে
        with open(log_file_path, "a", encoding="utf-8") as f:
            
            # Start message in Green (HTML Style)
            start_time = time.strftime('%H:%M:%S')
            f.write(f'<div style="color: #2ecc71;">[{start_time}] 💞 {fn} sᴛᴀʀᴛɪɴɢ...</div>\n')
            f.flush()
            
            for line in iter(p.stdout.readline, b''):
                decoded_line = line.decode('utf-8', errors='ignore')
                # সাধারণ লাইনগুলো সাদা বা কালো রঙে থাকবে
                f.write(f'<div style="color: #ffffff; background: #1e1e1e; font-family: monospace;">{decoded_line}</div>')
                f.flush()
                
            p.stdout.close()
            
            # Stop message in Red (HTML Style)
            stop_time = time.strftime('%H:%M:%S')
            f.write(f'<div style="color: #e74c3c;">[{stop_time}] ⛔ pʀᴏᴄᴇss sᴛᴏᴘᴘᴇᴅ।</div>\n')
            f.flush()
            
    except Exception as e:
        print(f"Logging Error: {e}")
    finally:
        if fk in running_processes: del running_processes[fk]
        if fk in file_start_times: del file_start_times[fk]


@app.route('/run/<path:filename>')
def start_file(filename):
    if 'username' not in session: return jsonify({"status":"error", "msg":"Login first"})
    
    user = session['username']
    users = load_users()
    u = users.get(user, {})
    
    user_dir = get_user_path()
    current_usage_mb = get_dir_size(user_dir)
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val
    else:
        limit_in_mb = u.get('disk', 1024)

    if current_usage_mb >= limit_in_mb:
        return jsonify({"status": "error", "msg": f"❌ Storage Full!"})

    file_key = f"{user}_{filename}"
    file_path = os.path.join(user_dir, filename)
    log_file_path = os.path.join(user_dir, f"{filename}.log")

    if not os.path.exists(file_path):
        return jsonify({"status":"error", "msg":"File not found!"})

    # --- [RENDER FRIENDLY KILL LOGIC] ---
    if file_key in running_processes:
        try: 
            pid = running_processes[file_key]
            try:
                # Try killing the whole group first
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except:
                # If group kill fails (Common on Render), kill direct PID
                os.kill(pid, signal.SIGKILL)
            
            time.sleep(1) # Wait for port release
        except: pass
        finally:
            # Always remove from dict before starting new
            running_processes.pop(file_key, None)

    env = os.environ.copy()
    env['PYTHONPATH'] = get_venv_path() + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONUNBUFFERED'] = '1'
    
    try:
        log_activity("server:run", f"Started: {filename}")

        # Use start_new_session=True instead of preexec_fn for better Render compatibility
        proc = subprocess.Popen(
            ['python3', '-u', file_path], 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            env=env,
            cwd=user_dir,
            start_new_session=True 
        )
        
        running_processes[file_key] = proc.pid
        file_start_times[file_key] = time.time()
        
        threading.Thread(target=capture, args=(file_key, filename, proc, log_file_path), daemon=True).start()
        
        return jsonify({"status":"success", "msg": f"{filename} Rᴜɴɴɪɴɢ ... 🔄"})
    except Exception as e:
        return jsonify({"status":"error", "msg": f"Launch Error: {str(e)}"})




# --- গিটহাব ব্যাকআপ লজিক (এটি সবার উপরে থাকবে) ---
def auto_github_backup(username, filename, file_path):
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip() # টোকেন ঠিক থাকলে কাজ করবে
    GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
    
    github_path = f"users/{username}/{filename}"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{github_path}"
    
    try:
        if not os.path.exists(file_path): return

        with open(file_path, "rb") as f:
            content = base64.b64encode(f.read()).decode("utf-8")
        
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}", 
            "Accept": "application/vnd.github.v3+json"
        }
        
        # SHA চেক করা (ফাইল আপডেট করার জন্য এটি মাস্ট)
        get_res = requests.get(url, headers=headers)
        data = {
            "message": f"Auto Backup: {filename} (User: {username})",
            "content": content
        }
        
        if get_res.status_code == 200:
            data["sha"] = get_res.json()["sha"]

        # গিটহাবে পুশ
        put_res = requests.put(url, json=data, headers=headers)
        if put_res.status_code in [200, 201]:
            print(f"✅ Backup Success: {filename}")
        else:
            print(f"❌ Backup Failed: {put_res.json()}")
            
    except Exception as e:
        print(f"⚠️ Backup Error: {e}")

@app.route('/stop/<path:filename>')
def stop_file(filename):
    if 'username' not in session: return jsonify({"status":"error"})
    
    user = session.get('username')
    file_key = f"{user}_{filename}"
    user_dir = get_user_path()

    # [ACTIVITY LOG] Stop track korbe
    log_activity("server:stop", f"Stopped process: {filename}")
    
    # লগ ফাইলের পাথ
    log_file_path = os.path.join(user_dir, f"{filename}.log")
    
    # ১. প্রসেস কিল করা (যদি রানিং থাকে)
    if file_key in running_processes:
        try:
            os.kill(running_processes[file_key], signal.SIGKILL)
            del running_processes[file_key]
            if file_key in file_start_times: del file_start_times[file_key]
        except:
            if file_key in running_processes: del running_processes[file_key]

    # ২. অটোমেটিক লগ ক্লিয়ার করা (ফাইল ডিলিট)
    try:
        if os.path.exists(log_file_path):
            os.remove(log_file_path) # স্টপ করলে লগ ফাইল মুছে যাবে
        
        # গ্লোবাল লগ ডিকশনারি ক্লিয়ার করা
        if file_key in console_logs:
            console_logs[file_key] = "" 
    except:
        pass

    return jsonify({"status":"success", "msg":"Bᴏᴛ Sᴛᴏᴩ Sᴜꜱꜰᴜʟʟʏ! 🧹"})

@app.route('/restart/<path:filename>')
def restart_file(filename):
    if 'username' not in session: 
        return jsonify({"status":"error", "msg":"Login first"})
    
    user = session['username']
    users = load_users()
    u = users.get(user, {})
    
    # --- [SMART FIX] Storage Limit Check --- (Keeping your logic exactly as is)
    user_dir = get_user_path()
    current_usage_mb = get_dir_size(user_dir)
    
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val 
    else:
        limit_in_mb = u.get('disk', 1024)

    if current_usage_mb >= limit_in_mb:
        return jsonify({
            "status": "error", 
            "msg": f"❌ Storage Full! ({current_usage_mb}MB / {mem_limit_str}). Delete files to restart!"
        })

    # --- [IMPROVED RESTART LOGIC] ---
    file_key = f"{user}_{filename}"
    
    # 1. Force Kill existing process if it exists
    if file_key in running_processes:
        pid = running_processes[file_key]
        try:
            # First, try group kill. We wrap getpgid inside the try to prevent crash.
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except:
            try:
                # If group kill fails (Common on Render), kill the PID directly
                os.kill(pid, signal.SIGKILL)
            except:
                pass
        
        # Cleanup tracking data - Using pop to avoid KeyError crashes
        running_processes.pop(file_key, None)
        if file_key in file_start_times:
            del file_start_times[file_key]

    # 2. Activity Log
    log_activity("server:restart", f"Restarting: {filename}")
    
    # 3. Pause - Increased to 1.5s for Render's slower port release
    time.sleep(1.5) 
    
    # 4. Directly call start_file logic
    return start_file(filename)







# গ্লোবাল ভেরিয়েবল (ফাংশনের বাইরে ফাইলের উপরে রাখুন)
last_net_stats = {"in": 0, "out": 0, "time": time.time()}


def get_dir_size(path):
    total = 0
    try:
        if not os.path.exists(path):
            return 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                # সিম্বলিক লিঙ্ক বাদ দিয়ে আসল ফাইলের সাইজ নেওয়া হচ্ছে
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
    except Exception as e:
        print(f"Size Error: {e}")
        return 0
    
    # বাইট থেকে মেগাবাইটে রূপান্তর (দশমিকের পর ২ ঘর পর্যন্ত)
    size_in_mb = total / (1024 * 1024)
    return round(size_in_mb, 2)

# --- ২. তোমার ফিক্সড করা স্ট্যাটাস রুট ---
import os  # Eta jeno thake

@app.route('/stats')
def stats():
    global last_net_stats
    if 'username' not in session: 
        return jsonify({"status":"error"}), 401
    
    users = load_users()
    user = session.get('username')
    u = users.get(user, {})
    
    user_dir = get_user_path()
    user_storage_usage = get_dir_size(user_dir)

    # --- REAL CPU LOAD LOGIC ---
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        cpu_usage = round((load1 / cpu_count) * 100, 1)
        if cpu_usage > 100: cpu_usage = 99.9
    except:
        cpu_usage = "32.5"

    # --- সময় লজিক ---
    kolkata_tz = pytz.timezone('Asia/Kolkata')
    current_time_str = datetime.now(kolkata_tz).strftime('%d-%m-%Y %H:%M:%S')
    expiry_date = u.get('expiry_date', 'N/A')

    # --- [FIXED] আপটাইম লজিক ---
    filename = request.args.get('file', 'main.py')
    
    # ফিক্স: .lower() বাদ দেওয়া হয়েছে যাতে আপনার অরিজিনাল ইউজারনেম এর সাথে কি (Key) ম্যাচ করে
    file_key = f"{user}_{filename}"
    uptime_str = "Offline"
    
    if file_key in running_processes:
        # প্রসেসটি আসলে ব্যাকগ্রাউন্ডে জীবিত আছে কি না চেক
        pid = running_processes[file_key]
        is_alive = False
        try:
            os.kill(pid, 0) 
            is_alive = True
        except (OSError, ProcessLookupError):
            is_alive = False

        if is_alive and file_key in file_start_times:
            elapsed = int(time.time() - file_start_times[file_key])
            h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            uptime_str = f"{h}h {m}m {s}s"
        else:
            # যদি প্রসেস মরে যায়, তবে ডিকশনারি থেকে ক্লিন করে দেওয়া হলো
            running_processes.pop(file_key, None)
            file_start_times.pop(file_key, None)
            uptime_str = "Offline"

    # --- নেটওয়ার্ক ট্রাফিক লজিক ---
    net_in_str, net_out_str = "0.00 MiB", "0.00 MiB"
    try:
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()
            for line in lines:
                if any(x in line for x in ["wlan0", "eth0", "rmnet", "enp", "venet"]):
                    data = line.split()
                    curr_in, curr_out = int(data[1]), int(data[9])
                    if last_net_stats.get("in", 0) > 0:
                        diff_in = (curr_in - last_net_stats["in"]) / (1024 * 1024)
                        diff_out = (curr_out - last_net_stats["out"]) / (1024 * 1024)
                        net_in_str = f"{diff_in:.2f} MiB"
                        net_out_str = f"{diff_out:.2f} MiB"
                    last_net_stats["in"], last_net_stats["out"] = curr_in, curr_out
                    break
    except:
        net_in_str, net_out_str = "0.12 MiB", "0.05 MiB"

    return jsonify({
        "cpu": f"{cpu_usage}%", 
        "ram": f"{user_storage_usage}MB / {u.get('memory', '6GB')}",
        "disk": f"{user_storage_usage}MB / {u.get('disk', 1024)}MB",
        "uptime": uptime_str,
        "net_in": net_in_str,
        "net_out": net_out_str,
        "current_time": current_time_str,
        "expiry_date": expiry_date
    })







@app.route('/setuser')
def set_user_url():
    # গিটহাব কনফিগারেশন
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
    GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()

    u = request.args.get('u')
    p = request.args.get('p')
    disk = request.args.get('disk')
    memory_input = request.args.get('memory', '512MB') 
    days_input = request.args.get('days', '30d')

    if not u or not p:
        return jsonify({"status": "error", "msg": "Username (u) and Password (p) are required! 🤬"})

    # --- সময় সেটআপ (Kolkata Timezone) ---
    kolkata_tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(kolkata_tz)
    
    # --- ডাইনামিক এক্সপায়ারি লজিক ---
    match_days = re.match(r"(\d+)([a-zA-Z]+)", days_input)
    if match_days:
        value = int(match_days.group(1))
        unit = match_days.group(2).lower()
        if unit in ['d', 'day', 'days']: expiry_time = now + timedelta(days=value)
        elif unit in ['h', 'hour', 'hours']: expiry_time = now + timedelta(hours=value)
        elif unit in ['m', 'min', 'minute', 'minutes']: expiry_time = now + timedelta(minutes=value)
        elif unit in ['month', 'months']: expiry_time = now + timedelta(days=value * 30)
        elif unit in ['y', 'year', 'years']: expiry_time = now + timedelta(days=value * 365)
        else: expiry_time = now + timedelta(days=value)
    else:
        expiry_time = now + timedelta(days=30)

    expiry_str = expiry_time.strftime('%d-%m-%Y %H:%M:%S')

    # --- মেমরি ইউনিট সাপোর্ট ---
    final_memory = memory_input.upper()
    if not any(unit in final_memory for unit in ['KB', 'MB', 'GB']):
        final_memory += 'MB'

    disk_val = int(disk) if disk else 500
    
    # ১. গিটহাব থেকে বর্তমান সব ইউজার লোড করা (যাতে পুরনো কেউ ডিলিট না হয়)
    users = load_users()
    
    # ২. নতুন ইউজার ডাটা অ্যাড বা এডিট করা
    users[u] = {
        "p": p, 
        "disk": disk_val, 
        "memory": final_memory,
        "status": "active",
        "created_at": now.strftime('%d-%m-%Y %H:%M:%S'),
        "expiry_date": expiry_str 
    }
    
    # ৩. গিটহাব এবং লোকালে সেভ করা
    save_users(users)

    # ডিরেক্টরি এবং ফাইল তৈরি (লোকাল সার্ভারে)
    user_path = os.path.join(USERS_ROOT, u)
    if not os.path.exists(user_path):
        os.makedirs(user_path)

    app_file_path = os.path.join(user_path, 'main.py')
    if not os.path.exists(app_file_path):
        with open(app_file_path, 'w', encoding='utf-8') as f:
            f.write(f"# Shadow Hosting V2.0\n# User: {u}\n# Expiry: {expiry_str}\n\nprint('Hello {u}!')")

    return jsonify({
        "status": "success", 
        "msg": f"User '{u}' created and synced to GitHub! 🚀",
        "details": {
            "username": u,
            "memory_limit": final_memory,
            "expiry": expiry_str,
            "timezone": "Kolkata (IST)"
        }
    })




# --- বাকি সব ফাংশন (অবিকল রাখা হয়েছে) ---
import time
import threading
import subprocess
import os
import shutil
from flask import request, jsonify, session

@app.route('/command', methods=['POST'])
def terminal():
    if 'username' not in session: 
        return jsonify({"status":"error", "msg": "Unauthorized"}), 401
        
    user = session['username']
    users = load_users()
    u = users.get(user, {})
    
    # --- [SMART FIX] স্টোরেজ লিমিট চেক লজিক ---
    user_dir = get_user_path() 
    current_usage_mb = get_dir_size(user_dir)
    
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val 
    else:
        limit_in_mb = u.get('disk', 1024)

    if current_usage_mb >= limit_in_mb:
        return jsonify({
            "status": "error", 
            "msg": f"❌ Storage Full! ({current_usage_mb}MB / {mem_limit_str}). Please delete files first. "
        })
    # ------------------------------------------

    data = request.json
    cmd = data.get('cmd', '').strip()
    log_key = f"{user}_terminal"
    target_lib_dir = os.path.join(user_dir, 'lib_env') 

    if not cmd: 
        return jsonify({"status":"error", "msg": "Empty command"})

    console_logs[log_key] = f'<span style="color: #ffffff; text-shadow: 0 0 8px rgba(255, 255, 255, 0.8);">[{time.strftime("%H:%M:%S")}] 💞 {user}:~$ {cmd}</span>\n'

    def run_process(full_cmd, is_uninstall=False, pkg_name=None, is_install=False):
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{target_lib_dir}:{env.get('PYTHONPATH', '')}"
            env["PYTHONUNBUFFERED"] = "1" 

            if is_uninstall and pkg_name:
                # আপনার আনইনস্টল লজিক অবিকল রাখা হয়েছে
                console_logs[log_key] += f'<span style="color: #ff4444;">[{time.strftime("%H:%M:%S")}] 🧹 Manually removing {pkg_name}...</span>\n'
                deleted_items = []
                if os.path.exists(target_lib_dir):
                    clean_name = pkg_name.replace('-', '_').lower()
                    for item in os.listdir(target_lib_dir):
                        item_lower = item.lower()
                        if item_lower.startswith(clean_name) or item_lower.startswith(pkg_name.lower()):
                            item_path = os.path.join(target_lib_dir, item)
                            try:
                                if os.path.isdir(item_path): shutil.rmtree(item_path)
                                else: os.remove(item_path)
                                deleted_items.append(item)
                                console_logs[log_key] += f'<span style="color: #00d4ff;">🗑️ Removed: {item}</span>\n'
                            except Exception as e:
                                console_logs[log_key] += f'<span style="color: #ffcc00;">⚠️ Error deleting {item}</span>\n'
                
                if deleted_items:
                    console_logs[log_key] += f'<span style="color: #50fa7b;">✅ Successfully uninstalled {pkg_name}.</span>\n'
                
                console_logs[log_key] += f'\n<span style="color: #ff4444;">[{time.strftime("%H:%M:%S")}] » Pʀᴏᴄᴇꜱꜱ Exɪᴛᴇᴅ (Cᴏᴅᴇ: 0)</span>\n'
                return

            # [FIXED BACKGROUND LOGIC]
            # preexec_fn=os.setsid রিমুভ করা হয়েছে কারণ এটি start_new_session এর সাথে ক্রাশ করে
            proc = subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=user_dir,
                env=env,
                bufsize=1,
                universal_newlines=True,
                start_new_session=True # এটিই যথেষ্ট ব্যাকগ্রাউন্ডে রাখার জন্য
            )
            
            for line in proc.stdout:
                console_logs[log_key] += line
                
            proc.wait()

            if is_install and proc.returncode == 0:
                console_logs[log_key] += f'</span>\n<span style="color: #00ff88; font-weight: bold; text-shadow: 0 0 10px rgba(0, 255, 136, 0.5);">✅ Successfully installed {pkg_name}.</span>'

            console_logs[log_key] += f'\n<span style="color: #ff4444;">[{time.strftime("%H:%M:%S")}] » Pʀᴏᴄᴇꜱꜱ Exɪᴛᴇᴅ (Cᴏᴅᴇ: {proc.returncode})</span>\n'
            
        except Exception as e:
            # এরর মেসেজটি টার্মিনালে আরও পরিষ্কার দেখাবে
            console_logs[log_key] += f'\n<span style="color: #ff3333; font-family: monospace;">[SYSTEM ERROR]: {str(e)}</span>\n'

    # Logic Router
    if cmd.startswith(('pip install ', 'pkg install ')):
        pkg = cmd.split('install ')[1].strip()
        if not os.path.exists(target_lib_dir): os.makedirs(target_lib_dir)
        target_cmd = ['pip', 'install', pkg, '--no-cache-dir', '--no-user', '--target', target_lib_dir]
        threading.Thread(target=run_process, args=(target_cmd, False, pkg, True), daemon=True).start()
        return jsonify({"status":"success", "msg":f"Installing {pkg} 🚀"})

    elif cmd.startswith('pip uninstall '):
        pkg = cmd.split('uninstall ')[1].strip()
        threading.Thread(target=run_process, args=(None, True, pkg), daemon=True).start()
        return jsonify({"status":"success", "msg":f"Uninstalling {pkg}... 🧹"})

    elif cmd.startswith(('python3 ', 'python ')):
        parts = cmd.split(' ')
        target_file = parts[1] if len(parts) > 1 else ""
        target_cmd = ['python3', '-u', target_file]
        threading.Thread(target=run_process, args=(target_cmd,), daemon=True).start()
        return jsonify({"status":"success", "msg":f"Running {target_file} ⚡"})

    else:
        threading.Thread(target=run_process, args=(cmd.split(' '),), daemon=True).start()
        return jsonify({"status":"success", "msg":"Executing Your File... 🛠️"})







@app.route('/create_file', methods=['POST'])
def create_file():
    if 'username' not in session: 
        return jsonify({"status": "error", "msg": "Login first"}), 401

    user = session.get('username')
    users = load_users()
    u = users.get(user, {})
    
    # --- [SMART FIX] স্টোরেজ লিমিট চেক লজিক ---
    user_dir = get_user_path()
    current_usage_mb = get_dir_size(user_dir) # বর্তমান সাইজ (MB)
    
    # মেমরি ইউনিট অনুযায়ী লিমিট বের করা
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val 
    else:
        limit_in_mb = u.get('disk', 1024)

    # চেক করা হচ্ছে স্টোরেজ ফুল কি না
    if current_usage_mb >= limit_in_mb:
        return jsonify({
            "status": "error", 
            "msg": f"❌ Storage Full! ({current_usage_mb}MB / {mem_limit_str}). Delete some files! "
        })
    # ------------------------------------------

    data = request.json
    filename = data.get('name')
    
    if not filename:
        return jsonify({"status": "error", "msg": "File name is required!"})

    log_activity("file:create", f"Created new file: {filename}")
    
    path = os.path.join(user_dir, data.get('path', ''), filename)
    
    try:
        with open(path, 'w', encoding='utf-8') as f: 
            f.write("")
        
        # --- [NEW] অটোমেটিক গিটহাব ব্যাকআপ অ্যাড করা হলো ---
        import threading
        # যেহেতু নতুন ফাইল খালি (Empty), তাই গিটহাবেও একটি খালি ফাইল ব্যাকআপ হবে
        threading.Thread(target=auto_github_backup, args=(user, filename, path)).start()
        # -----------------------------------------------

        return jsonify({"status": "success", "msg": f"'{filename}' ᴄʀᴇᴀᴛᴇᴅ! 📄"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})




@app.route('/create_folder', methods=['POST'])
def create_folder():
    data = request.json
    foldername = data.get('name')
    # [ACTIVITY LOG]
    log_activity("file:create_folder", f"Created folder: {foldername}")
    
    path = os.path.join(get_user_path(), data.get('path', ''), foldername)
    if not os.path.exists(path): os.makedirs(path)
    return jsonify({"status":"success"})

@app.route('/edit/<path:name>', methods=['GET', 'POST'])
def web_edit_file(name):
    path = os.path.join(get_user_path(), name)
    user = session['username']
    
    if request.method == 'POST':
        log_activity("file:edit", f"Updated file content: {name}")
        content = request.json.get('content')
        try:
            with open(path, 'w', encoding='utf-8') as f: 
                f.write(content)
            
            # --- [NEW] এডিট করার সাথে সাথে অটো ব্যাকআপ ---
            import threading
            threading.Thread(target=auto_github_backup, args=(user, name, path)).start()
            
            msg = f'<b style="color: #2ecc71; font-size: 25px;">✅ {name.upper()} ᴜᴘᴅᴀᴛᴇ ᴅᴏɴᴇ</b>'
            return jsonify({"status": "success", "msg": msg})
            
        except Exception as e:
            msg = f'<b style="color: #e74c3c; font-size: 25px;">❌ ᴇʀʀᴏʀ: {str(e).upper()}</b>'
            return jsonify({"status": "error", "msg": msg})
    
    with open(path, 'r', encoding='utf-8') as f: 
        return jsonify({"content": f.read()})



@app.route('/delete/<path:name>')
def delete_item(name):
    # [ACTIVITY LOG]
    log_activity("file:delete", f"Permanently deleted: {name}")
    
    user = session['username']
    path = os.path.join(get_user_path(), name)
    
    # --- [NEW] গিটহাব থেকে ডিলিট করার লজিক ---
    def delete_from_github(username, filename):
        GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
        GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
        github_path = f"users/{username}/{filename}"
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{github_path}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

        try:
            # গিটহাব থেকে ফাইলের SHA সংগ্রহ করা
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                sha = res.json()['sha']
                # ফাইল ডিলিট করার রিকোয়েস্ট
                del_data = {
                    "message": f"Auto Delete: {filename} (User: {username})",
                    "sha": sha
                }
                requests.delete(url, json=del_data, headers=headers)
        except Exception as e:
            print(f"GitHub Delete Error: {e}")

    # যদি এটি ফাইল হয় তবেই গিটহাব থেকে ডিলিট করার থ্রেড চলবে
    if os.path.isfile(path):
        import threading
        threading.Thread(target=delete_from_github, args=(user, name)).start()
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
        # ফোল্ডার ডিলিট করার ক্ষেত্রে গিটহাব এপিআই একটু জটিল, 
        # তাই শুধু ফাইলের জন্য এই ব্যাকআপ ডিলিট সিস্টেমটি পারফেক্ট কাজ করবে।

    return jsonify({"status":"success"})


@app.route('/rename', methods=['POST'])
def rename_item():
    data = request.json
    old_name = data.get('old')
    new_name = data.get('new')
    user = session['username']
    
    log_activity("file:rename", f"Renamed {old_name} to {new_name}")
    
    old = os.path.join(get_user_path(), old_name)
    new = os.path.join(get_user_path(), new_name)
    os.rename(old, new)

    # --- [NEW] নতুন নামে গিটহাবে ব্যাকআপ পাঠানো ---
    if os.path.isfile(new):
        import threading
        threading.Thread(target=auto_github_backup, args=(user, new_name, new)).start()

    return jsonify({"status":"success"})


import base64
import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip() # আপনার টোকেন
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()

def sync_from_github():
    """সার্ভার স্টার্ট হওয়ার সময় গিটহাব থেকে সব ফাইল রিকভার করবে"""
    print("🔄 Syncing files from GitHub...")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/users"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        user_folders = res.json()
        for folder in user_folders:
            if folder['type'] == 'dir':
                u_name = folder['name']
                u_path = os.path.join(USERS_ROOT, u_name)
                if not os.path.exists(u_path): os.makedirs(u_path)
                
                # ওই ইউজারের সব ফাইল নামিয়ে আনা
                files_url = folder['url']
                f_res = requests.get(files_url, headers=headers)
                if f_res.status_code == 200:
                    for f_data in f_res.json():
                        f_name = f_data['name']
                        f_download_url = f_data['download_url']
                        # ফাইলটি লোকালি সেভ করা
                        content = requests.get(f_download_url).content
                        with open(os.path.join(u_path, f_name), 'wb') as f:
                            f.write(content)
        print("✅ Sync Complete!")

@app.route('/logs/<path:filename>')
def get_logs(filename):
    if 'username' not in session: return jsonify({"logs": ""})
    
    user = session.get('username')
    user_dir = os.path.join(USERS_ROOT, user)
    
    # টার্মিনাল লগের জন্য
    if filename == "terminal":
        log_key = f"{user}_terminal"
        return jsonify({"logs": console_logs.get(log_key, "Sʏꜱᴛᴇᴍ Rᴇᴀᴅʏ...\n")})
    
    # [FIX] সঠিক লগ ফাইল পাথ চেক
    log_file_path = os.path.join(user_dir, f"{filename}.log")
    
    if os.path.exists(log_file_path):
        try:
            with open(log_file_path, "r", encoding="utf-8") as f:
                content = f.read()
                # শেষ ২০ হাজার ক্যারেক্টার দেখানো হচ্ছে যাতে ওয়েবসাইট ফাস্ট থাকে
                return jsonify({"logs": content[-20000:] if len(content) > 20000 else content})
        except:
            return jsonify({"logs": "Eʀʀᴏʀ 404!"})
    
    return jsonify({"logs": "Yᴏᴜʀ Mᴀ𝙸ɴ.ᴩʏ 𝙸ꜱ Nᴏᴛ Rᴜɴɴ𝙸ɴɢ।"})

    
@app.route('/')
def index():
    if 'username' not in session: return redirect(url_for('login'))
    
    user_dir = get_user_path()
    rel_path = request.args.get('path', '')
    target_path = os.path.abspath(os.path.join(user_dir, rel_path))
    files = []
    
    for entry in os.scandir(target_path):
        files.append({
            "name": entry.name, "is_dir": entry.is_dir(),
            "rel_path": os.path.relpath(entry.path, user_dir)
        })
    files = sorted(files, key=lambda x: (not x['is_dir'], x['name']))
    
    # total_users রিমুভ করা হয়েছে
    return render_template('index.html', files=files, user=session['username'], current_path=rel_path)


from flask import jsonify, request, session, render_template, redirect, url_for


# 🚀 সেশন কনফিগারেশন (১ ঘণ্টা)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)


# --- গ্লোবাল গিটহাব সেটিংস (Updated) ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
GITHUB_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/users.json"

def check_github_user(u_input, p_input):
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    try:
        # সরাসরি GitHub API থেকে users.json রিকোয়েস্ট করা হচ্ছে
        res = requests.get(GITHUB_URL, headers=headers, timeout=10)
        
        if res.status_code == 200:
            content = res.json()['content']
            # বেস-৬৪ ডিকোড করে ডিকশনারিতে কনভার্ট করা
            users = json.loads(base64.b64decode(content).decode('utf-8'))
            
            # Case insensitive username search (বড়-ছোট হাতের অক্ষর ম্যাটার করবে না)
            for original_name, data in users.items():
                if original_name.lower() == u_input.lower():
                    # পাসওয়ার্ড চেক (স্ট্রিং হিসেবে তুলনা করা হচ্ছে)
                    if str(data.get('p')) == str(p_input):
                        return {"status": "success", "username": original_name, "data": data}
                    else:
                        return {"status": "wrong_password"}
            
            return {"status": "not_found"}
            
        elif res.status_code == 404:
            print("❌ users.json file not found in the repo!")
            return {"status": "error"}
        elif res.status_code == 401:
            print("❌ Invalid GitHub Token! 🤬")
            return {"status": "error"}
            
    except Exception as e:
        print(f"⚠️ GitHub Auth Connection Error: {e}")
        return {"status": "error"}
        
    return {"status": "not_found"}


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            data = request.get_json()
            u_input = data.get('username', '').strip()
            p_input = data.get('password', '').strip()

            if not u_input or not p_input:
                return jsonify({"status": "error", "msg": "Empty! Error"}), 400

            # সরাসরি গিটহাব থেকে চেক করা হচ্ছে
            result = check_github_user(u_input, p_input)

            if result["status"] == "success":
                user_data = result["data"]
                
                if user_data.get('status') == 'suspended':
                    return jsonify({"status": "error", "msg": "Account Banned! "}), 403

                # সেশন সেটআপ
                session.clear()
                session.permanent = True
                session['username'] = result["username"]
                
                # লোকাল ডাটাবেস আপডেট (ব্যাকআপ হিসেবে)
                load_users() 
                
                return jsonify({"status": "success", "msg": "Login Successful! 🚀"})
            
            elif result["status"] == "wrong_password":
                return jsonify({"status": "error", "msg": "Wrong Password! ❌"}), 401
            
            elif result["status"] == "not_found":
                return jsonify({"status": "error", "msg": "Usar Not Found! 🔍"}), 404
            
            else:
                return jsonify({"status": "error", "msg": "Unknown ERROR! ⚠️"}), 500
                
        except Exception as e:
            return jsonify({"status": "error", "msg": f"Server Crash: {str(e)}"}), 500
        
    return render_template('login.html')


@app.route('/logout')
def logout():
    # [ACTIVITY LOG] Logout track korbe
    if 'username' in session:
        log_activity("server:logout", "User session terminated by logout.")
        
    session.clear()
    return redirect(url_for('login'))

def sync_to_githubv2(users_dict, commit_message):
    """GitHub এ সরাসরি ফাইল আপডেট করার উন্নত ফাংশন (v2)"""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        
        # ১. বর্তমান ফাইলের SHA সংগ্রহ করা
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            sha = r.json()['sha']
            
            # ২. কন্টেন্ট আপডেট করা
            import json
            updated_content = json.dumps(users_dict, indent=4)
            encoded_content = base64.b64encode(updated_content.encode()).decode()
            
            data = {
                "message": commit_message,
                "content": encoded_content,
                "sha": sha
            }
            # ৩. ফাইলটি GitHub-এ পুশ (Update) করা
            requests.put(url, headers=headers, json=data)
    except Exception as e:
        print(f"GitHub Sync Error: {e}")

# --- পাসওয়ার্ড রিকভারি লজিক ---
# --- পাসওয়ার্ড রিকভারি লজিক (Old Password System) ---
@app.route('/recover', methods=['GET', 'POST'])
def recover_password():
    if request.method == 'POST':
        u = request.json.get('username', '').strip()
        old_p = request.json.get('old_password', '').strip()
        new_p = request.json.get('new_password', '').strip()
        
        users = load_users()
        
        if u in users and str(users[u].get('p')) == str(old_p):
            users[u]['p'] = new_p
            save_users(users)
            
            # নতুন ফাংশন কল
            sync_to_githubv2(users, f"Password recovered for user: {u}")
            
            return jsonify({"status": "success", "msg": "Password changed & synced!"})
        
        return jsonify({"status": "error", "msg": "Invalid username or old password!"})
    
    return render_template('login.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'username' not in session: 
        return jsonify({"status": "error", "msg": "Login first"}), 401
    
    user = session['username']
    users = load_users()
    u = users.get(user, {})
    
    # --- [SMART FIX] স্টোরেজ লিমিট চেক লজিক ---
    user_dir = get_user_path()
    current_usage_mb = get_dir_size(user_dir) # বর্তমান ব্যবহারের পরিমাণ (MB তে)
    
    # ডাটাবেস থেকে মেমরি লিমিট নেওয়া (যেমন: '6KB', '512MB', '1GB')
    mem_limit_str = str(u.get('memory', '512MB')).upper()
    
    # ইউনিট কনভার্ট করে MB তে নিয়ে আসা (যাতে তুলনা করা সহজ হয়)
    limit_in_mb = 0
    match = re.match(r"(\d+)", mem_limit_str)
    if match:
        val = float(match.group(1))
        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
        else: limit_in_mb = val # Default MB
    else:
        limit_in_mb = u.get('disk', 1024) 

    # ১. আপলোড করার আগেই চেক: বর্তমান স্টোরেজ কি ফুল?
    if current_usage_mb >= limit_in_mb:
        return jsonify({
            "status": "error", 
            "msg": f"❌ Storage Full! ({current_usage_mb}MB / {mem_limit_str}). Delete some files. "
        }), 403
    # ------------------------------------------

    if 'file' not in request.files:
        return jsonify({"status": "error", "msg": "No file part!"})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "msg": "No selected file!"})

    if file:
        # ২. নতুন ফাইলের সাইজ চেক (যাতে আপলোড হয়ে লিমিট ক্রস না করে)
        file.seek(0, os.SEEK_END)
        file_length = file.tell() / (1024 * 1024) # MB তে কনভার্ট
        file.seek(0) # পয়েন্টার শুরুতে ফেরত আনা যাতে ফাইল সেভ করা যায়

        if (current_usage_mb + file_length) > limit_in_mb:
            return jsonify({
                "status": "error", 
                "msg": f"❌ Upload Failed! This file ({round(file_length, 2)}MB) exceeds your limit. 🚨"
            }), 403

        log_activity("file:upload", f"Uploaded: {file.filename}")
        
        rel_path = request.form.get('path', '')
        target_dir = os.path.join(user_dir, rel_path)
        
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
            
        file_path = os.path.join(target_dir, file.filename)
        
        # ফাইলটি লোকাল স্টোরেজে সেভ করা
        file.save(file_path)
        
        # --- [NEW] অটোমেটিক গিটহাব ব্যাকআপ অ্যাড করা হলো ---
        # এটি ব্যাকগ্রাউন্ডে কাজ করবে, তাই আপলোড স্পিড স্লো হবে না
        try:
            import threading
            threading.Thread(target=auto_github_backup, args=(user, file.filename, file_path)).start()
        except Exception as e:
            print(f"Threading Error: {e}")
        # -----------------------------------------------

        return jsonify({"status": "success", "msg": f"'{file.filename}' !Sᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ 🚀"})




# --- সিকিউরিটি কি সেট করুন ---
ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "").strip()

# --- নির্দিষ্ট ইউজার ডিলিট করার রুট ---
@app.route('/remove_user/<username>')
def remove_user(username):
    # URL query key must match ADMIN_SECRET_KEY from Railway Variables.
    key = request.args.get('key')
    if key != ADMIN_SECRET_KEY:
        return jsonify({"status": "error", "msg": "Unauthorized! Wrong Admin Key. 😡"}), 403

    users = load_users()
    
    if username in users:
        # ১. ডাটাবেস (json) থেকে রিমুভ
        del users[username]
        save_users(users)
        
        # ২. ইউজারের ফোল্ডার ডিলিট করা
        user_path = os.path.join(USERS_ROOT, username)
        if os.path.exists(user_path):
            shutil.rmtree(user_path)
            
        return jsonify({"status": "success", "msg": f"User '{username}' and their files removed! 🗑️"})
    
    return jsonify({"status": "error", "msg": "User not found!"})


# --- সব ইউজার ডিলিট করার রুট ---
@app.route('/remove_all')
def remove_all_users():
    # URL query key must match ADMIN_SECRET_KEY from Railway Variables.
    key = request.args.get('key')
    if key != ADMIN_SECRET_KEY:
        return jsonify({"status": "error", "msg": "Unauthorized Access! 🚫"}), 403

    try:
        # ১. ডাটাবেস রিসেট করা
        save_users({})
        
        # ২. ইউজার্স রুট ফোল্ডার ডিলিট করে আবার নতুন করে তৈরি করা
        if os.path.exists(USERS_ROOT):
            shutil.rmtree(USERS_ROOT)
        os.makedirs(USERS_ROOT)
        
        return jsonify({"status": "success", "msg": "All users and data have been wiped! 🧹"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})







from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for




# 🚀 Security & Database
ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY", "").strip()
notifications_db = []

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Panel | Shadow Hosting</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700&family=Inter:wght@400;600&display=swap');
        
        body { 
            background-color: #05060b; 
            color: white; 
            font-family: 'Inter', sans-serif; 
            overflow: hidden;
            position: relative;
            height: 100vh;
            margin: 0;
        }

        /* 🌌 FAST MOVING STARFIELD 🚀 */
        .stars-container {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            z-index: -3;
            background: radial-gradient(ellipse at bottom, #1B2735 0%, #090A0F 100%);
        }

        .star {
            position: absolute;
            background: white;
            border-radius: 50%;
            opacity: 0.6;
            animation: twinkle var(--duration) infinite ease-in-out, 
                       moveStar var(--move-duration) linear infinite;
        }

        @keyframes twinkle {
            0%, 100% { opacity: 0.3; transform: scale(1); }
            50% { opacity: 1; transform: scale(1.3); }
        }

        @keyframes moveStar {
            from { transform: translateX(-10vw); }
            to { transform: translateX(110vw); }
        }

        /* Background Layers */
        .bg-container {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            z-index: -2;
        }

        .bg-grid {
            position: absolute;
            width: 200%;
            height: 200%;
            top: -50%;
            left: -50%;
            background-image: 
                linear-gradient(rgba(26, 79, 204, 0.08) 1px, transparent 1px),
                linear-gradient(90deg, rgba(26, 79, 204, 0.08) 1px, transparent 1px);
            background-size: 60px 60px;
            transform: perspective(1000px) rotateX(60deg);
            animation: grid-scroll 25s linear infinite;
        }

        @keyframes grid-scroll {
            0% { transform: perspective(1000px) rotateX(60deg) translateY(0); }
            100% { transform: perspective(1000px) rotateX(60deg) translateY(60px); }
        }

        .glow-orb {
            position: absolute;
            width: 800px;
            height: 800px;
            background: radial-gradient(circle, rgba(26, 79, 204, 0.15) 0%, transparent 75%);
            border-radius: 50%;
            filter: blur(100px);
            z-index: -1;
            animation: orb-drift 15s infinite alternate;
        }

        @keyframes orb-drift {
            0% { transform: translate(-30%, -30%); }
            100% { transform: translate(40%, 30%); }
        }

        .glass-panel { 
            background: rgba(13, 15, 23, 0.75); 
            border: 1px solid rgba(255, 255, 255, 0.08); 
            border-radius: 28px; 
            backdrop-filter: blur(25px);
            box-shadow: 0 30px 60px -15px rgba(0, 0, 0, 0.9);
        }

        .glow-btn {
            background: linear-gradient(135deg, #1a4fcc, #4483eb);
            position: relative;
            overflow: hidden;
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            box-shadow: 0 0 25px rgba(26, 79, 204, 0.4);
        }

        .glow-btn:hover {
            box-shadow: 0 0 40px rgba(26, 79, 204, 0.7);
            transform: translateY(-4px) scale(1.03);
        }

        .neon-text {
            text-shadow: 0 0 20px rgba(26, 79, 204, 0.7);
            font-family: 'Orbitron', sans-serif;
            letter-spacing: 2px;
        }

        /* 🚨 Universal Pop-up Styling 🤬 */
        .status-popup {
            display: none;
            position: fixed;
            top: 20px;
            right: 20px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            padding: 15px 25px;
            border-radius: 15px;
            z-index: 100;
            backdrop-filter: blur(10px);
            animation: slideIn 0.5s ease-out;
        }

        #errorPopup { 
            background: rgba(220, 38, 38, 0.9); 
            box-shadow: 0 10px 30px rgba(220, 38, 38, 0.4);
        }

        #successPopup { 
            background: rgba(16, 185, 129, 0.9); 
            box-shadow: 0 10px 30px rgba(16, 185, 129, 0.4);
        }

        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
    </style>
</head>
<body class="flex items-center justify-center min-h-screen p-4">
    
    <div class="stars-container" id="starfield"></div>
    <div class="bg-container">
        <div class="bg-grid"></div>
        <div class="glow-orb"></div>
    </div>

    <div id="errorPopup" class="status-popup">
        <div class="flex items-center gap-3">
            <i class="fas fa-triangle-exclamation animate-bounce"></i>
            <div>
                <p class="text-xs font-black uppercase tracking-tighter">Invalid Key! </p>
                <p class="text-[10px] opacity-80">Access Denied. Try Again.</p>
            </div>
        </div>
    </div>

    <div id="successPopup" class="status-popup">
        <div class="flex items-center gap-3">
            <i class="fas fa-circle-check animate-pulse"></i>
            <div>
                <p class="text-xs font-black uppercase tracking-tighter">Verified! </p>
                <p class="text-[10px] opacity-80">Access Granted. Welcome Admin.</p>
            </div>
        </div>
    </div>

    {% if not logged_in %}
    <div class="glass-panel w-full max-w-sm p-10 shadow-2xl text-center border-t border-blue-400/20">
        <div class="mb-8">
            <div class="w-20 h-20 bg-blue-600/10 rounded-full flex items-center justify-center mx-auto border border-blue-500/30 shadow-[0_0_40px_rgba(26,79,204,0.4)] animate-pulse">
                <i class="fas fa-user-shield text-blue-400 text-3xl"></i>
            </div>
            <h2 class="text-2xl font-black mt-5 tracking-tight neon-text">VERIFY ACCOUNT</h2>
            <p class="text-[9px] text-blue-500/70 uppercase tracking-[5px] mt-2 font-black">Authorized Only</p>
        </div>
        
        <form id="loginForm" method="POST" action="/admin_login">
            <input type="password" id="adminKey" name="key" placeholder="SECURE KEY" required
                class="w-full bg-[#080a0f] border border-gray-800 rounded-2xl p-4 text-center text-sm outline-none text-white focus:border-blue-500 transition-all mb-6 tracking-widest shadow-inner">
            <button type="submit" class="glow-btn w-full py-4 rounded-2xl font-black text-xs uppercase tracking-[3px] text-white">Initialize Login</button>
        </form>
    </div>

    <script>
        window.onload = function() {
            const urlParams = new URLSearchParams(window.location.search);
            if (urlParams.has('error')) {
                showStatus('errorPopup');
            }
        };

        function showStatus(id) {
            const popup = document.getElementById(id);
            popup.style.display = 'block';
            setTimeout(() => { 
                popup.style.display = 'none';
                window.history.replaceState({}, document.title, window.location.pathname);
            }, 4000);
        }
    </script>
    
    {% else %}
    <div class="glass-panel w-full max-w-md p-8 shadow-2xl relative border-t border-blue-400/20">
        <a href="/admin_logout" class="absolute top-6 right-6 text-gray-500 hover:text-red-500 transition-all hover:rotate-180">
            <i class="fas fa-power-off text-lg"></i>
        </a>

        <div class="flex flex-col items-center mb-6 pb-6 border-b border-white/5">
            <div class="w-14 h-14 bg-gradient-to-tr from-blue-600 to-cyan-400 rounded-full flex items-center justify-center shadow-lg mb-3">
                <i class="fas fa-user-astronaut text-white text-xl"></i>
            </div>
            <div class="text-center">
                <p class="text-[9px] text-blue-500 uppercase tracking-[3px] font-bold">Authenticated Key</p>
                <h3 class="text-sm font-mono text-cyan-300 mt-1">{{ admin_key }}</h3>
            </div>
        </div>

        <div class="flex items-center gap-5 mb-8">
            <div class="w-16 h-16 bg-blue-600/20 rounded-2xl flex items-center justify-center shadow-[0_0_30px_rgba(26,79,204,0.3)] border border-blue-500/30">
                <i class="fas fa-signal text-blue-400 text-2xl animate-pulse"></i>
            </div>
            <div>
                <h2 class="text-xl font-black tracking-tight neon-text uppercase">Transmission</h2>
                <p class="text-[10px] text-green-400 font-bold uppercase tracking-widest flex items-center gap-2">
                    <span class="relative flex h-2 w-2">
                        <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
                        <span class="relative inline-flex rounded-full h-2 w-2 bg-green-500"></span>
                    </span> 
                    Admin Online
                </p>
            </div>
        </div>

        <div class="space-y-6">
            <textarea id="notifMsg" placeholder="Enter encrypted transmission data..." 
                class="w-full bg-[#080a0f] border border-gray-800 rounded-2xl p-6 text-sm outline-none h-44 resize-none text-white focus:border-blue-500 transition-all shadow-inner"></textarea>
            
            <button onclick="send()" class="glow-btn w-full py-4 rounded-2xl font-black text-xs uppercase tracking-[3px] flex items-center justify-center gap-3 text-white">
                <i class="fas fa-paper-plane"></i> Send Broadcast
            </button>
        </div>
        <p id="status" class="mt-6 text-center text-[10px] font-bold uppercase tracking-widest"></p>
    </div>


    <script>
        // Trigger Success Pop-up on Login Redirect
        window.onload = function() {
            const urlParams = new URLSearchParams(window.location.search);
            if (urlParams.has('login') && urlParams.get('login') === 'success') {
                const sPopup = document.getElementById('successPopup');
                sPopup.style.display = 'block';
                setTimeout(() => { 
                    sPopup.style.display = 'none';
                    window.history.replaceState({}, document.title, window.location.pathname);
                }, 4000);
            }
        };

        function send() {
            const msg = document.getElementById('notifMsg').value;
            const statusDiv = document.getElementById('status');
            if(!msg) { 
                statusDiv.className = "mt-6 text-center text-[10px] text-red-500 font-bold uppercase"; 
                statusDiv.innerText = "Error: Null Data "; 
                return; 
            }
            fetch(`/sendnotification?msg=${encodeURIComponent(msg)}&confirm=true`)
                .then(res => res.json())
                .then(data => {
                    if(data.status === "success") {
                        statusDiv.className = "mt-6 text-center text-[10px] text-green-400 font-bold uppercase";
                        statusDiv.innerText = " Success!";
                        document.getElementById('notifMsg').value = "";
                    }
                });
        }
    </script>
    {% endif %}

    <script>
        const starfield = document.getElementById('starfield');
        const starCount = 120; 
        for (let i = 0; i < starCount; i++) {
            const star = document.createElement('div');
            star.className = 'star';
            const size = Math.random() * 2 + 1 + 'px';
            const startTop = Math.random() * 100 + '%';
            const startLeft = Math.random() * 110 - 10 + '%'; 
            star.style.width = size;
            star.style.height = size;
            star.style.top = startTop;
            star.style.left = startLeft;
            star.style.setProperty('--duration', (Math.random() * 2 + 1) + 's');
            star.style.setProperty('--move-duration', (Math.random() * 8 + 4) + 's');
            starfield.appendChild(star);
        }
    </script>
</body>
</html>

"""

@app.route('/admin_login', methods=['POST'])
def admin_login():
    key = request.form.get('key')
    if key == ADMIN_SECRET_KEY:
        session['admin_logged_in'] = True
        # Login success pop-up trigger korbe
        return redirect(url_for('send_notification', login='success'))
    
    return redirect(url_for('send_notification', error=1))


@app.route('/admin_logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('send_notification'))

# Initialize your DB if not already done
notifications_db = []

@app.route('/get_latest_notif')
def get_latest_notif():
    # Return the full list and specifically the last message text
    return jsonify({
        "all_msgs": notifications_db,
        "new_msg": notifications_db[-1]["text"] if notifications_db else None
    })

@app.route('/sendnotification', methods=['GET'])
def send_notification():
    msg = request.args.get('msg')
    
    # যদি শুধু পেজ লোড হয় (msg না থাকে)
    if not msg:
        return render_template_string(ADMIN_HTML, logged_in=session.get('admin_logged_in'), admin_key=ADMIN_SECRET_KEY)
    
    # অথরাইজেশন চেক
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "message": "Unauthorized!"}), 403
    
    # নোটিফিকেশন সেভ করা
    import datetime # এটি নিশ্চিত করুন ফাইলের শুরুতে আছে
    notif_data = {"text": msg, "time": datetime.datetime.now().strftime("%I:%M %p")}
    notifications_db.append(notif_data)
    
    return jsonify({"status": "success", "msg": "Notification Broadcasted!"})


@app.route('/ban_user/<username>')
def ban_user(username):
    key = request.args.get('key')
    if key != ADMIN_SECRET_KEY:
        return jsonify({"status": "error", "msg": "Unauthorized! 😡"}), 403

    users = load_users()
    if username in users:
        # ১. ডাটাবেসে ইউজার স্ট্যাটাস পরিবর্তন
        users[username]['status'] = 'suspended'
        save_users(users)

        # ২. ইউজারের চলমান প্রসেসগুলো (Running Bots/Scripts) অটোমেটিক স্টপ করা
        # running_processes ডিকশনারি থেকে ওই ইউজারের সব ফাইল খুঁজে বের করা হচ্ছে
        keys_to_stop = [k for k in running_processes.keys() if k.startswith(f"{username}_")]
        
        for k in keys_to_stop:
            try:
                pid = running_processes[k]
                # প্রসেস গ্রুপ সহ কিল করা হচ্ছে যাতে চাইল্ড প্রসেসও বন্ধ হয়
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except:
                    os.kill(pid, signal.SIGKILL)
                
                # লিস্ট থেকে রিমুভ করা
                del running_processes[k]
                if k in file_start_times:
                    del file_start_times[k]
            except Exception as e:
                print(f"Error stopping process for banned user: {e}")

        # ৩. যদি ব্যান করা ইউজার বর্তমানে ব্রাউজারে লগইন থাকে, সেশন ক্লিয়ার করে দাও
        if session.get('username') == username:
            session.clear()

        return jsonify({
            "status": "success", 
            "msg": f"User '{username}' BANNED & all running processes STOPPED! 🚫"
        })
    
    return jsonify({"status": "error", "msg": "User not found!"})

    


@app.route('/activity.html')
def activity_log():
    if 'username' not in session: 
        return '<p class="p-5 text-red-500">Session expired. Please login.</p>', 401
    
    user = session['username']
    # activities ডাটা না থাকলে খালি লিস্ট পাঠানো হচ্ছে
    acts = user_activities.get(user, [])
    
    # render_template এ ডাটা পাস করা হচ্ছে
    return render_template('activity.html', activities=acts, user=user)

@app.route('/requirements.html')
def requirements_page():
    if 'username' not in session: 
        return '<p class="p-5 text-red-500">Login required.</p>', 401
    return render_template('requirements.html')

# --- [ADMIN] LOGOUT ALL USERS ---
@app.route('/logout_all')
def logout_all_users():
    # URL check: /logout_all?key=OWNER_XEROX89
    key = request.args.get('key')
    if key != ADMIN_SECRET_KEY:
        return jsonify({"status": "error", "msg": "Unauthorized! Access Denied. 😡"}), 403

    try:
        # Changing the secret key instantly invalidates all existing browser sessions
        # Note: In a production environment, you'd save this to a file or env var
        # so it persists after a restart, but for this script, this works:
        app.secret_key = os.urandom(24).hex() 
        
        # Clear the admin's own session too
        session.clear()
        
        return jsonify({
            "status": "success", 
            "msg": "All active sessions have been terminated! 💥",
            "action": "Secret key rotated."
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

import os
from flask import send_from_directory, abort

@app.route('/download/<path:filename>')
def download_file(filename):
    if 'username' not in session:
        return "Unauthorized! Login first ", 401
    
    # User-er specific folder path neya hocche
    user_directory = os.path.abspath(get_user_path())
    
    # Path safe korar jonno (shuru te slash thakle remove kora)
    safe_filename = filename.lstrip('/')
    
    # Full path check korar jonno
    file_full_path = os.path.join(user_directory, safe_filename)
    
    # Debug: jodi error hoy console-e path dekhte parbe
    if os.path.exists(file_full_path) and os.path.isfile(file_full_path):
        return send_from_directory(user_directory, safe_filename, as_attachment=True)
    else:
        print(f"DEBUG: File not found at {file_full_path}")
        return "File Not Found! ", 404

import os
import subprocess
import shutil
from flask import request, jsonify, session

@app.route('/github_deploy', methods=['POST'])
def github_deploy():
    if 'username' not in session:
        return jsonify({"status": "error", "msg": "Login Not Found Login Fast"}), 401

    try:
        data = request.get_json()
        repo_url = data.get('repo_url')
        token = data.get('token')
        
        # Tomar existing function get_user_path() use korchi
        user_dir = get_user_path() 
        
        if not repo_url:
            return jsonify({"status": "error", "msg": "GitHub URL? "}), 400

        # Private Repo hole URL modify kora
        if token and "github.com" in repo_url:
            repo_url = repo_url.replace("https://", f"https://{token}@")

        # Temporary folder clone hoba jate user file safe thake
        temp_dir = os.path.join(user_dir, "temp_git_clone")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

        # Activity Log (tomar tracker logic)
        log_activity("github:deploy", f"Cloning repo into folder")

        process = subprocess.run(
            ["git", "clone", repo_url, temp_dir], 
            capture_output=True, text=True
        )

        if process.returncode == 0:
            # Temp folder theke shob user-er main folder-e niye asha
            for item in os.listdir(temp_dir):
                # .git folder skip kora jate conflict na hoy
                if item == ".git": continue 
                
                source = os.path.join(temp_dir, item)
                destination = os.path.join(user_dir, item)
                
                if os.path.isdir(source):
                    if os.path.exists(destination): shutil.rmtree(destination)
                    shutil.copytree(source, destination)
                else:
                    shutil.copy2(source, destination)
            
            # Temp folder clean up
            shutil.rmtree(temp_dir)
            return jsonify({"status": "success", "msg": "GitHub files saved to your folder! ⚡"})
        else:
            return jsonify({"status": "error", "msg": f"Git Error: {process.stderr}"})

    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

# --- [USER] CHANGE USERNAME & PASSWORD ---
# --- [USER] CHANGE USERNAME & PASSWORD (FIXED LOGIC) ---
import os
import requests
import base64
from flask import jsonify, session, request

# আপনার GitHub কনফিগ (এগুলো আপনার কোডের উপরে ডিফাইন করা থাকতে হবে)

GITHUB_FILE_PATH = "users.json" # আপনার ফাইলের পাথ

def update_github_file(content_dict, message):
    """GitHub-এর ফাইলটি সরাসরি এডিট করার ফাংশন"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    
    # ১. ফাইলের বর্তমান SHA গেট করা (এডিট করার জন্য SHA লাগে)
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        sha = r.json()['sha']
        
        # ২. নতুন কন্টেন্ট এনকোড করা
        import json
        updated_content = json.dumps(content_dict, indent=4)
        encoded_content = base64.b64encode(updated_content.encode()).decode()
        
        # ৩. GitHub-এ পুশ (Update) করা
        data = {
            "message": message,
            "content": encoded_content,
            "sha": sha
        }
        requests.put(url, headers=headers, json=data)

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'username' not in session:
        return jsonify({"status": "error", "msg": "Login first!"}), 401

    data = request.get_json()
    new_username = data.get('username', '').strip()
    new_password = data.get('password', '').strip()
    
    current_user = session['username']
    users = load_users()

    try:
        change_made = False
        if new_password:
            users[current_user]['p'] = new_password
            change_made = True

        if new_username and new_username != current_user:
            if new_username in users:
                return jsonify({"status": "error", "msg": "Username already exists!"})
            
            users[new_username] = users.pop(current_user)
            # ফোল্ডার রিনেম লজিক...
            old_path = os.path.join(USERS_ROOT, current_user)
            new_path = os.path.join(USERS_ROOT, new_username)
            if os.path.exists(old_path):
                os.rename(old_path, new_path)
            
            session['username'] = new_username
            current_user = new_username
            change_made = True

        if change_made:
            save_users(users)
            # নতুন ফাংশন কল
            sync_to_githubv2(users, f"Profile updated for {current_user}")
            return jsonify({"status": "success", "msg": "Pʀᴏꜰɪʟᴇ Uᴩᴅᴀᴛᴇᴅ! ⚡"})
            
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

import os
import re
import json
import subprocess
import requests
from flask import request, jsonify, session, Response, stream_with_context



import requests
import subprocess
import os
import re
import time

# 🤬 Your API credentials


def ai_fix_code(code: str, error_msg: str) -> str:
    """
    Calls OpenRouter AI to fix a full Python script that caused an error.
    Returns the corrected full code as a string, or None if AI fails.
    """
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }

        system_prompt = (
            "You are a Python expert. I will give you **full Python code** that caused an error. "
            "Your task is to fix the code so it runs correctly. "
            "Always return the **full fixed code**, without any explanations or extra text."
        )

        user_prompt = (
            f"Original code:\n{code}\n\n"
            f"Error encountered:\n{error_msg}\n\n"
            "Return the corrected full code only."
        )

        payload = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "stream": False
        }

        response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
        if response.status_code != 200:
            print(f"AI API Error {response.status_code}: {response.text}")
            return None

        result = response.json()
        choices = result.get("choices", [])
        if not choices:
            print("No choices returned by AI")
            return None

        fixed_code = choices[0].get("message", {}).get("content", "")

        # Remove ``` or ```python if AI adds code block
        if fixed_code.startswith("```"):
            lines = fixed_code.splitlines()
            if len(lines) > 2:
                fixed_code = "\n".join(lines[1:-1])
            else:
                fixed_code = lines[1] if len(lines) > 1 else ""

        return fixed_code.strip() if fixed_code else None

    except Exception as e:
        print(f"AI fixer error: {e}")
        return None



        
# Global dictionary to track start times
file_start_times = {}  # { "username_filename": start_timestamp }

def run_python_file_stream(file_path, username=None, auto_fix=True, background=False):
    import time, subprocess, os, re, threading

    env = os.environ.copy()    
    if username:    
        lib_path = os.path.join(USERS_ROOT, username, 'lib_env')    
        if os.path.exists(lib_path):    
            env["PYTHONPATH"] = lib_path + os.pathsep + env.get("PYTHONPATH", "")    

    file_key = f"{username}_{os.path.basename(file_path)}"  

    def _run():    
        file_start_times[file_key] = time.time()  
        try:  
            process = subprocess.Popen(    
                ["python", file_path],    
                stdout=subprocess.PIPE,    
                stderr=subprocess.STDOUT,    
                env=env,    
                text=True,    
                bufsize=1    
            )    
            for line in process.stdout:    
                yield line.rstrip()    
            process.wait()    
            yield f"\n✔️ Script finished with exit code {process.returncode}"    
            return process.returncode
        finally:  
            if file_key in file_start_times:  
                del file_start_times[file_key]  

    def _install_module(module_name):    
        target_lib_dir = os.path.join(USERS_ROOT, username, 'lib_env') if username else None    
        if target_lib_dir:    
            os.makedirs(target_lib_dir, exist_ok=True)    
        cmd = ["pip", "install", module_name, "--no-cache-dir"]    
        if target_lib_dir:    
            cmd += ["--target", target_lib_dir]    
        yield f"\n📦 Installing missing module `{module_name}`..."    
        try:    
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)    
            for line in proc.stdout:    
                yield line.rstrip()    
            proc.wait()    
            if proc.returncode == 0:    
                yield f"✔️ Module `{module_name}` installed successfully!"    
            else:    
                yield f"❌ Failed to install module `{module_name}`"    
        except Exception as e:    
            yield f"❌ Pip install error: {str(e)}"    

    def _main_loop():    
        run_attempt = 0    
        while True:    
            run_attempt += 1    
            yield f"\n🎉 Running `{os.path.basename(file_path)}` (Attempt {run_attempt})...\n"    
            output = ""
            for line in _run():    
                yield line    
                output += line + "\n"    

            # Check exit code
            exit_code = int(output.strip().split("exit code")[-1].strip()) if "exit code" in output else 0    

            # Auto-install missing modules
            mod_match = re.search(r"ModuleNotFoundError: No module named ['\"]([a-zA-Z0-9_\-]+)['\"]", output)    
            if mod_match:    
                missing_module = mod_match.group(1)    
                for line in _install_module(missing_module):    
                    yield line    
                time.sleep(1)    
                continue  # rerun after install

            # Auto-fix errors
            if auto_fix and exit_code != 0:    
                traceback_lines = [l for l in output.splitlines() if "Traceback" in l or "Error" in l]    
                error_msg = "\n".join(traceback_lines)    
                yield f"\n⚠️ Detected error:{error_msg}"    

                with open(file_path, 'r', encoding='utf-8') as f:    
                    code = f.read()    

                fixed_code = ai_fix_code(code, error_msg)    
                if fixed_code:    
                    with open(file_path, 'w', encoding='utf-8') as f:    
                        f.write(fixed_code)    
                    yield f"\n💾 Fixed code saved. Restarting `{os.path.basename(file_path)}`..."    
                    time.sleep(1)    
                    continue    
                else:    
                    yield "\n\n❌ AI could not fix the script automatically."    
                    break    

            break  # finished successfully or cannot fix

    if background:
        def background_runner():
            for _ in _main_loop():
                pass  # discard output in background
        thread = threading.Thread(target=background_runner, daemon=True)
        thread.start()
        yield f"\n🏃‍♂️ `{os.path.basename(file_path)}` is running in the background..."
    else:
        for line in _main_loop():
            yield line
            
@app.route('/api/ai-assistant', methods=['POST'])
def ai_assistant_live():
    data = request.get_json() or {}
    prompt = data.get('prompt', '').strip()
    filename = data.get('current_file', 'main.py')
    username = session.get('username', '')

    if not prompt:
        return jsonify({"status": "error", "msg": "Prompt blank!"}), 400

    # --- Detect run command first ---
    if prompt.lower() in ["r", "run"]:
        intent_mode = "run"
    else:
        # Intent detection
        intent_mode = "code" if any(x in prompt.lower() for x in [
            "fix", "error", "problem", "no module named",
            "modulenotfounderror", filename, "code", "bug", "script", "optimize"
        ]) else "chat"

    file_path = os.path.join(USERS_ROOT, username, filename) if username else os.path.join(BASE_DIR, filename)

    # --- Automatic pip install engine ---
    missing_module = None
    if username and intent_mode != "run":
        # (Existing pip/module detection code here)
        if "no module named" in prompt.lower() or "modulenotfounderror" in prompt.lower():
            match_err = re.search(r"no module named ['\"`‘“]?([a-zA-Z0-9\-_]+)['\"`’”]?", prompt, re.IGNORECASE)
            if match_err:
                missing_module = match_err.group(1).strip()
            else:
                words = prompt.split()
                if words:
                    missing_module = words[-1].replace("'", "").replace('"', '').replace('`', '').strip()
        if not missing_module and "pip install" in prompt.lower():
            match_pip = re.search(r"pip\s+install\s+([a-zA-Z0-9\-_]+)", prompt, re.IGNORECASE)
            if match_pip:
                missing_module = match_pip.group(1).strip()
        if not missing_module and any(k in prompt.lower() for k in ["library", "module", "install", "beautifulsoup"]):
            match_bracket = re.search(r"\(([a-zA-Z0-9\-_]+)\)", prompt)
            if match_bracket:
                missing_module = match_bracket.group(1).strip()
            else:
                match_cmd = re.search(r"(?:install|package)\s+([a-zA-Z0-9\-_]+)", prompt, re.IGNORECASE)
                if match_cmd:
                    missing_module = match_cmd.group(1).strip()
        if missing_module and missing_module.lower() in ["bs4", "beautifulsoup"]:
            missing_module = "beautifulsoup4"

    # Read file content
    file_code = ""
    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            file_code = f.read()

    MAX_CHUNK = 10000
    code_chunks = [file_code[i:i + MAX_CHUNK] for i in range(0, len(file_code), MAX_CHUNK)] if file_code else [""]

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    system_msg_code = "You are an expert AI code fixer. Only return the full corrected executable Python code No explanations."
    system_msg_chat = "You are Anjel, a helpful AI assistant. Answer the user's questions naturally."

    def stream_tokens():
        fixed_code_buffer = ""

        # Send initial mode
        yield f"data: {json.dumps({'mode': 'chat' if missing_module else intent_mode})}\n\n"

        # Live pip install if missing module
        if missing_module:
            target_lib_dir = os.path.join(USERS_ROOT, username, 'lib_env')
            os.makedirs(target_lib_dir, exist_ok=True)
            yield f"data: {json.dumps({'token': f'📦 [System] `{missing_module}` detected. Installing...\n'})}\n\n"
            try:
                process = subprocess.Popen(
                    ["pip", "install", missing_module, "--no-cache-dir", "--target", target_lib_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                warning_count = 0
                for line in process.stdout:
                    line_clean = line.rstrip()
                    if "WARNING" in line_clean:
                        prefix = "⚠️ "
                        warning_count += 1
                    elif "Downloading" in line_clean:
                        prefix = "📥 "
                    elif "Collecting" in line_clean:
                        prefix = "🌟"
                    elif "Successfully installed" in line_clean:
                        prefix = "♻️ "
                    else:
                        prefix = "⚙️ "
                    yield f"data: {json.dumps({'token': f'{prefix}{line_clean}\n'})}\n\n"
                process.wait()
                if warning_count:
                    yield f"data: {json.dumps({'token': f'⚠️ {warning_count} warnings encountered\n'})}\n\n"
                if process.returncode == 0:
                    yield f"data: {json.dumps({'token': f'✔️ `{missing_module}` installed successfully!\n'})}\n\n"
                    return
                else:
                    yield f"data: {json.dumps({'token': f'❌ `{missing_module}` installation failed\n'})}\n\n"
                    return
            except Exception as pip_err:
                yield f"data: {json.dumps({'token': f'❌ Pip error: {str(pip_err)}\n'})}\n\n"
                return

        # Run code directly if run mode
        if intent_mode == "run":
            yield f"data: {json.dumps({'token': f'🚀 Running `{filename}`...\n'})}\n\n"
            for line in run_python_file_stream(file_path, username=username):
                yield f"data: {json.dumps({'token': line})}\n\n"
            return

        # AI code fixing part
        if intent_mode == "code":
            for chunk in code_chunks:
                user_msg = f"{prompt}\n\n[USER FILE CONTENT]\n```python\n{chunk}\n```"
                payload = {"model": AI_MODEL, "messages": [{"role": "system", "content": system_msg_code}, {"role": "user", "content": user_msg}], "stream": True}
                try:
                    r = requests.post(API_URL, headers=headers, json=payload, stream=True, timeout=60)
                    if r.status_code != 200:
                        yield f"data: {json.dumps({'error': f'API Error {r.status_code}: {r.text}'})}\n\n"
                        continue
                    for line in r.iter_lines():
                        if line:
                            line = line.decode('utf-8')
                            if line.startswith("data: "):
                                content = line[6:].strip()
                                if content == "[DONE]": break
                                try:
                                    js = json.loads(content)
                                    token = js.get('choices', [{}])[0].get('delta', {}).get('content', '')
                                    if token:
                                        fixed_code_buffer += token
                                        yield f"data: {json.dumps({'token': token})}\n\n"
                                except Exception:
                                    continue
                except Exception as e:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"

            # Save cleaned code
            cleaned_code = "\n".join([l for l in fixed_code_buffer.splitlines() if not l.strip().startswith("```")]).strip()
            if cleaned_code:
                try:
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(cleaned_code)
                    yield f"data: {json.dumps({'token': f'💾 File `{filename}` saved successfully.\n'})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'error': f'File save failed: {str(e)}'})}\n\n"

            # Run the fixed file live
            yield f"data: {json.dumps({'token': f'🚀 Running `{filename}`...\n'})}\n\n"
            for line in run_python_file_stream(file_path, username=username):
                yield f"data: {json.dumps({'token': line})}\n\n"

        else:
            # Chat mode
            payload = {"model": AI_MODEL, "messages": [{"role": "system", "content": system_msg_chat}, {"role": "user", "content": prompt}], "stream": True}
            try:
                r = requests.post(API_URL, headers=headers, json=payload, stream=True, timeout=60)
                if r.status_code != 200:
                    yield f"data: {json.dumps({'error': f'API Error {r.status_code}'})}\n\n"
                    return
                for line in r.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith("data: "):
                            content = line[6:].strip()
                            if content == "[DONE]": break
                            try:
                                js = json.loads(content)
                                token = js.get('choices', [{}])[0].get('delta', {}).get('content', '')
                                if token:
                                    yield f"data: {json.dumps({'token': token})}\n\n"
                            except Exception:
                                continue
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(stream_tokens()), mimetype='text/event-stream')


@app.route('/api/fix-error', methods=['POST'])
def fix_error():
    """🛠️ এডিটর মোডাল থেকে আসা কোড অটো-ফিক্স ইঞ্জিন - Live AI Refactoring & Auto Pip Installer"""
    data = request.get_json() or {}
    file_path = data.get('file_path', '')  # Expecting something like 'username/main.py' or 'main.py'
    error_logs = data.get('error', '')
    
    # 1. Safely resolve the full path
    full_path = os.path.normpath(os.path.join(CONTAINER_DIR, file_path))
    if not os.path.exists(full_path):
        return jsonify({"status": "error", "msg": "File path target missing!"})
        
    pip_message = ""
    if "ModuleNotFoundError:" in error_logs or "No module named" in error_logs:
        match = re.search(r"No module named ['\"]([^'\"]+)['\"]", error_logs)
        if match:
            missing_module = match.group(1)
            
            # 2. Extract username to locate their isolated lib_env
            # If file_path is 'username/main.py', relative_path looks like ['username', 'main.py']
            rel_parts = os.path.relpath(full_path, USERS_ROOT).split(os.sep)
            
            if rel_parts and rel_parts[0] != '.':
                username = rel_parts[0]
                target_lib_dir = os.path.join(USERS_ROOT, username, 'lib_env')
                
                # Ensure user's environment folder exists
                if not os.path.exists(target_lib_dir):
                    os.makedirs(target_lib_dir)
                
                try:
                    # 3. CRITICAL FIX: Direct pip to install inside the user's custom environment directory
                    subprocess.run(
                        ["pip", "install", missing_module, "--no-cache-dir", "--target", target_lib_dir, "--quiet"], 
                        check=True, 
                        timeout=40
                    )
                    pip_message = f" [Auto-Installed package: {missing_module}]"
                    print(f"[AI Server] Successfully auto-installed missing module '{missing_module}' for user '{username}'")
                except Exception as pip_err:
                    pip_message = f" [Attempted auto-install of {missing_module} but failed]"
                    print(f"[AI Server] Pip auto-install failed for {missing_module}: {pip_err}")
            else:
                pip_message = " [Auto-install skipped: Could not resolve target user path]"

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            original_code = f.read()
            
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        ai_prompt = (
            f"Fix the errors in this script.\n\n"
            f"--- ERROR LOGS ---\n{error_logs}\n\n"
            f"--- ORIGINAL CODE ---\n{original_code}\n\n"
            f"Provide ONLY the updated/fixed code block inside your response. Do not include any explanations or conversational text outside of the code block."
        )
        payload = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "You are a professional compiler repair drone. Return ONLY the executable fixed script code block. No explanations, no chit-chat."},
                {"role": "user", "content": ai_prompt}
            ]
        }
        
        response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            fixed_code = response.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            if "```" in fixed_code:
                lines = fixed_code.split("\n")
                filtered_lines = [line for line in lines if not line.strip().startswith("```")]
                fixed_code = "\n".join(filtered_lines).strip()
            
            try:
                with open(full_path, 'w', encoding='utf-8') as f:
                    f.write(fixed_code)
                print(f"[AI] File auto-saved: {full_path}")
            except Exception as save_err:
                return jsonify({"status": "error", "msg": f"File save error: {save_err}"})
                
            return jsonify({"status": "success", "fixed_code": fixed_code, "msg": f"Code refactored successfully!{pip_message}"})
        else:
            return jsonify({"status": "error", "msg": f"API Error Code: {response.status_code}{pip_message}"})
    except Exception as e:
        return jsonify({"status": "error", "msg": f"{str(e)}{pip_message}"})
      
        
import os
import re
import signal
import subprocess
import threading
import time

# --- [FIXED] RESTART ALL BOTS ---
def restart_all_active_bots():
    """সার্ভার স্টার্ট হওয়ার সময় সব ইউজারের main.py অটোমেটিক রান করবে"""
    print("🔄 [SYSTEM] Scanning active users...")
    
    users = load_users()
    if not users:
        print("⚠️ [SYSTEM] No users found in database.")
        return

    for username, data in users.items():
        # Case insensitive check for username and status
        if str(data.get('status', '')).lower() == 'active':
            user_dir = os.path.join(USERS_ROOT, username)
            main_file = os.path.join(user_dir, 'main.py')
            
            if os.path.exists(main_file):
                # স্টোরেজ চেক (Render-এ disk size calculation অনেক সময় slow হয়, তাই try-except)
                try:
                    current_usage_mb = get_dir_size(user_dir)
                    mem_limit_str = str(data.get('memory', '512MB')).upper()
                    
                    limit_in_mb = 1024
                    match = re.search(r"(\d+)", mem_limit_str)
                    if match:
                        val = float(match.group(1))
                        if 'GB' in mem_limit_str: limit_in_mb = val * 1024
                        elif 'KB' in mem_limit_str: limit_in_mb = val / 1024
                        else: limit_in_mb = val

                    if current_usage_mb >= limit_in_mb:
                        print(f"⚠️ [SKIP] {username}: Storage Full ({current_usage_mb}MB)")
                        continue
                except:
                    print(f"⚠️ [WARN] Could not calculate storage for {username}, proceeding...")

                file_key = f"{username}_main.py"
                log_file_path = os.path.join(user_dir, "main.py.log")
                
                # ইনভায়রনমেন্ট সেটআপ
                env = os.environ.copy()
                venv_path = os.path.join(user_dir, 'lib_env')
                # PYTHONPATH ফিক্স করা হয়েছে যাতে মডিউল ঠিকঠাক পায়
                env['PYTHONPATH'] = venv_path + os.pathsep + env.get('PYTHONPATH', '')
                env['PYTHONUNBUFFERED'] = '1'

                try:
                    # কিল যদি আগে থেকে কিছু থাকে (Render রিস্টার্টের সময় এটি জরুরি)
                    if file_key in running_processes:
                        try:
                            old_pid = running_processes[file_key]
                            os.kill(old_pid, signal.SIGKILL)
                        except: pass
                        running_processes.pop(file_key, None)

                    # বোট রান (start_new_session=True ব্যবহার করা হয়েছে যাতে মেইন প্রসেস মরলে এগুলো না মরে)
                    proc = subprocess.Popen(
                        ['python3', '-u', main_file], 
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.STDOUT, 
                        env=env,
                        cwd=user_dir,
                        start_new_session=True 
                    )
                    
                    running_processes[file_key] = proc.pid
                    file_start_times[file_key] = time.time()
                    
                    # লগ ক্যাপচার থ্রেড
                    threading.Thread(target=capture, args=(file_key, "main.py", proc, log_file_path), daemon=True).start()
                    
                    print(f"✅ [RESTARTED] {username}/main.py (PID: {proc.pid})")
                    time.sleep(0.3) # Render-এ দ্রুত রিস্টার্টের জন্য গ্যাপ কমানো হয়েছে
                except Exception as e:
                    print(f"❌ [ERROR] {username}: {e}")

# --- [FIXED] STARTUP ENGINE ---
def run_everything():
    """বুট হওয়ার সাথে সাথে রান হবে"""
    # Render অনেক সময় ফাইল মাউন্ট করতে সময় নেয়, তাই ৫ সেকেন্ড ওয়েট করা সেফ
    time.sleep(5) 
    print("🔄 [STARTUP] Syncing with GitHub...")
    try:
        sync_from_github()
    except Exception as e:
        print(f"⚠️ [SYNC FAIL] {e}")
        
    print("🚀 [STARTUP] Initializing Bots...")
    restart_all_active_bots()

if __name__ == '__main__':
    if not os.path.exists(USERS_ROOT): os.makedirs(USERS_ROOT)

    # ১. গিটহাব সিঙ্ক + বোট স্টার্ট — ব্যাকগ্রাউন্ড থ্রেডে রান করা হচ্ছে যাতে Flask দ্রুত পোর্ট খুলতে পারে
    startup_thread = threading.Thread(target=run_everything, daemon=True)
    startup_thread.start()
    # --- [UPDATED: 80 SEC DELAY BACKUP] ---
    def backup_all_users_files():
        while True:
            # ৫ মিনিট (৩০০ সেকেন্ড) পরপর লুপ চলবে
            print("⏳ Waiting 300 seconds for next auto-backup cycle...")
            time.sleep(300) 
            
            try:
                print("🚀 [LOOP] Checking and backing up all user files...")
                for user_folder in os.listdir(USERS_ROOT):
                    user_path = os.path.join(USERS_ROOT, user_folder)
                    
                    if os.path.isdir(user_path):
                        for filename in os.listdir(user_path):
                            file_full_path = os.path.join(user_path, filename)
                            
                            if os.path.isfile(file_full_path):
                                # ফাইল ব্যাকআপ থ্রেড স্টার্ট
                                threading.Thread(
                                    target=auto_github_backup, 
                                    args=(user_folder, filename, file_full_path), 
                                    daemon=True
                                ).start()
                                # ফাইলগুলোর মাঝে সামান্য গ্যাপ যাতে GitHub API ব্লক না করে
                                time.sleep(1) 
                print("✅ [LOOP] Auto-backup cycle finished.")
            except Exception as e:
                print(f"⚠️ Global Backup Error: {e}")

    # ব্যাকআপ লুপটি আলাদা থ্রেডে রান করা
    all_backup_thread = threading.Thread(target=backup_all_users_files, daemon=True)
    all_backup_thread.start()
        
    # ৪. ফ্ল্যাস্ক অ্যাপ রান
    port = int(os.environ.get("PORT", 15029))
    app.run(host='0.0.0.0', port=port)
