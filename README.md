# IAWM Discord article notifier

Posts a Discord message (title, excerpt, author, image, link) and pings a role whenever a new article goes live on
inside-a-wandering-mind.com. No bot token, no server: it uses a channel webhook and runs free on GitHub Actions.

## Setup (about 10 minutes)

### 1. Discord: webhook + role
1. Server Settings -> Roles -> create a role named e.g. `Article Alerts`. Leave "Allow anyone to @mention this role" **on**
   (webhooks can only ping roles that are mentionable). Let people self-assign it (Discord's Onboarding / Channels & Roles, or a reaction-role bot).
2. Turn on User Settings -> Advanced -> Developer Mode, then right-click the role -> **Copy Role ID**.
3. Channel settings -> Integrations -> Webhooks -> New Webhook -> choose the channel -> **Copy Webhook URL**.
   Treat that URL like a password: anyone who has it can post to your channel.

### 2. GitHub: a repo for the notifier
1. Create a **private** repo (e.g. `iawm-discord-notifier`) and add these files at the same paths:
   `iawm_discord_notifier.py`, `.github/workflows/notify.yml`
2. Settings -> Secrets and variables -> Actions:
   - **Secrets** tab -> New repository secret: `DISCORD_WEBHOOK_URL` = the webhook URL
   - **Variables** tab -> New repository variable: `DISCORD_ROLE_ID` = the role ID
3. Settings -> Actions -> General -> Workflow permissions -> **Read and write permissions** (lets it save `state.json`).
4. Actions tab -> "Notify Discord of new articles" -> **Run workflow**. The first run only memorises existing posts (no pings).
   After that, each new article gets announced within about 15 minutes of publishing.

### 3. Verify the webhook (optional, from any computer with Python 3)
    DISCORD_WEBHOOK_URL="..." DISCORD_ROLE_ID="..." python3 iawm_discord_notifier.py --test

## How it behaves
- Tracks post IDs (not "newest ID": WordPress IDs on this site aren't in date order).
- Edits to old posts never re-ping. Drafts and scheduled posts are ignored until they are actually published.
- If Discord or the site is down, nothing is lost: the post stays "unseen" and is retried next run.
- Safety limits: max 5 announcements per run, and posts older than 72 hours are never announced
  (change with `MAX_PER_RUN` / `MAX_AGE_HOURS`).
- Only the configured role can be pinged, so a title containing `@everyone` cannot ping anyone.
- `DISCORD_ROLE_ID` can also be `everyone`, `here`, or empty (post with no ping).

## Notes
- GitHub's scheduler is best effort: runs can be a few minutes late. GitHub also pauses scheduled workflows in repos with
  no activity for 60 days; the bot's own `state.json` commits only happen when there's a new article, so if the blog goes
  quiet for two months, press "Enable workflow" once.
- Want it instant instead of every 15 minutes? A tiny WordPress snippet can call the same webhook the moment a post is
  published. Say the word and I'll write it.
- Tests: `python3 tests/test_notifier.py` (mock WordPress + mock Discord, 37 checks).
