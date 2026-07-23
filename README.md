# US campaign dashboard — setup checklist

## 1. Create the repo
1. Go to github.com → New repository.
2. Name it something like `us-campaign-dashboard`. Private is fine — GitHub
   Pages can serve from a private repo too (on paid plans) but if you're on
   a free personal account, Pages from a private repo needs GitHub Pro/Team.
   If you're on the free plan, make the repo **public** — the data it exposes
   is just aggregate send/open/click counts, no lead emails or content.
3. Don't initialize with a README (we already have one).

## 2. Upload the files
Upload these four files/folders, keeping the exact folder structure:
```
.github/workflows/daily-update.yml
docs/index.html
docs/data.json
fetch_data.py
README.md
```
Easiest way: on the repo's main page, click "Add file" → "Upload files",
drag in everything (GitHub preserves folder paths when you drag a folder).

## 3. Add your Instantly API key as a secret
1. Repo → Settings → Secrets and variables → Actions.
2. Click "New repository secret".
3. Name: `INSTANTLY_API_KEY`
4. Value: your Instantly API v2 key (Instantly dashboard → Settings → API Keys).
   Generate a new one here if you don't have a v2 key yet — v1 keys won't work.
5. Save. This value is encrypted and never visible again, even to you — that's
   expected.

## 4. Enable GitHub Pages
1. Repo → Settings → Pages.
2. Under "Build and deployment" → Source: "Deploy from a branch".
3. Branch: `main`, folder: `/docs`. Save.
4. GitHub will give you a URL like `https://<username>.github.io/us-campaign-dashboard/`
   — that's the link to share with the US team.

## 5. Run it once manually to check it works
1. Repo → Actions tab → "Daily Instantly dashboard update" (left sidebar).
2. Click "Run workflow" → "Run workflow" (this uses the `workflow_dispatch`
   trigger, so you don't have to wait for 8am IST to test it).
3. Wait ~30-60 seconds, refresh — you should see a green checkmark.
4. Open your Pages URL. If data.json only just got its first real numbers,
   give Pages another minute or two to redeploy, then refresh.

## What happens after that
Every day at 8:00 AM IST (02:30 UTC), the Action runs automatically:
fetches the last 14 days of daily metrics, the rolling-24h sent/replies count,
and the lifetime bounce count for every active campaign whose name starts
with `US_`, and commits the updated `docs/data.json`. The dashboard page
re-fetches that file on every page load, so anyone with the link always sees
the latest committed numbers — no manual refresh step needed on your end.

## When you add a new US campaign
Just make sure its name starts with `US_` and follows the
`Geography_Name_DD/MM/YY` convention. The next scheduled run will pick it up
automatically — nothing else to configure.

## If something breaks
Check Repo → Actions → click the failed run → expand the "Run fetch script"
step. Common causes: the API key secret is missing/wrong, or Instantly's rate
limit was hit (unlikely at this campaign volume, but the `/emails` endpoint is
capped at 20 requests/minute specifically).
