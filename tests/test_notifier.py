import json, os, subprocess, sys, tempfile, threading, time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "iawm_discord_notifier.py")

class Mock:
    posts = []; media = {}; hooks = []; hook_mode = []   # hook_mode: queue of status codes to return first
    wp_down = False

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code); self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if Mock.wp_down: return self._send(503, b"{}")
        if self.path.startswith("/wp-json/wp/v2/posts"): return self._send(200, json.dumps(Mock.posts).encode())
        if self.path.startswith("/wp-json/wp/v2/media/"):
            mid = int(self.path.split("/")[-1].split("?")[0])
            if mid in Mock.media: return self._send(200, json.dumps({"source_url": Mock.media[mid]}).encode())
        self._send(404, b"{}")
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if Mock.hook_mode:
            code = Mock.hook_mode.pop(0)
            if code == 429: return self._send(429, json.dumps({"retry_after": 0.1}).encode())
            if code != 204: return self._send(code, b'{"message":"boom"}')
        Mock.hooks.append(body); self._send(204)

srv = HTTPServer(("127.0.0.1", 0), H); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

def iso(hours_ago): return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")
def post(i, title="Hello", hours_ago=1, status="publish", img=None, author="Ada", media_id=0, excerpt="<p>Some excerpt text.</p>"):
    p = {"id": i, "date_gmt": iso(hours_ago), "status": status, "link": f"{BASE}/p/{i}/",
         "title": {"rendered": title}, "excerpt": {"rendered": excerpt}, "featured_media": media_id}
    emb = {}
    if author: emb["author"] = [{"name": author}]
    if img: emb["wp:featuredmedia"] = [{"source_url": img}]
    if emb: p["_embedded"] = emb
    return p

state_dir = tempfile.mkdtemp(); STATE = os.path.join(state_dir, "state.json")
def run(*args, role="123456789", **env):
    e = dict(os.environ, SITE_URL=BASE, DISCORD_WEBHOOK_URL=f"{BASE}/hook", DISCORD_ROLE_ID=role, STATE_FILE=STATE)
    e.update({k: str(v) for k, v in env.items()})
    for k in [k for k, v in e.items() if v == "__unset__"]: del e[k]
    r = subprocess.run([sys.executable, SCRIPT, *args], env=e, capture_output=True, text=True, timeout=60)
    return r
def state_ids(): return set(json.load(open(STATE))["seen"]) if os.path.exists(STATE) else None
def reset():
    Mock.hooks.clear(); Mock.hook_mode.clear(); Mock.wp_down = False
    if os.path.exists(STATE): os.remove(STATE)

passed = failed = 0
def check(name, ok, extra=""):
    global passed, failed
    if ok: passed += 1
    else: failed += 1
    print(("PASS " if ok else "FAIL ") + name + (f"   [{extra}]" if (extra and not ok) else ""))

# 1. first run seeds silently
reset(); Mock.posts = [post(3149), post(3066), post(3134)]
r = run(); check("1 first run exits 0", r.returncode == 0, r.stderr + r.stdout)
check("1 first run sends nothing", len(Mock.hooks) == 0)
check("1 first run remembers existing ids", state_ids() == {3149, 3066, 3134})
# 2. rerun, nothing new
r = run(); check("2 rerun with no change sends nothing", r.returncode == 0 and not Mock.hooks)
# 3. new post with full detail; non-chronological id (lower than max)
Mock.posts = [post(3100, title="It&#8217;s &amp; a <em>test</em> of @everyone", img="https://x/img.png", author="Xavier Thompson",
                   excerpt="<p>Introduction &nbsp; A common practice&hellip; students.</p><p>More text here.</p>", hours_ago=0.1)] + Mock.posts
r = run(); check("3 new post exits 0", r.returncode == 0, r.stderr + r.stdout)
check("3 exactly one ping", len(Mock.hooks) == 1, str(len(Mock.hooks)))
if Mock.hooks:
    h = Mock.hooks[0]; em = h["embeds"][0]
    check("3 content pings the role", h["content"].startswith("<@&123456789> "))
    check("3 allowed_mentions limited to that role", h["allowed_mentions"] == {"parse": [], "roles": ["123456789"]})
    check("3 title entities decoded and tags stripped", em["title"] == "It’s & a test of @everyone", em["title"])
    check("3 @everyone in title cannot ping (role-only mentions)", "everyone" not in h["allowed_mentions"]["parse"])
    check("3 description cleaned", em["description"].startswith("Introduction A common practice") and "<" not in em["description"] and "\xa0" not in em["description"], em["description"])
    check("3 link/author/image/timestamp", em["url"].endswith("/p/3100/") and em["author"]["name"] == "by Xavier Thompson" and em["image"]["url"] == "https://x/img.png" and "T" in em["timestamp"])
check("3 state updated with new id", state_ids() == {3149, 3066, 3134, 3100})
# 4. idempotent
n = len(Mock.hooks); run(); check("4 rerun does not re-ping", len(Mock.hooks) == n)
# 5. editing an old post (same id) doesn't re-ping
Mock.posts[0]["title"]["rendered"] = "Edited title"; run(); check("5 edited post not re-announced", len(Mock.hooks) == n)
# 6. two new at once, oldest first
Mock.hooks.clear()
Mock.posts = [post(4002, "Newer", 0.1), post(4001, "Older", 0.5)] + Mock.posts
r = run(); titles = [h["embeds"][0]["title"] for h in Mock.hooks]
check("6 two new posts sent oldest first", titles == ["Older", "Newer"], str(titles))
# 7. Discord failure: not marked seen, exit 1, retried next run
Mock.hooks.clear(); Mock.posts = [post(5001, "Retry me", 0.1)] + Mock.posts
Mock.hook_mode = [500, 500, 500, 500, 500]
r = run(); check("7 discord outage -> exit 1", r.returncode == 1, r.stdout)
check("7 failed post NOT marked seen", 5001 not in state_ids())
Mock.hook_mode.clear(); r = run()
check("7 next run announces it exactly once", [h["embeds"][0]["title"] for h in Mock.hooks] == ["Retry me"] and 5001 in state_ids())
# 8. rate limit 429 then success
Mock.hooks.clear(); Mock.posts = [post(5002, "Rate limited", 0.1)] + Mock.posts; Mock.hook_mode = [429]
r = run(); check("8 429 retried once, one ping", r.returncode == 0 and len(Mock.hooks) == 1, r.stdout)
# 9. old unseen post: no ping, marked seen
Mock.hooks.clear(); Mock.posts = [post(5003, "Ancient", hours_ago=200)] + Mock.posts
r = run(); check("9 old post not pinged", len(Mock.hooks) == 0); check("9 old post marked seen", 5003 in state_ids())
# 10. flood guard
Mock.hooks.clear(); Mock.posts = [post(6000 + i, f"F{i}", hours_ago=0.1 + i / 100) for i in range(8)] + Mock.posts
r = run(MAX_PER_RUN=5); check("10 flood: only 5 pings", len(Mock.hooks) == 5, str(len(Mock.hooks)))
check("10 flood: all 8 marked seen", all(6000 + i in state_ids() for i in range(8)))
check("10 flood: kept the 5 newest", {h["embeds"][0]["title"] for h in Mock.hooks} == {"F0", "F1", "F2", "F3", "F4"}, str([h["embeds"][0]["title"] for h in Mock.hooks]))
# 11. image fallback via media endpoint (no _embedded media)
Mock.hooks.clear(); Mock.media = {77: "https://x/fallback.jpg"}
Mock.posts = [post(7001, "Fallback img", 0.1, media_id=77)] + Mock.posts; run()
check("11 featured image fetched via media endpoint", Mock.hooks and Mock.hooks[0]["embeds"][0].get("image", {}).get("url") == "https://x/fallback.jpg")
Mock.hooks.clear(); Mock.posts = [post(7002, "Dead media", 0.1, media_id=999)] + Mock.posts; r = run()
check("11 missing image never blocks the announcement", r.returncode == 0 and len(Mock.hooks) == 1 and "image" not in Mock.hooks[0]["embeds"][0])
# 12. non-published posts ignored
Mock.hooks.clear(); Mock.posts = [post(8001, "Draft", 0.1, status="draft"), post(8002, "Future", 0.1, status="future")] + Mock.posts
r = run(); check("12 draft/scheduled posts ignored", len(Mock.hooks) == 0 and 8001 not in state_ids())
# 13. role variants
for role, exp_allowed, exp_prefix in [("", {"parse": []}, "📝"), ("everyone", {"parse": ["everyone"]}, "@everyone "), ("here", {"parse": ["everyone"]}, "@here ")]:
    Mock.hooks.clear(); pid = 9000 + len(role); Mock.posts = [post(pid, "R", 0.1)] + Mock.posts; run(role=role)
    ok = Mock.hooks and Mock.hooks[0]["allowed_mentions"] == exp_allowed and Mock.hooks[0]["content"].startswith(exp_prefix)
    check(f"13 role='{role}' payload", ok, str(Mock.hooks[:1]))
Mock.hooks.clear(); Mock.posts = [post(9100, "Bad role", 0.1)] + Mock.posts; r = run(role="abc; drop")
check("13 invalid role id -> exit 1, nothing sent, not marked seen", r.returncode == 1 and not Mock.hooks and 9100 not in state_ids(), r.stdout)
# 14. --test and --dry-run
Mock.hooks.clear(); r = run("--test"); check("14 --test sends one message", r.returncode == 0 and len(Mock.hooks) == 1 and "Test" in Mock.hooks[0]["content"])
Mock.hooks.clear(); before = state_ids(); Mock.posts = [post(9200, "Dry", 0.1)] + Mock.posts
r = run("--dry-run"); check("14 --dry-run sends nothing and leaves state alone", r.returncode == 0 and not Mock.hooks and state_ids() == before and "Dry" in r.stdout)
# 15. missing webhook
r = run(DISCORD_WEBHOOK_URL="__unset__"); check("15 missing webhook -> exit 2", r.returncode == 2)
# 16. WordPress down: exit non-zero, state untouched
Mock.hooks.clear(); before = state_ids(); Mock.wp_down = True; r = run()
check("16 site down -> non-zero exit, state untouched, no pings", r.returncode != 0 and state_ids() == before and not Mock.hooks)
Mock.wp_down = False
# 17. corrupt state file shouldn't silently spam: should error, not re-announce everything
open(STATE, "w").write("{not json"); Mock.hooks.clear(); r = run()
check("17 corrupt state -> refuses to run (no mass ping)", r.returncode != 0 and not Mock.hooks, f"rc={r.returncode} hooks={len(Mock.hooks)}")

print(f"\n{passed} passed, {failed} failed"); sys.exit(1 if failed else 0)
