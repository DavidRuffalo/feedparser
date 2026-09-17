# Internship watcher v2

Polls internship sources and pings Discord the moment a posting matches your filters.

**Sources**
- Aggregators (structured data, not README diffs): Simplify `listings.json`, vanshb03 `listings.json`, zshah101 `internships.csv`
- Direct ATS boards for your target companies: Greenhouse, Lever, Ashby, Workday — this is upstream of every aggregator and LinkedIn

**Behaviour**
- Dedup by normalised job URL + company|title, persisted in `state.json`
- Keyword in title or priority company → instant alert with role ping
- Keyword only in category/skills/team → batched digest every 3h
- Daily heartbeat; a source that breaks or returns 0 rows gets a throttled ⚠️ alert
- First run only alerts on postings < 48h old (everything older is silently marked seen)

## Setup (GitHub Actions)

1. Repo layout:
   ```
   watcher.py
   config.json
   .github/workflows/internship-watcher.yml
   ```
2. Settings → Secrets and variables → Actions → **New repository secret** `DISCORD_WEBHOOK`
   (Discord: channel → Edit → Integrations → Webhooks → New → Copy URL).
3. Optional phone-buzz: Discord server → Roles → create role `internships`, give yourself the role,
   enable Developer Mode (User Settings → Advanced), right-click the role → Copy ID → paste into
   `config.json` → `discord_role_id`. Set that channel to notify on @mentions.
4. Actions tab → Internship watcher → **Run workflow** → mode `check-targets`. Open the log;
   fix or delete any `FAIL` lines in `config.json` (wrong board token, or a company that moved ATS).
5. Run workflow again with mode `run`. You'll get alerts for anything posted in the last 48h.
6. Done — it runs every ~10 min. Check `state.json` is being committed by `github-actions[bot]`.

## Setup (always-on box — recommended for true 5-minute polling)

GitHub cron is best-effort and often 15–30 min late during the day. Any Linux box works:
Oracle Cloud "Always Free" VM, Raspberry Pi, old laptop, WSL that stays on.

```bash
sudo apt install -y python3 git
git clone <your repo> ~/internship-watcher && cd ~/internship-watcher
echo 'DISCORD_WEBHOOK=https://discord.com/api/webhooks/...' > .env && chmod 600 .env
python3 watcher.py --check-targets          # verify boards
python3 watcher.py                          # first run

sudo cp systemd/internship-watcher.service /etc/systemd/system/internship-watcher@.service
sudo cp systemd/internship-watcher.timer   /etc/systemd/system/internship-watcher@.timer
sudo systemctl daemon-reload
sudo systemctl enable --now internship-watcher@$USER.timer
systemctl list-timers | grep internship     # confirm next run
journalctl -u internship-watcher@$USER -n 30   # logs
```
If you run it on a box, disable the GitHub cron (delete the `schedule:` block) so you don't double-alert.

## Tuning `config.json`

| key | what it does |
|---|---|
| `keywords` | word → points when it appears in the **title**; 1 point if only in category/skills/team. Matches prefixes (`network` hits `networking`). |
| `require_title` / `exclude_title` | regexes; must match / must not match the title |
| `allowed_terms` | keep postings whose stated term matches (`["Summer 2027"]`); postings with no term are kept |
| `priority_companies` | alert on **any** internship from these even without a keyword hit. All `targets` are priority automatically. |
| `instant_min_score` | raise to 5+ if you're getting too many pings |
| `targets` | boards polled directly. Token = the slug in the careers URL. Run `--check-targets` after edits. |

Finding board tokens:
- `boards.greenhouse.io/<board>` or `job-boards.greenhouse.io/<board>` → `"ats":"greenhouse","board":"<board>"`
- `jobs.lever.co/<site>` → `"ats":"lever","site":"<site>"`
- `jobs.ashbyhq.com/<board>` → `"ats":"ashby","board":"<board>"`
- `<tenant>.wd5.myworkdayjobs.com/<site>/job/...` → `"ats":"workday","tenant":"<tenant>","wd":"wd5","site":"<site>"`

Use the zshah101 **Drop Radar** (bottom of their README) to see which companies are expected to post next and add them as targets a week ahead.

## CLI
```
python watcher.py                  normal run
python watcher.py --check-targets  verify every target returns data
python watcher.py --dry-run        show what would be posted; don't post or save
python watcher.py --seed           mark everything seen, alert nothing (use after a long outage)
```
