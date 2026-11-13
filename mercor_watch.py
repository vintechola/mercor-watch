#!/usr/bin/env python3
"""
Mercor job watcher -> Discord / Telegram alerts.

Detects:
  NEW       a listing ID never seen before
  REOPENED  a listing ID that had closed (vanished) and is live again
  REPOSTED  a new listing ID whose title matches a role that previously closed
  (optional) CLOSED  a live listing that disappeared  (NOTIFY_CLOSED=1)

Stdlib only - no pip installs needed. Python 3.8+.

Config (environment variables):
  DISCORD_WEBHOOK_URL   Discord channel webhook URL           (optional)
  TELEGRAM_BOT_TOKEN    Telegram bot token from @BotFather     (optional)
  TELEGRAM_CHAT_ID      your chat / group / channel id         (optional)
  KEYWORDS              comma list; only alert if title/desc matches (optional)
  MIN_RATE              only alert if max hourly rate >= this  (optional)
  MENTION               who to ping on Discord (default @everyone; "none" = no ping)
  NOTIFY_CLOSED         "1" to also alert when roles close     (default off)
  MERCOR_API_URL        override the listings endpoint         (optional)
  STATE_FILE            path for memory file (default state.json)
  CLOSE_AFTER_MISSES    polls a role must be missing before it counts as closed (default 2)
  POLL_SECONDS          interval when run with --loop (default 300)

Usage:
  python mercor_watch.py            # one check (for cron / GitHub Actions)
  python mercor_watch.py --loop     # keep running, check every POLL_SECONDS
  python mercor_watch.py --test     # send a test message to your channels
  python mercor_watch.py --dump     # print what the API returned (debugging)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Endpoints tried in order. Mercor's explore page calls its "listings-explore-page"
# API on aws.api.mercor.com. If Mercor changes the path, set MERCOR_API_URL
# (see README: "If the bot finds 0 listings").
API_CANDIDATES = [
    "https://aws.api.mercor.com/work/listings-explore-page",
    "https://aws.api.mercor.com/listings-explore-page",
    "https://aws.api.mercor.com/work/listings/explore-page",
]
EXPLORE_PAGE = "https://work.mercor.com/explore"
JOB_URL = "https://work.mercor.com/jobs/{id}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

STATE_FILE = os.getenv("STATE_FILE", "state.json")
SLOTS_UP_MIN = int(os.getenv("SLOTS_UP_MIN") or 3)   # slot increase that counts as "slots added"
LOW_SLOTS = int(os.getenv("LOW_SLOTS") or 5)         # "almost full" threshold
CLOSE_AFTER = int(os.getenv("CLOSE_AFTER_MISSES", "2"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- fetch
def http_get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": accept,
        "Origin": "https://work.mercor.com", "Referer": EXPLORE_PAGE})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def _looks_like_listing(d):
    if not isinstance(d, dict):
        return False
    lid = _first(d, "listingId", "listing_id", "id")
    return isinstance(lid, str) and lid.startswith("list_")


def _walk(obj, out):
    """Find every dict anywhere in the JSON that looks like a Mercor listing."""
    if isinstance(obj, dict):
        if _looks_like_listing(obj):
            out.append(obj)
            return
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def normalize(raw):
    lid = _first(raw, "listingId", "listing_id", "id")
    title = _first(raw, "title", "jobTitle", "listingTitle", "name", "roleTitle") or "Untitled role"
    rmin = _first(raw, "rateMin", "hourlyRateMin", "payRateMin", "minRate", "salaryMin")
    rmax = _first(raw, "rateMax", "hourlyRateMax", "payRateMax", "maxRate", "salaryMax")
    freq = _first(raw, "payRateFrequency", "rateFrequency", "rateType") or "hourly"
    desc = _first(raw, "description", "shortDescription", "summary") or ""
    return {
        "id": lid,
        "title": str(title).strip(),
        "rate_min": _num(rmin),
        "rate_max": _num(rmax),
        "rate_freq": str(freq),
        "commitment": _first(raw, "commitment", "commitmentType", "employmentType", "type"),
        "location": _first(raw, "location", "locationText", "locations"),
        "category": _first(raw, "listingDomain", "category", "domain"),
        "posted": _first(raw, "postedAt", "createdAt", "posted_at", "postedDate"),
        "desc": desc if isinstance(desc, str) else "",
        "slots": find_slots(raw),
    }


SLOT_KEYS = ("slotsLeft", "slotsRemaining", "remainingSlots", "slots_left", "slots_remaining",
             "openSlots", "availableSlots", "spotsLeft", "spotsRemaining", "remainingSpots",
             "openings", "numOpenings", "openingsRemaining", "remainingOpenings", "slots",
             "numSlots", "headcount", "remainingHeadcount", "capacity")
_SLOT_RE = re.compile(r"slot|spot|opening|remaining|headcount|capacity", re.I)


def find_slots(raw):
    """Mercor's slots-left field name isn't documented, so try known names first,
    then any numeric field whose name looks slot-like."""
    for k in SLOT_KEYS:
        v = _num(raw.get(k))
        if v is not None:
            return int(v)
    for k, v in raw.items():
        if _SLOT_RE.search(k) and not isinstance(v, bool) and _num(v) is not None:
            return int(_num(v))
    return None


def ago(ts):
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        d = (datetime.now(timezone.utc) - t).days
        return "today" if d < 1 else f"{d}d ago"
    except ValueError:
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_listings():
    """Return {listing_id: listing}. Raises if nothing could be fetched."""
    urls = [os.getenv("MERCOR_API_URL")] if os.getenv("MERCOR_API_URL") else API_CANDIDATES
    errors = []
    for url in urls:
        try:
            data = json.loads(http_get(url))
            found = []
            _walk(data, found)
            if found:
                return {l["id"]: l for l in map(normalize, found)}
            errors.append(f"{url}: JSON had no listings")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")

    # Fallback: pull listing IDs out of the explore page HTML (titles may be missing).
    try:
        html = http_get(EXPLORE_PAGE, accept="text/html")
        found = []
        for m in re.finditer(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S):
            try:
                _walk(json.loads(m.group(1)), found)
            except ValueError:
                pass
        if found:
            return {l["id"]: l for l in map(normalize, found)}
        ids = sorted(set(re.findall(r"list_[A-Za-z0-9_-]{10,}", html)))
        if ids:
            return {i: normalize({"listingId": i}) for i in ids}
        errors.append(f"{EXPLORE_PAGE}: no listing IDs in HTML")
    except Exception as e:  # noqa: BLE001
        errors.append(f"{EXPLORE_PAGE}: {e}")
    raise RuntimeError("Could not fetch Mercor listings:\n  " + "\n  ".join(errors))


# --------------------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"listings": {}, "initialized": False}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def diff(state, live):
    """Update state in place; return list of (event, listing) tuples."""
    known = state["listings"]
    events = []
    ts = now_iso()

    # Sanity guard: if the API suddenly returns far fewer roles, treat it as a glitch
    # and don't mark anything closed this round.
    prev_active = sum(1 for v in known.values() if v["active"])
    glitch = prev_active >= 10 and len(live) < prev_active * 0.5

    closed_titles = {norm_title(v["title"]): k for k, v in known.items() if not v["active"]}

    for lid, l in live.items():
        rec = known.get(lid)
        if rec is None:
            event = "NEW"
            if norm_title(l["title"]) in closed_titles and l["title"] != "Untitled role":
                event = "REPOSTED"
            known[lid] = {**l, "first_seen": ts, "last_seen": ts, "active": True,
                          "misses": 0, "reopen_count": 0, "closed_at": None}
            events.append((event, known[lid]))
        else:
            was_closed = not rec["active"]
            old_slots, old_pay = rec.get("slots"), rec.get("rate_max") or rec.get("rate_min")
            rec.update({k: v for k, v in l.items() if v not in (None, "")})
            rec["last_seen"], rec["misses"] = ts, 0
            if was_closed:
                rec["active"] = True
                rec["reopen_count"] = rec.get("reopen_count", 0) + 1
                rec["low_alerted"] = False
                events.append(("REOPENED", rec))
                rec["closed_at"] = None
                continue
            new_slots, new_pay = l.get("slots"), l.get("rate_max") or l.get("rate_min")
            # Slots refilled: Mercor added capacity (or a full role came back to life)
            if old_slots is not None and new_slots is not None and new_slots > old_slots and (
                    old_slots == 0 or new_slots - old_slots >= SLOTS_UP_MIN):
                rec["slots_before"] = old_slots
                rec["low_alerted"] = False
                events.append(("SLOTS_ADDED", rec))
            # Almost full: crossed below the threshold (alert once per fill)
            elif (new_slots is not None and 0 < new_slots <= LOW_SLOTS
                  and not rec.get("low_alerted")
                  and (old_slots is None or old_slots > LOW_SLOTS)):
                rec["low_alerted"] = True
                events.append(("ALMOST_FULL", rec))
            # Pay went up
            if old_pay is not None and new_pay is not None and new_pay > old_pay:
                rec["pay_before"] = old_pay
                events.append(("PAY_UP", rec))
            # Track slot history so we can show how fast a role is filling
            if new_slots is not None and new_slots != old_slots:
                hist = rec.setdefault("slot_hist", [])
                hist.append([ts, new_slots])
                del hist[:-20]

    if not glitch:
        for lid, rec in known.items():
            if rec["active"] and lid not in live:
                rec["misses"] = rec.get("misses", 0) + 1
                if rec["misses"] >= CLOSE_AFTER:
                    rec["active"] = False
                    rec["closed_at"] = ts
                    events.append(("CLOSED", rec))
    return events


# --------------------------------------------------------------------------- filters
def passes_filters(l):
    kws = [k.strip().lower() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
    if kws:
        hay = (l.get("title", "") + " " + l.get("desc", "")).lower()
        if not any(k in hay for k in kws):
            return False
    min_rate = _num(os.getenv("MIN_RATE"))
    if min_rate is not None:
        top = l.get("rate_max") or l.get("rate_min")
        if top is not None and top < min_rate:
            return False
    return True


# --------------------------------------------------------------------------- notify
LABEL = {"NEW": "🆕 New role", "REOPENED": "🔄 Reopened", "REPOSTED": "♻️ Reposted",
         "CLOSED": "🔒 Closed", "SLOTS_ADDED": "➕ Slots added", "ALMOST_FULL": "⏳ Almost full",
         "PAY_UP": "📈 Pay increased"}
COLOR = {"NEW": 0x2ECC71, "REOPENED": 0xF1C40F, "REPOSTED": 0x3498DB, "CLOSED": 0x95A5A6,
         "SLOTS_ADDED": 0x1ABC9C, "ALMOST_FULL": 0xE74C3C, "PAY_UP": 0x9B59B6}


def rate_str(l):
    a, b = l.get("rate_min"), l.get("rate_max")
    if a is None and b is None:
        return None
    fmt = lambda x: f"${x:,.0f}" if x == int(x) else f"${x:,.2f}"  # noqa: E731
    per = "/hr" if "hour" in str(l.get("rate_freq", "")).lower() else f" ({l.get('rate_freq')})"
    if a is not None and b is not None and a != b:
        return f"{fmt(a)}–{fmt(b)}{per}"
    return f"{fmt(a if a is not None else b)}{per}"


def details(l):
    parts = []
    if rate_str(l):
        parts.append(f"💰 {rate_str(l)}")
    if l.get("commitment"):
        parts.append(f"⏱ {l['commitment']}")
    loc = l.get("location")
    if loc:
        parts.append(f"📍 {', '.join(loc) if isinstance(loc, list) else loc}")
    if l.get("category"):
        parts.append(f"🏷 {l['category']}")
    if l.get("slots") is not None:
        parts.append(f"👥 {l['slots']} slots left")
    return parts


def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited
                time.sleep(float(e.headers.get("Retry-After", 2)) + 0.5)
                continue
            print(f"  notify failed ({e.code}): {e.read()[:200]!r}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            print(f"  notify error: {e}", file=sys.stderr)
            time.sleep(2)


EMOJI = {"NEW": "🆕", "REOPENED": "🔄", "REPOSTED": "♻️", "CLOSED": "🔒",
         "SLOTS_ADDED": "➕", "ALMOST_FULL": "⏳", "PAY_UP": "📈"}
HEADLINE = {"NEW": "New Mercor Role!", "REOPENED": "Mercor Role Reopened!",
            "REPOSTED": "Mercor Role Reposted!", "CLOSED": "Mercor Role Closed",
            "SLOTS_ADDED": "More Slots Just Opened!", "ALMOST_FULL": "Almost Full — Apply Now!",
            "PAY_UP": "Pay Went Up!"}


def status_line(ev, l):
    if ev == "REOPENED":
        s = f"🔄 **Reopened Job (v{l.get('reopen_count', 1) + 1})**"
    elif ev == "REPOSTED":
        s = "♻️ **Reposted Job** (new link, same title)"
    elif ev == "CLOSED":
        s = "🔒 **Closed**"
    elif ev == "SLOTS_ADDED":
        s = f"➕ **Slots added: {l.get('slots_before')} → {l.get('slots')}**"
    elif ev == "ALMOST_FULL":
        s = f"⏳ **Only {l.get('slots')} slots left**"
    elif ev == "PAY_UP":
        s = f"📈 **Pay up: ${l.get('pay_before'):,.0f} → ${(l.get('rate_max') or l.get('rate_min')):,.0f}**"
    else:
        s = "🆕 **New Job**"
    when = ago(l.get("posted"))
    if when and ev != "NEW":
        s += f" • *Originally posted {when}*"
    elif when:
        s += f" • *Posted {when}*"
    return s


def fill_speed(l):
    """e.g. 'filling ~6/hr' from recent slot history."""
    h = l.get("slot_hist") or []
    if len(h) < 2:
        return None
    try:
        t0 = datetime.fromisoformat(h[0][0].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(h[-1][0].replace("Z", "+00:00"))
    except ValueError:
        return None
    hrs = (t1 - t0).total_seconds() / 3600
    drop = h[0][1] - h[-1][1]
    if hrs <= 0 or drop <= 0:
        return None
    rate = drop / hrs
    eta = l["slots"] / rate if rate else None
    out = f"filling ~{rate:.0f}/hr" if rate >= 1 else f"filling ~{rate * 24:.0f}/day"
    if eta is not None and eta < 48:
        out += f", full in ~{eta:.0f}h" if eta >= 1 else ", full within the hour"
    return out


def mention_for(l):
    """DOMAIN_MENTIONS='engineering=<@&111>;finance=<@&222>' pings a role per domain."""
    rules = os.getenv("DOMAIN_MENTIONS", "")
    hay = f"{l.get('category', '')} {l.get('title', '')}".lower()
    for part in rules.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and k.strip().lower() in hay:
                return v.strip()
    m = (os.getenv("MENTION") or "@everyone").strip()
    return "" if m.lower() == "none" else m


QUIET_EVENTS = {"CLOSED"}  # these post without pinging


def discord_embed(ev, l):
    url = JOB_URL.format(id=l["id"])
    fields = [{"name": "📊 Status", "value": status_line(ev, l), "inline": False}]
    if rate_str(l):
        fields.append({"name": "💰 Pay", "value": rate_str(l), "inline": True})
    if l.get("commitment"):
        fields.append({"name": "⏱️ Commitment", "value": str(l["commitment"]), "inline": True})
    if l.get("category"):
        fields.append({"name": "🏷️ Domain", "value": str(l["category"]), "inline": True})
    loc = l.get("location")
    if loc:
        fields.append({"name": "📍 Location",
                       "value": ", ".join(loc) if isinstance(loc, list) else str(loc), "inline": True})
    if l.get("slots") is not None:
        v = str(l["slots"])
        if 0 < l["slots"] <= 10:
            v += " 🔥"
        speed = fill_speed(l)
        if speed:
            v += f"\n*{speed}*"
        fields.append({"name": "👥 Slots Left", "value": v, "inline": True})
    if l.get("reopen_count", 0) >= 1 and ev != "REOPENED":
        fields.append({"name": "🔁 History", "value": f"Reopened {l['reopen_count']}× before",
                       "inline": True})
    return {
        "title": f"{EMOJI[ev]} {l['title']}"[:256],
        "url": url,
        "description": f"👉 **[View Job & Apply on Mercor]({url})**",
        "color": COLOR[ev],
        "fields": [{**f, "value": f["value"][:1024]} for f in fields],
        "footer": {"text": f"Listing ID: {l['id']}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_discord(events):
    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if not hook:
        return
    for ev, l in events:  # one message per role so every alert pings
        mention = mention_for(l) if ev not in QUIET_EVENTS else ""
        payload = {
            "username": "Mercor Watch",
            "content": f"{mention} {EMOJI[ev]} **{HEADLINE[ev]}**".strip(),
            "embeds": [discord_embed(ev, l)],
            "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
        }
        post_json(hook, payload)
        time.sleep(1)


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(events):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for ev, l in events:
        lines = [f"<b>{LABEL[ev]}</b>",
                 f'<a href="{JOB_URL.format(id=l["id"])}">{_esc(l["title"])}</a>']
        lines += [_esc(d) for d in details(l)]
        post_json(url, {"chat_id": chat, "text": "\n".join(lines),
                        "parse_mode": "HTML", "disable_web_page_preview": True})
        time.sleep(0.5)


def notify(events):
    if not (os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("TELEGRAM_BOT_TOKEN")):
        print("  (no DISCORD_WEBHOOK_URL / TELEGRAM_BOT_TOKEN set - printing only)")
    send_discord(events)
    send_telegram(events)


# --------------------------------------------------------------------------- main
def run_once():
    state = load_state()
    live = fetch_listings()
    print(f"[{now_iso()}] fetched {len(live)} live listings")
    events = diff(state, live)

    if not state.get("initialized"):
        state["initialized"] = True
        save_state(state)
        print(f"  first run: remembered {len(live)} existing roles (no alerts sent)")
        return

    off = {x.strip().upper() for x in (os.getenv("MUTE_EVENTS") or "").split(",") if x.strip()}
    if os.getenv("NOTIFY_CLOSED") != "1":
        off.add("CLOSED")
    want = [(e, l) for e, l in events if e not in off and passes_filters(l)]
    for e, l in events:
        print(f"  {e:9} {l['id']}  {l['title']}")
    if want:
        notify(want)
    maybe_digest(state, live)
    save_state(state)


def maybe_digest(state, live):
    """Once a day at DIGEST_HOUR (Lagos time, UTC+1) post a summary to Discord."""
    hour = os.getenv("DIGEST_HOUR")
    hook = os.getenv("DISCORD_WEBHOOK_URL")
    if hour in (None, "") or not hook:
        return
    lagos = datetime.now(timezone.utc).timestamp() + 3600
    local = datetime.fromtimestamp(lagos, timezone.utc)
    today = local.strftime("%Y-%m-%d")
    if local.hour < int(hour) or state.get("last_digest") == today:
        return
    state["last_digest"] = today
    known = state["listings"]
    cutoff = datetime.now(timezone.utc).timestamp() - 86400

    def recent(ts):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() >= cutoff
        except (AttributeError, ValueError):
            return False
    new24 = [v for v in known.values() if recent(v.get("first_seen"))]
    open_ = [v for v in known.values() if v["active"] and passes_filters(v)]
    top = sorted(open_, key=lambda v: -(v.get("rate_max") or v.get("rate_min") or 0))[:8]
    lines = [f"**{len(live)}** roles open now · **{len(new24)}** new in the last 24h", "",
             "**💰 Top-paying open roles**"]
    for v in top:
        extra = f" · {v['slots']} slots" if v.get("slots") is not None else ""
        lines.append(f"• [{v['title'][:70]}]({JOB_URL.format(id=v['id'])}) — {rate_str(v) or 'n/a'}{extra}")
    post_json(hook, {"username": "Mercor Watch", "embeds": [{
        "title": f"☀️ Mercor daily digest — {today}", "description": "\n".join(lines)[:4000],
        "color": 0x5865F2}]})


def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        sample = {"id": "list_TEST", "title": "Test alert – bot is working", "rate_min": 40.0,
                  "rate_max": 80.0, "rate_freq": "hourly", "commitment": "Part-time",
                  "location": "Remote", "category": "Test", "reopen_count": 0}
        notify([("NEW", sample)])
        print("test message sent")
        return
    if "--dump" in args:
        global normalize
        _orig = normalize
        normalize = lambda raw: {**_orig(raw), "_raw": raw}  # noqa: E731
        live = fetch_listings()
        first = next(iter(live.values()), {})
        print("RAW FIELDS FROM MERCOR:", sorted(first.get("_raw", {}).keys()))
        print(json.dumps(list(live.values())[:3], indent=2, default=str))
        print(f"... {len(live)} listings total")
        return
    if "--loop" in args:
        every = int(os.getenv("POLL_SECONDS", "300"))
        while True:
            try:
                run_once()
            except Exception as e:  # noqa: BLE001
                print(f"[{now_iso()}] error: {e}", file=sys.stderr)
            time.sleep(every)
    run_once()


if __name__ == "__main__":
    main()
