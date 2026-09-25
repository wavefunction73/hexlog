#!/usr/bin/env python3
"""
rustlog - pull BattleMetrics data for one Rust server and build clan timelines.

Commands
  python rustlog.py init                 create config.json to fill in
  python rustlog.py server               server name, status, last wipe
  python rustlog.py leaderboard          playtime ranking for this wipe
  python rustlog.py find NAME [NAME...]  look up BattleMetrics player IDs by name
  python rustlog.py timeline             build output/timeline.html + summary
  python rustlog.py demo                 build a timeline from fake data (no API)

Common options (most commands)
  --since ISO|auto    window start (default: config "wipe_start", "auto" = last wipe)
  --until ISO         window end (default: now)

Only uses the Python standard library (Python 3.8+).
"""
import argparse
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.battlemetrics.com"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
OUT = HERE / "output"
TEMPLATE_PATH = HERE / "timeline_template.html"
UTC = timezone.utc

DEFAULT_CONFIG = {
    "server_id": "20839213",
    "api_token": "",
    "wipe_start": "auto",
    "clan": [{"name": f"Member{i}", "bm_id": ""} for i in range(1, 9)],
}


# --------------------------------------------------------------------------- utils

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def local(dt):
    return dt.astimezone()  # system timezone, DST-aware


def fmt_local(dt):
    return local(dt).strftime("%a %d %b %H:%M")


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_config():
    if not CONFIG_PATH.exists():
        die("config.json not found. Run: python rustlog.py init")
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"config.json is not valid JSON: {e}")


def token_from(cfg):
    # BM_TOKEN environment variable wins, so the token never has to sit in config.json
    return os.environ.get("BM_TOKEN") or cfg.get("api_token", "")


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- API

class BattleMetrics:
    def __init__(self, token=""):
        self.token = token.strip()
        # Unauthenticated limit is lower, so go slower without a token.
        self.delay = 0.25 if self.token else 1.1

    def get(self, path, params=None):
        url = path if path.startswith("http") else API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "rustlog/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        for attempt in range(6):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                time.sleep(self.delay)
                return data
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After") or 10)
                    print(f"  rate limited, waiting {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                if e.code in (401, 403):
                    die(f"BattleMetrics refused the request ({e.code}). "
                        "This endpoint needs an API token: add it as \"api_token\" "
                        "in config.json (see README).\n  " + body)
                if e.code >= 500 and attempt < 5:
                    time.sleep(3)
                    continue
                die(f"HTTP {e.code} for {url}\n  {body}")
            except urllib.error.URLError as e:
                if attempt < 5:
                    time.sleep(3)
                    continue
                die(f"network error: {e.reason}")
        die("gave up after repeated rate limits")

    def paged(self, path, params, max_pages=50):
        data = self.get(path, params)
        pages = 1
        while True:
            for item in data.get("data", []):
                yield item
            nxt = (data.get("links") or {}).get("next")
            if not nxt or pages >= max_pages:
                return
            data = self.get(nxt)
            pages += 1

    # --- endpoints

    def server(self, sid):
        return self.get(f"/servers/{sid}")["data"]

    def leaderboard(self, sid, start, end, max_pages=10):
        params = {"filter[period]": f"{iso(start)}:{iso(end)}", "page[size]": 100}
        rows = []
        for item in self.paged(f"/servers/{sid}/relationships/leaderboards/time",
                               params, max_pages=max_pages):
            a = item.get("attributes", {})
            rows.append({"id": str(item.get("id")), "name": a.get("name", "?"),
                         "seconds": a.get("value", 0), "rank": a.get("rank")})
        return rows

    def search_players(self, name, sid):
        params = {"filter[search]": name, "filter[servers]": sid, "page[size]": 10}
        return self.get("/players", params).get("data", [])

    def sessions(self, pid, sid, since, until):
        """Sessions for one player on one server, clipped to [since, until]."""
        params = {"filter[servers]": sid, "page[size]": 100}
        out = []
        now = datetime.now(UTC)
        for item in self.paged(f"/players/{pid}/relationships/sessions", params):
            a = item.get("attributes", {})
            rel = (((item.get("relationships") or {}).get("server") or {})
                   .get("data") or {}).get("id")
            if rel and str(rel) != str(sid):
                continue
            start = parse_ts(a["start"])
            stop = parse_ts(a["stop"]) if a.get("stop") else None
            end_eff = stop or now
            if end_eff < since:
                break  # results are newest first; everything after is older
            if start > until:
                continue
            out.append({
                "start": max(start, since),
                "stop": min(end_eff, until),
                "ongoing": stop is None,
                "name": a.get("name", ""),
            })
        out.sort(key=lambda s: s["start"])
        return out


# --------------------------------------------------------------------------- window

def resolve_window(args, cfg, bm, server=None):
    until = parse_ts(args.until) if getattr(args, "until", None) else datetime.now(UTC)
    since_raw = getattr(args, "since", None) or cfg.get("wipe_start") or "auto"
    if since_raw == "auto":
        server = server or bm.server(cfg["server_id"])
        wipe = (server["attributes"].get("details") or {}).get("rust_last_wipe")
        if not wipe:
            die("server doesn't report a last wipe time. Use --since 2026-09-18T18:00:00Z")
        since = parse_ts(wipe)
    else:
        since = parse_ts(since_raw)
    if since >= until:
        die("window start is after window end")
    return since, until, server


# --------------------------------------------------------------------------- analysis

def analyse(members, since, until):
    """Per-minute occupancy, hourly patterns, overlap stats."""
    total_min = int((until - since).total_seconds() // 60) + 1
    counts = [0] * total_min
    days = max((until - since).total_seconds() / 86400, 1e-9)

    for m in members:
        online = bytearray(total_min)
        for s in m["sessions"]:
            a = int((s["start"] - since).total_seconds() // 60)
            b = int((s["stop"] - since).total_seconds() // 60)
            for i in range(max(a, 0), min(b, total_min - 1) + 1):
                online[i] = 1
        for i, v in enumerate(online):
            counts[i] += v
        # hour-of-day pattern: on how many distinct days was the member on in that hour
        buckets = set()
        for i in range(0, total_min, 1):
            if online[i]:
                t = local(since + timedelta(minutes=i))
                buckets.add((t.date(), t.hour))
        per_hour = [0] * 24
        for _, h in buckets:
            per_hour[h] += 1
        m["hourly"] = [min(v / days, 1.0) for v in per_hour]
        m["minutes"] = sum(online)

    # average members online by local hour of day
    hour_sum = [0.0] * 24
    hour_n = [0] * 24
    for i in range(0, total_min, 5):
        h = local(since + timedelta(minutes=i)).hour
        hour_sum[h] += counts[i]
        hour_n[h] += 1
    avg_by_hour = [hour_sum[h] / hour_n[h] if hour_n[h] else 0 for h in range(24)]

    overlap = {k: sum(1 for c in counts if c >= k) for k in range(1, len(members) + 1)}
    return {"counts": counts, "avg_by_hour": avg_by_hour, "overlap_min": overlap}


def summary_text(server_name, sid, since, until, members, stats):
    L = []
    days = (until - since).total_seconds() / 86400
    tzname = local(until).strftime("%Z") or "local"
    L.append(f"SERVER  {server_name} ({sid})")
    L.append(f"WINDOW  {fmt_local(since)} -> {fmt_local(until)}  [{tzname}, {days:.1f} days]")
    L.append("")
    w = max([len(m["label"]) for m in members] + [6])
    L.append(f"{'PLAYER':<{w}}  {'HOURS':>6}  {'SESS':>4}  {'FIRST SEEN':<16}  {'LAST SEEN':<16}  NOW")
    for m in sorted(members, key=lambda m: -m["minutes"]):
        ss = m["sessions"]
        first = fmt_local(ss[0]["start"]) if ss else "-"
        last = fmt_local(ss[-1]["stop"]) if ss else "-"
        now = "ONLINE" if ss and ss[-1]["ongoing"] else ""
        L.append(f"{m['label']:<{w}}  {m['minutes']/60:>6.1f}  {len(ss):>4}  {first:<16}  {last:<16}  {now}")
    L.append("")
    L.append("HOURLY PATTERN (local time; 0-9 = share of days online in that hour, . = never)")
    L.append(f"{'':<{w}}  " + "".join(str(h // 10) for h in range(24)))
    L.append(f"{'':<{w}}  " + "".join(str(h % 10) for h in range(24)))
    for m in members:
        row = "".join("." if v == 0 else str(min(9, max(1, round(v * 9)))) for v in m["hourly"])
        L.append(f"{m['label']:<{w}}  {row}")
    L.append("")
    L.append("CLAN OVERLAP (total time with at least N members online)")
    for k, mins in stats["overlap_min"].items():
        if mins:
            L.append(f"  {k}+ online: {mins/60:6.1f} h")
    best = sorted(range(24), key=lambda h: -stats["avg_by_hour"][h])[:5]
    L.append("BUSIEST HOURS (avg members online): " +
             ", ".join(f"{h:02d}:00 ({stats['avg_by_hour'][h]:.1f})" for h in best))
    quiet = sorted(range(24), key=lambda h: stats["avg_by_hour"][h])[:5]
    L.append("QUIETEST HOURS: " +
             ", ".join(f"{h:02d}:00 ({stats['avg_by_hour'][h]:.1f})" for h in quiet))
    return "\n".join(L)


# --------------------------------------------------------------------------- output

def write_outputs(server_name, sid, since, until, members):
    OUT.mkdir(exist_ok=True)
    stats = analyse(members, since, until)

    with open(OUT / "sessions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["member", "bm_id", "in_game_name", "start_local", "stop_local",
                    "minutes", "ongoing"])
        for m in members:
            for s in m["sessions"]:
                w.writerow([m["label"], m["id"], s["name"],
                            local(s["start"]).isoformat(timespec="minutes"),
                            local(s["stop"]).isoformat(timespec="minutes"),
                            round((s["stop"] - s["start"]).total_seconds() / 60),
                            s["ongoing"]])

    payload = {
        "server": server_name, "serverId": sid,
        "start": int(since.timestamp() * 1000), "end": int(until.timestamp() * 1000),
        "generated": int(datetime.now(UTC).timestamp() * 1000),
        "members": [{
            "name": m["label"], "id": m["id"], "minutes": m["minutes"],
            "sessions": [[int(s["start"].timestamp() * 1000),
                          int(s["stop"].timestamp() * 1000),
                          1 if s["ongoing"] else 0, s["name"]] for s in m["sessions"]],
        } for m in members],
    }
    (OUT / "sessions.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")

    if not TEMPLATE_PATH.exists():
        die("timeline_template.html is missing; keep it next to rustlog.py")
    html = TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(payload))
    (OUT / "timeline.html").write_text(html, encoding="utf-8")

    text = summary_text(server_name, sid, since, until, members, stats)
    (OUT / "summary.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"\nWrote {OUT / 'timeline.html'}")
    print(f"      {OUT / 'sessions.csv'}")
    print(f"      {OUT / 'summary.txt'}  (paste this to Claude)")


# --------------------------------------------------------------------------- commands

def cmd_init(args):
    if CONFIG_PATH.exists() and not args.force:
        die("config.json already exists (use --force to overwrite)")
    save_config(DEFAULT_CONFIG)
    print(f"Created {CONFIG_PATH}. Fill in your clan names, then run: python rustlog.py find <name>")


def cmd_server(args):
    cfg = load_config()
    bm = BattleMetrics(token_from(cfg))
    s = bm.server(cfg["server_id"])
    a = s["attributes"]
    d = a.get("details") or {}
    print(f"{a.get('name')}")
    print(f"  status   {a.get('status')}  players {a.get('players')}/{a.get('maxPlayers')}")
    print(f"  address  {a.get('ip')}:{a.get('port')}")
    if d.get("rust_last_wipe"):
        print(f"  wiped    {fmt_local(parse_ts(d['rust_last_wipe']))} (local)")
    if d.get("rust_next_wipe"):
        print(f"  next     {fmt_local(parse_ts(d['rust_next_wipe']))} (local)")
    if d.get("map"):
        print(f"  map      {d.get('map')}  size {d.get('rust_world_size', '?')}  seed {d.get('rust_world_seed', '?')}")


def cmd_leaderboard(args):
    cfg = load_config()
    bm = BattleMetrics(token_from(cfg))
    since, until, _ = resolve_window(args, cfg, bm)
    rows = bm.leaderboard(cfg["server_id"], since, until,
                          max_pages=max(1, (args.top + 99) // 100))
    clan_ids = {str(m.get("bm_id")) for m in cfg.get("clan", []) if m.get("bm_id")}
    print(f"Playtime {fmt_local(since)} -> {fmt_local(until)}  (* = clan)")
    for r in rows[:args.top]:
        mark = "*" if r["id"] in clan_ids else " "
        print(f"{mark}{r['rank']:>4}  {r['seconds']/3600:6.1f} h  {r['name']:<32} id {r['id']}")


def cmd_find(args):
    cfg = load_config()
    bm = BattleMetrics(token_from(cfg))
    since, until, _ = resolve_window(args, cfg, bm)
    print("Searching this wipe's leaderboard...")
    board = bm.leaderboard(cfg["server_id"], since, until, max_pages=10)
    for name in args.names:
        q = name.lower()
        hits = [r for r in board if q in r["name"].lower()]
        print(f"\n'{name}':")
        for r in hits[:10]:
            print(f"  id {r['id']:<12} {r['name']:<32} {r['seconds']/3600:.1f} h this wipe")
        if not hits:
            print("  not on this wipe's leaderboard")
        if bm.token:
            try:
                for p in bm.search_players(name, cfg["server_id"]):
                    if not any(h["id"] == str(p["id"]) for h in hits):
                        print(f"  id {p['id']:<12} {p['attributes'].get('name'):<32} (player search)")
            except SystemExit:
                pass
    print("\nPut the right id into \"bm_id\" for each member in config.json.")


def cmd_timeline(args):
    cfg = load_config()
    bm = BattleMetrics(token_from(cfg))
    if not bm.token:
        print("note: no api_token set; session history usually needs one (see README).",
              file=sys.stderr)
    sid = cfg["server_id"]
    since, until, server = resolve_window(args, cfg, bm)
    server = server or bm.server(sid)
    server_name = server["attributes"].get("name", sid)

    clan = cfg.get("clan", [])
    missing = [m for m in clan if not m.get("bm_id")]
    board = {}
    if missing:
        print("Some members have no bm_id; matching names on the wipe leaderboard...")
        for r in bm.leaderboard(sid, since, until, max_pages=10):
            board.setdefault(r["name"].lower(), r["id"])

    members = []
    for m in clan:
        pid = str(m.get("bm_id") or board.get(m["name"].lower(), ""))
        if not pid:
            print(f"  skipping {m['name']}: no bm_id and no exact name match "
                  f"(try: python rustlog.py find \"{m['name']}\")")
            continue
        print(f"  fetching sessions for {m['name']} ({pid})...")
        members.append({"label": m["name"], "id": pid,
                        "sessions": bm.sessions(pid, sid, since, until)})
    if not members:
        die("no clan members to fetch")
    print()
    write_outputs(server_name, sid, since, until, members)


def cmd_demo(args):
    random.seed(7)
    until = datetime.now(UTC).replace(second=0, microsecond=0)
    since = until - timedelta(days=7, hours=5)
    names = ["Ferris", "Sully", "Knox", "Moth", "Pike", "Ringo", "Vex", "Wren"]
    members = []
    for i, n in enumerate(names):
        sessions, t = [], since + timedelta(minutes=random.randint(0, 300))
        while t < until:
            dur = timedelta(minutes=random.randint(30, 400))
            stop = min(t + dur, until)
            sessions.append({"start": t, "stop": stop,
                             "ongoing": stop == until and i % 3 == 0, "name": n})
            t = stop + timedelta(minutes=random.randint(120, 1200))
        members.append({"label": n, "id": str(1000 + i), "sessions": sessions})
    write_outputs("Demo server (fake data)", "0", since, until, members)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def window(sp):
        sp.add_argument("--since", help="ISO time or 'auto' (last wipe)")
        sp.add_argument("--until", help="ISO time (default now)")

    sp = sub.add_parser("init"); sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_init)
    sub.add_parser("server").set_defaults(fn=cmd_server)
    sp = sub.add_parser("leaderboard"); window(sp)
    sp.add_argument("--top", type=int, default=50); sp.set_defaults(fn=cmd_leaderboard)
    sp = sub.add_parser("find"); window(sp)
    sp.add_argument("names", nargs="+"); sp.set_defaults(fn=cmd_find)
    sp = sub.add_parser("timeline"); window(sp); sp.set_defaults(fn=cmd_timeline)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
