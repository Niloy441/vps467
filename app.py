import os, subprocess, signal, threading, time, json, resource, shutil
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, g
from datetime import datetime, timedelta, timezone
import pytz
import re
import shlex
import sys

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

# --- Central Server সেটিংস ---
# এই একটাই app.py দুইভাবে চলে:
#   CENTRAL_MODE (SUPABASE_KEY env var আছে) -> এই deployment-ই CENTRAL server (সব panel-এর data + admin এখানে)।
#   PANEL_MODE (SUPABASE_KEY নেই) -> customer panel, data-র জন্য CENTRAL_URL-এর central server-এ কথা বলে।
# Supabase secret key কখনো code-এ লেখা নেই — শুধু central deployment-এর Netrun Secrets (env var) থেকে আসে।
# CENTRAL_URL public (secret না) — সব panel-এ একই URL। যেকোনো panel-এর লিঙ্ক + /admin + owner password
# = একই central admin panel, নির্দিষ্ট central লিঙ্ক মনে রাখতে হয় না। Central project সরালে env var-এ বদলাও।
CENTRAL_URL = os.environ.get("CENTRAL_URL", "https://myt.de1.netrun.io").strip().rstrip("/")
PANEL_KEY = os.environ.get("PANEL_KEY", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
CENTRAL_SECRET = os.environ.get("CENTRAL_SECRET", "change-this-central-secret")
CENTRAL_MODE = bool(SUPABASE_URL and SUPABASE_KEY)

# User-দের code (terminal / script / pip install) যে env পায়, সেখানে secret থাকবে না।
# না হলে যেকোনো panel user `import os; print(os.environ)` দিয়ে SUPABASE_KEY / PANEL_KEY পড়ে ফেলতে পারতো।
_SECRET_ENV_KEYS = {"SUPABASE_KEY", "SUPABASE_URL", "CENTRAL_SECRET", "PANEL_KEY",
                    "GITHUB_TOKEN", "GITHUB_REPO", "OPENROUTER_API_KEY",
                    "ADMIN_SECRET_KEY", "FLASK_SECRET_KEY"}


def _safe_env():
    """User code-এর জন্য scrub করা env — secret key গুলো ছাড়া।"""
    env = os.environ.copy()
    for k in _SECRET_ENV_KEYS:
        env.pop(k, None)
    return env

# এই process যে domain serve করছে (Host header থেকে) — background thread-এ request না থাকলে এটা ব্যবহার হয়।
_LAST_PANEL_DOMAIN = {"domain": None}


def get_panel_domain():
    # Admin অন্য panel manage করলে override (শুধু admin route-এ set হয়)
    try:
        override = getattr(g, 'admin_panel_override', None)
        if override:
            return str(override).strip().lower()
    except Exception:
        pass
    try:
        host = request.host or ''
        if host:
            domain = host.split(':')[0].strip().lower()
            _LAST_PANEL_DOMAIN["domain"] = domain
            return domain
    except Exception:
        pass
    cached = _LAST_PANEL_DOMAIN.get("domain")
    if cached:
        return cached
    return os.environ.get("PANEL_DOMAIN", "default").strip().lower()


PANEL_IDENTITY_FILE = os.path.join(BASE_DIR, "panel_identity.json")
_ENROLL_BLOCKED = {"until": 0}


def _has_admin_token():
    try:
        return bool(session.get('admin_token'))
    except Exception:
        return False


def _get_panel_key():
    # ১. Netrun Secrets-এ হাতে বসানো key (recovery / override) — সবার আগে।
    env_key = os.environ.get("PANEL_KEY", "").strip()
    if env_key:
        return env_key
    # ২. Auto-enroll-এর সময় save করা key file (এই panel-এর domain-এর সাথে মিলতে হবে)।
    try:
        if os.path.exists(PANEL_IDENTITY_FILE):
            with open(PANEL_IDENTITY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get("domain") == get_panel_domain() and data.get("panel_key"):
                return data["panel_key"]
    except Exception:
        pass
    return None


def _save_panel_identity(domain, key):
    try:
        with open(PANEL_IDENTITY_FILE, 'w', encoding='utf-8') as f:
            json.dump({"domain": domain, "panel_key": key}, f)
        try:
            os.chmod(PANEL_IDENTITY_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"⚠️ Could not save panel identity: {e}")


def _ensure_panel_key():
    """Panel-এর key নিশ্চিত করা: env > identity file > auto-enroll (central-এ register)।
    হাতে key বসাতে হয় না — panel প্রথমবার চালু হলেই নিজে register করে, owner /admin থেকে Approve করবে।
    Returns True if we have a usable key."""
    if _get_panel_key():
        return True
    if time.time() < _ENROLL_BLOCKED.get("until", 0):
        return False
    domain = get_panel_domain()
    if not domain or domain in ("default", "all"):
        return False  # আসল domain ছাড়া bogus enroll নয় — প্রথম আসল request-এ enroll হবে ("all" = admin-এর All-panels selector, panel না)
    new_key = os.urandom(24).hex()
    try:
        if CENTRAL_MODE:
            res = c_register_panel(domain, new_key)
            ok = isinstance(res, dict) and res.get("status") in ("pending", "active", "suspended")
        else:
            resp = requests.post(CENTRAL_URL.rstrip("/") + "/api/central/panels/register",
                                 headers={"Content-Type": "application/json"},
                                 json={"domain": domain, "panel_key": new_key}, timeout=15)
            ok = (resp is not None and resp.status_code == 200 and isinstance(resp.json(), dict)
                  and resp.json().get("status") in ("pending", "active", "suspended"))
    except Exception as e:
        print(f"⚠️ Panel auto-enroll failed: {e}")
        ok = False
    if ok:
        _save_panel_identity(domain, new_key)
        print(f"✅ Panel auto-enrolled: {domain} (owner approval pending if new)")
        return True
    _ENROLL_BLOCKED["until"] = time.time() + 300
    return False


def central_headers():
    h = {"Content-Type": "application/json",
         "X-Panel-Key": _get_panel_key() or "",
         "X-Panel-Domain": get_panel_domain()}
    try:
        tok = session.get('admin_token')
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    except Exception:
        pass
    return h


def central_request(method, path, json_data=None, params=None):
    """Central server-এ call। CENTRAL_MODE-এ in-process (HTTP loopback ছাড়া), না হলে HTTP-তে CENTRAL_URL-এ। Fail হলে None।"""
    if _get_panel_key() is None:
        _ensure_panel_key()  # best-effort auto-enroll; fail হলে caller fallback দেখবে
    if CENTRAL_MODE:
        return central_dispatch(method, path, json_data, params)
    url = CENTRAL_URL.rstrip("/") + path
    try:
        if method == "GET":
            return requests.get(url, headers=central_headers(), params=params, timeout=15)
        if method == "POST":
            return requests.post(url, headers=central_headers(), json=json_data, timeout=15)
        if method == "DELETE":
            return requests.delete(url, headers=central_headers(), params=params, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Central request failed: {e}")
    return None


def central_list_users():
    """Central থেকে এই panel-এর users (fail হলে None — save_users অনুমান করে delete করবে না)।"""
    resp = central_request("GET", "/api/central/users")
    if resp is not None and resp.status_code == 200:
        d = resp.json()
        if isinstance(d, dict) and d.get("status") == "success":
            return d.get("users", {})
    return None


def sb_delete_user(username):
    """Central থেকে একটা user-এর row মুছে ফেলা (remove/rename-এর সময়)।"""
    central_request("DELETE", "/api/central/users", params={"username": username})


def sb_delete_all_users():
    """Central থেকে এই panel-এর সব user row মুছে ফেলা (remove_all-এর সময়)।"""
    users = central_list_users() or {}
    for username in list(users.keys()):
        central_request("DELETE", "/api/central/users", params={"username": username})

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
AI_MODEL = "meta-llama/llama-3-8b-instruct"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
# --- ডাটাবেস লজিক (Central Server) ---
def load_users():
    # Primary: central server। যেকোনো সমস্যায় local users.json fallback।
    users = central_list_users()
    if users is not None:
        return users
    print("⚠️ Central unreachable. Using local users.json.")

    # Fallback: local users.json (save_users সবসময় এটা fresh রাখে)
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


# --- লগইন রুট (Fixed) ---



def save_users(data):
    # ১. লোকাল সেভ (load_users-এর fallback-এর জন্য সবসময় fresh থাকবে)
    try:
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"⚠️ Local users.json save failed: {e}")

    # ২. Central sync — diff করে: নতুন/বদলানো upsert, মুছে যাওয়া delete।
    # Central-এ পৌঁছাতে না পারলে sync skip (অনুমান করে delete করা হবে না)।
    if not _ensure_panel_key() and not _has_admin_token():
        print("⚠️ Panel not enrolled. Skipping central sync.")
        return
    current = central_list_users()
    if current is None:
        print("⚠️ Central unreachable. Skipping central sync.")
        return
    data = data or {}
    for username, u in data.items():
        if current.get(username) != u:
            central_request("POST", "/api/central/users",
                            json_data={"user": {"username": username, **u}})
    for username in list(current.keys()):
        if username not in data:
            central_request("DELETE", "/api/central/users", params={"username": username})



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

    env = _safe_env()
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
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return  # GitHub file backup off (Supabase mode) — error ছাড়াই skip
    
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
    # Central admin login ছাড়া /setuser চলবে না (আগের open endpoint বন্ধ)।
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required. Login at /sendnotification first."}), 403

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
        "msg": f"User '{u}' created and synced to Central! 🚀",
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

# Stdlib module names (pip-এ নেই) — requirements.txt-এ থাকলে skip করা হবে
try:
    _STDLIB_NAMES = set(sys.stdlib_module_names)
except AttributeError:
    _STDLIB_NAMES = set()

def _req_package_name(line):
    # "Flask==2.0" -> "Flask", "requests>=2" -> "requests", "numpy ; python_version>'3.8'" -> "numpy"
    return re.split(r"[<>=!~\s;\[]", line, 1)[0].strip()

def run_requirements_file(req_path, log_key, target_lib_dir):
    """pip install -r <file> — লাইন বাই লাইন install (একটা খারাপ লাইনে পুরোটা fail হবে না)।"""
    console_logs.setdefault(log_key, "")
    try:
        with open(req_path, 'r', encoding='utf-8', errors='ignore') as f:
            raw_lines = f.read().splitlines()
    except Exception as e:
        console_logs[log_key] += f'\n<span style="color: #ff3333;">❌ Could not read {req_path}: {e}</span>\n'
        return
    lines = []
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('-'):
            console_logs[log_key] += f'<span style="color: #ffcc00;">⏭️ Skipped option line: {line}</span>\n'
            continue
        pkg = _req_package_name(line)
        if not pkg:
            continue
        if pkg.lower() in _STDLIB_NAMES or pkg == 'resource':
            console_logs[log_key] += f'<span style="color: #ffcc00;">⏭️ Skipped stdlib module (already in Python): {pkg}</span>\n'
            continue
        lines.append(line)
    if not lines:
        console_logs[log_key] += '\n<span style="color: #ffcc00;">⚠️ No installable packages found in requirements file.</span>\n'
        return
    pip_check = subprocess.run([sys.executable, '-m', 'pip', '--version'], capture_output=True)
    pip_base = [sys.executable, '-m', 'pip'] if pip_check.returncode == 0 else [shutil.which('pip') or shutil.which('pip3')]
    if not os.path.exists(target_lib_dir):
        os.makedirs(target_lib_dir)
    ok, fail = 0, 0
    for line in lines:
        console_logs[log_key] += f'<span style="color: #00d4ff;">📦 Installing {line}...</span>\n'
        try:
            proc = subprocess.Popen(pip_base + ['install', line, '--no-cache-dir', '--no-user', '--target', target_lib_dir],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_safe_env())
            for out_line in proc.stdout:
                console_logs[log_key] += out_line
            proc.wait()
            if proc.returncode == 0:
                ok += 1
                console_logs[log_key] += f'<span style="color: #00ff88;">✅ {line} installed.</span>\n'
            else:
                fail += 1
                console_logs[log_key] += f'<span style="color: #ff3333;">❌ {line} failed (exit {proc.returncode}).</span>\n'
        except Exception as e:
            fail += 1
            console_logs[log_key] += f'<span style="color: #ff3333;">❌ {line} error: {e}</span>\n'
    console_logs[log_key] += f'\n<span style="color: #ff4444;">» Requirements done: {ok} ok, {fail} failed.</span>\n'

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
            env = _safe_env()
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

            if is_install and proc.returncode != 0:
                console_logs[log_key] += f'\n<span style="color: #ff3333; font-weight: bold;">❌ Installation failed (exit code {proc.returncode}). Read the pip error lines above to find the cause.</span>\n'

            if is_install and proc.returncode == 0:
                console_logs[log_key] += f'</span>\n<span style="color: #00ff88; font-weight: bold; text-shadow: 0 0 10px rgba(0, 255, 136, 0.5);">✅ Successfully installed {pkg_name}.</span>'

            console_logs[log_key] += f'\n<span style="color: #ff4444;">[{time.strftime("%H:%M:%S")}] » Pʀᴏᴄᴇꜱꜱ Exɪᴛᴇᴅ (Cᴏᴅᴇ: {proc.returncode})</span>\n'
            
        except Exception as e:
            # এরর মেসেজটি টার্মিনালে আরও পরিষ্কার দেখাবে
            console_logs[log_key] += f'\n<span style="color: #ff3333; font-family: monospace;">[SYSTEM ERROR]: {str(e)}</span>\n'

    # Logic Router
    install_args = None
    try:
        _parts = shlex.split(cmd)
    except ValueError as e:
        return jsonify({"status": "error", "msg": f"Invalid command syntax: {e}"})

    # Supports: pip install / pip3 install / pkg install / python -m pip install / python3 -m pip install
    if len(_parts) >= 2 and _parts[0] in ('pip', 'pip3', 'pkg') and _parts[1] == 'install':
        install_args = _parts[2:]
    elif len(_parts) >= 4 and _parts[0] in ('python', 'python3') and _parts[1] == '-m' and _parts[2] == 'pip' and _parts[3] == 'install':
        install_args = _parts[4:]

    if install_args is not None:
        if not install_args:
            return jsonify({"status": "error", "msg": "Nothing to install. Usage: pip install <package>  or  pip install -r requirements.txt"})

        # Resolve requirements file path(s) (-r / --requirement)
        current_dir = os.path.join(user_dir, str(data.get('path', '') or '').strip('/'))
        resolved_args = []
        i = 0
        while i < len(install_args):
            a = install_args[i]
            req_file = None
            if a in ('-r', '--requirement'):
                if i + 1 >= len(install_args):
                    return jsonify({"status": "error", "msg": "Option -r needs a file name. Example: pip install -r requirements.txt"})
                req_file = install_args[i + 1]
                i += 1
            elif a.startswith('--requirement='):
                req_file = a.split('=', 1)[1]
            elif a.startswith('-r') and len(a) > 2 and not a.startswith('--'):
                req_file = a[2:]

            if req_file is not None:
                found = None
                for base in (user_dir, current_dir):
                    cand = os.path.abspath(os.path.join(base, req_file))
                    if cand.startswith(os.path.abspath(user_dir) + os.sep) and os.path.isfile(cand):
                        found = cand
                        break
                if not found and 'requ' in req_file.lower():
                    # বানান ভুল হলে (requerment.txt / requirement.txt) আসল file-টা খুঁজে নেওয়া
                    for variant in ('requirements.txt', 'requirement.txt', 'requerment.txt', 'Requirements.txt'):
                        for base in (user_dir, current_dir):
                            cand = os.path.abspath(os.path.join(base, variant))
                            if cand.startswith(os.path.abspath(user_dir) + os.sep) and os.path.isfile(cand):
                                found = cand
                                break
                        if found:
                            break
                if not found:
                    return jsonify({"status": "error", "msg": f"Requirements file '{req_file}' not found in your folder. Upload it first, then run the command again."})
                resolved_args += ['-r', found]
            else:
                resolved_args.append(a)
            i += 1

        # শুধু -r file হলে: লাইন বাই লাইন install (একটা খারাপ লাইনে পুরোটা fail হবে না)
        if len(resolved_args) == 2 and resolved_args[0] == '-r':
            if not os.path.exists(target_lib_dir): os.makedirs(target_lib_dir)
            threading.Thread(target=run_requirements_file, args=(resolved_args[1], log_key, target_lib_dir), daemon=True).start()
            return jsonify({"status": "success", "msg": f"Installing from {os.path.basename(resolved_args[1])} (line by line) 🚀"})

        # Use the same Python that runs this app (works even if the 'pip' command is missing)
        pip_check = subprocess.run([sys.executable, '-m', 'pip', '--version'], capture_output=True)
        if pip_check.returncode == 0:
            pip_base = [sys.executable, '-m', 'pip']
        else:
            pip_exe = shutil.which('pip') or shutil.which('pip3')
            if not pip_exe:
                return jsonify({"status": "error", "msg": "pip is not installed on this server (no 'pip' command and no 'python -m pip')."})
            pip_base = [pip_exe]

        if not os.path.exists(target_lib_dir): os.makedirs(target_lib_dir)
        target_cmd = pip_base + ['install'] + resolved_args + ['--no-cache-dir', '--no-user', '--target', target_lib_dir]
        pkg = ' '.join(install_args)
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
        # --- [NEW] DB backup ---
        threading.Thread(target=_backup_file_to_central, args=(user, path, data.get('path', ''), get_panel_domain()), daemon=True).start()
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
            # --- [NEW] DB backup (edit-এর নতুন content) ---
            threading.Thread(target=_backup_file_to_central, args=(user, path, os.path.dirname(name), get_panel_domain()), daemon=True).start()
            
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
        if not GITHUB_TOKEN or not GITHUB_REPO:
            return  # GitHub file backup off (Supabase mode) — error ছাড়াই skip
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
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("⏭️ GitHub sync skipped (GITHUB_TOKEN / GITHUB_REPO not set).")
        return
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

    
_AI_RATE = {}

# AI chat widget — user panel-এ inject হয় (index route)। Anjel-এর সাথে chat, voice reply, on/off, error explain।
AI_WIDGET_HTML = """
<style>
#ai-fab { position: fixed; right: 18px; bottom: 18px; width: 56px; height: 56px; border-radius: 50%; border: 0; background: #2563eb; color: #fff; font-size: 24px; cursor: pointer; z-index: 9999; box-shadow: 0 6px 20px rgba(0,0,0,.5); }
#ai-fab.off { background: #475569; }
#ai-panel { position: fixed; right: 18px; bottom: 86px; width: 360px; max-width: calc(100vw - 36px); height: 480px; max-height: calc(100vh - 120px); background: #0d1220; border: 1px solid #1e2a44; border-radius: 14px; display: none; flex-direction: column; z-index: 9999; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,.6); }
#ai-panel.open { display: flex; }
#ai-head { display: flex; align-items: center; gap: 6px; padding: 10px 12px; background: #121a2e; border-bottom: 1px solid #1e2a44; font-weight: 700; font-size: 14px; color: #e8ecf8; }
#ai-head .spacer { flex: 1; }
#ai-head button { background: #1e2a44; color: #fff; border: 0; border-radius: 8px; padding: 6px 9px; cursor: pointer; font-size: 14px; }
#ai-head button.off { opacity: .45; }
#ai-hint { display: none; margin: 8px 10px 0; padding: 8px 10px; background: #422006; border: 1px solid #f59e0b; border-radius: 10px; font-size: 12px; color: #fde68a; cursor: pointer; }
#ai-messages { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.ai-msg { max-width: 88%; padding: 8px 11px; border-radius: 12px; font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.ai-msg.user { align-self: flex-end; background: #2563eb; color: #fff; }
.ai-msg.ai { align-self: flex-start; background: #1e2a44; color: #e8ecf8; }
.ai-msg.sys { align-self: center; background: #26324f; color: #fca5a5; font-size: 12px; }
#ai-input-row { display: flex; gap: 6px; padding: 8px; border-top: 1px solid #1e2a44; }
#ai-input { flex: 1; padding: 9px; border-radius: 10px; border: 1px solid #26324f; background: #080b14; color: #fff; }
#ai-input-row button { background: #2563eb; color: #fff; border: 0; border-radius: 10px; padding: 9px 12px; cursor: pointer; font-weight: 700; }
#ai-explain { width: calc(100% - 16px); margin: 0 8px 8px; padding: 8px; background: #7c3aed; color: #fff; border: 0; border-radius: 10px; cursor: pointer; font-weight: 700; font-size: 12px; }
</style>
<button id="ai-fab" title="AI Assistant">🤖</button>
<div id="ai-panel">
  <div id="ai-head"><span>🤖 Anjel — AI Assistant</span><span class="spacer"></span><button id="ai-voice-btn" title="Voice on/off">🔊</button><button id="ai-power-btn" title="AI on/off">⏻</button><button id="ai-close" title="Close">✕</button></div>
  <div id="ai-hint">⚠️ Terminal-এ error দেখছি — AI-কে জিজ্ঞেস করবো?</div>
  <div id="ai-messages"></div>
  <button id="ai-explain">🔍 Explain my last error</button>
  <div id="ai-input-row"><input id="ai-input" placeholder="Ask AI anything..."><button id="ai-mic" title="Voice input">🎤</button><button id="ai-send">Send</button></div>
</div>
<script>
(function() {
  var ENABLE_KEY = 'shadow_ai_enabled', VOICE_KEY = 'shadow_ai_voice';
  var aiOn = localStorage.getItem(ENABLE_KEY) !== 'off';
  var voiceOn = localStorage.getItem(VOICE_KEY) !== 'off';
  var fab = document.getElementById('ai-fab');
  var panel = document.getElementById('ai-panel');
  var msgs = document.getElementById('ai-messages');
  var input = document.getElementById('ai-input');
  var hint = document.getElementById('ai-hint');
  var voiceBtn = document.getElementById('ai-voice-btn');
  var powerBtn = document.getElementById('ai-power-btn');
  function renderState() {
    fab.classList.toggle('off', !aiOn);
    fab.textContent = aiOn ? '🤖' : '💤';
    voiceBtn.classList.toggle('off', !voiceOn);
    voiceBtn.textContent = voiceOn ? '🔊' : '🔇';
    powerBtn.classList.toggle('off', !aiOn);
  }
  function sysMsg(t) {
    var d = document.createElement('div'); d.className = 'ai-msg sys'; d.textContent = t;
    msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
  }
  function addMsg(t, who) {
    var d = document.createElement('div'); d.className = 'ai-msg ' + who; d.textContent = t;
    msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
    return d;
  }
  function speak(text) {
    if (!voiceOn || !('speechSynthesis' in window)) return;
    try {
      window.speechSynthesis.cancel();
      var clean = text.replace(/```[\\s\\S]*?```/g, ' ').replace(/[#*`_>]/g, ' ').replace(/\\s+/g, ' ').trim().slice(0, 800);
      if (!clean) return;
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(clean));
    } catch (e) {}
  }
  async function sendAI(text) {
    if (!text) return;
    if (!aiOn) { sysMsg('AI is off. Press ⏻ to turn it on.'); return; }
    addMsg(text, 'user');
    var aiEl = addMsg('...', 'ai');
    var full = '';
    try {
      var r = await fetch('/api/ai-assistant', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: text, current_file: 'main.py' }) });
      if (!r.ok) {
        var err = {};
        try { err = await r.json(); } catch (e) {}
        aiEl.textContent = err.msg || ('AI error (HTTP ' + r.status + ')');
        return;
      }
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var parts = buf.split('\\n\\n');
        buf = parts.pop();
        parts.forEach(function(part) {
          var line = part.trim();
          if (line.indexOf('data: ') !== 0) return;
          try {
            var d = JSON.parse(line.slice(6));
            if (d.token) { full += d.token; aiEl.textContent = full; msgs.scrollTop = msgs.scrollHeight; }
            if (d.error) { full += '\\n' + d.error; aiEl.textContent = full; }
          } catch (e) {}
        });
      }
      if (!full) aiEl.textContent = '(no reply)';
      else speak(full);
    } catch (e) {
      aiEl.textContent = 'Network error: ' + e.message;
    }
  }
  async function explainError() {
    try {
      var r = await fetch('/logs/terminal');
      var d = await r.json();
      var logs = (d.logs || '').replace(/<[^>]*>/g, ' ');
      var tail = logs.slice(-3000);
      if (!tail.trim()) { sysMsg('No terminal output found.'); return; }
      sendAI('Explain this terminal error and how to fix it in simple steps:\\n' + tail);
    } catch (e) { sysMsg('Could not read terminal logs.'); }
  }
  async function checkHint() {
    if (!aiOn) return;
    try {
      var r = await fetch('/logs/terminal');
      var d = await r.json();
      var logs = (d.logs || '').replace(/<[^>]*>/g, ' ');
      var tail = logs.slice(-2000);
      if (/(Traceback|ModuleNotFound|Error|error|failed|FAILED)/.test(tail)) {
        hint.style.display = 'block';
      } else { hint.style.display = 'none'; }
    } catch (e) {}
  }
  fab.addEventListener('click', function() {
    var opening = !panel.classList.contains('open');
    panel.classList.toggle('open');
    if (opening) checkHint();
  });
  document.getElementById('ai-close').addEventListener('click', function() { panel.classList.remove('open'); });
  powerBtn.addEventListener('click', function() {
    aiOn = !aiOn;
    localStorage.setItem(ENABLE_KEY, aiOn ? 'on' : 'off');
    if (!aiOn && ('speechSynthesis' in window)) window.speechSynthesis.cancel();
    renderState();
    sysMsg(aiOn ? 'AI turned on.' : 'AI turned off. Press ⏻ to turn it back on.');
  });
  voiceBtn.addEventListener('click', function() {
    voiceOn = !voiceOn;
    localStorage.setItem(VOICE_KEY, voiceOn ? 'on' : 'off');
    if (!voiceOn && ('speechSynthesis' in window)) window.speechSynthesis.cancel();
    renderState();
  });
  document.getElementById('ai-send').addEventListener('click', function() {
    var t = input.value.trim(); input.value = '';
    if (t) sendAI(t);
  });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { var t = input.value.trim(); input.value = ''; if (t) sendAI(t); }
  });
  document.getElementById('ai-explain').addEventListener('click', explainError);
  hint.addEventListener('click', function() { hint.style.display = 'none'; explainError(); });
  var micBtn = document.getElementById('ai-mic');
  if ('webkitSpeechRecognition' in window) {
    var rec = new webkitSpeechRecognition();
    rec.lang = 'en-US';
    rec.onresult = function(ev) {
      var t = ev.results[0][0].transcript;
      if (t) sendAI(t);
    };
    micBtn.addEventListener('click', function() { try { rec.start(); } catch (e) {} });
  } else { micBtn.style.display = 'none'; }
  renderState();
})();
</script>
"""


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
    html = render_template('index.html', files=files, user=session['username'], current_path=rel_path)
    # AI chat widget (voice + on/off + explain-error) — user panel-এ inject করা
    if isinstance(html, str) and '</body>' in html:
        html = html.replace('</body>', AI_WIDGET_HTML + '</body>', 1)
    return html


from flask import jsonify, request, session, render_template, redirect, url_for


# 🚀 সেশন কনফিগারেশন (১ ঘণ্টা)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)


# --- গ্লোবাল গিটহাব সেটিংস (Updated) ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
GITHUB_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/users.json"

def check_central_user(u_input, p_input):
    # Login check — central server-এর মাধ্যমে (এই panel-এর domain scope-এ)।
    # আগের check ফাংশনগুলোর মতোই return contract: success / wrong_password / not_found / error.
    resp = central_request("POST", "/api/central/auth",
                           json_data={"panel_domain": get_panel_domain(),
                                      "username": u_input, "password": p_input})
    if resp is None:
        return {"status": "error", "msg": "Cannot reach central server. Try again in a moment."}
    try:
        d = resp.json()
    except Exception:
        return {"status": "error", "msg": f"Central returned an unexpected response (HTTP {resp.status_code})."}
    if isinstance(d, dict) and d.get("status") in ("success", "wrong_password", "not_found", "error"):
        return d
    return {"status": "error", "msg": "Central returned an unexpected response."}


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"status": "error", "msg": "Invalid request: a JSON body is required."}), 400

            u_input = str(data.get('username', '')).strip()
            p_input = str(data.get('password', '')).strip()

            if not u_input or not p_input:
                return jsonify({"status": "error", "msg": "Username and password are required."}), 400

            result = check_central_user(u_input, p_input)

            if result["status"] == "success":
                user_data = result["data"]

                if user_data.get('status') == 'suspended':
                    return jsonify({"status": "error", "msg": "Account Banned!"}), 403

                session.clear()
                session.permanent = True
                session['username'] = result["username"]

                # Local database update (backup)
                load_users()

                return jsonify({"status": "success", "msg": "Login Successful! 🚀"})

            elif result["status"] == "wrong_password":
                return jsonify({"status": "error", "msg": "Wrong password."}), 401

            elif result["status"] == "not_found":
                return jsonify({"status": "error", "msg": "User not found."}), 404

            else:
                # NOTE: never use HTTP 403 here, the login page treats 403 as "account banned".
                return jsonify({"status": "error", "msg": result.get("msg", "Login failed for an unknown reason. Check the server logs.")}), 502

        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"status": "error", "msg": f"Server error: {type(e).__name__}: {e}"}), 500

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
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return  # GitHub sync off (Supabase mode) — error ছাড়াই skip
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


# ---------- File backup to central DB (upload/edit-এর সাথে সাথে) ----------
MAX_DB_BACKUP_BYTES = 5 * 1024 * 1024


def _backup_file_to_central(username, file_path, rel_path="", domain=None):
    """Upload/create/edit-এর সাথে সাথে file-এর copy central DB-তে (background)।
    Server delete/expire হলেও admin panel থেকে file ফেরত পাওয়া যাবে।
    domain request-এর সময় capture করা হয় — background thread-এ cache-এর উপর নির্ভর করা হয় না।"""
    try:
        if domain:
            _LAST_PANEL_DOMAIN["domain"] = domain
        rel = os.path.normpath(rel_path or "")
        if rel == "lib_env" or rel.startswith("lib_env" + os.sep):
            return  # installed packages — requirements.txt দিয়ে reinstall হয়
        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            return
        size = os.path.getsize(file_path)
        if size > MAX_DB_BACKUP_BYTES:
            print(f"⚠️ DB backup skipped (too large {size} bytes): {file_path}")
            return
        with open(file_path, 'rb') as f:
            content_b64 = base64.b64encode(f.read()).decode()
        file_path_rel = os.path.join(rel, os.path.basename(file_path)) if rel and rel != "." else os.path.basename(file_path)
        file_path_rel = file_path_rel.replace("\\", "/")
        resp = central_request("POST", "/api/central/files",
                               json_data={"username": username, "file_path": file_path_rel,
                                          "filename": os.path.basename(file_path),
                                          "size_bytes": size, "content_b64": content_b64})
        if resp is None or resp.status_code not in (200, 201, 204):
            print(f"⚠️ DB backup failed for {file_path_rel}")
    except Exception as e:
        print(f"⚠️ DB backup error: {e}")


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
        # --- [NEW] DB backup: upload-এর সাথে সাথে central DB-তে copy ---
        try:
            threading.Thread(target=_backup_file_to_central, args=(user, file_path, rel_path, get_panel_domain()), daemon=True).start()
        except Exception as e:
            print(f"DB backup thread error: {e}")

        # --- [NEW] requirements.txt upload দিলেই auto-install ---
        # Terminal-এ আলাদা করে pip install -r লিখতে হবে না — upload-এর সাথে সাথে
        # line-by-line install background-এ চলবে, progress terminal log-এ দেখা যাবে।
        if file.filename.strip().lower() in ('requirements.txt', 'requirement.txt', 'requerment.txt'):
            try:
                log_key = f"{user}_terminal"
                console_logs.setdefault(log_key, "")
                console_logs[log_key] += '\n<span style="color: #00d4ff;">📦 requirements.txt uploaded — auto-installing...</span>\n'
                target_lib = os.path.join(user_dir, 'lib_env')
                if not os.path.exists(target_lib):
                    os.makedirs(target_lib)
                threading.Thread(target=run_requirements_file, args=(file_path, log_key, target_lib), daemon=True).start()
            except Exception as e:
                print(f"Auto-install trigger error: {e}")

        return jsonify({"status": "success", "msg": f"'{file.filename}' !Sᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ 🚀"})




# --- নির্দিষ্ট ইউজার ডিলিট করার রুট ---
@app.route('/remove_user/<username>')
def remove_user(username):
    # Central admin login ছাড়া চলবে না (আগের query-key auth বন্ধ)।
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Unauthorized! Admin login required. 😡"}), 403

    users = load_users()
    
    if username in users:
        # ১. ডাটাবেস (json) থেকে রিমুভ
        del users[username]
        save_users(users)
        sb_delete_user(username)  # Central row-ও মুছে ফেলা (না হলে user ফিরে আসবে)
        
        # ২. ইউজারের ফোল্ডার ডিলিট করা
        user_path = os.path.join(USERS_ROOT, username)
        if os.path.exists(user_path):
            shutil.rmtree(user_path)
            
        return jsonify({"status": "success", "msg": f"User '{username}' and their files removed! 🗑️"})
    
    return jsonify({"status": "error", "msg": "User not found!"})


# --- সব ইউজার ডিলিট করার রুট ---
@app.route('/remove_all')
def remove_all_users():
    # Central admin login ছাড়া চলবে না (আগের query-key auth বন্ধ)।
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Unauthorized Access! 🚫"}), 403

    try:
        # ১. ডাটাবেস রিসেট করা
        save_users({})
        sb_delete_all_users()  # Central-এর সব row-ও মুছে ফেলা
        
        # ২. ইউজার্স রুট ফোল্ডার ডিলিট করে আবার নতুন করে তৈরি করা
        if os.path.exists(USERS_ROOT):
            shutil.rmtree(USERS_ROOT)
        os.makedirs(USERS_ROOT)
        
        return jsonify({"status": "success", "msg": "All users and data have been wiped! 🧹"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})







from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for




# 🚀 Security & Database — admin login এখন Central username+password দিয়ে (ADMIN_SECRET_KEY আর লাগে না)
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
            <input type="text" id="adminUser" name="username" placeholder="ADMIN USERNAME" required
                class="w-full bg-[#080a0f] border border-gray-800 rounded-2xl p-4 text-center text-sm outline-none text-white focus:border-blue-500 transition-all mb-3 tracking-widest shadow-inner">
            <input type="password" id="adminPass" name="password" placeholder="PASSWORD" required
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
        <a href="/admin" class="absolute top-6 left-6 text-cyan-300 hover:text-cyan-100 transition-all text-xs font-bold">Admin Panel</a>

        <div class="flex flex-col items-center mb-6 pb-6 border-b border-white/5">
            <div class="w-14 h-14 bg-gradient-to-tr from-blue-600 to-cyan-400 rounded-full flex items-center justify-center shadow-lg mb-3">
                <i class="fas fa-user-astronaut text-white text-xl"></i>
            </div>
            <div class="text-center">
                <p class="text-[9px] text-blue-500 uppercase tracking-[3px] font-bold">Authenticated Admin</p>
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
    # Central admin login: username + password -> central verify -> signed token in session।
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    if not username or not password:
        return redirect(url_for('admin_panel', error=1))
    resp = central_request("POST", "/api/central/admin/login",
                           json_data={"username": username, "password": password})
    if resp is not None and resp.status_code == 200:
        d = resp.json()
        if isinstance(d, dict) and d.get("status") == "success" and d.get("token"):
            session['admin_logged_in'] = True
            session['admin_token'] = d["token"]
            session['admin_user'] = username
            # Login success -> একটাই admin dashboard (/admin)
            return redirect(url_for('admin_panel'))

    return redirect(url_for('admin_panel', error=1))


@app.route('/admin_logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_token', None)
    session.pop('admin_user', None)
    return redirect(url_for('admin_panel'))

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
        if session.get('admin_logged_in'):
            # পুরনো key-ওয়ালা broadcast page বাদ — dashboard-এর Broadcast section-এ যাবে
            return redirect(url_for('admin_panel'))
        return render_template_string(ADMIN_HTML, logged_in=False)
    
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
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Unauthorized! Admin login required. 😡"}), 403

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
    # Central admin login ছাড়া চলবে না (আগের query-key auth বন্ধ)।
    if not session.get('admin_logged_in'):
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
            "action": "Admin session cleared."
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
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return  # GitHub sync off (Supabase mode) — error ছাড়াই skip
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
    old_username = current_user  # rename হলে পুরনো Supabase row মোছার জন্য
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
            # Supabase: নাম বদলে থাকলে পুরনো row মুছে ফেলা (না হলে পুরনো নাম ফিরে আসবে)
            if old_username != current_user:
                sb_delete_user(old_username)
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

    env = _safe_env()
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
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=_safe_env())
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

    if not OPENROUTER_API_KEY:
        return jsonify({"status": "error", "msg": "AI is not enabled on this panel."}), 503

    # Light rate limit: 30 messages/hour per user
    _ai_rate = _AI_RATE.setdefault(username, [])
    now_ts = time.time()
    _AI_RATE[username] = [t for t in _ai_rate if now_ts - t < 3600]
    if len(_AI_RATE[username]) >= 30:
        return jsonify({"status": "error", "msg": "AI rate limit reached (30/hour). Try again later."}), 429
    _AI_RATE[username].append(now_ts)

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
            yield f"data: {json.dumps({'token': f'📦 [System] `{missing_module}` detected. Installing...' + chr(10)})}\n\n"
            try:
                process = subprocess.Popen(
                    ["pip", "install", missing_module, "--no-cache-dir", "--target", target_lib_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=_safe_env()
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
                    yield f"data: {json.dumps({'token': f'{prefix}{line_clean}' + chr(10)})}\n\n"
                process.wait()
                if warning_count:
                    yield f"data: {json.dumps({'token': f'⚠️ {warning_count} warnings encountered' + chr(10)})}\n\n"
                if process.returncode == 0:
                    yield f"data: {json.dumps({'token': f'✔️ `{missing_module}` installed successfully!' + chr(10)})}\n\n"
                    return
                else:
                    yield f"data: {json.dumps({'token': f'❌ `{missing_module}` installation failed' + chr(10)})}\n\n"
                    return
            except Exception as pip_err:
                yield f"data: {json.dumps({'token': f'❌ Pip error: {str(pip_err)}' + chr(10)})}\n\n"
                return

        # Run code directly if run mode
        if intent_mode == "run":
            yield f"data: {json.dumps({'token': f'🚀 Running `{filename}`...' + chr(10)})}\n\n"
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
                    yield f"data: {json.dumps({'token': f'💾 File `{filename}` saved successfully.' + chr(10)})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'error': f'File save failed: {str(e)}'})}\n\n"

            # Run the fixed file live
            yield f"data: {json.dumps({'token': f'🚀 Running `{filename}`...' + chr(10)})}\n\n"
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
                env = _safe_env()
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

# --- [ADMIN PANEL] User list / add / edit / ban / delete / extend ---
# Admin login (Central username+password) দিয়ে ঢুকতে হয়। URL: /admin — যেকোনো panel-এর /admin থেকে সব panel manage করা যাবে।
ADMIN_PANEL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Panel | Shadow HOST</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; background: #070a12; color: #e8ecf8; font-family: system-ui, sans-serif; }
header { padding: 18px 16px; background: #0d1220; border-bottom: 1px solid #1e2a44; display: flex; justify-content: space-between; align-items: center; }
header h1 { margin: 0; font-size: 20px; letter-spacing: 2px; }
header h1 span { color: #3b82f6; }
header a { color: #f87171; text-decoration: none; font-weight: 700; }
.card { background: #0d1220; border: 1px solid #1e2a44; border-radius: 14px; padding: 16px; margin: 14px; }
.layout { display: flex; align-items: flex-start; }
.sidebar { position: sticky; top: 0; width: 200px; min-width: 200px; padding: 14px; display: flex; flex-direction: column; gap: 8px; }
.menu-btn { background: #121a2e; color: #e8ecf8; border: 1px solid #1e2a44; text-align: left; }
.menu-btn.active { background: #2563eb; border-color: #2563eb; }
main { flex: 1; min-width: 0; }
main .card { margin-left: 0; }
#keybox { display: none; background: #121a2e; border: 1px dashed #3b82f6; border-radius: 10px; padding: 12px; margin-top: 10px; word-break: break-all; }
@media (max-width: 760px) { .layout { flex-direction: column; } .sidebar { position: static; width: 100%; min-width: 0; flex-direction: row; overflow-x: auto; } .menu-btn { white-space: nowrap; } }
.stats { display: flex; gap: 10px; flex-wrap: wrap; }
.stat { flex: 1; min-width: 100px; background: #121a2e; border-radius: 10px; padding: 12px; text-align: center; }
.stat b { display: block; font-size: 22px; }
input, select { width: 100%; padding: 11px; margin: 6px 0; border-radius: 10px; border: 1px solid #26324f; background: #080b14; color: #fff; }
button { padding: 11px 16px; border: 0; border-radius: 10px; font-weight: 700; cursor: pointer; }
.btn-add { background: #2563eb; color: #fff; width: 100%; }
.btn-edit { background: #f59e0b; color: #000; }
.btn-ban { background: #ef4444; color: #fff; }
.btn-unban { background: #22c55e; color: #000; }
.btn-del { background: #7f1d1d; color: #fff; }
.btn-ext { background: #0ea5e9; color: #fff; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 10px 8px; border-bottom: 1px solid #1e2a44; text-align: left; font-size: 13px; }
.badge { padding: 3px 10px; border-radius: 20px; font-size: 11px; }
.badge.active { background: #14532d; color: #4ade80; }
.badge.suspended { background: #7f1d1d; color: #fca5a5; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; }
.actions button { padding: 7px 10px; font-size: 12px; }
#modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7); align-items: center; justify-content: center; padding: 16px; }
#modal.open { display: flex; }
.modal-box { background: #0d1220; border: 1px solid #1e2a44; border-radius: 14px; padding: 18px; width: 100%; max-width: 420px; }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: #14532d; padding: 12px 20px; border-radius: 10px; display: none; z-index: 99; }
#toast.err { background: #7f1d1d; }
@media (max-width: 640px) { table, thead, tbody, th, td, tr { display: block; } thead { display: none; } tr { border: 1px solid #1e2a44; border-radius: 10px; margin-bottom: 10px; padding: 8px; } td { border: 0; } }
</style>
</head>
<body>
<header><h1>Shadow <span>HOST</span> — Admin Panel</h1><a href="/admin_logout">Logout</a></header>
<div class="layout">
<nav class="sidebar">
  <button class="menu-btn active" id="menu-users" onclick="showSection('users')">👥 Users</button>
  <button class="menu-btn" id="menu-panels" onclick="showSection('panels')">🖥️ Panels</button>
  <button class="menu-btn" id="menu-broadcast" onclick="showSection('broadcast')">📣 Broadcast</button>
  <button class="menu-btn" id="menu-backups" onclick="showSection('backups')">💾 Backups</button>
</nav>
<main>
<section id="sec-users">
<div class="card">
  <h3 style="margin-top:0">Panel</h3>
  <select id="panelSelect" onchange="switchPanel()"></select>
</div>
<div class="card stats">
  <div class="stat"><b id="totalUsers">0</b>Total Users</div>
  <div class="stat"><b id="activeUsers">0</b>Active</div>
  <div class="stat"><b id="suspUsers">0</b>Suspended</div>
</div>
<div class="card">
  <h3 style="margin-top:0">Add User</h3>
  <input id="f_user" placeholder="Username">
  <input id="f_pass" placeholder="Password" type="password">
  <input id="f_disk" placeholder="Disk (MB)" type="number" value="500">
  <input id="f_mem" placeholder="Memory (e.g. 512MB)" value="512MB">
  <input id="f_days" placeholder="Days" type="number" value="30">
  <select id="newPanel" title="Which panel this new user belongs to"></select>
  <button class="btn-add" onclick="addUser()">Create User</button>
</div>
<div class="card">
  <h3 style="margin-top:0">User List</h3>
  <input id="search" placeholder="Search username..." oninput="render()">
  <div style="overflow-x:auto"><table><thead><tr><th>Username</th><th>Panel</th><th>Status</th><th>Disk</th><th>Memory</th><th>Expiry</th><th>Actions</th></tr></thead><tbody id="tbody"></tbody></table></div>
</div>
</section>
<section id="sec-panels" style="display:none">
<div class="card">
  <h3 style="margin-top:0">New Panel (Pre-register)</h3>
  <input id="np_domain" placeholder="Panel domain (e.g. customer1.de1.netrun.io)">
  <input id="np_days" placeholder="Validity days (0 = no expiry)" type="number" value="30">
  <button class="btn-add" onclick="createPanel()">Pre-register Panel</button>
  <div id="keybox"></div>
</div>
<div class="card">
  <h3 style="margin-top:0">Panels</h3>
  <div style="overflow-x:auto"><table><thead><tr><th>Domain</th><th>Status</th><th>Expiry</th><th>Users</th><th>Created</th><th>Actions</th></tr></thead><tbody id="panelsTbody"></tbody></table></div>
</div>
</section>
<section id="sec-broadcast" style="display:none">
<div class="card">
  <h3 style="margin-top:0">Send Broadcast</h3>
  <input id="bc_msg" placeholder="Message to all online admins...">
  <button class="btn-add" onclick="sendBroadcast()">Send Broadcast</button>
</div>
</section>
<section id="sec-backups" style="display:none">
<div class="card">
  <h3 style="margin-top:0">File Backups (DB)</h3>
  <p style="font-size:12px;color:#94a3b8">Upload/edit-এর সাথে সাথে file-এর copy central DB-তে যায়। Server delete/expire হলেও এখান থেকে Download করে user-কে ফিরিয়ে দিতে পারবে। Panel থেকে delete করা file-এর backup-ও এখানেই থাকে।</p>
  <select id="bk_panel"></select>
  <input id="bk_user" placeholder="Username filter (খালি = সব)">
  <button class="btn-add" onclick="loadBackups()">Load Backups</button>
</div>
<div class="card">
  <h3 style="margin-top:0">Backed-up Files</h3>
  <div style="overflow-x:auto"><table><thead><tr><th>User</th><th>Server</th><th>File</th><th>Size</th><th>Updated</th><th>Actions</th></tr></thead><tbody id="backupsTbody"></tbody></table></div>
</div>
</section>
</main>
</div>
<div id="modal"><div class="modal-box">
  <h3 style="margin-top:0">Edit <span id="editName"></span></h3>
  <input id="e_pass" placeholder="New password (blank = keep)" type="password">
  <input id="e_disk" placeholder="Disk (MB)" type="number">
  <input id="e_mem" placeholder="Memory (e.g. 512MB)">
  <select id="e_status"><option value="active">active</option><option value="suspended">suspended</option></select>
  <input id="e_expiry" placeholder="Expiry (DD-MM-YYYY HH:MM:SS)">
  <button class="btn-add" onclick="saveEdit()">Save Changes</button>
  <button onclick="closeModal()" style="width:100%;margin-top:8px;background:#26324f;color:#fff">Cancel</button>
</div></div>
<div id="toast"></div>
<script>
let usersList = [];
let currentPanel = 'all';
// Silent failure বন্ধ করা: কোনো fetch/JS error হলে screen-এ toast দেখাবে (আগে চুপচাপ কিছুই হতো না)।
window.addEventListener('error', e => { try { toast('JS Error: ' + (e.message || 'unknown'), true); } catch (_) {} });
window.addEventListener('unhandledrejection', e => { try { toast('Error: ' + ((e.reason && e.reason.message) || e.reason || 'request failed'), true); } catch (_) {} });
function apiFetch(path, opts, panel) {
  const sep = path.includes('?') ? '&' : '?';
  return fetch(path + sep + 'panel=' + encodeURIComponent(panel || currentPanel), opts);
}
async function loadPanels() {
  const sel = document.getElementById('panelSelect');
  const addSel = document.getElementById('newPanel');
  const bkSel = document.getElementById('bk_panel');
  try {
    const r = await fetch('/api/admin/panels');
    const d = await r.json();
    if (d.status === 'success' && d.panels) {
      const total = d.panels.reduce((s, p) => s + (p.users ?? 0), 0);
      sel.innerHTML = `<option value="all" selected>All panels (${total} users)</option>` + d.panels.map(p => `<option value="${p.domain}">${p.domain} (${p.users ?? 0} users)</option>`).join('');
      if (addSel) addSel.innerHTML = d.panels.map(p => `<option value="${p.domain}">${p.domain}</option>`).join('') || '<option value="">No panel</option>';
      if (bkSel) bkSel.innerHTML = `<option value="">All servers</option>` + d.panels.map(p => `<option value="${p.domain}">${p.domain}</option>`).join('');
      return;
    }
    toast(d.msg || 'Failed to load panels', true);
  } catch (e) { toast('Panels load failed: ' + (e.message || e), true); }
  sel.innerHTML = `<option value="all" selected>All panels</option>`;
  if (addSel) addSel.innerHTML = '<option value="">No panel</option>';
  if (bkSel) bkSel.innerHTML = '<option value="">All servers</option>';
}
function switchPanel() {
  currentPanel = document.getElementById('panelSelect').value;
  if (currentPanel !== 'all') {
    const addSel = document.getElementById('newPanel');
    if (addSel) addSel.value = currentPanel;
  }
  load();
}
function showSection(name) {
  ['users', 'panels', 'broadcast', 'backups'].forEach(n => {
    document.getElementById('sec-' + n).style.display = (n === name) ? 'block' : 'none';
    document.getElementById('menu-' + n).classList.toggle('active', n === name);
  });
  if (name === 'panels') loadPanelsList();
  if (name === 'backups') loadBackups();
}
async function loadPanelsList() {
  let d;
  try {
    const r = await fetch('/api/admin/panels');
    d = await r.json();
  } catch (e) { toast('Panels load failed: ' + (e.message || e), true); return; }
  const tb = document.getElementById('panelsTbody');
  if (d.status !== 'success') { toast(d.msg || 'Failed to load panels', true); return; }
  let html = '';
  (d.panels || []).forEach(p => {
    let badge;
    if (p.expired) badge = '<span class="badge suspended">expired</span>';
    else if (p.status === 'pending') badge = '<span class="badge" style="background:#422006;color:#facc15">pending</span>';
    else if (p.status === 'suspended') badge = '<span class="badge suspended">suspended</span>';
    else badge = '<span class="badge active">active</span>';
    const approveBtn = p.status === 'pending' ? `<button class="btn-unban" onclick="setPanelStatus('${p.domain}','active')">Approve</button>` : '';
    const toggle = p.status === 'suspended'
      ? `<button class="btn-unban" onclick="setPanelStatus('${p.domain}','active')">Activate</button>`
      : `<button class="btn-ban" onclick="setPanelStatus('${p.domain}','suspended')">Suspend</button>`;
    html += `<tr><td><b>${p.domain}</b></td><td>${badge}</td><td>${p.expiry_date ?? '—'}</td><td>${p.users ?? 0}</td><td>${p.created_at ?? ''}</td><td><div class="actions">${approveBtn}<button class="btn-ext" onclick="extendPanel('${p.domain}')">+30 Days</button>${toggle}<button class="btn-edit" onclick="reissuePanel('${p.domain}')">Re-issue Key</button><button class="btn-del" onclick="delPanel('${p.domain}')">Delete</button></div></td></tr>`;
  });
  tb.innerHTML = html || '<tr><td colspan="6">No panels yet.</td></tr>';
}
async function createPanel() {
  const domain = document.getElementById('np_domain').value.trim().toLowerCase();
  const days = document.getElementById('np_days').value || 30;
  if (!domain) { toast('Domain required', true); return; }
  const r = await fetch('/api/admin/panels', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ domain, days }) });
  const d = await r.json();
  if (d.status !== 'success') { toast(d.msg || 'Failed', true); return; }
  toast(d.msg || 'Panel pre-registered');
  document.getElementById('np_domain').value = '';
  loadPanelsList(); loadPanels();
}
async function extendPanel(domain) {
  const r = await fetch('/api/admin/panels/extend', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ domain, days: 30 }) });
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') { loadPanelsList(); loadPanels(); }
}
async function reissuePanel(domain) {
  if (!confirm('Re-issue key for ' + domain + '? The panel\'s old key will stop working.')) return;
  const r = await fetch('/api/admin/panels/reissue', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ domain }) });
  const d = await r.json();
  if (d.status !== 'success') { toast(d.msg || 'Failed', true); return; }
  const kb = document.getElementById('keybox');
  kb.style.display = 'block';
  kb.innerHTML = '<b>Panel:</b> ' + d.domain + '<br><b>NEW PANEL_KEY (copy now — shown only once):</b><br>' + d.panel_key + '<br>Set it as PANEL_KEY in the panel project.';
  loadPanelsList(); loadPanels();
}
async function setPanelStatus(domain, status) {
  const r = await fetch('/api/admin/panels/status', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ domain, status }) });
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') { loadPanelsList(); loadPanels(); }
}
async function delPanel(domain) {
  if (!confirm('Delete panel ' + domain + ' and ALL its users?')) return;
  const r = await fetch('/api/admin/panels?domain=' + encodeURIComponent(domain), { method: 'DELETE' });
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') { loadPanelsList(); loadPanels(); }
}
async function sendBroadcast() {
  const msg = document.getElementById('bc_msg').value.trim();
  if (!msg) { toast('Message required', true); return; }
  const r = await fetch('/sendnotification?msg=' + encodeURIComponent(msg));
  const d = await r.json();
  toast(d.msg || d.message || 'Sent', d.status !== 'success');
  if (d.status === 'success') document.getElementById('bc_msg').value = '';
}
async function loadBackups() {
  const panel = document.getElementById('bk_panel').value || '';
  const user = document.getElementById('bk_user').value.trim();
  let d;
  try {
    const r = await fetch('/api/admin/backups?panel=' + encodeURIComponent(panel) + '&user=' + encodeURIComponent(user));
    d = await r.json();
  } catch (e) { toast('Backups load failed: ' + (e.message || e), true); return; }
  const tb = document.getElementById('backupsTbody');
  if (d.status !== 'success') { toast(d.msg || 'Failed to load backups', true); return; }
  let html = '';
  (d.backups || d.files || []).forEach(b => {
    const size = b.size_bytes ? (b.size_bytes > 1048576 ? (b.size_bytes / 1048576).toFixed(2) + ' MB' : (b.size_bytes / 1024).toFixed(1) + ' KB') : '—';
    html += `<tr><td><b>${b.username}</b></td><td>${b.panel_domain}</td><td>${b.file_path}</td><td>${size}</td><td>${b.updated_at ?? ''}</td><td><div class="actions"><button class="btn-add" onclick="downloadBackup('${b.panel_domain}','${b.username}','${b.file_path}')">Download</button><button class="btn-ext" onclick="restoreBackup('${b.panel_domain}','${b.username}','${b.file_path}')">Restore to this panel</button><button class="btn-del" onclick="deleteBackup('${b.panel_domain}','${b.username}','${b.file_path}')">Delete</button></div></td></tr>`;
  });
  tb.innerHTML = html || '<tr><td colspan="6">No backups yet.</td></tr>';
}
async function downloadBackup(panel, user, file) {
  const r = await fetch('/api/admin/backups/download?panel=' + encodeURIComponent(panel) + '&user=' + encodeURIComponent(user) + '&file=' + encodeURIComponent(file));
  const d = await r.json();
  if (d.status !== 'success') { toast(d.msg || 'Download failed', true); return; }
  const bin = atob(d.content_b64 || '');
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const blob = new Blob([bytes], { type: 'application/octet-stream' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (d.filename || file.split('/').pop());
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
async function restoreBackup(panel, user, file) {
  if (!confirm('Restore ' + file + ' to THIS panel for user ' + user + '?')) return;
  const r = await fetch('/api/admin/backups/restore', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ panel, user, file }) });
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
}
async function deleteBackup(panel, user, file) {
  if (!confirm('Delete backup ' + file + ' (' + user + ')? The panel file is NOT affected.')) return;
  const r = await fetch('/api/admin/backups?panel=' + encodeURIComponent(panel) + '&user=' + encodeURIComponent(user) + '&file=' + encodeURIComponent(file), { method: 'DELETE' });
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') loadBackups();
}
async function load() {
  let d;
  try {
    const r = await apiFetch('/api/admin/users');
    d = await r.json();
  } catch (e) { toast('Users load failed: ' + (e.message || e), true); return; }
  if (d.status !== 'success') { toast(d.msg || 'Failed to load', true); return; }
  if (Array.isArray(d.users)) {
    usersList = d.users;
  } else {
    usersList = Object.entries(d.users || {}).map(([name, u]) => ({ username: name, panel_domain: currentPanel, ...u }));
  }
  render();
}
function render() {
  const q = (document.getElementById('search').value || '').toLowerCase();
  const tb = document.getElementById('tbody');
  let total = 0, act = 0, susp = 0;
  let html = '';
  usersList.slice().sort((a, b) => ((a.panel_domain || '') + a.username).localeCompare((b.panel_domain || '') + b.username)).forEach(u => {
    const name = u.username;
    const panel = u.panel_domain || currentPanel;
    total++;
    if (u.status === 'suspended') susp++; else act++;
    if (q && !name.toLowerCase().includes(q) && !(panel || '').toLowerCase().includes(q)) return;
    const badge = u.status === 'suspended' ? '<span class="badge suspended">suspended</span>' : '<span class="badge active">active</span>';
    const banBtn = u.status === 'suspended'
      ? `<button class="btn-unban" onclick="setStatus('${panel}','${name}','active')">Unban</button>`
      : `<button class="btn-ban" onclick="setStatus('${panel}','${name}','suspended')">Ban</button>`;
    html += `<tr><td><b>${name}</b></td><td>${panel}</td><td>${badge}</td><td>${u.disk ?? ''} MB</td><td>${u.memory ?? ''}</td><td>${u.expiry_date ?? ''}</td><td><div class="actions"><button class="btn-edit" onclick="openEdit('${panel}','${name}')">Edit</button><button class="btn-ext" onclick="extend('${panel}','${name}')">+30 Days</button>${banBtn}<button class="btn-del" onclick="delUser('${panel}','${name}')">Delete</button></div></td></tr>`;
  });
  tb.innerHTML = html || '<tr><td colspan="7">No users yet.</td></tr>';
  document.getElementById('totalUsers').textContent = total;
  document.getElementById('activeUsers').textContent = act;
  document.getElementById('suspUsers').textContent = susp;
}
let editing = null;
function toast(msg, isErr) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = isErr ? 'err' : '';
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}
async function addUser() {
  const targetPanel = currentPanel === 'all' ? document.getElementById('newPanel').value : currentPanel;
  if (!targetPanel) { toast('Select a panel first', true); return; }
  const body = { username: document.getElementById('f_user').value.trim(),
    password: document.getElementById('f_pass').value,
    disk: document.getElementById('f_disk').value,
    memory: document.getElementById('f_mem').value,
    days: document.getElementById('f_days').value };
  if (!body.username || !body.password) { toast('Username + password required', true); return; }
  let d;
  try {
    const r = await apiFetch('/api/admin/users', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) }, targetPanel);
    d = await r.json();
  } catch (e) { toast('Create failed: ' + (e.message || e), true); return; }
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') { document.getElementById('f_user').value=''; document.getElementById('f_pass').value=''; load(); }
}
function openEdit(panel, name) {
  editing = { panel, name };
  const u = usersList.find(x => x.username === name && (x.panel_domain || currentPanel) === panel) || {};
  document.getElementById('editName').textContent = name + ' (' + panel + ')';
  document.getElementById('e_pass').value = '';
  document.getElementById('e_disk').value = u.disk ?? 500;
  document.getElementById('e_mem').value = u.memory ?? '512MB';
  document.getElementById('e_status').value = u.status ?? 'active';
  document.getElementById('e_expiry').value = u.expiry_date ?? '';
  document.getElementById('modal').classList.add('open');
}
function closeModal() { document.getElementById('modal').classList.remove('open'); }
async function saveEdit() {
  const body = { password: document.getElementById('e_pass').value,
    disk: document.getElementById('e_disk').value,
    memory: document.getElementById('e_mem').value,
    status: document.getElementById('e_status').value,
    expiry_date: document.getElementById('e_expiry').value };
  const r = await apiFetch('/api/admin/users/' + encodeURIComponent(editing.name), { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) }, editing.panel);
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') { closeModal(); load(); }
}
async function setStatus(panel, name, status) {
  const r = await apiFetch('/api/admin/users/' + encodeURIComponent(name), { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ status }) }, panel);
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') load();
}
async function extend(panel, name) {
  const r = await apiFetch('/api/admin/users/' + encodeURIComponent(name) + '/extend', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ days: 30 }) }, panel);
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') load();
}
async function delUser(panel, name) {
  if (!confirm('Delete user ' + name + ' (' + panel + ') and all their files?')) return;
  const r = await apiFetch('/api/admin/users/' + encodeURIComponent(name), { method: 'DELETE' }, panel);
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') load();
}
// Init: users + panels দুটোই load করতে হবে — loadPanels() না চললে Panel dropdown + Add User-এর
// panel selector খালি থাকে, আর Create User "Select a panel first"-এ আটকে যায়।
loadPanels();
load();
</script>
</body>
</html>
"""

@app.route('/admin')
def admin_panel():
    # একটাই admin entry: login না থাকলে login form, থাকলে dashboard (side menu-তে সব)।
    if not session.get('admin_logged_in'):
        return render_template_string(ADMIN_HTML, logged_in=False)
    return Response(ADMIN_PANEL_HTML, mimetype='text/html')

def _admin_target_panel():
    # Admin কোন panel manage করছে — ?panel= param থেকে, না থাকলে এই panel-এর domain।
    target = request.args.get('panel')
    if not target and request.method in ('POST', 'PUT', 'PATCH'):
        j = request.get_json(silent=True) or {}
        target = j.get('panel')
    if target:
        g.admin_panel_override = str(target).strip().lower()

@app.route('/api/admin/panels', methods=['GET', 'POST', 'DELETE'])
def api_admin_panels():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    if request.method == 'GET':
        resp = central_request("GET", "/api/central/admin/panels")
    elif request.method == 'POST':
        data = request.get_json() or {}
        resp = central_request("POST", "/api/central/admin/panels",
                               json_data={"domain": (data.get('domain') or '').strip().lower(),
                                          "days": data.get('days', 30)})
    else:  # DELETE
        resp = central_request("DELETE", "/api/central/admin/panels",
                               params={"domain": request.args.get('domain', '')})
    if resp is None:
        return jsonify({"status": "error", "msg": "Central unreachable"}), 502
    return jsonify(resp.json()), resp.status_code

@app.route('/api/admin/panels/status', methods=['POST'])
def api_admin_panels_status():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    data = request.get_json() or {}
    resp = central_request("POST", "/api/central/admin/panels/status",
                           json_data={"domain": (data.get('domain') or '').strip().lower(),
                                      "status": data.get('status')})
    if resp is None:
        return jsonify({"status": "error", "msg": "Central unreachable"}), 502
    return jsonify(resp.json()), resp.status_code

@app.route('/api/admin/backups', methods=['GET', 'DELETE'])
def api_admin_backups():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    if request.method == 'GET':
        resp = central_request("GET", "/api/central/admin/files",
                               params={"panel": request.args.get('panel') or '', "user": request.args.get('user') or ''})
    else:
        resp = central_request("DELETE", "/api/central/admin/files",
                               params={"panel": request.args.get('panel') or '', "user": request.args.get('user') or '',
                                       "file": request.args.get('file') or ''})
    if resp is None:
        return jsonify({"status": "error", "msg": "Central unreachable"}), 502
    return jsonify(resp.json()), resp.status_code


@app.route('/api/admin/backups/download', methods=['GET'])
def api_admin_backups_download():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    resp = central_request("GET", "/api/central/admin/files/download",
                           params={"panel": request.args.get('panel') or '', "user": request.args.get('user') or '',
                                   "file": request.args.get('file') or ''})
    if resp is None:
        return jsonify({"status": "error", "msg": "Central unreachable"}), 502
    return jsonify(resp.json()), resp.status_code


@app.route('/api/admin/backups/restore', methods=['POST'])
def api_admin_backups_restore():
    """Backup থেকে file এই panel-এর user folder-এ ফিরিয়ে আনা (user-এর নতুন server-এ)।"""
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    data = request.get_json() or {}
    panel = (data.get('panel') or '').strip().lower()
    username = (data.get('user') or '').strip()
    file_path = (data.get('file') or '').strip().replace("\\", "/")
    if not panel or not username or not file_path:
        return jsonify({"status": "error", "msg": "panel + user + file required"}), 400
    if file_path.startswith("/") or ".." in file_path.split("/"):
        return jsonify({"status": "error", "msg": "Invalid file path"}), 400
    resp = central_request("GET", "/api/central/admin/files/download",
                           params={"panel": panel, "user": username, "file": file_path})
    if resp is None:
        return jsonify({"status": "error", "msg": "Central unreachable"}), 502
    d = resp.json()
    if d.get("status") != "success":
        return jsonify(d), resp.status_code
    try:
        content = base64.b64decode(d.get("content_b64") or "")
    except Exception:
        return jsonify({"status": "error", "msg": "Corrupt backup content"}), 502
    user_dir = os.path.join(USERS_ROOT, username)
    target = os.path.abspath(os.path.join(user_dir, file_path))
    if not target.startswith(os.path.abspath(user_dir) + os.sep):
        return jsonify({"status": "error", "msg": "Invalid file path"}), 400
    os.makedirs(os.path.dirname(target) or user_dir, exist_ok=True)
    with open(target, 'wb') as f:
        f.write(content)
    return jsonify({"status": "success", "msg": f"Restored {file_path} to this panel for user '{username}'."})


@app.route('/api/admin/panels/extend', methods=['POST'])
def api_admin_panels_extend():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    data = request.get_json() or {}
    resp = central_request("POST", "/api/central/admin/panels/extend",
                           json_data={"domain": (data.get('domain') or '').strip().lower(),
                                      "days": data.get('days', 30)})
    if resp is None:
        return jsonify({"status": "error", "msg": "Central unreachable"}), 502
    return jsonify(resp.json()), resp.status_code

@app.route('/api/admin/panels/reissue', methods=['POST'])
def api_admin_panels_reissue():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    data = request.get_json() or {}
    resp = central_request("POST", "/api/central/admin/panels/reissue",
                           json_data={"domain": (data.get('domain') or '').strip().lower()})
    if resp is None:
        return jsonify({"status": "error", "msg": "Central unreachable"}), 502
    return jsonify(resp.json()), resp.status_code

@app.route('/api/admin/users', methods=['GET', 'POST'])
def api_admin_users():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    _admin_target_panel()
    if request.method == 'GET':
        if request.args.get('panel') == 'all':
            resp = central_request("GET", "/api/central/admin/users")
            if resp is None:
                return jsonify({"status": "error", "msg": "Central unreachable"}), 502
            return jsonify(resp.json()), resp.status_code
        users = load_users()
        return jsonify({"status": "success", "users": users})
    # POST = create user (same rules as /setuser, saved to Central)
    data = request.get_json() or {}
    u = (data.get('username') or '').strip()
    p = (data.get('password') or '').strip()
    if not u or not p:
        return jsonify({"status": "error", "msg": "Username and password are required."}), 400
    users = load_users()
    if u in users:
        return jsonify({"status": "error", "msg": "Username already exists!"}), 400
    kolkata_tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(kolkata_tz)
    try:
        days = int(data.get('days', 30))
    except Exception:
        days = 30
    expiry_time = now + timedelta(days=days)
    expiry_str = expiry_time.strftime('%d-%m-%Y %H:%M:%S')
    final_memory = str(data.get('memory', '512MB')).upper()
    if not any(unit in final_memory for unit in ['KB', 'MB', 'GB']):
        final_memory += 'MB'
    try:
        disk_val = int(data.get('disk', 500))
    except Exception:
        disk_val = 500
    users[u] = {"p": p, "disk": disk_val, "memory": final_memory,
                "status": "active",
                "created_at": now.strftime('%d-%m-%Y %H:%M:%S'),
                "expiry_date": expiry_str}
    save_users(users)
    user_path = os.path.join(USERS_ROOT, u)
    if not os.path.exists(user_path):
        os.makedirs(user_path)
    app_file_path = os.path.join(user_path, 'main.py')
    if not os.path.exists(app_file_path):
        with open(app_file_path, 'w', encoding='utf-8') as f:
            f.write(f"# Shadow Hosting V2.0\n# User: {u}\n# Expiry: {expiry_str}\n\nprint('Hello {u}!')")
    return jsonify({"status": "success", "msg": f"User '{u}' created!"})

@app.route('/api/admin/users/<username>', methods=['POST', 'DELETE'])
def api_admin_user(username):
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    _admin_target_panel()
    users = load_users()
    if username not in users:
        return jsonify({"status": "error", "msg": "User not found!"}), 404
    if request.method == 'DELETE':
        del users[username]
        save_users(users)
        sb_delete_user(username)
        user_path = os.path.join(USERS_ROOT, username)
        if os.path.exists(user_path):
            shutil.rmtree(user_path)
        return jsonify({"status": "success", "msg": f"User '{username}' deleted!"})
    # POST = update user (password blank = keep old)
    data = request.get_json() or {}
    u = users[username]
    if data.get('password'):
        u['p'] = str(data['password'])
    if data.get('disk') is not None and str(data.get('disk')) != '':
        try:
            u['disk'] = int(data['disk'])
        except Exception:
            pass
    if data.get('memory'):
        mem = str(data['memory']).upper()
        if not any(unit in mem for unit in ['KB', 'MB', 'GB']):
            mem += 'MB'
        u['memory'] = mem
    if data.get('status') in ('active', 'suspended'):
        u['status'] = data['status']
    if data.get('expiry_date'):
        u['expiry_date'] = str(data['expiry_date'])
    users[username] = u
    save_users(users)
    return jsonify({"status": "success", "msg": f"User '{username}' updated!"})

@app.route('/api/admin/users/<username>/extend', methods=['POST'])
def api_admin_extend(username):
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    _admin_target_panel()
    users = load_users()
    if username not in users:
        return jsonify({"status": "error", "msg": "User not found!"}), 404
    data = request.get_json() or {}
    try:
        days = int(data.get('days', 30))
    except Exception:
        days = 30
    kolkata_tz = pytz.timezone('Asia/Kolkata')
    try:
        current = datetime.strptime(users[username].get('expiry_date', ''), '%d-%m-%Y %H:%M:%S')
        current = kolkata_tz.localize(current)
    except Exception:
        current = datetime.now(kolkata_tz)
    new_expiry = current + timedelta(days=days)
    users[username]['expiry_date'] = new_expiry.strftime('%d-%m-%Y %H:%M:%S')
    save_users(users)
    return jsonify({"status": "success", "msg": f"Extended by {days} days!",
                    "expiry_date": users[username]['expiry_date']})

# =====================================================================
# CENTRAL SERVER SECTION (একই app.py-এর ভিতরে)
# শুধু CENTRAL_MODE-এ (SUPABASE_KEY env var থাকলে) এই section কাজ করে।
# Panel mode-এ /api/central/* route গুলো 503 দেয় — কোনো secret leak হয় না।
# =====================================================================
import hashlib
import hmac
import secrets as _secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

_central_serializer = URLSafeTimedSerializer(CENTRAL_SECRET, salt="shadow-central-admin")


class _CentralResp:
    """central_dispatch-এর return — requests Response-এর মতো .status_code + .json()।"""
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


# ---------- Supabase REST helpers (শুধু central mode) ----------

def csb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def csb_get(table, params):
    return requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=csb_headers(),
                        params=params, timeout=15)


def csb_post(table, json_data, params=None, prefer=None):
    h = csb_headers()
    if prefer:
        h["Prefer"] = prefer
    return requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=h,
                         params=params, json=json_data, timeout=15)


def csb_patch(table, params, json_data):
    return requests.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=csb_headers(),
                          params=params, json=json_data, timeout=15)


def csb_delete(table, params):
    return requests.delete(f"{SUPABASE_URL}/rest/v1/{table}", headers=csb_headers(),
                           params=params, timeout=15)


def c_user_row_to_dict(row):
    return {"p": row.get("p"), "disk": row.get("disk", 500),
            "memory": row.get("memory", "512MB"), "status": row.get("status", "active"),
            "created_at": row.get("created_at"), "expiry_date": row.get("expiry_date")}


# ---------- Central auth ----------

def c_verify_panel_key(domain, key):
    """PANEL_KEY check — key-টা ওই domain-এর সাথে মিলতে হবে।"""
    if not domain or not key:
        return None
    r = csb_get("panels", {"domain": f"eq.{domain}", "select": "domain,status,panel_key_hash,expiry_date"})
    if r.status_code != 200 or not r.json():
        return None
    row = r.json()[0]
    digest = hashlib.sha256(key.encode()).hexdigest()
    if not hmac.compare_digest(digest, row.get("panel_key_hash", "")):
        return None
    return row


def _panel_expired(panel_row):
    """Panel-এর date শেষ হয়েছে কিনা (expiry_date: DD-MM-YYYY HH:MM:SS, Kolkata)।"""
    if not panel_row:
        return False
    expiry = panel_row.get("expiry_date")
    if not expiry:
        return False
    try:
        kolkata_tz = pytz.timezone('Asia/Kolkata')
        exp = datetime.strptime(str(expiry), '%d-%m-%Y %H:%M:%S')
        exp = kolkata_tz.localize(exp)
        return datetime.now(kolkata_tz) > exp
    except Exception:
        return False


def c_verify_admin_token(token):
    """Admin token (itsdangerous, 24h) -> admin username, না হলে None।"""
    if not token:
        return None
    try:
        return _central_serializer.loads(token, max_age=86400)
    except (BadSignature, SignatureExpired):
        return None


def c_authorize(headers):
    """headers (dict-like) থেকে (domain, is_admin, error)।
    Panel key -> শুধু নিজের domain। Admin token -> যেকোনো domain।"""
    def _get(name):
        try:
            return headers.get(name, "")
        except Exception:
            return ""
    auth = _get("Authorization")
    if auth.startswith("Bearer "):
        admin = c_verify_admin_token(auth[7:])
        if admin:
            domain = (_get("X-Panel-Domain") or "").strip().lower()
            if not domain:
                return None, True, "panel_domain is required"
            return domain, True, None
    key = _get("X-Panel-Key")
    domain = (_get("X-Panel-Domain") or "").strip().lower()
    if not key or not domain:
        return None, False, "Missing panel credentials"
    row = c_verify_panel_key(domain, key)
    if row is None:
        return None, False, "Invalid panel key or domain"
    # Panel-key auth-এ pending / expired panel-এর user data বন্ধ। Admin token-এ এই check নেই (owner manage করতে পারবে)।
    if row.get("status") == "pending":
        return None, False, "Panel pending approval. Contact admin."
    if row.get("status") == "suspended":
        return None, False, "Panel suspended. Contact admin."
    if _panel_expired(row):
        return None, False, "Panel expired. Contact admin to renew."
    return domain, False, None


def c_panel_registered(domain):
    r = csb_get("panels", {"domain": f"eq.{domain}", "select": "domain,status,expiry_date"})
    if r.status_code == 200 and r.json():
        return r.json()[0]
    return None


# ---------- Central core logic (Flask route + in-process dispatcher দুইজনেই ব্যবহার করে) ----------

def c_auth_user(panel_domain, username, password):
    if not panel_domain or not username:
        return {"status": "not_found"}
    panel = c_panel_registered(panel_domain)
    if panel is None:
        return {"status": "error", "msg": "Panel not registered in central server."}
    if panel.get("status") == "pending":
        return {"status": "error", "msg": "Panel pending approval. Contact admin."}
    if panel.get("status") == "suspended":
        return {"status": "error", "msg": "Panel suspended. Contact admin."}
    if _panel_expired(panel):
        return {"status": "error", "msg": "Panel expired. Contact admin to renew."}
    r = csb_get("users", {"panel_domain": f"eq.{panel_domain}", "username": f"ilike.{username}", "select": "*"})
    if r.status_code != 200:
        return {"status": "error", "msg": f"DB error {r.status_code}"}
    rows = r.json()
    if not rows:
        return {"status": "not_found"}
    d = rows[0]
    if str(d.get("p")) == str(password):
        return {"status": "success", "username": d.get("username", username), "data": c_user_row_to_dict(d)}
    return {"status": "wrong_password"}


def c_list_users(domain):
    r = csb_get("users", {"panel_domain": f"eq.{domain}", "select": "*"})
    if r.status_code != 200:
        return False, f"DB error {r.status_code}"
    out = {}
    for row in r.json():
        out[row["username"]] = c_user_row_to_dict(row)
    return True, out


def c_upsert_user(domain, user):
    username = ((user or {}).get("username") or "").strip()
    if not username:
        return False, "username is required"
    row = {"panel_domain": domain, "username": username, "p": user.get("p"),
           "disk": user.get("disk", 500), "memory": user.get("memory", "512MB"),
           "status": user.get("status", "active"),
           "created_at": user.get("created_at"), "expiry_date": user.get("expiry_date")}
    r = csb_post("users", row, params={"on_conflict": "panel_domain,username"},
                 prefer="resolution=merge-duplicates")
    if r.status_code not in (200, 201):
        return False, f"DB error {r.status_code}: {r.text[:200]}"
    return True, f"User '{username}' saved."


def c_delete_user(domain, username):
    username = (username or "").strip()
    if not username:
        return False, "username is required"
    r = csb_delete("users", {"panel_domain": f"eq.{domain}", "username": f"eq.{username}"})
    if r.status_code not in (200, 204):
        return False, f"DB error {r.status_code}"
    return True, f"User '{username}' deleted."


def c_admin_login(username, password):
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return False, "Username and password are required."
    # verify_admin() SQL function: password_hash = crypt(p, password_hash) মিলিয়ে check করে
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/verify_admin", headers=csb_headers(),
                      json={"u": username, "p": password}, timeout=15)
    if r.status_code == 200 and r.json() is True:
        return True, _central_serializer.dumps(username)
    return False, "Wrong username or password."


def c_register_panel(domain, panel_key):
    """Panel auto-enroll (open endpoint — key ছাড়া)। নতুন domain -> pending row।
    Pre-registered (hash খালি) -> key bind। একই key দিয়ে আবার -> ok। অন্য key -> error (hijack রোধ)।"""
    domain = (domain or "").strip().lower()
    panel_key = (panel_key or "").strip()
    if not domain or not panel_key:
        return {"status": "error", "msg": "domain + panel_key required"}
    row = c_panel_registered(domain)
    if row is None:
        digest = hashlib.sha256(panel_key.encode()).hexdigest()
        r = csb_post("panels", {"domain": domain, "panel_key_hash": digest, "status": "pending"})
        if r.status_code not in (200, 201):
            return {"status": "error", "msg": f"DB error {r.status_code}"}
        return {"status": "pending", "msg": "Panel registered. Waiting for owner approval."}
    existing = row.get("panel_key_hash") or ""
    if not existing:
        digest = hashlib.sha256(panel_key.encode()).hexdigest()
        r = csb_patch("panels", {"domain": f"eq.{domain}"}, {"panel_key_hash": digest})
        if r.status_code not in (200, 204):
            return {"status": "error", "msg": f"DB error {r.status_code}"}
        return {"status": row.get("status", "pending"), "msg": "Panel key bound."}
    digest = hashlib.sha256(panel_key.encode()).hexdigest()
    if hmac.compare_digest(digest, existing):
        return {"status": row.get("status", "pending"), "msg": "Already enrolled."}
    return {"status": "error", "msg": "Domain already registered with a different key. Ask owner to re-issue."}


def c_panel_status(domain):
    """Public panel status (sensitive data নেই) — panel-এর expiry hook এটা দিয়ে check করে।"""
    domain = (domain or "").strip().lower()
    row = c_panel_registered(domain)
    if row is None:
        return {"domain": domain, "status": "unknown", "expiry_date": None, "expired": False}
    return {"domain": domain, "status": row.get("status", "active"),
            "expiry_date": row.get("expiry_date"), "expired": _panel_expired(row)}


def c_backup_file(domain, data):
    """Upload/edit-এর সাথে সাথে file-এর copy DB-তে (upsert on panel_domain+username+file_path)।
    domain সবসময় auth (panel key / admin token) থেকে আসে — client-পাঠানো domain বিশ্বাস করা হয় না।"""
    username = (data.get("username") or "").strip()
    file_path = (data.get("file_path") or "").strip().replace("\\", "/")
    filename = (data.get("filename") or "").strip()
    content_b64 = data.get("content_b64") or ""
    try:
        size = int(data.get("size_bytes") or 0)
    except Exception:
        size = 0
    if not username or not file_path or not filename or not content_b64:
        return False, "username + file_path + filename + content_b64 required"
    if file_path.startswith("/") or ".." in file_path.split("/"):
        return False, "Invalid file_path"
    if size > 5 * 1024 * 1024:
        return False, "File too large for DB backup (max 5MB)"
    row = {"panel_domain": domain, "username": username, "filename": filename,
           "file_path": file_path, "size_bytes": size, "content_b64": content_b64,
           "updated_at": datetime.now(timezone.utc).isoformat()}
    r = csb_post("user_files", row, params={"on_conflict": "panel_domain,username,file_path"},
                 prefer="resolution=merge-duplicates")
    if r.status_code not in (200, 201, 204):
        return False, f"DB error {r.status_code}: {r.text[:200]}"
    return True, f"Backup saved: {file_path}"


def c_list_backups(panel=None, username=None):
    """Admin: backed-up file-এর list (content ছাড়া — হালকা)।"""
    params = {"select": "panel_domain,username,filename,file_path,size_bytes,updated_at",
              "order": "updated_at.desc"}
    if panel:
        params["panel_domain"] = f"eq.{panel}"
    if username:
        params["username"] = f"eq.{username}"
    r = csb_get("user_files", params)
    if r.status_code != 200:
        return False, f"DB error {r.status_code}"
    return True, r.json()


def c_get_backup(panel, username, file_path):
    """Admin: একটা backup-এর content (download / restore-এর জন্য)।"""
    params = {"panel_domain": f"eq.{panel}", "username": f"eq.{username}",
              "file_path": f"eq.{file_path}", "select": "*"}
    r = csb_get("user_files", params)
    if r.status_code != 200 or not r.json():
        return False, "Backup not found"
    row = r.json()[0]
    return True, {"panel_domain": row.get("panel_domain"), "username": row.get("username"),
                  "filename": row.get("filename"), "file_path": row.get("file_path"),
                  "size_bytes": row.get("size_bytes"), "content_b64": row.get("content_b64"),
                  "updated_at": row.get("updated_at")}


def c_delete_backup(panel, username, file_path):
    """Admin: একটা backup মুছা (panel-এর আসল file-এ হাত দেয় না)।"""
    params = {"panel_domain": f"eq.{panel}", "username": f"eq.{username}",
              "file_path": f"eq.{file_path}"}
    r = csb_delete("user_files", params)
    if r.status_code not in (200, 204):
        return False, f"DB error {r.status_code}"
    return True, f"Backup deleted: {file_path}"


def c_all_users():
    """সব panel-এর সব user (admin token দিয়ে) — /admin-এর All panels view-এর জন্য।"""
    r = csb_get("users", {"select": "*"})
    if r.status_code != 200:
        return False, f"DB error {r.status_code}"
    out = []
    for row in r.json():
        out.append({"username": row.get("username"), "panel_domain": row.get("panel_domain"),
                    "p": row.get("p"), "disk": row.get("disk"), "memory": row.get("memory"),
                    "status": row.get("status"), "created_at": row.get("created_at"),
                    "expiry_date": row.get("expiry_date")})
    return True, out


def c_list_panels():
    r = csb_get("panels", {"select": "domain,status,expiry_date,created_at"})
    if r.status_code != 200:
        return False, f"DB error {r.status_code}"
    panels = r.json()
    counts = {}
    ru = csb_get("users", {"select": "panel_domain"})
    if ru.status_code == 200:
        for row in ru.json():
            d = row.get("panel_domain") or ""
            counts[d] = counts.get(d, 0) + 1
    for p in panels:
        p["users"] = counts.get(p.get("domain"), 0)
        p["expired"] = _panel_expired(p)
    return True, panels


def c_extend_panel(domain, days):
    domain = (domain or "").strip().lower()
    try:
        days = int(days or 0)
    except Exception:
        days = 0
    if not domain or days <= 0:
        return False, "domain + days (>0) required"
    row = c_panel_registered(domain)
    if row is None:
        return False, "Panel not found"
    kolkata_tz = pytz.timezone('Asia/Kolkata')
    base = datetime.now(kolkata_tz)
    try:
        cur = datetime.strptime(str(row.get("expiry_date") or ""), '%d-%m-%Y %H:%M:%S')
        cur = kolkata_tz.localize(cur)
        if cur > base:
            base = cur
    except Exception:
        pass
    new_expiry = (base + timedelta(days=days)).strftime('%d-%m-%Y %H:%M:%S')
    r = csb_patch("panels", {"domain": f"eq.{domain}"}, {"expiry_date": new_expiry})
    if r.status_code not in (200, 204):
        return False, f"DB error {r.status_code}"
    return True, f"Panel '{domain}' extended to {new_expiry}"


def c_reissue_panel_key(domain):
    """Owner recovery: নতুন key বানিয়ে hash বসানো — key একবারই দেখানো হবে, panel-এর env-এ PANEL_KEY হিসেবে বসাতে হবে।"""
    domain = (domain or "").strip().lower()
    if not domain:
        return False, "domain is required"
    if c_panel_registered(domain) is None:
        return False, "Panel not found"
    key = os.urandom(24).hex()
    r = csb_patch("panels", {"domain": f"eq.{domain}"},
                  {"panel_key_hash": hashlib.sha256(key.encode()).hexdigest()})
    if r.status_code not in (200, 204):
        return False, f"DB error {r.status_code}"
    return True, {"domain": domain, "panel_key": key,
                  "msg": "New key issued. Set it as PANEL_KEY in the panel project (shown only once)."}


def c_create_panel(domain, days=None):
    """Owner pre-register: key ছাড়া pending row + expiry। Panel deploy হলে auto-enroll করে key bind করবে।"""
    domain = (domain or "").strip().lower()
    if not domain:
        return False, "domain is required"
    try:
        days = int(days) if days not in (None, "") else 30
    except Exception:
        days = 30
    expiry = None
    if days > 0:
        kolkata_tz = pytz.timezone('Asia/Kolkata')
        expiry = (datetime.now(kolkata_tz) + timedelta(days=days)).strftime('%d-%m-%Y %H:%M:%S')
    row = {"domain": domain, "status": "pending", "expiry_date": expiry}
    r = csb_post("panels", row)
    if r.status_code not in (200, 201):
        return False, f"DB error {r.status_code}: {r.text[:200]}"
    return True, {"domain": domain, "panel_status": "pending", "expiry_date": expiry,
                  "msg": f"Panel pre-registered. Deploy it — it will auto-enroll and wait for approval. Expiry: {expiry or 'none'}."}


def c_delete_panel(domain):
    domain = (domain or "").strip().lower()
    if not domain:
        return False, "domain is required"
    csb_delete("users", {"panel_domain": f"eq.{domain}"})
    r = csb_delete("panels", {"domain": f"eq.{domain}"})
    if r.status_code not in (200, 204):
        return False, f"DB error {r.status_code}"
    return True, f"Panel '{domain}' and its users deleted."


def c_set_panel_status(domain, status):
    domain = (domain or "").strip().lower()
    if not domain or status not in ("active", "suspended"):
        return False, "domain + status (active/suspended) required"
    r = csb_patch("panels", {"domain": f"eq.{domain}"}, {"status": status})
    if r.status_code not in (200, 204):
        return False, f"DB error {r.status_code}"
    return True, f"Panel '{domain}' -> {status}"


# ---------- In-process dispatcher (CENTRAL_MODE-এ panel-এর central_request() এখানে আসে) ----------

def central_dispatch(method, path, json_data=None, params=None):
    headers = central_headers()
    if path == "/api/central/auth" and method == "POST":
        d = json_data or {}
        return _CentralResp(200, c_auth_user((d.get("panel_domain") or "").strip().lower(),
                                             (d.get("username") or "").strip(),
                                             d.get("password") or ""))
    if path == "/api/central/users" and method == "GET":
        domain, is_admin, err = c_authorize(headers)
        if err:
            return _CentralResp(403, {"status": "error", "msg": err})
        ok, res = c_list_users(domain)
        if not ok:
            return _CentralResp(502, {"status": "error", "msg": res})
        return _CentralResp(200, {"status": "success", "users": res})
    if path == "/api/central/users" and method == "POST":
        domain, is_admin, err = c_authorize(headers)
        if err:
            return _CentralResp(403, {"status": "error", "msg": err})
        ok, msg = c_upsert_user(domain, (json_data or {}).get("user") or {})
        return _CentralResp(200 if ok else 400, {"status": "success" if ok else "error", "msg": msg})
    if path == "/api/central/users" and method == "DELETE":
        domain, is_admin, err = c_authorize(headers)
        if err:
            return _CentralResp(403, {"status": "error", "msg": err})
        ok, msg = c_delete_user(domain, (params or {}).get("username"))
        return _CentralResp(200 if ok else 400, {"status": "success" if ok else "error", "msg": msg})
    if path == "/api/central/admin/login" and method == "POST":
        d = json_data or {}
        ok, res = c_admin_login(d.get("username"), d.get("password"))
        if ok:
            return _CentralResp(200, {"status": "success", "token": res, "username": (d.get("username") or "").strip()})
        return _CentralResp(401, {"status": "error", "msg": res})
    if path == "/api/central/files" and method == "POST":
        domain, is_admin, err = c_authorize(headers)
        if err:
            return _CentralResp(403, {"status": "error", "msg": err})
        ok, res = c_backup_file(domain, json_data or {})
        return _CentralResp(200 if ok else 400, {"status": "success" if ok else "error", "msg": res})
    if path == "/api/central/admin/files" and method == "GET":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, res = c_list_backups((params or {}).get("panel"), (params or {}).get("user"))
        if not ok:
            return _CentralResp(502, {"status": "error", "msg": res})
        return _CentralResp(200, {"status": "success", "backups": res})
    if path == "/api/central/admin/files/download" and method == "GET":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, res = c_get_backup((params or {}).get("panel"), (params or {}).get("user"), (params or {}).get("file"))
        if not ok:
            return _CentralResp(404, {"status": "error", "msg": res})
        return _CentralResp(200, {"status": "success", **res})
    if path == "/api/central/admin/files" and method == "DELETE":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, msg = c_delete_backup((params or {}).get("panel"), (params or {}).get("user"), (params or {}).get("file"))
        return _CentralResp(200 if ok else 400, {"status": "success" if ok else "error", "msg": msg})
    if path == "/api/central/admin/users" and method == "GET":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, res = c_all_users()
        if not ok:
            return _CentralResp(502, {"status": "error", "msg": res})
        return _CentralResp(200, {"status": "success", "users": res})
    if path == "/api/central/admin/panels" and method == "GET":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, res = c_list_panels()
        if not ok:
            return _CentralResp(502, {"status": "error", "msg": res})
        # Fresh central: কোনো panel না থাকলে Add User-এর target-ই থাকে না ("Select a panel first")।
        # তাই admin /admin খুললেই central নিজেই একটা ACTIVE panel হয়ে যায় (expiry নেই)।
        # শুধু admin-token-এর পরেই চলে — Host-spoof দিয়ে কেউ অন্যের domain active করতে পারবে না।
        # Row না থাকলে বানায় (random hash — এই row দিয়ে panel-key auth হবে না, শুধু admin token)।
        # Pending row থাকলে: hash এই deployment-এর নিজের key-এর সাথে মিললে auto-activate
        # (central নিজে enroll করেছিল); না মিললে squat করা row — হাত দেওয়া হবে না।
        try:
            domain = get_panel_domain()
        except Exception:
            domain = ""
        if domain and domain not in ("default", "all"):
            mine = next((p for p in (res or []) if (p.get("domain") or "").lower() == domain), None)
            need_relist = False
            if mine is None:
                digest = hashlib.sha256(os.urandom(32)).hexdigest()
                r = csb_post("panels", {"domain": domain, "status": "active",
                                        "expiry_date": None, "panel_key_hash": digest})
                need_relist = r.status_code in (200, 201)
            elif mine.get("status") == "pending":
                key = _get_panel_key()
                row = c_panel_registered(domain)  # hash সহ full row (server-side only, browser-এ যায় না)
                if key and row and row.get("panel_key_hash"):
                    if hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), row.get("panel_key_hash")):
                        r = csb_patch("panels", {"domain": f"eq.{domain}"}, {"status": "active"})
                        need_relist = r.status_code in (200, 204)
            if need_relist:
                ok2, res2 = c_list_panels()
                if ok2:
                    res = res2
        return _CentralResp(200, {"status": "success", "panels": res})
    if path == "/api/central/admin/panels" and method == "POST":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, res = c_create_panel((json_data or {}).get("domain"), (json_data or {}).get("days"))
        if not ok:
            return _CentralResp(400, {"status": "error", "msg": res})
        return _CentralResp(200, {"status": "success", **res})
    if path == "/api/central/admin/panels" and method == "DELETE":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, msg = c_delete_panel((params or {}).get("domain"))
        return _CentralResp(200 if ok else 400, {"status": "success" if ok else "error", "msg": msg})
    if path == "/api/central/admin/panels/status" and method == "POST":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, msg = c_set_panel_status((json_data or {}).get("domain"), (json_data or {}).get("status"))
        return _CentralResp(200 if ok else 400, {"status": "success" if ok else "error", "msg": msg})
    if path == "/api/central/panels/register" and method == "POST":
        d = json_data or {}
        res = c_register_panel(d.get("domain"), d.get("panel_key"))
        return _CentralResp(200 if res.get("status") != "error" else 400, res)
    if path == "/api/central/panels/status" and method == "GET":
        domain = ((params or {}).get("domain") or headers.get("X-Panel-Domain") or "").strip().lower()
        return _CentralResp(200, c_panel_status(domain))
    if path == "/api/central/admin/panels/extend" and method == "POST":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, msg = c_extend_panel((json_data or {}).get("domain"), (json_data or {}).get("days"))
        return _CentralResp(200 if ok else 400, {"status": "success" if ok else "error", "msg": msg})
    if path == "/api/central/admin/panels/reissue" and method == "POST":
        if not c_verify_admin_token(headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""):
            return _CentralResp(403, {"status": "error", "msg": "Admin login required"})
        ok, res = c_reissue_panel_key((json_data or {}).get("domain"))
        if not ok:
            return _CentralResp(400, {"status": "error", "msg": res})
        return _CentralResp(200, {"status": "success", **res})
    return _CentralResp(404, {"status": "error", "msg": "Unknown central path"})


# ---------- Panel expiry / pending enforcement (auto-stop) ----------
# Date শেষ হলে বা Approve না হলে panel-এর user feature বন্ধ: expired/pending page + সব bot stop।
# Admin route গুলো সবসময় খোলা (owner manage করতে পারবে)।
_EXPIRY_CACHE = {"at": 0, "domain": None, "status": "unknown", "expired": False, "stopped": False}
EXPIRED_HTML = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Panel Expired</title><style>body{margin:0;background:#070a12;color:#e8ecf8;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:20px}h1{color:#f87171}</style></head>
<body><div><h1>⛔ Panel Expired</h1><p>This panel's validity has ended. All bots are stopped and login is closed.</p><p>Contact the admin to renew.</p></div></body></html>
"""
PENDING_HTML = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Panel Pending Approval</title><style>body{margin:0;background:#070a12;color:#e8ecf8;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:20px}h1{color:#facc15}</style></head>
<body><div><h1>⏳ Waiting for Owner Approval</h1><p>This panel is registered and waiting for the owner to approve it.</p><p>Please try again later.</p></div></body></html>
"""


def _panel_status_cached():
    now = time.time()
    domain = get_panel_domain()
    if _EXPIRY_CACHE.get("domain") == domain and now - _EXPIRY_CACHE.get("at", 0) < 300:
        return _EXPIRY_CACHE
    try:
        if CENTRAL_MODE:
            st = c_panel_status(domain)
        else:
            resp = requests.get(CENTRAL_URL.rstrip("/") + "/api/central/panels/status",
                                params={"domain": domain}, timeout=10)
            st = resp.json() if resp is not None and resp.status_code == 200 else None
    except Exception:
        st = None
    if st is None:
        return None  # central-এ পৌঁছানো যাচ্ছে না -> fail-open (login central-এ block হবে)
    _EXPIRY_CACHE.update({"at": now, "domain": domain, "status": st.get("status", "unknown"),
                          "expired": bool(st.get("expired")), "stopped": _EXPIRY_CACHE.get("stopped", False)})
    return _EXPIRY_CACHE


def _stop_all_user_processes():
    for k in list(running_processes.keys()):
        try:
            pid = running_processes[k]
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        finally:
            running_processes.pop(k, None)


@app.before_request
def _check_panel_expiry():
    p = request.path or ""
    if p.startswith(("/admin", "/api/admin", "/admin_login", "/admin_logout", "/sendnotification", "/api/central", "/static", "/central_admin")):
        return None
    _ensure_panel_key()  # প্রথম visit-এই enroll হবে, যাতে pending panel-এ pending page দেখায়
    st = _panel_status_cached()
    if st is None:
        return None
    if st.get("status") == "pending":
        return Response(PENDING_HTML, mimetype="text/html"), 403
    if st.get("status") == "suspended" or st.get("expired"):
        if not st.get("stopped"):
            _stop_all_user_processes()
            _EXPIRY_CACHE["stopped"] = True
        return Response(EXPIRED_HTML, mimetype="text/html"), 403
    if st.get("stopped"):
        _EXPIRY_CACHE["stopped"] = False
    return None


# ---------- Central API routes (customer panel থেকে HTTP-তে আসে) ----------

def _central_only():
    if not CENTRAL_MODE:
        return jsonify({"status": "error", "msg": "This deployment is not the central server."}), 503
    return None


@app.route("/api/central/auth", methods=["POST"])
def central_api_auth():
    blocked = _central_only()
    if blocked:
        return blocked
    data = request.get_json() or {}
    return jsonify(c_auth_user((data.get("panel_domain") or "").strip().lower(),
                               (data.get("username") or "").strip(),
                               data.get("password") or ""))


@app.route("/api/central/users", methods=["GET"])
def central_api_list_users():
    blocked = _central_only()
    if blocked:
        return blocked
    domain, is_admin, err = c_authorize(request.headers)
    if err:
        return jsonify({"status": "error", "msg": err}), 403
    ok, res = c_list_users(domain)
    if not ok:
        return jsonify({"status": "error", "msg": res}), 502
    return jsonify({"status": "success", "users": res})


@app.route("/api/central/users", methods=["POST"])
def central_api_upsert_user():
    blocked = _central_only()
    if blocked:
        return blocked
    domain, is_admin, err = c_authorize(request.headers)
    if err:
        return jsonify({"status": "error", "msg": err}), 403
    data = request.get_json() or {}
    ok, msg = c_upsert_user(domain, data.get("user") or {})
    return jsonify({"status": "success" if ok else "error", "msg": msg}), (200 if ok else 400)


@app.route("/api/central/users", methods=["DELETE"])
def central_api_delete_user():
    blocked = _central_only()
    if blocked:
        return blocked
    domain, is_admin, err = c_authorize(request.headers)
    if err:
        return jsonify({"status": "error", "msg": err}), 403
    ok, msg = c_delete_user(domain, request.args.get("username"))
    return jsonify({"status": "success" if ok else "error", "msg": msg}), (200 if ok else 400)


@app.route("/api/central/admin/login", methods=["POST"])
def central_api_admin_login():
    blocked = _central_only()
    if blocked:
        return blocked
    data = request.get_json() or {}
    ok, res = c_admin_login(data.get("username"), data.get("password"))
    if ok:
        return jsonify({"status": "success", "token": res, "username": (data.get("username") or "").strip()})
    return jsonify({"status": "error", "msg": res}), 401


@app.route("/api/central/files", methods=["POST"])
def central_api_backup_file():
    blocked = _central_only()
    if blocked:
        return blocked
    domain, is_admin, err = c_authorize(request.headers)
    if err:
        return jsonify({"status": "error", "msg": err}), 403
    ok, res = c_backup_file(domain, request.get_json() or {})
    return jsonify({"status": "success" if ok else "error", "msg": res}), (200 if ok else 400)


@app.route("/api/central/admin/files", methods=["GET", "DELETE"])
def central_api_admin_files():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    if request.method == "GET":
        ok, res = c_list_backups(request.args.get("panel"), request.args.get("user"))
        if not ok:
            return jsonify({"status": "error", "msg": res}), 502
        return jsonify({"status": "success", "backups": res})
    ok, msg = c_delete_backup(request.args.get("panel"), request.args.get("user"), request.args.get("file"))
    return jsonify({"status": "success" if ok else "error", "msg": msg}), (200 if ok else 400)


@app.route("/api/central/admin/files/download", methods=["GET"])
def central_api_admin_files_download():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    ok, res = c_get_backup(request.args.get("panel"), request.args.get("user"), request.args.get("file"))
    if not ok:
        return jsonify({"status": "error", "msg": res}), 404
    return jsonify({"status": "success", **res})


@app.route("/api/central/admin/users", methods=["GET"])
def central_api_admin_users():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    ok, res = c_all_users()
    if not ok:
        return jsonify({"status": "error", "msg": res}), 502
    return jsonify({"status": "success", "users": res})


@app.route("/api/central/admin/panels", methods=["GET"])
def central_api_admin_panels():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    ok, res = c_list_panels()
    if not ok:
        return jsonify({"status": "error", "msg": res}), 502
    return jsonify({"status": "success", "panels": res})


@app.route("/api/central/admin/panels", methods=["POST"])
def central_api_admin_create_panel():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    data = request.get_json() or {}
    ok, res = c_create_panel(data.get("domain"), data.get("days"))
    if not ok:
        return jsonify({"status": "error", "msg": res}), 400
    return jsonify({"status": "success", **res})


@app.route("/api/central/admin/panels", methods=["DELETE"])
def central_api_admin_delete_panel():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    ok, msg = c_delete_panel(request.args.get("domain"))
    return jsonify({"status": "success" if ok else "error", "msg": msg}), (200 if ok else 400)


@app.route("/api/central/panels/register", methods=["POST"])
def central_api_register_panel():
    blocked = _central_only()
    if blocked:
        return blocked
    data = request.get_json() or {}
    res = c_register_panel(data.get("domain"), data.get("panel_key"))
    return jsonify(res), (200 if res.get("status") != "error" else 400)


@app.route("/api/central/panels/status", methods=["GET"])
def central_api_panel_status():
    blocked = _central_only()
    if blocked:
        return blocked
    domain = (request.args.get("domain") or request.headers.get("X-Panel-Domain") or "").strip().lower()
    return jsonify(c_panel_status(domain))


@app.route("/api/central/admin/panels/extend", methods=["POST"])
def central_api_admin_extend_panel():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    data = request.get_json() or {}
    ok, msg = c_extend_panel(data.get("domain"), data.get("days"))
    return jsonify({"status": "success" if ok else "error", "msg": msg}), (200 if ok else 400)


@app.route("/api/central/admin/panels/reissue", methods=["POST"])
def central_api_admin_reissue_panel():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    data = request.get_json() or {}
    ok, res = c_reissue_panel_key(data.get("domain"))
    if not ok:
        return jsonify({"status": "error", "msg": res}), 400
    return jsonify({"status": "success", **res})


@app.route("/api/central/admin/panels/status", methods=["POST"])
def central_api_admin_panel_status():
    blocked = _central_only()
    if blocked:
        return blocked
    if not c_verify_admin_token(request.headers.get("Authorization", "")[7:] if request.headers.get("Authorization", "").startswith("Bearer ") else ""):
        return jsonify({"status": "error", "msg": "Admin login required"}), 403
    data = request.get_json() or {}
    ok, msg = c_set_panel_status(data.get("domain"), data.get("status"))
    return jsonify({"status": "success" if ok else "error", "msg": msg}), (200 if ok else 400)


# ---------- Central admin UI (/central_admin — panel-এর /admin থেকে আলাদা) ----------

CENTRAL_ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Central Admin | Shadow HOST</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; background: #070a12; color: #e8ecf8; font-family: system-ui, sans-serif; }
header { padding: 18px 16px; background: #0d1220; border-bottom: 1px solid #1e2a44; display: flex; justify-content: space-between; align-items: center; }
header h1 { margin: 0; font-size: 20px; letter-spacing: 2px; }
header h1 span { color: #3b82f6; }
.card { background: #0d1220; border: 1px solid #1e2a44; border-radius: 14px; padding: 16px; margin: 14px; }
input { width: 100%; padding: 11px; margin: 6px 0; border-radius: 10px; border: 1px solid #26324f; background: #080b14; color: #fff; }
button { padding: 11px 16px; border: 0; border-radius: 10px; font-weight: 700; cursor: pointer; }
.btn-add { background: #2563eb; color: #fff; width: 100%; }
.btn-del { background: #7f1d1d; color: #fff; }
.btn-ban { background: #ef4444; color: #fff; }
.btn-unban { background: #22c55e; color: #000; }
.btn-small { padding: 7px 10px; font-size: 12px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 10px 8px; border-bottom: 1px solid #1e2a44; text-align: left; font-size: 13px; }
.badge { padding: 3px 10px; border-radius: 20px; font-size: 11px; }
.badge.active { background: #14532d; color: #4ade80; }
.badge.suspended { background: #7f1d1d; color: #fca5a5; }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: #14532d; padding: 12px 20px; border-radius: 10px; display: none; z-index: 99; }
#toast.err { background: #7f1d1d; }
#keybox { display: none; background: #121a2e; border: 1px dashed #3b82f6; border-radius: 10px; padding: 12px; margin-top: 10px; word-break: break-all; }
</style>
</head>
<body>
<header><h1>Shadow <span>HOST</span> — Central Admin</h1><a href="#" onclick="logout()" style="color:#f87171;text-decoration:none;font-weight:700">Logout</a></header>
<div id="loginCard" class="card">
  <h3 style="margin-top:0">Admin Login</h3>
  <input id="l_user" placeholder="Username">
  <input id="l_pass" placeholder="Password" type="password">
  <button class="btn-add" onclick="login()">Login</button>
</div>
<div id="main" style="display:none">
  <div class="card">
    <h3 style="margin-top:0">New Panel</h3>
    <input id="f_domain" placeholder="Panel domain (e.g. customer1.de1.netrun.io)">
    <button class="btn-add" onclick="createPanel()">Create Panel + Get Key</button>
    <div id="keybox"></div>
  </div>
  <div class="card">
    <h3 style="margin-top:0">Panels</h3>
    <div style="overflow-x:auto"><table><thead><tr><th>Domain</th><th>Status</th><th>Users</th><th>Created</th><th>Actions</th></tr></thead><tbody id="tbody"></tbody></table></div>
  </div>
</div>
<div id="toast"></div>
<script>
let token = localStorage.getItem('central_admin_token') || '';
function toast(msg, isErr) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = isErr ? 'err' : '';
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 4000);
}
function authHeaders() { return { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token }; }
function showMain(show) {
  document.getElementById('loginCard').style.display = show ? 'none' : 'block';
  document.getElementById('main').style.display = show ? 'block' : 'none';
}
async function login() {
  const r = await fetch('/api/central/admin/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: document.getElementById('l_user').value.trim(), password: document.getElementById('l_pass').value }) });
  const d = await r.json();
  if (d.status !== 'success') { toast(d.msg || 'Login failed', true); return; }
  token = d.token;
  localStorage.setItem('central_admin_token', token);
  toast('Logged in as ' + d.username);
  showMain(true);
  loadPanels();
}
function logout() { token = ''; localStorage.removeItem('central_admin_token'); showMain(false); }
async function loadPanels() {
  const r = await fetch('/api/central/admin/panels', { headers: authHeaders() });
  const d = await r.json();
  if (d.status !== 'success') { toast(d.msg || 'Failed to load', true); if (r.status === 403) showMain(false); return; }
  const tb = document.getElementById('tbody');
  let html = '';
  (d.panels || []).forEach(p => {
    const badge = p.status === 'suspended' ? '<span class="badge suspended">suspended</span>' : '<span class="badge active">active</span>';
    const toggle = p.status === 'suspended'
      ? `<button class="btn-unban btn-small" onclick="setStatus('${p.domain}','active')">Activate</button>`
      : `<button class="btn-ban btn-small" onclick="setStatus('${p.domain}','suspended')">Suspend</button>`;
    html += `<tr><td><b>${p.domain}</b></td><td>${badge}</td><td>${p.users ?? 0}</td><td>${p.created_at ?? ''}</td><td>${toggle} <button class="btn-del btn-small" onclick="delPanel('${p.domain}')">Delete</button></td></tr>`;
  });
  tb.innerHTML = html || '<tr><td colspan="5">No panels yet.</td></tr>';
}
async function createPanel() {
  const domain = document.getElementById('f_domain').value.trim().toLowerCase();
  if (!domain) { toast('Domain required', true); return; }
  const r = await fetch('/api/central/admin/panels', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ domain }) });
  const d = await r.json();
  if (d.status !== 'success') { toast(d.msg || 'Failed', true); return; }
  const kb = document.getElementById('keybox');
  kb.style.display = 'block';
  kb.innerHTML = '<b>Panel:</b> ' + d.domain + '<br><b>PANEL_KEY (copy now — shown only once):</b><br>' + d.panel_key;
  document.getElementById('f_domain').value = '';
  loadPanels();
}
async function setStatus(domain, status) {
  const r = await fetch('/api/central/admin/panels/status', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ domain, status }) });
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') loadPanels();
}
async function delPanel(domain) {
  if (!confirm('Delete panel ' + domain + ' and ALL its users?')) return;
  const r = await fetch('/api/central/admin/panels?domain=' + encodeURIComponent(domain), { method: 'DELETE', headers: authHeaders() });
  const d = await r.json();
  toast(d.msg, d.status !== 'success');
  if (d.status === 'success') loadPanels();
}
if (token) { showMain(true); loadPanels(); }
</script>
</body>
</html>
"""


@app.route("/central_admin")
def central_admin_page():
    # Central admin এখন /admin dashboard-এর Panels menu-তে — এক জায়গায় সব।
    return redirect(url_for('admin_panel'))


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
