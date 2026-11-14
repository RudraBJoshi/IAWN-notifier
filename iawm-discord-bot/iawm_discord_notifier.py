#!/usr/bin/env python3
"""Ping a Discord channel when a new article is published on a WordPress site.

Standard library only. Configure with environment variables:

  DISCORD_WEBHOOK_URL  (required) channel webhook URL
  DISCORD_ROLE_ID      role to ping; or "everyone" / "here"; empty = post without a ping
  SITE_URL             default https://inside-a-wandering-mind.com
  STATE_FILE           default state.json (list of post IDs already announced)
  MAX_PER_RUN          default 5  (more new posts than this are skipped silently, to avoid floods)
  MAX_AGE_HOURS        default 72 (unseen posts older than this are marked seen without a ping)

Usage:
  python3 iawm_discord_notifier.py            normal run
  python3 iawm_discord_notifier.py --dry-run  print what would be sent, change nothing
  python3 iawm_discord_notifier.py --test     send a test message to verify the webhook
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SITE_URL = os.environ.get("SITE_URL", "https://inside-a-wandering-mind.com").rstrip("/")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "72"))
SEEN_LIMIT = 500
BRAND_COLOR = 0x7A5CFF
USER_AGENT = "IAWM-Discord-Notifier/1.0 (+https://inside-a-wandering-mind.com)"


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def request(url, payload=None, retries=3):
    """GET (payload None) or POST JSON. Returns (status, body_bytes). Retries on 429/5xx."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            last = f"HTTP {e.code}: {body[:200]!r}"
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 2))
                except Exception:
                    wait = float(e.headers.get("Retry-After", 2) or 2)
                time.sleep(min(wait, 30))
                continue
            if e.code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"network error: {e}"
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(last or "request failed")


def fetch_posts():
    url = f"{SITE_URL}/wp-json/wp/v2/posts?per_page=20&orderby=date&order=desc&_embed=1"
    _, body = request(url)
    posts = json.loads(body)
    if not isinstance(posts, list):
        raise RuntimeError("unexpected response from WordPress REST API")
    return [p for p in posts if isinstance(p, dict) and "id" in p and p.get("status", "publish") == "publish"]


def clean_text(raw, limit=280):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*(\[…\]|\[&hellip;\]|Continue reading.*|Read more.*)$", "", text, flags=re.I).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(".,;: ") + "…"
    return text


def post_date(p):
    raw = p.get("date_gmt") or ""
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def featured_image(p):
    emb = (p.get("_embedded") or {}).get("wp:featuredmedia") or []
    if emb and isinstance(emb[0], dict) and emb[0].get("source_url"):
        return emb[0]["source_url"]
    mid = p.get("featured_media")
    if mid:
        try:
            _, body = request(f"{SITE_URL}/wp-json/wp/v2/media/{int(mid)}?_fields=source_url")
            return json.loads(body).get("source_url")
        except Exception as e:  # image is optional; never block the announcement
            log(f"no image for post {p['id']}: {e}")
    return None


def author_name(p):
    emb = (p.get("_embedded") or {}).get("author") or []
    if emb and isinstance(emb[0], dict):
        return emb[0].get("name")
    return None


def mention():
    """Return (content_prefix, allowed_mentions)."""
    if not ROLE_ID:
        return "", {"parse": []}
    if ROLE_ID.lower() in ("everyone", "here"):
        return f"@{ROLE_ID.lower()} ", {"parse": ["everyone"]}
    if not ROLE_ID.isdigit():
        raise RuntimeError("DISCORD_ROLE_ID must be a numeric role ID, 'everyone', 'here', or empty")
    return f"<@&{ROLE_ID}> ", {"parse": [], "roles": [ROLE_ID]}


def build_payload(p):
    prefix, allowed = mention()
    title = clean_text((p.get("title") or {}).get("rendered"), 250) or "New article"
    embed = {
        "title": title,
        "url": p["link"],
        "description": clean_text((p.get("excerpt") or {}).get("rendered")),
        "color": BRAND_COLOR,
        "timestamp": post_date(p).isoformat(),
        "footer": {"text": "Inside A Wandering Mind"},
    }
    name = author_name(p)
    if name:
        embed["author"] = {"name": f"by {clean_text(name, 80)}"}
    img = featured_image(p)
    if img:
        embed["image"] = {"url": img}
    return {
        "content": f"{prefix}📝 New article on **Inside A Wandering Mind**!",
        "embeds": [embed],
        "allowed_mentions": allowed,
    }


def send(payload):
    status, _ = request(WEBHOOK, payload)
    if status not in (200, 204):
        raise RuntimeError(f"Discord returned HTTP {status}")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(state):
    state["seen"] = state["seen"][-SEEN_LIMIT:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def main(argv):
    dry = "--dry-run" in argv
    if not WEBHOOK and "--dry-run" not in argv:
        log("DISCORD_WEBHOOK_URL is not set")
        return 2

    if "--test" in argv:
        prefix, allowed = mention()
        send({"content": f"{prefix}✅ Test from the IAWM article notifier. If you can see this (and the ping), setup works.",
              "allowed_mentions": allowed})
        log("test message sent")
        return 0

    posts = fetch_posts()
    state = load_state()
    if state is None:
        state = {"seen": [p["id"] for p in posts]}
        if not dry:
            save_state(state)
        log(f"first run: remembered {len(posts)} existing posts, no pings sent")
        return 0

    seen = set(state["seen"])
    new = sorted((p for p in posts if p["id"] not in seen), key=post_date)
    if not new:
        log("no new articles")
        return 0

    now = datetime.now(timezone.utc)
    to_send, skipped = [], []
    for p in new:
        age_h = (now - post_date(p)).total_seconds() / 3600
        (skipped if age_h > MAX_AGE_HOURS else to_send).append(p)
    if len(to_send) > MAX_PER_RUN:
        skipped = to_send[:-MAX_PER_RUN] + skipped
        to_send = to_send[-MAX_PER_RUN:]
    for p in skipped:
        log(f"skipping (too old or flood guard): {p['id']} {p.get('link')}")
        state["seen"].append(p["id"])

    failed = False
    for p in to_send:
        try:
            payload = build_payload(p)
            if dry:
                log("DRY RUN payload:\n" + json.dumps(payload, indent=1, ensure_ascii=False))
                continue
            send(payload)
            state["seen"].append(p["id"])
            save_state(state)
            log(f"announced {p['id']}: {p['link']}")
        except Exception as e:
            log(f"FAILED to announce {p['id']}: {e}")
            failed = True
            break  # keep order; retry this post next run
    if not dry:
        save_state(state)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # one clear line instead of a traceback; non-zero exit marks the run as failed
        log(f"ERROR: {e}")
        sys.exit(1)
