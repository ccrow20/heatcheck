# Heat Check

Shows which NBA players are playing above or below their normal level over their last 5 games.

A Lambda pulls box scores every morning, scores each player against their own baseline, and writes one JSON file that a static site on CloudFront reads.

(Screenshot uses sample data, the players are made up.)

## How it works

- EventBridge runs the Lambda twice a day. It re-fetches the last two nights (to catch stat corrections) and saves one JSON file per night in S3.
- ESPN's API is the main source. API-NBA is a backup if ESPN fails or returns something weird.
- Each player's last 5 games are compared to their previous 30 as a z-score. The player's SD is shrunk toward the league SD so low-variance bench guys don't blow up. Hot = z >= 1.5 and above a minimum average, cold = z <= -1.5 and normally above it. Details in `backend/src/heatcheck/analytics.py`.
- The output goes to `data/streaks.json` in the site bucket. The frontend is plain HTML/CSS/JS, no build step.
- CloudWatch alarms email me if a run fails, no run succeeds for 24h, or ESPN starts failing.

## Layout

```
backend/   Lambda code (stdlib only), CLI, tests, scripts
frontend/  the site
infra/     AWS CDK stack
```

## Running locally

Needs Python 3.11+.

```bash
make install
make test
make sample   # fake league data into frontend/data/
make serve    # http://localhost:8000
```

`make run-local` pulls the last two real nights from ESPN instead.

## Deploying

Needs the AWS CLI set up and Node 20+ (for CDK).

```bash
cp infra/config.example.json infra/config.json   # fill in alert_email, github_repo
make infra-install
make bootstrap   # once per account/region
make deploy
```

Then:

1. Confirm the SNS email subscription.
2. `make invoke` to do a first run.
3. Backfill so players have a baseline: `make backfill START=2025-10-21 END=2026-11-05 && make publish` (about 30 min per season).
4. Optional: `make set-api-key` to add an API-NBA key for the backup source.
5. Optional: `make github-vars` so pushes to main deploy through GitHub Actions (OIDC, no stored keys).

`make help` lists everything else.

## Cost

Basically free. Lambda, Scheduler and CloudWatch stay in the free tier, S3 and CloudFront are a few cents. There's a budget alarm at $5/month.

## Known issues

- ESPN's API is undocumented and could change anytime. `make check-sources` (also run weekly in CI) checks the parsers against the live API.
- Players are matched by name across sources, so two players with the same name would merge. `NAME_ALIASES` in `names.py` handles that.
- API-NBA doesn't filter preseason games.
- Doesn't account for injuries, opponent, rest, etc.
