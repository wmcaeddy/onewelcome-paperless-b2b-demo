"""
Auto-provision a Paperless-ngx "space" for each DMv2 organization:
  group  <Org>   (permissions copied from TEMPLATE_GROUP, default "Despair")
  tag    <Org>   (no auto-matching; view+change for the group)
  storage path <Org>  -> "<Org>/{{ created_year }}/{{ title }}"
  workflow "<Org> - share with org"
      triggers: consumed from */<org>/*  OR  document added with tag <Org>
      action:   assign tag + storage path, view+change for the group
Idempotent: only creates what is missing. Never raises into the hook.
"""
import json, os, sys, urllib.request, urllib.parse, threading

PL_URL   = os.environ.get("PAPERLESS_API", "http://paperless-webserver:8000").rstrip("/")
PL_HOST  = os.environ.get("PAPERLESS_HOST", "localhost")   # must be in Paperless ALLOWED_HOSTS
PL_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
TEMPLATE = os.environ.get("TEMPLATE_GROUP", "Despair")
TIMEOUT  = float(os.environ.get("PAPERLESS_TIMEOUT", "4"))
ENABLED  = os.environ.get("AUTO_PROVISION", "1") == "1" and bool(PL_TOKEN)

_done = set()            # orgs fully provisioned in this process
_lock = threading.Lock()

def _log(*a):
    print(*a, file=sys.stderr, flush=True)

def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(PL_URL + path, data=data, method=method, headers={
        "Authorization": f"Token {PL_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Host": PL_HOST})
    with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None

def _find(endpoint, name):
    res = _req("GET", f"/api/{endpoint}/?name__iexact={urllib.parse.quote(name)}")
    items = (res or {}).get("results", [])
    return items[0] if items else None

def _perms(gid):
    return {"view": {"users": [], "groups": [gid]}, "change": {"users": [], "groups": [gid]}}

def _ensure_group(name):
    g = _find("groups", name)
    if g:
        return g["id"], False
    tpl = _find("groups", TEMPLATE)
    perms = (tpl or {}).get("permissions", [])
    g = _req("POST", "/api/groups/", {"name": name, "permissions": perms})
    _log(f"PROVISION group '{name}' created (id {g['id']}, {len(perms)} perms from '{TEMPLATE}')")
    return g["id"], True

def _ensure_tag(name, gid):
    t = _find("tags", name)
    if t:
        return t["id"]
    t = _req("POST", "/api/tags/", {"name": name, "matching_algorithm": 0, "set_permissions": _perms(gid)})
    _log(f"PROVISION tag '{name}' created (id {t['id']})")
    return t["id"]

def _ensure_storage_path(name, gid):
    s = _find("storage_paths", name)
    if s:
        return s["id"]
    s = _req("POST", "/api/storage_paths/", {
        "name": name, "path": f"{name}/{{{{ created_year }}}}/{{{{ title }}}}",
        "matching_algorithm": 0, "set_permissions": _perms(gid)})
    _log(f"PROVISION storage path '{name}' created (id {s['id']})")
    return s["id"]

def _ensure_workflow(name, gid, tag_id, sp_id):
    wf_name = f"{name} - share with org"
    res = _req("GET", "/api/workflows/?page_size=100")
    if any(w.get("name") == wf_name for w in (res or {}).get("results", [])):
        return
    slug = name.lower().replace(" ", "-")
    _req("POST", "/api/workflows/", {
        "name": wf_name, "order": 0, "enabled": True,
        "triggers": [
            {"type": 1, "sources": [1, 2, 3], "filter_path": f"*/{slug}/*"},   # consumption started
            {"type": 2, "sources": [1, 2, 3], "filter_has_tags": [tag_id]},   # document added
        ],
        "actions": [{"type": 1, "assign_tags": [tag_id], "assign_storage_path": sp_id,
                     "assign_view_groups": [gid], "assign_change_groups": [gid]}]})
    _log(f"PROVISION workflow '{wf_name}' created")

def ensure_orgs(names):
    """Make sure every org in `names` has its Paperless space. Safe to call on every login."""
    if not ENABLED:
        return
    for name in names:
        if name in _done:
            continue
        with _lock:
            if name in _done:
                continue
            try:
                gid, _ = _ensure_group(name)
                tag_id = _ensure_tag(name, gid)
                sp_id  = _ensure_storage_path(name, gid)
                _ensure_workflow(name, gid, tag_id, sp_id)
                _done.add(name)
            except Exception as e:
                # Never block the login; retry on the next login
                _log(f"PROVISION FAILED for '{name}': {e!r}")
