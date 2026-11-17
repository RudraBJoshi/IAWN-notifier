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
- Repo set up: flattened layout, workflow moved to .github/workflows/. Tests re-run from repo root.

---

# Weekly "most-read article" highlight (branch: weekly-highlight)

## Plan (written before coding)
Goal: every Sunday evening (Pacific) post a highlight of the article with the most views **among articles published that week** (Mon 00:00 to Sun 24:00 Pacific).
Decisions confirmed with Rudra: "published that week" (not any article with views that week).

1. New file `weekly_highlight.py` (stdlib only), reusing helpers from `iawm_discord_notifier.py` (request, clean_text, featured_image, mention, send).
2. Week rule: if today (Pacific) is Sunday -> the current week; any other day -> the most recently completed Mon-Sun week. `--week-of YYYY-MM-DD` overrides. A Sunday run covers Mon 00:00 .. now.
3. Posts: REST API (`per_page=50`, `_embed=1`), filtered client-side on `date_gmt` (WP's `after/before` filters use site-local time, which is ambiguous).
4. Views come from a pluggable provider chosen by `VIEW_SOURCE`:
   - `jetpack`: WordPress.com API `GET {WPCOM_API_BASE}/sites/{WPCOM_SITE}/stats/post/{id}` with `Authorization: Bearer WPCOM_TOKEN`, reads `views`. UNVERIFIED against the live site (needs Rudra's token). Because every candidate was published inside the window, all-time views == views this week.
   - `url`: `VIEWS_URL_TEMPLATE` (with `{id}`), optional `VIEWS_TOKEN`; JSON `{"views": n}`. For a future self-hosted counter after the Next.js cutover.
5. Pick: most views; tie -> earlier published, then lower id. Show top 3 (winner embed + runner-up embed). Missing/failed views -> that post is excluded with a warning; ALL failing -> exit 1, nothing posted, nothing recorded. All zero views -> skip quietly, not recorded.
6. Idempotence: `weekly_state.json` records each highlighted week start; a rerun for the same week does nothing (`--force` overrides). A failed send is never recorded.
7. Safety: mentions limited to the one configured role (`WEEKLY_PING=0` disables the ping); titles cannot ping or break markdown.
8. Workflow `.github/workflows/weekly-highlight.yml`: cron Mon 02:30 UTC (= Sun 6:30/7:30pm Pacific), manual dispatch with dry_run/week_of/force; commits `weekly_state.json` with `git pull --rebase` first because the other workflow also pushes.
9. Tests (`tests/test_weekly.py`, mock WP + mock stats + mock Discord): window boundaries incl. the DST change on 2026-11-01, tie-break, runner-ups, exclusion of out-of-week posts, idempotence/force, empty week, all-zero, partial and total provider failure, Discord failure then retry, week rule by weekday, role/ping variants, markdown/@everyone safety, jetpack request shape, dry-run, config errors.
10. Not merged until Rudra says so.

## Log
- Wrote `weekly_highlight.py`; added an optional `headers` argument to `request()` in the main script (existing 37 tests still pass).
- `tests/test_weekly.py`: 50/50 pass. One initial failure was a test bug (stale mock data), fixed. Mutation checks (wrong sort order, inclusive end boundary, state never saved) each make the suite fail.
- Workflow + README added. Manual runs default to dry-run so a stray click can't post.
- NOT VERIFIED: the real Jetpack/WordPress.com stats response. Needs Rudra's WPCOM_TOKEN to spike `GET /sites/{site}/stats/post/{id}` once; if the shape differs only `JetpackViews.views` / `parse_views` change.
