"""
Daily fetch script for the US campaign dashboard.

What it does, each time it runs:
  1. Finds active campaigns whose name starts with "US_" (our naming convention:
     Geography_Name_DD/MM/YY).
  2. Pulls the last 14 calendar days of sent/opened/clicked/replied per campaign.
  3. Pulls a true rolling-24h sent/replies count per campaign, using per-email
     timestamps (this is the one metric Instantly can give as a real sliding
     window rather than a calendar-day bucket).
  4. Pulls the lifetime bounce count per campaign (one call, no date range).
  5. Upserts everything into docs/data.json, keyed by campaign name and date.
     "Upsert" means: overwrite the entry for a given date if it already exists,
     don't append a duplicate. This lets late-arriving opens/replies correct
     earlier days without creating dupes.

Run this once a day at a fixed time (we chose 8am IST) so "today" always means
the same thing run over run. See .github/workflows/daily-update.yml for the
schedule.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_KEY = os.environ.get("INSTANTLY_API_KEY")
if not API_KEY:
    sys.exit("INSTANTLY_API_KEY environment variable is not set.")

BASE_URL = "https://api.instantly.ai/api/v2"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

DATA_FILE = os.path.join(os.path.dirname(__file__), "docs", "data.json")
DAILY_WINDOW_DAYS = 14  # how many days of calendar-day history we keep/refresh


def api_get(path, params=None):
    resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_active_us_campaigns():
    """Active campaigns (status=1) whose name starts with 'US_'."""
    campaigns = []
    starting_after = None
    while True:
        params = {"limit": 100, "status": 1}
        if starting_after:
            params["starting_after"] = starting_after
        page = api_get("/campaigns", params)
        items = page.get("items", [])
        for c in items:
            if c["name"].startswith("US_"):
                campaigns.append({"id": c["id"], "name": c["name"]})
        starting_after = page.get("next_starting_after")
        if not starting_after or not items:
            break
    return campaigns


def get_daily_analytics(campaign_id, start_date, end_date):
    """Calendar-day buckets: sent, opened, clicks, replies. No bounce field here."""
    return api_get(
        "/campaigns/analytics/daily",
        {"campaign_id": campaign_id, "start_date": start_date, "end_date": end_date},
    )


def get_lifetime_bounce_count(campaign_id):
    """One call, no date range = lifetime totals. We only need bounced_count."""
    overview = api_get("/campaigns/analytics/overview", {"id": campaign_id})
    if isinstance(overview, list) and overview:
        return overview[0].get("bounced_count", 0)
    return 0


def count_recent_emails(campaign_id, email_type, since_dt):
    """
    True rolling-window count: paginate /emails for this campaign and count how
    many have timestamp_email >= since_dt. Stops paging once results are older
    than the window, since /emails is returned newest-first.

    email_type: 'sent' or 'received' (received = replies/inbound)
    """
    count = 0
    starting_after = None
    while True:
        params = {"campaign_id": campaign_id, "email_type": email_type, "limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        page = api_get("/emails", params)
        items = page.get("items", [])
        if not items:
            break
        stop = False
        for item in items:
            ts = datetime.fromisoformat(item["timestamp_email"].replace("Z", "+00:00"))
            if ts >= since_dt:
                count += 1
            else:
                stop = True
                break
        if stop:
            break
        starting_after = page.get("next_starting_after")
        if not starting_after:
            break
    return count


def load_existing_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"campaigns": {}, "generated_at": None}


def main():
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    window_start = (now - timedelta(days=DAILY_WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
    window_end = now.strftime("%Y-%m-%d")

    data = load_existing_data()
    campaigns = get_active_us_campaigns()

    if not campaigns:
        print("No active US_ campaigns found. Check naming convention or campaign status.")

    for c in campaigns:
        name, cid = c["name"], c["id"]
        print(f"Fetching: {name}")

        bucket = data["campaigns"].setdefault(name, {"id": cid, "days": {}, "current": {}})
        bucket["id"] = cid  # keep id fresh in case it's a new entry

        # 1. Calendar-day rows (sent/opened/clicks/replies) — upsert by date
        daily_rows = get_daily_analytics(cid, window_start, window_end)
        for row in daily_rows:
            date = row["date"]
            bucket["days"][date] = {
                "sent": row.get("sent", 0),
                "opened": row.get("opened", 0),
                "unique_opened": row.get("unique_opened", 0),
                "clicks": row.get("clicks", 0),
                "unique_clicks": row.get("unique_clicks", 0),
                "replies": row.get("replies", 0),
            }

        # Drop days older than our window so the file doesn't grow forever
        cutoff_date = (now - timedelta(days=DAILY_WINDOW_DAYS)).strftime("%Y-%m-%d")
        bucket["days"] = {d: v for d, v in bucket["days"].items() if d >= cutoff_date}

        # 2. True rolling-24h sent/replies, from per-email timestamps
        sent_24h = count_recent_emails(cid, "sent", cutoff_24h)
        replies_24h = count_recent_emails(cid, "received", cutoff_24h)

        # 3. Lifetime bounce count (1 call, no date range)
        bounced_lifetime = get_lifetime_bounce_count(cid)

        bucket["current"] = {
            "sent_24h": sent_24h,
            "replies_24h": replies_24h,
            "bounced_lifetime": bounced_lifetime,
            "as_of": now.isoformat(),
        }

    data["generated_at"] = now.isoformat()

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)

    print(f"Wrote {DATA_FILE}")


if __name__ == "__main__":
    main()
