import json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "weekly_highlight.py")

# Sunday 2026-11-15 18:30 Pacific (PST) = 2026-11-16 02:30 UTC. Target week: Mon Nov 9 .. Sun Nov 15.
SUN_NOW = "2026-11-16T02:30:00"
WEEK_START_Z = "2026-11-09T08:00:00"   # Mon Nov 9 00:00 PST
WEEK_END_Z = "2026-11-16T08:00:00"     # Mon Nov 16 00:00 PST


class Mock:
    posts = []; views = {}; view_fail = set(); hooks = []; hook_mode = []; wp_down = False
    views_auth = []; views_paths = []; media = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body=b"", extra=None):
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/wp-json/wp/v2/posts"):
            return self._send(503, b"{}") if Mock.wp_down else self._send(200, json.dumps(Mock.posts).encode())
        if path.startswith("/wp-json/wp/v2/media/"):
            mid = int(path.split("/")[-1])
            return self._send(200, json.dumps({"source_url": Mock.media[mid]}).encode()) if mid in Mock.media else self._send(404, b"{}")
        # jetpack-style: /rest/v1.1/sites/<site>/stats/post/<id>      url-style: /views/<id>
        if "/stats/post/" in path or path.startswith("/views/"):
            pid = int(path.rstrip("/").split("/")[-1])
            Mock.views_auth.append(self.headers.get("Authorization")); Mock.views_paths.append(path)
            if pid in Mock.view_fail: return self._send(500, b'{"error":"boom"}')
            if pid not in Mock.views: return self._send(404, b'{"error":"unknown"}')
            return self._send(200, json.dumps({"views": Mock.views[pid]}).encode())
        self._send(404, b"{}")
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if Mock.hook_mode:
            code = Mock.hook_mode.pop(0)
            if code != 204: return self._send(code, b'{"message":"boom"}')
        Mock.hooks.append(body); self._send(204)


srv = HTTPServer(("127.0.0.1", 0), H); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

state_dir = tempfile.mkdtemp(); WSTATE = os.path.join(state_dir, "weekly_state.json")


def post(i, title="Hello", date_gmt="2026-11-12T20:00:00", author="Ada", img=None, excerpt="<p>Some excerpt.</p>", status="publish"):
    p = {"id": i, "date_gmt": date_gmt, "status": status, "link": f"{BASE}/p/{i}/",
         "title": {"rendered": title}, "excerpt": {"rendered": excerpt}, "featured_media": 0}
    emb = {}
    if author: emb["author"] = [{"name": author}]
    if img: emb["wp:featuredmedia"] = [{"source_url": img}]
    if emb: p["_embedded"] = emb
    return p


def run(*args, now=SUN_NOW, role="123456789", **env):
    e = dict(os.environ, SITE_URL=BASE, DISCORD_WEBHOOK_URL=f"{BASE}/hook", DISCORD_ROLE_ID=role, WEEKLY_STATE_FILE=WSTATE,
             HIGHLIGHT_NOW=now, VIEW_SOURCE="url", VIEWS_URL_TEMPLATE=f"{BASE}/views/{{id}}")
    e.update({k: str(v) for k, v in env.items()})
    for k in [k for k, v in e.items() if v == "__unset__"]: del e[k]
    return subprocess.run([sys.executable, SCRIPT, *args], env=e, capture_output=True, text=True, timeout=60)


def reset():
    Mock.hooks.clear(); Mock.hook_mode.clear(); Mock.wp_down = False; Mock.view_fail = set()
    Mock.views_auth.clear(); Mock.views_paths.clear(); Mock.media = {}
    if os.path.exists(WSTATE): os.remove(WSTATE)


def wstate(): return json.load(open(WSTATE)) if os.path.exists(WSTATE) else None
def titles(h): return [h["embeds"][0]["title"]] + ([h["embeds"][1]["description"]] if len(h["embeds"]) > 1 else [])


passed = failed = 0
def check(name, ok, extra=""):
    global passed, failed
    if ok: passed += 1
    else: failed += 1
    print(("PASS " if ok else "FAIL ") + name + (f"   [{extra}]" if (extra and not ok) else ""))


# ---- 1. basic selection: highest views among this week's posts wins; out-of-week posts ignored ------------
reset()
Mock.posts = [post(1, "Low", "2026-11-10T20:00:00"), post(2, "Winner", "2026-11-11T20:00:00"), post(3, "Mid", "2026-11-14T20:00:00"),
              post(90, "Last week, huge", "2026-11-08T20:00:00"), post(91, "Next week, huge", "2026-11-17T20:00:00")]
Mock.views = {1: 10, 2: 500, 3: 120, 90: 99999, 91: 99999}
r = run(); check("1 exits 0", r.returncode == 0, r.stderr + r.stdout)
check("1 exactly one message", len(Mock.hooks) == 1, str(len(Mock.hooks)))
h = Mock.hooks[0] if Mock.hooks else {"embeds": [{}], "content": ""}
check("1 winner is the most-viewed in-week post", h["embeds"][0].get("title", "").endswith("Winner"), h["embeds"][0].get("title", ""))
check("1 winner embed shows views and week label", {"name": "Views", "value": "500", "inline": True} in h["embeds"][0].get("fields", [])
      and "Nov 9 – Nov 15, 2026" in h["embeds"][0]["footer"]["text"], str(h["embeds"][0].get("footer")))
check("1 out-of-week posts excluded even with huge views", "99999" not in json.dumps(h) and "huge" not in json.dumps(h))
check("1 runner-ups ordered by views", h["embeds"][1]["description"].index("Mid") < h["embeds"][1]["description"].index("Low") if len(h["embeds"]) > 1 else False)
check("1 role ping with role-only allowed_mentions", h["content"].startswith("<@&123456789> ") and h["allowed_mentions"] == {"parse": [], "roles": ["123456789"]})
check("1 week recorded in state", wstate() and wstate()["highlighted"].get("2026-11-09", {}).get("post_id") == 2, str(wstate()))

# ---- 2. idempotent; --force reposts --------------------------------------------------------------------
n = len(Mock.hooks); r = run(); check("2 rerun same week does nothing", r.returncode == 0 and len(Mock.hooks) == n)
r = run("--force"); check("2 --force posts again", r.returncode == 0 and len(Mock.hooks) == n + 1)

# ---- 3. tie -> earlier published; then lower id ---------------------------------------------------------
reset(); Mock.posts = [post(11, "Later", "2026-11-13T20:00:00"), post(12, "Earlier", "2026-11-10T20:00:00")]
Mock.views = {11: 50, 12: 50}; run()
check("3 tie goes to the earlier-published article", Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("Earlier"), str(titles(Mock.hooks[0])) if Mock.hooks else "")
reset(); Mock.posts = [post(22, "B", "2026-11-10T20:00:00"), post(21, "A", "2026-11-10T20:00:00")]; Mock.views = {21: 5, 22: 5}; run()
check("3 same instant tie -> lower id", Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("A"))

# ---- 4. window boundaries (week start/end are Pacific midnight, incl. DST change on 2026-11-01) ---------
reset(); Mock.posts = [post(31, "Just before start", "2026-11-09T07:59:59"), post(32, "At start", WEEK_START_Z),
                       post(33, "Last second", "2026-11-16T02:29:59"), post(34, "Published after run", "2026-11-16T02:31:00")]
Mock.views = {31: 900, 32: 5, 33: 7, 34: 800}; run()
check("4 start boundary: 07:59:59Z is last week, 08:00:00Z is this week; future-of-now excluded",
      Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("Last second") and "At start" in Mock.hooks[0]["embeds"][1]["description"]
      and "Just before" not in json.dumps(Mock.hooks[0]) and "after run" not in json.dumps(Mock.hooks[0]), str(Mock.hooks[:1]))
# DST: week of Mon Oct 26 .. Sun Nov 1 2026 (clocks go back 02:00 Nov 1) -> ends Nov 2 00:00 PST = 08:00Z
reset(); Mock.posts = [post(41, "Sat night", "2026-11-02T07:59:00"), post(42, "Monday 00:00 PST", "2026-11-02T08:00:00"), post(43, "Start PDT", "2026-10-26T07:00:00")]
Mock.views = {41: 3, 42: 1000, 43: 2}
run("--week-of", "2026-10-28", now="2026-11-10T12:00:00")
check("4 DST week: 41 and 43 are in, 42 (next Monday 00:00 PST) is out",
      Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("Sat night") and "Start PDT" in Mock.hooks[0]["embeds"][1]["description"]
      and "Monday 00:00" not in json.dumps(Mock.hooks[0]), str(Mock.hooks[:1]))
check("4 --week-of anchors to that week's Monday", wstate() and "2026-10-26" in wstate()["highlighted"], str(wstate()))

# ---- 5. which week by weekday ---------------------------------------------------------------------------
reset(); Mock.posts = [post(51, "Week A", "2026-11-04T20:00:00"), post(52, "Week B", "2026-11-11T20:00:00")]; Mock.views = {51: 9, 52: 4}
run(now="2026-11-18T18:00:00")   # Wednesday Nov 18 10:00 PST -> last completed week (Nov 9-15)
check("5 Wednesday run highlights the previous completed week", Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("Week B") and "2026-11-09" in wstate()["highlighted"], str(wstate()))
reset(); run(now="2026-11-16T09:00:00")   # Monday 01:00 PST, i.e. delayed past midnight -> week that just ended
check("5 run delayed to Monday 01:00 still highlights the week that just ended", Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("Week B"))
reset(); run(now="2026-11-15T20:00:00")   # Sunday 12:00 PST -> current week
check("5 Sunday daytime run highlights the current week", Mock.hooks and "2026-11-09" in wstate()["highlighted"])
reset(); run(now="2026-11-09T20:00:00")   # Monday Nov 9 12:00 PST -> previous week (Nov 2-8): Week A published Nov 4
check("5 Monday run highlights the week before", Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("Week A") and "2026-11-02" in wstate()["highlighted"], str(wstate()))

# ---- 6. empty / all-zero / min posts ------------------------------------------------------------------
reset(); Mock.posts = [post(60, "Old", "2026-10-01T20:00:00")]; Mock.views = {60: 100}; r = run()
check("6 no article that week -> no post, exit 0, nothing recorded", r.returncode == 0 and not Mock.hooks and wstate() is None)
reset(); Mock.posts = [post(61, "Zero", "2026-11-10T20:00:00")]; Mock.views = {61: 0}; r = run()
check("6 all-zero views -> no post, exit 0, not recorded (so a later run can still post)", r.returncode == 0 and not Mock.hooks and wstate() is None)
reset(); Mock.posts = [post(62, "Solo", "2026-11-10T20:00:00")]; Mock.views = {62: 5}; run(MIN_POSTS=2)
check("6 MIN_POSTS=2 with one article -> no post", not Mock.hooks)
reset(); run()
check("6 a single article is highlighted by default, with no runner-up embed", len(Mock.hooks) == 1 and len(Mock.hooks[0]["embeds"]) == 1)

# ---- 7. view provider failures -----------------------------------------------------------------------
reset(); Mock.posts = [post(71, "Ok", "2026-11-10T20:00:00"), post(72, "Broken stats", "2026-11-11T20:00:00")]; Mock.views = {71: 3, 72: 999}; Mock.view_fail = {72}
r = run(); check("7 one failing post is excluded with a warning, the rest still post",
                 r.returncode == 0 and Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("Ok") and "views unavailable for post 72" in r.stdout, r.stdout)
reset(); Mock.view_fail = {71, 72}; r = run()
check("7 all failing -> exit 1, nothing posted, nothing recorded", r.returncode == 1 and not Mock.hooks and wstate() is None, r.stdout)
reset(); Mock.view_fail = set(); Mock.views = {71: "oops"}
Mock.posts = [post(71, "Bad value", "2026-11-10T20:00:00")]; r = run()
check("7 non-numeric views counted as unavailable", r.returncode == 1 and not Mock.hooks, r.stdout)

# ---- 8. Discord failure then retry --------------------------------------------------------------------
reset(); Mock.posts = [post(81, "Retry me", "2026-11-10T20:00:00")]; Mock.views = {81: 8}; Mock.hook_mode = [500] * 6
r = run(); check("8 discord outage -> exit 1 and week NOT recorded", r.returncode == 1 and wstate() is None, r.stdout)
Mock.hook_mode.clear(); r = run()
check("8 next run posts exactly once and records", r.returncode == 0 and len(Mock.hooks) == 1 and wstate() is not None)

# ---- 9. mentions & markdown safety --------------------------------------------------------------------
reset(); Mock.posts = [post(91, "Hi @everyone <@&999> [x](http://evil) *bold*", "2026-11-10T20:00:00"), post(92, "Run-up [click](http://evil)", "2026-11-11T20:00:00")]
Mock.views = {91: 10, 92: 5}; run()
h = Mock.hooks[0] if Mock.hooks else {}
check("9 titles cannot ping anyone but the configured role", h.get("allowed_mentions") == {"parse": [], "roles": ["123456789"]})
check("9 runner-up link label is markdown-escaped", "\\[click\\]" in h["embeds"][1]["description"] and "[Run-up \\[click\\]" in h["embeds"][1]["description"], h["embeds"][1]["description"] if h else "")
reset(); Mock.posts = [post(93, "P", "2026-11-10T20:00:00")]; Mock.views = {93: 4}; run(WEEKLY_PING=0)
check("9 WEEKLY_PING=0 posts with no mention", Mock.hooks and not Mock.hooks[0]["content"].startswith("<@") and Mock.hooks[0]["allowed_mentions"] == {"parse": []})
reset(); run(role="")
check("9 no role configured -> no ping", Mock.hooks and Mock.hooks[0]["allowed_mentions"] == {"parse": []})
reset(); r = run(role="abc; drop")
check("9 invalid role id -> exit 1, nothing sent or recorded", r.returncode == 1 and not Mock.hooks and wstate() is None, r.stdout)

# ---- 10. image / author / thumbnails ------------------------------------------------------------------
reset(); Mock.posts = [post(101, "Pic", "2026-11-10T20:00:00", author="Zhengji Li", img="https://x/img.png")]; Mock.views = {101: 1}; run()
em = Mock.hooks[0]["embeds"][0] if Mock.hooks else {}
check("10 author and image carried over", em.get("author", {}).get("name") == "by Zhengji Li" and em.get("image", {}).get("url") == "https://x/img.png", str(em))

# ---- 11. jetpack provider request shape ---------------------------------------------------------------
reset(); Mock.posts = [post(111, "J", "2026-11-10T20:00:00")]; Mock.views = {111: 77}
r = run(VIEW_SOURCE="jetpack", WPCOM_TOKEN="tok123", WPCOM_SITE="example.com", WPCOM_API_BASE=f"{BASE}/rest/v1.1")
check("11 jetpack: posts using the Bearer token on /sites/<site>/stats/post/<id>",
      r.returncode == 0 and Mock.views_auth == ["Bearer tok123"] and Mock.views_paths == ["/rest/v1.1/sites/example.com/stats/post/111"] and len(Mock.hooks) == 1,
      f"{r.stdout} {Mock.views_auth} {Mock.views_paths}")
check("11 jetpack: token never appears in the Discord payload or logs", "tok123" not in json.dumps(Mock.hooks) and "tok123" not in r.stdout + r.stderr)
reset(); r = run(VIEW_SOURCE="jetpack", WPCOM_TOKEN="__unset__", WPCOM_SITE="example.com")
check("11 jetpack without token -> exit 2 with a clear message", r.returncode == 2 and "WPCOM_TOKEN" in r.stdout and not Mock.hooks, r.stdout)
reset(); Mock.views = {}   # stats API answers 404 for this post
r = run(VIEW_SOURCE="jetpack", WPCOM_TOKEN="bad", WPCOM_SITE="s", WPCOM_API_BASE=f"{BASE}/nope")
check("11 jetpack 404/401 style failure -> exit 1, nothing posted", r.returncode == 1 and not Mock.hooks, r.stdout)

# ---- 12. url provider with token ----------------------------------------------------------------------
reset(); Mock.posts = [post(121, "U", "2026-11-10T20:00:00")]; Mock.views = {121: 5}; run(VIEWS_TOKEN="secret1")
check("12 url provider sends optional bearer token", Mock.views_auth == ["Bearer secret1"] and len(Mock.hooks) == 1, str(Mock.views_auth))
reset(); r = run(VIEWS_URL_TEMPLATE="https://x/no-placeholder")
check("12 url template without {id} -> exit 2", r.returncode == 2, r.stdout)

# ---- 13. dry run, config errors, site down, bad state --------------------------------------------------
reset(); Mock.posts = [post(131, "Dry", "2026-11-10T20:00:00")]; Mock.views = {131: 5}; r = run("--dry-run")
check("13 --dry-run sends nothing, records nothing, prints the payload", r.returncode == 0 and not Mock.hooks and wstate() is None and "Dry" in r.stdout)
reset(); r = run(DISCORD_WEBHOOK_URL="__unset__"); check("13 missing webhook -> exit 2", r.returncode == 2)
r = run(VIEW_SOURCE="magic"); check("13 unknown VIEW_SOURCE -> exit 2", r.returncode == 2 and not Mock.hooks)
r = run("--week-of", "nonsense"); check("13 bad --week-of -> exit 2", r.returncode == 2)
r = run("--bogus"); check("13 unknown flag -> exit 2", r.returncode == 2)
r = run(WEEK_TZ="Mars/Olympus"); check("13 unknown WEEK_TZ -> exit 2", r.returncode == 2)
reset(); Mock.wp_down = True; r = run(); check("13 site down -> non-zero exit, nothing posted/recorded", r.returncode != 0 and not Mock.hooks and wstate() is None)
Mock.wp_down = False
reset(); open(WSTATE, "w").write("{broken"); Mock.hooks.clear(); r = run()
check("13 corrupt state -> refuses to run (no duplicate post)", r.returncode != 0 and not Mock.hooks, r.stdout)

# ---- 14. non-published ignored ------------------------------------------------------------------------
reset(); Mock.posts = [post(141, "Draft", "2026-11-10T20:00:00", status="draft"), post(142, "Real", "2026-11-11T20:00:00")]
Mock.views = {141: 9999, 142: 1}; run()
check("14 drafts are ignored", Mock.hooks and Mock.hooks[0]["embeds"][0]["title"].endswith("Real") and "Draft" not in json.dumps(Mock.hooks[0]))

# ---- 15. top-n -----------------------------------------------------------------------------------------
reset(); Mock.posts = [post(150 + i, f"T{i}", "2026-11-10T20:00:00") for i in range(6)]; Mock.views = {150 + i: 100 - i for i in range(6)}; run(TOP_N=2)
check("15 TOP_N=2 -> winner plus one runner-up", Mock.hooks and Mock.hooks[0]["embeds"][1]["description"].count("views") == 1, str(Mock.hooks[:1]))

print(f"\n{passed} passed, {failed} failed"); sys.exit(1 if failed else 0)
