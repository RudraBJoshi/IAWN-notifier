# Build log: IAWM Discord notifier

## Plan
1. Approach: poll the public WordPress REST API, post new articles to a Discord webhook with a role ping.
   - Webhook, not a bot token: nothing to host, nothing to keep online, fewer secrets.
   - Runs on a schedule via GitHub Actions (free), or any cron.
2. State: JSON file of seen post IDs (IDs are NOT chronological on this site: 3149, 3066, 3134 were seen newest-first).
3. Safety rules: first run seeds silently; ping once per post; failures never mark a post as seen;
   old posts and floods are skipped silently; mentions limited to the one configured role.
4. Test plan: mock WP + mock Discord on localhost; 12 scenarios (see tests/test_notifier.py).

## Log
- Checked live site: /wp-json/wp/v2/posts is public. Multi-author, IDs out of date order, featured media is a link only (needs embed or a second request).
- Wrote notifier, workflow. Syntax ok. Next: tests.
- Tests 37/37 pass. Live site unreachable from sandbox shell (allowlist) -> verified structure via WebFetch, mock mirrors it. Wrapped main in friendly error handler; retested. README written.
