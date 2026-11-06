"""
OneWelcome Access "User Details Customization" hook for Paperless-ngx.

Adds a `groups` claim = the user's DMv2 organization names.
- Looks up GET {DM_BASE}/users/{user.id}/organizations with a client-credentials token
- Only acts for PAPERLESS_CLIENT_ID (other clients get their claims back unchanged)
- Safety net: if the DM lookup fails, falls back to the user's last known groups
  (cached on disk); if none are cached, returns the claims unchanged.
"""
import json, os, sys, time, base64, urllib.request, urllib.parse, threading
import provision

SECRET          = os.environ["HOOK_SECRET"]
TOKEN_URL       = os.environ["OIP_TOKEN_URL"]          # e.g. https://<tenant>.gw.<region-domain>/oauth/token
DM_BASE         = os.environ["DM_BASE"]                # e.g. https://<tenant>.gw.<region-domain>/dm/correlation/api/v1
CLIENT_ID       = os.environ["DM_CLIENT_ID"]
CLIENT_SECRET   = os.environ["DM_CLIENT_SECRET"]
DM_SCOPES       = os.environ.get("DM_SCOPES", "correlation:user:get correlation:organization:get")
PAPERLESS_CLIENT_ID = os.environ.get("PAPERLESS_CLIENT_ID", "")   # empty = apply to every client
EXCLUDE_ORGS    = {o.strip() for o in os.environ.get("EXCLUDE_ORGS", "root").split(",") if o.strip()}
TIMEOUT         = float(os.environ.get("DM_TIMEOUT", "3"))
CACHE_FILE      = os.environ.get("CACHE_FILE", "/data/groups-cache.json")
DEBUG           = os.environ.get("DEBUG", "1") == "1"

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def log(*a):
    print(*a, file=sys.stderr, flush=True)

# ---------- last-known-groups cache ----------
_lock = threading.Lock()
def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}
def _save_cache(c):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(c, f)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        log("CACHE WRITE ERROR:", e)
_cache = _load_cache()

# ---------- client-credentials token (cached) ----------
_tok = {"value": None, "exp": 0}
def get_token():
    if _tok["value"] and time.time() < _tok["exp"] - 30:
        return _tok["value"]
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": DM_SCOPES}).encode()
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        t = json.load(r)
    _tok["value"] = t["access_token"]
    _tok["exp"] = time.time() + int(t.get("expires_in", 300))
    return _tok["value"]

def dm_get(path):
    req = urllib.request.Request(DM_BASE + path, headers={
        "Authorization": f"Bearer {get_token()}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)

def _items(resp):
    """Accept a bare list or common paging wrappers."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("data", "items", "results", "organizations", "content", "resources", "Resources"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []

def _org_name(o):
    if isinstance(o, str):
        return None
    for k in ("name", "organizationName", "displayName"):
        if isinstance(o.get(k), str) and o[k]:
            return o[k]
    inner = o.get("organization") or o.get("properties") or {}
    if isinstance(inner, dict):
        for k in ("name", "organizationName", "displayName"):
            if isinstance(inner.get(k), str) and inner[k]:
                return inner[k]
    return None

def _org_id(o):
    if isinstance(o, str):
        return o
    return o.get("id") or o.get("organizationId") or (o.get("organization") or {}).get("id")

from datetime import datetime, timezone

def _active(o):
    if isinstance(o, dict) and o.get("status") not in (None, "ENABLED"):
        return False
    now = datetime.now(timezone.utc)
    rels = (o.get("relationships") or []) if isinstance(o, dict) else []
    rels = [r for r in rels if r.get("relationshipType", "is_member_of") == "is_member_of"] or rels
    if not rels:
        return True
    for r in rels:
        try:
            start = r.get("startDate"); end = r.get("endDate")
            ok_start = not start or datetime.fromisoformat(start.replace("Z", "+00:00")) <= now
            ok_end   = not end   or datetime.fromisoformat(end.replace("Z", "+00:00")) > now
            if ok_start and ok_end:
                return True
        except Exception:
            return True          # unparseable dates: don't lock the user out
    return False

def lookup_groups(user_id):
    resp = dm_get(f"/users/{urllib.parse.quote(user_id)}/organizations?allProperties=true&pageSize=100&pageNumber=1")
    if DEBUG:
        log("DM ORGS RESPONSE:", json.dumps(resp)[:2000])
    names = []
    for o in _items(resp):
        if not _active(o):
            continue
        n = _org_name(o)
        if not n:
            oid = _org_id(o)
            if oid:
                n = _org_name(dm_get(f"/organizations/{urllib.parse.quote(str(oid))}"))
        if n and n not in EXCLUDE_ORGS and n not in names:
            names.append(n)
    return names

# ---------- hook ----------
def handle(body):
    claims = body.get("claims")
    if not isinstance(claims, dict):
        return body
    if PAPERLESS_CLIENT_ID and (body.get("client") or {}).get("id") != PAPERLESS_CLIENT_ID:
        return body
    user_id = (body.get("user") or {}).get("id") or claims.get("sub")
    if not user_id:
        return body
    try:
        groups = lookup_groups(user_id)
        provision.ensure_orgs(groups)
        claims["groups"] = groups
        with _lock:
            _cache[user_id] = groups
            _save_cache(_cache)
        log(f"GROUPS {user_id}: {groups}")
    except Exception as e:
        cached = _cache.get(user_id)
        if cached is not None:
            claims["groups"] = cached
            log(f"DM LOOKUP FAILED ({e!r}) -> using cached groups {cached} for {user_id}")
        else:
            log(f"DM LOOKUP FAILED ({e!r}) -> no cache, returning claims unchanged for {user_id}")
    return body

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if not self.path.startswith(f"/{SECRET}"):
            self.send_response(404); self.end_headers(); return
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
            if DEBUG:
                log("HOOK REQUEST from", (body.get("executionContext") or {}).get("triggeredBy"))
            out = handle(body)
        except Exception as e:
            log("HOOK ERROR:", repr(e))
            out = json.loads(raw or b"{}") if raw else {}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, fmt, *args):          # keep the secret path out of access logs
        log(("%s %s" % (self.address_string(), fmt % args)).replace(SECRET, "<secret>"))

if __name__ == "__main__":
    import autoshare; autoshare.start()
    log(f"claims-hook starting; DM_BASE={DM_BASE} client filter={'on' if PAPERLESS_CLIENT_ID else 'off'}")
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
