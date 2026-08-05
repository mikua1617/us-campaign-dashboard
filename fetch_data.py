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


def api_post(path, body=None):
    resp = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=body or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_all_leads(campaign_id):
    """
    Full lead list for a campaign -- needed to compute TRUE per-person
    open/click rates (distinct people who engaged, out of total leads),
    not just event counts. NOTE: unlike /campaigns (GET), this is a POST
    endpoint with a JSON body -- confirmed via developer.instantly.ai.
    """
    leads = []
    starting_after = None
    page_num = 0
    while True:
        page_num += 1
        body = {"campaign": campaign_id, "limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        page = api_post("/leads/list", body)
        items = page.get("items", [])
        if not items and page_num == 1:
            # Empty first page is suspicious -- log the raw response so we
            # can tell a real "zero leads" apart from a silent API issue
            # (e.g. rate limiting that returns 200 with an unexpected body).
            print(f"    WARNING: /leads/list page 1 for campaign {campaign_id} returned no items. Raw response keys: {list(page.keys())}, full response: {page}")
        leads.extend(items)
        starting_after = page.get("next_starting_after") or (page.get("pagination") or {}).get("next_starting_after")
        if not starting_after or not items:
            break
    return leads


def get_all_us_campaigns():
    """
    ALL campaigns whose name starts with 'US_', regardless of status --
    no status filter. A completed campaign can still accumulate opens/
    clicks/replies for weeks after its last send, so we keep tracking it
    rather than freezing or dropping it. Each campaign's status is kept so
    the dashboard can split into Active vs Completed sections.
    """
    campaigns = []
    starting_after = None
    while True:
        params = {"limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        page = api_get("/campaigns", params)
        items = page.get("items", [])
        for c in items:
            if c["name"].startswith("US_"):
                campaigns.append({"id": c["id"], "name": c["name"], "status": c["status"]})
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


def get_lifetime_overview(campaign_id):
    """
    Lifetime totals: bounces, opens, clicks, replies -- all from one call.
    NOTE: omitting start_date/end_date does NOT mean 'all time' on this
    endpoint -- it appears to default to today only. We force a wide explicit
    range instead (campaign launch dates are all in 2026, so 2020-01-01
    comfortably covers everything).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    overview = api_get(
        "/campaigns/analytics/overview",
        {"id": campaign_id, "start_date": "2020-01-01", "end_date": today},
    )
    row = {}
    if isinstance(overview, list) and overview:
        row = overview[0]
    elif isinstance(overview, dict):
        row = overview
    return {
        "sent_lifetime": row.get("emails_sent_count", 0),
        "bounced_lifetime": row.get("bounced_count", 0),
        "opens_lifetime": row.get("open_count", 0),
        "clicks_lifetime": row.get("link_click_count", 0),
        # Unique = distinct people who opened/clicked at least once. Use
        # THESE for rate/percentage calculations -- opens_lifetime/
        # clicks_lifetime are total EVENTS (can exceed sent count if people
        # open the same email multiple times) and will produce nonsensical
        # rates over 100% if used as a percentage denominator's numerator.
        "opens_unique_lifetime": row.get("open_count_unique", 0),
        "clicks_unique_lifetime": row.get("link_click_count_unique", 0),
        "replies_lifetime": row.get("reply_count", 0),
    }


def count_recent_emails(campaign_id, email_type, since_dt):
    """
    True rolling-window count: paginate /emails for this campaign and count how
    many have timestamp_email >= since_dt. Stops paging once results are older
    than the window, since /emails is returned newest-first.

    email_type: 'sent' or 'received' (received = replies/inbound)
    """
    count = 0
    starting_after = None
    page_num = 0
    while True:
        page_num += 1
        params = {"campaign_id": campaign_id, "email_type": email_type, "limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        page = api_get("/emails", params)
        items = page.get("items", [])
        if not items:
            if page_num == 1:
                print(f"    WARNING: /emails ({email_type}) page 1 for campaign {campaign_id} returned no items. Raw response keys: {list(page.keys())}, full response: {page}")
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
    campaigns = get_all_us_campaigns()

    if not campaigns:
        print("No US_ campaigns found at all. Check naming convention.")

    for c in campaigns:
        name, cid, status = c["name"], c["id"], c["status"]
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

        # 3. Lifetime totals (1 call): bounces, opens, clicks, replies
        lifetime = get_lifetime_overview(cid)

        # 4. TRUE per-person open/click rate. The overview endpoint's
        # open_count/open_count_unique are EVENT counts (they can exceed
        # total leads if a sequence has multiple follow-up steps, since
        # each step's opens/clicks get counted separately). To answer
        # "what % of people we reached actually opened something", we need
        # to pull every lead and count how many have email_open_count >= 1
        # ourselves -- a genuinely person-level count.
        leads = get_all_leads(cid)
        leads_count = len(leads)
        unique_openers = sum(1 for l in leads if l.get("email_open_count", 0) >= 1)
        unique_clickers = sum(1 for l in leads if l.get("email_click_count", 0) >= 1)

        bucket["current"] = {
            "status": status,  # 1 = active, anything else = completed/paused/other
            "sent_24h": sent_24h,
            "replies_24h": replies_24h,
            **lifetime,
            "leads_count": leads_count,
            "unique_openers_lifetime": unique_openers,
            "unique_clickers_lifetime": unique_clickers,
            "as_of": now.isoformat(),
        }
        print(f"    -> status={status}, sent_24h={sent_24h}, replies_24h={replies_24h}, leads_count={leads_count}, unique_openers={unique_openers}, unique_clickers={unique_clickers}, sent_lifetime={lifetime.get('sent_lifetime')}")

    data["generated_at"] = now.isoformat()

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)

    print(f"Wrote {DATA_FILE}")


if __name__ == "__main__":
    main()
