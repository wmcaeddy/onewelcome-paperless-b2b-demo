"""
Auto-share (lightweight): share each newly added document with its owner's
organization group(s) (view + change) and add the org tag.

Per tick (default every 10 s) it makes ONE small request:
  GET /api/documents/?ordering=-id&page_size=50&fields=id,owner,tags,permissions,title&full_perms=true
Users/groups/tags are only fetched when new documents exist, and cached for 5 min.
The last seen document id is persisted, so restarts don't rescan.
Org groups = Paperless groups that have a tag with the same name (spaces made by provision.py).
"""
import json, os, threading, time
import provision as P

INTERVAL   = float(os.environ.get("AUTOSHARE_INTERVAL", "10"))
MAP_TTL    = float(os.environ.get("AUTOSHARE_MAP_TTL", "300"))
STATE_FILE = os.environ.get("AUTOSHARE_STATE", "/data/autoshare-state.json")
LOOKBACK_H = int(os.environ.get("AUTOSHARE_LOOKBACK_HOURS", "24"))   # only used on very first start
ENABLED    = os.environ.get("AUTOSHARE", "1") == "1" and P.ENABLED
FIELDS     = "id,owner,tags,permissions,title"

_map = {"t": 0, "owner_orgs": {}, "org_name": {}, "tag_of": {}}

def _all(path):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        res = P._req("GET", f"{path}{sep}page={page}&page_size=100") or {}
        out += res.get("results", [])
        if not res.get("next"):
            return out
        page += 1

def _load_state():
    try:
        with open(STATE_FILE) as f:
            return int(json.load(f).get("last_id", 0))
    except Exception:
        return None

def _save_state(last_id):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_id": last_id}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        P._log(f"AUTOSHARE state write error: {e!r}")

def _initial_last_id():
    """First ever start: begin just before documents added in the last LOOKBACK_H hours."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - LOOKBACK_H * 3600))
    res = P._req("GET", f"/api/documents/?added__gt={since}&ordering=id&fields=id&page_size=1") or {}
    first = (res.get("results") or [{}])[0].get("id")
    if first:
        return first - 1
    res = P._req("GET", "/api/documents/?ordering=-id&fields=id&page_size=1") or {}
    return (res.get("results") or [{"id": 0}])[0]["id"]

def _refresh_map(force=False):
    if not force and time.time() - _map["t"] < MAP_TTL:
        return
    groups = {g["id"]: g["name"] for g in _all("/api/groups/")}
    tags   = {t["name"]: t["id"] for t in _all("/api/tags/")}
    org_name = {gid: n for gid, n in groups.items() if n in tags}
    owner_orgs = {}
    for u in _all("/api/users/"):
        gs = [g for g in u.get("groups", []) if g in org_name]
        if gs and not u.get("is_superuser"):
            owner_orgs[u["id"]] = gs
    _map.update(t=time.time(), owner_orgs=owner_orgs, org_name=org_name,
                tag_of={gid: tags[n] for gid, n in org_name.items()})

def _share(d, gs):
    perms = d.get("permissions") or {}
    hv = set((perms.get("view") or {}).get("groups", []))
    hc = set((perms.get("change") or {}).get("groups", []))
    need_perms = any(g not in hv or g not in hc for g in gs)
    missing_tags = [_map["tag_of"][g] for g in gs if _map["tag_of"].get(g) not in d.get("tags", [])]
    if not need_perms and not missing_tags:
        return
    if need_perms:
        P._req("POST", "/api/documents/bulk_edit/", {
            "documents": [d["id"]], "method": "set_permissions",
            "parameters": {"set_permissions": {"view": {"users": [], "groups": gs},
                                               "change": {"users": [], "groups": gs}},
                           "merge": True}})
    if missing_tags:
        P._req("POST", "/api/documents/bulk_edit/", {
            "documents": [d["id"]], "method": "modify_tags",
            "parameters": {"add_tags": missing_tags, "remove_tags": []}})
    P._log(f"AUTOSHARE doc {d['id']} '{d.get('title','')}' -> {[_map['org_name'][g] for g in gs]}")

def tick(last_id):
    res = P._req("GET", f"/api/documents/?id__gt={last_id}&ordering=-id&page_size=50"
                        f"&full_perms=true&fields={FIELDS}") or {}
    docs = [d for d in res.get("results", []) if d["id"] > last_id]
    if not docs:
        return last_id                                   # the common case: one tiny request, done
    _refresh_map()
    for d in reversed(docs):
        gs = _map["owner_orgs"].get(d.get("owner"))
        if gs is None and d.get("owner") is not None:    # brand-new user? refresh once
            _refresh_map(force=True)
            gs = _map["owner_orgs"].get(d.get("owner"))
        if gs:
            _share(d, gs)
        last_id = max(last_id, d["id"])
    _save_state(last_id)
    return last_id

def _loop():
    last_id = _load_state()
    while last_id is None:
        try:
            last_id = _initial_last_id(); _save_state(last_id)
        except Exception as e:
            P._log(f"AUTOSHARE init failed: {e!r}"); time.sleep(INTERVAL)
    P._log(f"autoshare running every {INTERVAL:g}s from document id > {last_id}")
    while True:
        try:
            last_id = tick(last_id)
        except Exception as e:
            P._log(f"AUTOSHARE FAILED: {e!r}")
        time.sleep(INTERVAL)

def start():
    if ENABLED:
        threading.Thread(target=_loop, daemon=True).start()
