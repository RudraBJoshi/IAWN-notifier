#!/usr/bin/env python3
"""Post a weekly "most-read article" highlight to Discord.

Considers only articles PUBLISHED during the target week (Mon 00:00 - Sun 24:00, Pacific by default),
picks the one with the most views, and posts it with the runner-ups.

Standard library only. Configure with environment variables:

  DISCORD_WEBHOOK_URL  (required) channel webhook URL
  DISCORD_ROLE_ID      role to ping; or "everyone" / "here"; empty = post without a ping
  WEEKLY_PING          "0" posts without the ping even if DISCORD_ROLE_ID is set (default 1)
  SITE_URL             default https://inside-a-wandering-mind.com
  WEEKLY_STATE_FILE    default weekly_state.json (weeks already highlighted)
  WEEK_TZ              default America/Los_Angeles
  MIN_POSTS            default 1 (fewer articles than this that week -> no highlight)
  TOP_N                default 3 (winner + runner-ups shown)

  VIEW_SOURCE          "jetpack" (default) or "url"
    jetpack:  WPCOM_TOKEN  (OAuth bearer token of a WordPress.com app)
              WPCOM_SITE   (blog ID or domain as WordPress.com knows it)
              WPCOM_API_BASE  default https://public-api.wordpress.com/rest/v1.1
              Calls GET {WPCOM_API_BASE}/sites/{WPCOM_SITE}/stats/post/{id} and reads "views".
    url:      VIEWS_URL_TEMPLATE  e.g. https://counter.example/views/{id}  (JSON {"views": n})
              VIEWS_TOKEN         optional bearer token

Which week: if today (in WEEK_TZ) is Sunday, the current week; on any other day, the most recently
completed Mon-Sun week. Override with --week-of YYYY-MM-DD (any date inside the wanted week).

Because every candidate was published inside the week, a post's all-time views are its views this week.

Usage:
  python3 weekly_highlight.py              normal run
  python3 weekly_highlight.py --dry-run    print what would be sent, change nothing
  python3 weekly_highlight.py --week-of 2026-11-09
  python3 weekly_highlight.py --force      post again even if that week was already highlighted
"""
import json
import os
import sys
import urllib.parse
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iawm_discord_notifier as base  # noqa: E402  (shares request/clean_text/mention/send helpers)

GOLD = 0xF5B700
MEDALS = ["🥇", "🥈", "🥉"] + ["▫️"] * 10


class Config(RuntimeError):
    """Bad configuration: exit code 2."""


def env(name, default=""):
    return os.environ.get(name, default).strip()


# ---------- week window ------------------------------------------------------------------------------
def now_utc():
    override = env("HIGHLIGHT_NOW")  # tests only: pretend it is this UTC instant
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def week_window(now, tz, week_of=None):
    """Return (start_utc, end_utc, monday_date, last_day_date) for the target week."""
    local_now = now.astimezone(tz)
    if week_of:
        anchor = week_of
    elif local_now.weekday() == 6:  # Sunday: highlight the week that is ending today
        anchor = local_now.date()
    else:                            # otherwise the week that most recently ended
        anchor = local_now.date() - timedelta(days=local_now.weekday() + 1)
    monday = anchor - timedelta(days=anchor.weekday())
    next_monday = monday + timedelta(days=7)
    start = datetime.combine(monday, dtime.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(next_monday, dtime.min, tzinfo=tz).astimezone(timezone.utc)
    return start, end, monday, monday + timedelta(days=6)


def week_label(monday, sunday):
    fmt = lambda d: f"{d:%b} {d.day}"
    return f"{fmt(monday)} – {fmt(sunday)}, {sunday.year}"


# ---------- posts ------------------------------------------------------------------------------------
def fetch_week_posts(start, end, now):
    url = f"{base.SITE_URL}/wp-json/wp/v2/posts?per_page=50&orderby=date&order=desc&_embed=1"
    _, body = base.request(url)
    posts = json.loads(body)
    if not isinstance(posts, list):
        raise RuntimeError("unexpected response from WordPress REST API")
    out = []
    for p in posts:
        if not (isinstance(p, dict) and "id" in p and p.get("status", "publish") == "publish"):
            continue
        d = p.get("date_gmt") or ""
        try:
            when = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start <= when < end and when <= now:
            out.append(p)
    return out


# ---------- view providers ---------------------------------------------------------------------------
def _get_json(url, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    _, body = base.request(url, headers=headers)
    return json.loads(body)


def parse_views(data):
    v = data.get("views") if isinstance(data, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise RuntimeError(f"no usable 'views' field in {str(data)[:120]!r}")
    return int(float(v))


class JetpackViews:
    def __init__(self):
        self.token = env("WPCOM_TOKEN")
        self.site = env("WPCOM_SITE")
        self.api = env("WPCOM_API_BASE", "https://public-api.wordpress.com/rest/v1.1").rstrip("/")
        if not self.token or not self.site:
            raise Config("VIEW_SOURCE=jetpack needs WPCOM_TOKEN and WPCOM_SITE")

    def views(self, post):
        site = urllib.parse.quote(self.site, safe="")
        return parse_views(_get_json(f"{self.api}/sites/{site}/stats/post/{int(post['id'])}", self.token))


class UrlViews:
    def __init__(self):
        self.template = env("VIEWS_URL_TEMPLATE")
        self.token = env("VIEWS_TOKEN") or None
        if "{id}" not in self.template:
            raise Config("VIEW_SOURCE=url needs VIEWS_URL_TEMPLATE containing {id}")

    def views(self, post):
        return parse_views(_get_json(self.template.replace("{id}", str(int(post["id"]))), self.token))


def make_provider():
    source = env("VIEW_SOURCE", "jetpack").lower()
    if source == "jetpack":
        return JetpackViews()
    if source == "url":
        return UrlViews()
    raise Config(f"unknown VIEW_SOURCE {source!r} (use 'jetpack' or 'url')")


# ---------- payload ----------------------------------------------------------------------------------
def md_safe(text):
    """Make a title safe to put inside a markdown link label."""
    for ch in ("\\", "[", "]", "*", "_", "`", "~", "|", ">"):
        text = text.replace(ch, "\\" + ch)
    return text


def title_of(p):
    return base.clean_text((p.get("title") or {}).get("rendered"), 250) or "Untitled"


def build_payload(ranked, label, ping, tz):
    """ranked: list of (post, views), best first."""
    prefix, allowed = base.mention() if ping else ("", {"parse": []})
    win, win_views = ranked[0]
    local_pub = base.post_date(win).astimezone(tz)  # the date readers saw (and that the post URL uses), not UTC
    embed = {
        "title": f"🏆 {title_of(win)}"[:256],
        "url": win["link"],
        "description": base.clean_text((win.get("excerpt") or {}).get("rendered")),
        "color": GOLD,
        "fields": [
            {"name": "Views", "value": f"{win_views:,}", "inline": True},
            {"name": "Published", "value": f"{local_pub:%b} {local_pub.day}", "inline": True},
        ],
        "footer": {"text": f"Most-read this week · {label}"},
        "timestamp": base.post_date(win).isoformat(),
    }
    name = base.author_name(win)
    if name:
        embed["author"] = {"name": f"by {base.clean_text(name, 80)}"}
    img = base.featured_image(win)
    if img:
        embed["image"] = {"url": img}
    embeds = [embed]
    rest = ranked[1:]
    if rest:
        lines = [f"{MEDALS[i + 1]} [{md_safe(title_of(p))}]({p['link']}) · {v:,} views" for i, (p, v) in enumerate(rest)]
        embeds.append({"title": "Also popular this week", "description": "\n".join(lines)[:4000], "color": base.BRAND_COLOR})
    return {
        "content": f"{prefix}🏆 **Most-read article this week** on **Inside A Wandering Mind**!",
        "embeds": embeds,
        "allowed_mentions": allowed,
    }


# ---------- state ------------------------------------------------------------------------------------
def state_path():
    return env("WEEKLY_STATE_FILE", "weekly_state.json")


def load_state():
    try:
        with open(state_path()) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"highlighted": {}}
    if not isinstance(data, dict) or not isinstance(data.get("highlighted"), dict):
        raise RuntimeError(f"{state_path()} is not valid; refusing to run (would risk a duplicate post)")
    return data


def save_state(state):
    tmp = state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, state_path())


# ---------- main -------------------------------------------------------------------------------------
def parse_args(argv):
    opts = {"dry": False, "force": False, "week_of": None}
    it = iter(argv)
    for a in it:
        if a == "--dry-run":
            opts["dry"] = True
        elif a == "--force":
            opts["force"] = True
        elif a == "--week-of":
            try:
                opts["week_of"] = date.fromisoformat(next(it))
            except (StopIteration, ValueError):
                raise Config("--week-of needs a date like 2026-11-09")
        else:
            raise Config(f"unknown argument {a!r}")
    return opts


def main(argv):
    opts = parse_args(argv)
    if not base.WEBHOOK and not opts["dry"]:
        base.log("DISCORD_WEBHOOK_URL is not set")
        return 2
    try:
        tz = ZoneInfo(env("WEEK_TZ", "America/Los_Angeles"))
    except Exception:
        raise Config(f"unknown WEEK_TZ {env('WEEK_TZ')!r}")
    top_n = max(1, int(env("TOP_N", "3")))
    min_posts = max(1, int(env("MIN_POSTS", "1")))
    ping = env("WEEKLY_PING", "1") != "0"

    now = now_utc()
    start, end, monday, sunday = week_window(now, tz, opts["week_of"])
    key, label = monday.isoformat(), week_label(monday, sunday)
    base.log(f"week {label} (UTC {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M})")

    state = load_state()
    if key in state["highlighted"] and not opts["force"]:
        base.log(f"week {key} already highlighted: nothing to do")
        return 0

    provider = make_provider()
    posts = fetch_week_posts(start, end, now)
    if len(posts) < min_posts:
        base.log(f"{len(posts)} article(s) published that week (need {min_posts}): no highlight")
        return 0

    scored, failures = [], 0
    for p in posts:
        try:
            scored.append((p, provider.views(p)))
        except Exception as e:
            failures += 1
            base.log(f"views unavailable for post {p['id']}: {e}")
    if not scored:
        base.log("could not get views for any article: not posting, not recording")
        return 1
    scored.sort(key=lambda pv: (-pv[1], base.post_date(pv[0]), pv[0]["id"]))
    if scored[0][1] <= 0:
        base.log("no views recorded for any article that week: not posting")
        return 0

    ranked = scored[:top_n]
    payload = build_payload(ranked, label, ping, tz)
    if opts["dry"]:
        base.log("DRY RUN payload:\n" + json.dumps(payload, indent=1, ensure_ascii=False))
        return 0
    base.send(payload)
    state["highlighted"][key] = {"post_id": ranked[0][0]["id"], "views": ranked[0][1],
                                 "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save_state(state)
    base.log(f"highlighted post {ranked[0][0]['id']} ({ranked[0][1]} views) for week {key}"
             + (f"; {failures} article(s) had no view count" if failures else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Config as e:
        base.log(f"CONFIG ERROR: {e}")
        sys.exit(2)
    except Exception as e:
        base.log(f"ERROR: {e}")
        sys.exit(1)
