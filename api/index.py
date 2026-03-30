"""
ColdNerd License Server — Flask API for Vercel
License management + TTS character-tracking via Hume.ai
"""

from flask import Flask, request, jsonify, make_response
from upstash_redis import Redis
import os
import json
import uuid
import time
import base64
import requests as http_requests
from datetime import datetime, timedelta

app = Flask(__name__)

# ==================== CONFIG ====================
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123").strip()
HUME_API_KEY = os.environ.get("HUME_API_KEY", "").strip()
HUME_SECRET_KEY = os.environ.get("HUME_SECRET_KEY", "").strip()
DEFAULT_CHAR_LIMIT = int(os.environ.get("DEFAULT_CHAR_LIMIT", os.environ.get("DEFAULT_WORD_LIMIT", "5000")))

TIERS = {
    "trial": {
        "name": "Trial", "max_machines": 1,
        "features": ["home_feed_warmup"],
        "max_profiles": 1, "duration_days": 3, "price": 0,
    },
    "basic": {
        "name": "Basic", "max_machines": 1,
        "features": ["home_feed_warmup", "dm_outreach"],
        "max_profiles": 1, "duration_days": 30, "price": 29,
    },
    "pro": {
        "name": "Pro", "max_machines": 3,
        "features": [
            "home_feed_warmup", "reels_warmup", "story_warmup",
            "keyword_search", "profile_visit", "dm_outreach", "voice_notes",
        ],
        "max_profiles": 3, "duration_days": 30, "price": 49,
    },
    "agency": {
        "name": "Agency", "max_machines": 10,
        "features": [
            "home_feed_warmup", "reels_warmup", "story_warmup",
            "keyword_search", "profile_visit", "dm_outreach",
            "voice_notes", "unlimited_profiles",
        ],
        "max_profiles": 999, "duration_days": 30, "price": 99,
    },
}

# ==================== HELPERS ====================

def get_redis():
    return Redis(
        url=os.environ.get("UPSTASH_REDIS_REST_URL", "").strip(),
        token=os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip(),
    )


def generate_key():
    parts = [uuid.uuid4().hex[:4].upper() for _ in range(4)]
    return f"IGTOOL-{'-'.join(parts)}"


def verify_admin(req):
    pw = req.headers.get("X-Admin-Password", "")
    if not pw:
        pw = (req.get_json(silent=True) or {}).get("admin_password", "")
    return pw == ADMIN_PASSWORD


def cors(data, status=200):
    resp = jsonify(data)
    resp.status_code = status
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Password"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    return resp


def get_lic(r, k):
    raw = r.get(f"license:{k}")
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def save_lic(r, k, d):
    r.set(f"license:{k}", json.dumps(d))


def get_tts(r, k):
    raw = r.get(f"tts:{k}")
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def save_tts(r, k, d):
    r.set(f"tts:{k}", json.dumps(d))


def get_tts_config(r):
    raw = r.get("tts:config")
    if raw:
        cfg = json.loads(raw) if isinstance(raw, str) else raw
        # Migrate old key name
        if "default_word_limit" in cfg and "default_char_limit" not in cfg:
            cfg["default_char_limit"] = cfg.pop("default_word_limit")
        return cfg
    return {"default_char_limit": DEFAULT_CHAR_LIMIT}


def _migrate_tts_usage(u: dict) -> dict:
    """Migrate old word-based keys to char-based keys in TTS usage records."""
    if "words_used" in u and "chars_used" not in u:
        u["chars_used"] = u.pop("words_used")
    if "words_limit" in u and "chars_limit" not in u:
        u["chars_limit"] = u.pop("words_limit")
    return u


def count_chars(text):
    return len(text)


def ts_human(ts):
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ==================== CORS PREFLIGHT ====================

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Password"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
        return resp


# ==================== APP ENDPOINTS ====================

@app.route("/api/validate", methods=["POST", "OPTIONS"])
def validate_license():
    d = request.get_json(silent=True)
    if not d:
        return cors({"valid": False, "error": "Invalid request"}, 400)

    key = d.get("key", "").strip()
    hwid = d.get("hwid", "").strip()
    if not key or not hwid:
        return cors({"valid": False, "error": "Missing key or hwid"}, 400)

    r = get_redis()
    lic = get_lic(r, key)
    if not lic:
        return cors({"valid": False, "error": "Invalid license key"})
    if lic.get("revoked"):
        return cors({"valid": False, "error": "License has been revoked"})
    expires_at = lic.get("expires_at", 0)
    if time.time() > expires_at:
        return cors({"valid": False, "error": "License has expired"})
    if hwid not in [m["hwid"] for m in lic.get("machines", [])]:
        return cors({"valid": False, "error": "Machine not activated"})

    tier = lic.get("tier", "basic")
    ti = TIERS.get(tier, TIERS["basic"])
    lic["last_validated"] = time.time()
    save_lic(r, key, lic)

    cfg = get_tts_config(r)
    usage = get_tts(r, key) or {"words_used": 0, "words_limit": cfg["default_word_limit"]}

    return cors({
        "valid": True,
        "tier": tier,
        "tier_name": ti["name"],
        "features": ti["features"],
        "max_profiles": ti["max_profiles"],
        "expires_at": expires_at,
        "expires_at_human": ts_human(expires_at),
        "tts_words_used": usage.get("words_used", 0),
        "tts_words_limit": usage.get("words_limit", cfg["default_word_limit"]),
    })


@app.route("/api/activate", methods=["POST", "OPTIONS"])
def activate_license():
    d = request.get_json(silent=True)
    if not d:
        return cors({"success": False, "error": "Invalid request"}, 400)

    key = d.get("key", "").strip()
    hwid = d.get("hwid", "").strip()
    machine_name = d.get("machine_name", "Unknown")
    if not key or not hwid:
        return cors({"success": False, "error": "Missing key or hwid"}, 400)

    r = get_redis()
    lic = get_lic(r, key)
    if not lic:
        return cors({"success": False, "error": "Invalid license key"})
    if lic.get("revoked"):
        return cors({"success": False, "error": "License has been revoked"})
    expires_at = lic.get("expires_at", 0)
    if time.time() > expires_at:
        return cors({"success": False, "error": "License has expired"})

    machines = lic.get("machines", [])
    tier = lic.get("tier", "basic")
    ti = TIERS.get(tier, TIERS["basic"])
    max_m = lic.get("max_machines_override") or ti["max_machines"]

    for m in machines:
        if m["hwid"] == hwid:
            return cors({
                "success": True, "message": "Machine already activated",
                "tier": tier, "tier_name": ti["name"],
                "features": ti["features"], "max_profiles": ti["max_profiles"],
                "expires_at": expires_at,
            })

    if len(machines) >= max_m:
        return cors({"success": False, "error": f"Machine limit reached ({max_m} max)."})

    machines.append({"hwid": hwid, "machine_name": machine_name, "activated_at": time.time()})
    lic["machines"] = machines
    lic["last_validated"] = time.time()
    save_lic(r, key, lic)

    return cors({
        "success": True, "message": "Machine activated successfully",
        "tier": tier, "tier_name": ti["name"],
        "features": ti["features"], "max_profiles": ti["max_profiles"],
        "expires_at": expires_at,
    })


@app.route("/api/trial", methods=["POST", "OPTIONS"])
def create_trial():
    d = request.get_json(silent=True)
    if not d:
        return cors({"success": False, "error": "Invalid request"}, 400)

    hwid = d.get("hwid", "").strip()
    mac_hash = d.get("mac_hash", "").strip()
    machine_name = d.get("machine_name", "Unknown")
    if not hwid:
        return cors({"success": False, "error": "Missing hwid"}, 400)

    r = get_redis()
    if r.get(f"trial_hwid:{hwid}"):
        return cors({"success": False, "error": "Trial already used on this machine."})
    if mac_hash and r.get(f"trial_mac:{mac_hash}"):
        return cors({"success": False, "error": "Trial already used on this machine."})

    key = generate_key()
    ti = TIERS["trial"]
    expires_at = time.time() + (ti["duration_days"] * 86400)

    lic = {
        "key": key, "tier": "trial", "created_at": time.time(),
        "expires_at": expires_at, "revoked": False,
        "machines": [{"hwid": hwid, "machine_name": machine_name, "activated_at": time.time()}],
        "last_validated": time.time(), "notes": "Auto-generated trial",
    }
    save_lic(r, key, lic)
    r.set(f"trial_hwid:{hwid}", key)
    if mac_hash:
        r.set(f"trial_mac:{mac_hash}", key)
    r.sadd("all_license_keys", key)

    return cors({
        "success": True, "key": key, "tier": "trial",
        "tier_name": ti["name"], "features": ti["features"],
        "max_profiles": ti["max_profiles"], "expires_at": expires_at,
        "expires_at_human": ts_human(expires_at),
    })


# ==================== TTS ENDPOINTS ====================

@app.route("/api/tts/generate", methods=["POST", "OPTIONS"])
def tts_generate():
    """Generate TTS via system Hume API — tracks character usage per license."""
    d = request.get_json(silent=True)
    if not d:
        return cors({"success": False, "error": "Invalid request"}, 400)

    license_key = d.get("license_key", "").strip()
    hwid = d.get("hwid", "").strip()
    text = d.get("text", "").strip()
    voice_id = d.get("voice_id", "").strip()

    if not license_key or not hwid or not text:
        return cors({"success": False, "error": "Missing required fields"}, 400)

    r = get_redis()
    lic = get_lic(r, license_key)
    if not lic:
        return cors({"success": False, "error": "Invalid license key"}, 401)
    if lic.get("revoked"):
        return cors({"success": False, "error": "License revoked"}, 401)
    if time.time() > lic.get("expires_at", 0):
        return cors({"success": False, "error": "License expired"}, 401)
    if hwid not in [m["hwid"] for m in lic.get("machines", [])]:
        return cors({"success": False, "error": "Machine not activated"}, 401)

    char_count = count_chars(text)
    cfg = get_tts_config(r)
    default_limit = cfg.get("default_char_limit", DEFAULT_CHAR_LIMIT)

    usage = _migrate_tts_usage(get_tts(r, license_key) or {
        "chars_used": 0, "chars_limit": default_limit,
        "requests_count": 0, "last_request": 0, "created_at": time.time(),
    })

    remaining = usage["chars_limit"] - usage["chars_used"]
    if char_count > remaining:
        return cors({
            "success": False, "error": "char_limit_reached",
            "message": f"Character limit reached! You have {max(0, remaining)} characters remaining out of {usage['chars_limit']}. Contact admin for more characters.",
            "chars_used": usage["chars_used"],
            "chars_limit": usage["chars_limit"],
            "chars_remaining": max(0, remaining),
        }, 403)

    if not HUME_API_KEY:
        return cors({"success": False, "error": "TTS service not configured on server"}, 503)

    hume_voice = voice_id or "964f54e6-b1f1-4934-8363-af5060ba6980"
    try:
        hume_resp = http_requests.post(
            "https://api.hume.ai/v0/tts",
            json={
                "utterances": [{
                    "text": text,
                    "description": "Speak in a warm, casual, natural conversational tone, like a friendly person chatting",
                    "voice": {"id": hume_voice},
                }],
                "format": {"type": "mp3"},
            },
            headers={
                "X-Hume-Api-Key": HUME_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=90,
        )
        if hume_resp.status_code != 200:
            return cors({"success": False, "error": f"TTS API error: {hume_resp.status_code} — {hume_resp.text[:200]}"}, 502)

        audio_b64 = ""
        ct = hume_resp.headers.get("Content-Type", "")
        if "application/json" in ct:
            rd = hume_resp.json()
            if "generations" in rd and rd["generations"]:
                audio_b64 = rd["generations"][0].get("audio", "")
        else:
            audio_b64 = base64.b64encode(hume_resp.content).decode()

        if not audio_b64:
            return cors({"success": False, "error": "No audio in TTS response"}, 502)

        # Update usage
        usage["chars_used"] += char_count
        usage["requests_count"] = usage.get("requests_count", 0) + 1
        usage["last_request"] = time.time()
        save_tts(r, license_key, usage)
        r.sadd("tts:all_users", license_key)

        # Daily stats
        today = datetime.now().strftime("%Y-%m-%d")
        day_raw = r.get(f"tts:daily:{today}")
        day = json.loads(day_raw) if isinstance(day_raw, str) else (day_raw or {"chars": 0, "requests": 0})
        day["chars"] = day.get("chars", day.get("words", 0)) + char_count
        day.pop("words", None)
        day["requests"] = day.get("requests", 0) + 1
        r.set(f"tts:daily:{today}", json.dumps(day))

        return cors({
            "success": True, "audio_base64": audio_b64,
            "chars_used_now": char_count,
            "chars_used_total": usage["chars_used"],
            "chars_limit": usage["chars_limit"],
            "chars_remaining": usage["chars_limit"] - usage["chars_used"],
        })

    except http_requests.exceptions.Timeout:
        return cors({"success": False, "error": "TTS API timeout"}, 504)
    except Exception as e:
        return cors({"success": False, "error": f"TTS error: {str(e)}"}, 500)


@app.route("/api/tts/check", methods=["POST", "OPTIONS"])
def tts_check():
    """Check TTS character balance for a license key."""
    d = request.get_json(silent=True)
    if not d:
        return cors({"success": False, "error": "Invalid request"}, 400)

    license_key = d.get("license_key", "").strip()
    if not license_key:
        return cors({"success": False, "error": "Missing license_key"}, 400)

    r = get_redis()
    lic = get_lic(r, license_key)
    if not lic:
        return cors({"success": False, "error": "Invalid license key"}, 401)

    cfg = get_tts_config(r)
    usage = _migrate_tts_usage(get_tts(r, license_key) or {
        "chars_used": 0,
        "chars_limit": cfg.get("default_char_limit", DEFAULT_CHAR_LIMIT),
        "requests_count": 0,
    })

    return cors({
        "success": True,
        "chars_used": usage["chars_used"],
        "chars_limit": usage["chars_limit"],
        "chars_remaining": usage["chars_limit"] - usage["chars_used"],
        "requests_count": usage.get("requests_count", 0),
    })


# ==================== ADMIN ENDPOINTS ====================

@app.route("/api/admin/generate", methods=["POST", "OPTIONS"])
def admin_generate():
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)

    d = request.get_json(silent=True) or {}
    tier = d.get("tier", "basic")
    duration_days = int(d.get("duration_days", 30))
    max_machines = int(d.get("max_machines", 0))
    notes = d.get("notes", "")

    if tier not in TIERS:
        return cors({"success": False, "error": f"Invalid tier: {tier}"}, 400)

    ti = TIERS[tier]
    if max_machines <= 0:
        max_machines = ti["max_machines"]

    key = generate_key()
    expires_at = time.time() + (duration_days * 86400)

    lic = {
        "key": key, "tier": tier, "created_at": time.time(),
        "expires_at": expires_at, "revoked": False, "machines": [],
        "max_machines_override": max_machines if max_machines != ti["max_machines"] else None,
        "last_validated": None, "notes": notes,
    }
    r = get_redis()
    save_lic(r, key, lic)
    r.sadd("all_license_keys", key)

    return cors({
        "success": True, "key": key, "tier": tier,
        "tier_name": ti["name"], "expires_at": expires_at,
        "expires_at_human": ts_human(expires_at), "max_machines": max_machines,
    })


@app.route("/api/admin/keys", methods=["GET", "OPTIONS"])
def admin_list_keys():
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)

    r = get_redis()
    all_keys = r.smembers("all_license_keys")
    if not all_keys:
        return cors({"success": True, "keys": []})

    keys_data = []
    for key in all_keys:
        lic = get_lic(r, key)
        if not lic:
            continue
        tier = lic.get("tier", "basic")
        ti = TIERS.get(tier, TIERS["basic"])
        exp = lic.get("expires_at", 0)
        status = "revoked" if lic.get("revoked") else ("expired" if time.time() > exp else "active")
        keys_data.append({
            "key": key, "tier": tier, "tier_name": ti["name"], "status": status,
            "created_at": lic.get("created_at", 0),
            "created_at_human": ts_human(lic.get("created_at")),
            "expires_at": exp, "expires_at_human": ts_human(exp),
            "machines": lic.get("machines", []),
            "machine_count": len(lic.get("machines", [])),
            "max_machines": lic.get("max_machines_override") or ti["max_machines"],
            "last_validated": lic.get("last_validated"),
            "notes": lic.get("notes", ""),
        })

    keys_data.sort(key=lambda x: x["created_at"], reverse=True)
    return cors({"success": True, "keys": keys_data})


@app.route("/api/admin/stats", methods=["GET", "OPTIONS"])
def admin_stats():
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    try:
        r = get_redis()
        all_keys = r.smembers("all_license_keys")
        s = {
            "total_keys": 0, "active": 0, "expired": 0, "revoked": 0,
            "trial": 0, "basic": 0, "pro": 0, "agency": 0,
            "total_machines": 0, "monthly_revenue": 0,
        }
        if all_keys:
            s["total_keys"] = len(all_keys)
            for key in all_keys:
                lic = get_lic(r, key)
                if not lic:
                    continue
                tier = lic.get("tier", "basic")
                s[tier] = s.get(tier, 0) + 1
                s["total_machines"] += len(lic.get("machines", []))
                if lic.get("revoked"):
                    s["revoked"] += 1
                elif time.time() > lic.get("expires_at", 0):
                    s["expired"] += 1
                else:
                    s["active"] += 1
                    s["monthly_revenue"] += TIERS.get(tier, {}).get("price", 0)

        # TTS stats
        tts_users = r.smembers("tts:all_users") or set()
        total_chars = 0
        total_requests = 0
        for tk in tts_users:
            u = _migrate_tts_usage(get_tts(r, tk) or {})
            if u:
                total_chars += u.get("chars_used", 0)
                total_requests += u.get("requests_count", 0)

        s["tts_active_users"] = len(tts_users)
        s["tts_total_chars"] = total_chars
        s["tts_total_requests"] = total_requests

        cfg = get_tts_config(r)
        s["tts_default_limit"] = cfg.get("default_char_limit", DEFAULT_CHAR_LIMIT)

        # Daily TTS data (last 30 days)
        daily = []
        for i in range(30):
            day_str = (datetime.now() - timedelta(days=29 - i)).strftime("%Y-%m-%d")
            day_raw = r.get(f"tts:daily:{day_str}")
            d = json.loads(day_raw) if isinstance(day_raw, str) else (day_raw or {"chars": 0, "requests": 0})
            daily.append({"date": day_str, "chars": d.get("chars", d.get("words", 0)), "requests": d.get("requests", 0)})
        s["tts_daily"] = daily

        return cors({"success": True, "stats": s})
    except Exception as e:
        return cors({"success": False, "error": f"Server error: {str(e)}"}, 500)


@app.route("/api/admin/revoke", methods=["POST", "OPTIONS"])
def admin_revoke():
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    if not key:
        return cors({"success": False, "error": "Missing key"}, 400)
    r = get_redis()
    lic = get_lic(r, key)
    if not lic:
        return cors({"success": False, "error": "Key not found"})
    lic["revoked"] = True
    lic["revoked_at"] = time.time()
    save_lic(r, key, lic)
    return cors({"success": True, "message": "License revoked"})


@app.route("/api/admin/extend", methods=["POST", "OPTIONS"])
def admin_extend():
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    days = int(d.get("days", 30))
    if not key:
        return cors({"success": False, "error": "Missing key"}, 400)
    r = get_redis()
    lic = get_lic(r, key)
    if not lic:
        return cors({"success": False, "error": "Key not found"})
    base = max(lic.get("expires_at", time.time()), time.time())
    lic["expires_at"] = base + (days * 86400)
    lic["revoked"] = False
    save_lic(r, key, lic)
    return cors({"success": True, "message": f"License extended by {days} days", "new_expires_at_human": ts_human(lic["expires_at"])})


@app.route("/api/admin/delete", methods=["POST", "OPTIONS"])
def admin_delete():
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    if not key:
        return cors({"success": False, "error": "Missing key"}, 400)
    r = get_redis()
    r.delete(f"license:{key}")
    r.srem("all_license_keys", key)
    return cors({"success": True, "message": "License deleted permanently"})


@app.route("/api/admin/deactivate", methods=["POST", "OPTIONS"])
def admin_deactivate_machine():
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    hwid = d.get("hwid", "").strip()
    if not key or not hwid:
        return cors({"success": False, "error": "Missing key or hwid"}, 400)
    r = get_redis()
    lic = get_lic(r, key)
    if not lic:
        return cors({"success": False, "error": "Key not found"})
    lic["machines"] = [m for m in lic.get("machines", []) if m["hwid"] != hwid]
    save_lic(r, key, lic)
    return cors({"success": True, "message": "Machine deactivated"})


# ==================== ADMIN TTS ENDPOINTS ====================

@app.route("/api/admin/tts/users", methods=["GET", "OPTIONS"])
def admin_tts_users():
    """List all TTS users with their character usage."""
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)

    r = get_redis()
    tts_keys = r.smembers("tts:all_users") or set()
    users = []
    for tk in tts_keys:
        u = _migrate_tts_usage(get_tts(r, tk) or {})
        if not u:
            continue
        lic = get_lic(r, tk)
        tier = lic.get("tier", "unknown") if lic else "unknown"
        tier_name = TIERS.get(tier, {}).get("name", "Unknown") if lic else "Unknown"
        cl = u.get("chars_limit", DEFAULT_CHAR_LIMIT)
        cu = u.get("chars_used", 0)
        users.append({
            "license_key": tk,
            "name": u.get("name", ""),
            "tier": tier, "tier_name": tier_name,
            "chars_used": cu, "chars_limit": cl,
            "chars_remaining": cl - cu,
            "usage_percent": round((cu / cl * 100) if cl > 0 else 0, 1),
            "requests_count": u.get("requests_count", 0),
            "last_request": ts_human(u.get("last_request")),
            "created_at": ts_human(u.get("created_at")),
        })

    users.sort(key=lambda x: x["chars_used"], reverse=True)
    return cors({"success": True, "users": users})


@app.route("/api/admin/tts/set-limit", methods=["POST", "OPTIONS"])
def admin_tts_set_limit():
    """Set character limit for a specific license key."""
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    limit = int(d.get("limit", 0))
    if not key or limit <= 0:
        return cors({"success": False, "error": "Missing key or invalid limit"}, 400)

    r = get_redis()
    cfg = get_tts_config(r)
    usage = _migrate_tts_usage(get_tts(r, key) or {
        "chars_used": 0, "chars_limit": cfg["default_char_limit"],
        "requests_count": 0, "last_request": 0, "created_at": time.time(),
    })
    usage["chars_limit"] = limit
    save_tts(r, key, usage)
    r.sadd("tts:all_users", key)
    return cors({"success": True, "message": f"Character limit set to {limit:,}"})


@app.route("/api/admin/tts/add-chars", methods=["POST", "OPTIONS"])
def admin_tts_add_chars():
    """Add more characters to a user's limit."""
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    chars = int(d.get("chars", d.get("words", 0)))
    if not key or chars <= 0:
        return cors({"success": False, "error": "Missing key or invalid character count"}, 400)

    r = get_redis()
    cfg = get_tts_config(r)
    usage = _migrate_tts_usage(get_tts(r, key) or {
        "chars_used": 0, "chars_limit": cfg["default_char_limit"],
        "requests_count": 0, "last_request": 0, "created_at": time.time(),
    })
    usage["chars_limit"] += chars
    save_tts(r, key, usage)
    r.sadd("tts:all_users", key)
    return cors({"success": True, "message": f"Added {chars:,} characters. New limit: {usage['chars_limit']:,}"})


@app.route("/api/admin/tts/reset", methods=["POST", "OPTIONS"])
def admin_tts_reset():
    """Reset character count for a user."""
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    if not key:
        return cors({"success": False, "error": "Missing key"}, 400)

    r = get_redis()
    usage = _migrate_tts_usage(get_tts(r, key) or {})
    if not usage:
        return cors({"success": False, "error": "TTS user not found"})
    usage["chars_used"] = 0
    usage["requests_count"] = 0
    save_tts(r, key, usage)
    return cors({"success": True, "message": "Character count reset to 0"})


@app.route("/api/admin/tts/remove", methods=["POST", "OPTIONS"])
def admin_tts_remove():
    """Remove a TTS user's tracking data."""
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    if not key:
        return cors({"success": False, "error": "Missing key"}, 400)

    r = get_redis()
    r.delete(f"tts:{key}")
    r.srem("tts:all_users", key)
    return cors({"success": True, "message": "TTS user removed"})


@app.route("/api/admin/tts/set-name", methods=["POST", "OPTIONS"])
def admin_tts_set_name():
    """Set a display name for a TTS user (for admin identification)."""
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    key = d.get("key", "").strip()
    name = d.get("name", "").strip()
    if not key:
        return cors({"success": False, "error": "Missing key"}, 400)

    r = get_redis()
    cfg = get_tts_config(r)
    usage = _migrate_tts_usage(get_tts(r, key) or {
        "chars_used": 0, "chars_limit": cfg["default_char_limit"],
        "requests_count": 0, "last_request": 0, "created_at": time.time(),
    })
    usage["name"] = name
    save_tts(r, key, usage)
    r.sadd("tts:all_users", key)
    return cors({"success": True, "message": f"Name set to '{name}'" if name else "Name cleared"})


@app.route("/api/admin/tts/default-limit", methods=["POST", "OPTIONS"])
def admin_tts_default_limit():
    """Set the global default character limit for new users."""
    if not verify_admin(request):
        return cors({"success": False, "error": "Unauthorized"}, 401)
    d = request.get_json(silent=True) or {}
    limit = int(d.get("limit", 0))
    if limit <= 0:
        return cors({"success": False, "error": "Invalid limit"}, 400)

    r = get_redis()
    cfg = get_tts_config(r)
    cfg["default_char_limit"] = limit
    cfg.pop("default_word_limit", None)
    r.set("tts:config", json.dumps(cfg))
    return cors({"success": True, "message": f"Default character limit set to {limit:,}"})


# ==================== UTILITY ====================

@app.route("/api/health", methods=["GET", "OPTIONS"])
def health():
    return cors({"status": "ok", "service": "ColdNerd License Server", "timestamp": time.time()})


@app.route("/api/debug", methods=["GET", "OPTIONS"])
def debug_env():
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    result = {
        "has_redis_url": bool(url),
        "redis_url_prefix": url[:30] + "..." if len(url) > 30 else url,
        "has_redis_token": bool(token), "token_length": len(token),
        "has_admin_pw": bool(os.environ.get("ADMIN_PASSWORD", "")),
        "has_hume_key": bool(HUME_API_KEY),
    }
    try:
        r = get_redis()
        r.ping()
        result["redis_connected"] = True
    except Exception as e:
        result["redis_connected"] = False
        result["redis_error"] = str(e)
    return cors(result)


# ==================== DASHBOARD ====================

DASHBOARD_HTML = None

def _load_dashboard():
    global DASHBOARD_HTML
    if DASHBOARD_HTML:
        return DASHBOARD_HTML
    # Try to load from file first
    for d in [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public", "index.html"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public", "index.html"),
        "/var/task/public/index.html",
    ]:
        if os.path.exists(d):
            with open(d, "r", encoding="utf-8") as f:
                DASHBOARD_HTML = f.read()
            return DASHBOARD_HTML
    return None

@app.route("/", methods=["GET"])
def serve_dashboard():
    html = _load_dashboard()
    if html:
        resp = make_response(html)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp
    # Fallback: fetch from GitHub raw
    try:
        gh = http_requests.get("https://raw.githubusercontent.com/Ahmed-DEVArea/ColdnerdServer/main/public/index.html", timeout=10)
        if gh.status_code == 200:
            DASHBOARD_HTML = gh.text
            resp = make_response(DASHBOARD_HTML)
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            return resp
    except Exception:
        pass
    return "Dashboard could not be loaded", 500
