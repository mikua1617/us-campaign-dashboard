"""
Lead-level engagement sync -> one persistent Google Sheet per active US campaign.

What it does, each run:
  1. For every active US_ campaign, pulls every lead via /leads (paginated) --
     each lead already carries email_open_count, email_click_count,
     email_reply_count as running totals, no need to reconstruct from events.
  2. Finds (or creates, on first run) a Google Sheet dedicated to that
     campaign, tracked in docs/sheet_links.json so we don't create a new
     sheet every day.
  3. Overwrites that sheet's data range with the current full lead list,
     sorted by opens desc. This is a living snapshot, not a history -- the
     daily email report is what gives you the historical trail; this gives
     you "who, right now".
  4. Shares the sheet (once) with the recipients list, as viewer -- viewers
     can still build their own personal sort/filter view in Sheets without
     needing edit access (Data > Filter views > Create new filter view).
  5. Writes each campaign's sheet URL back into docs/data.json so the
     dashboard and email report can link to it.

Auth: a Google Cloud service account, JSON key stored as the
GOOGLE_SERVICE_ACCOUNT_JSON secret (paste the whole key file contents as-is).
See README.md section 3c for one-time setup.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

INSTANTLY_API_KEY = os.environ.get("INSTANTLY_API_KEY")
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if not INSTANTLY_API_KEY or not GOOGLE_SA_JSON:
    sys.exit("INSTANTLY_API_KEY and/or GOOGLE_SERVICE_ACCOUNT_JSON are not set.")

BASE_URL = "https://api.instantly.ai/api/v2"
HEADERS = {"Authorization": f"Bearer {INSTANTLY_API_KEY}"}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file"]
SA_INFO = json.loads(GOOGLE_SA_JSON)
CREDS = service_account.Credentials.from_service_account_info(SA_INFO, scopes=SCOPES)
sheets_svc = build("sheets", "v4", credentials=CREDS)
drive_svc = build("drive", "v3", credentials=CREDS)

# Same recipient list as email_report.py -- kept in sync manually, both are
# small enough that a single shared constant would be overkill for now.
# Files must live in this Shared Drive, not owned directly by the service
# account -- see create_sheet_for_campaign() for why. This is the Shared
# Drive named "US Campaign Dashboard", with the service account added as a
# Content Manager member.
SHARED_DRIVE_FOLDER_ID = "0AHbv-XKfGFpmUk9PVA"

SHARE_WITH = [
    "ray.millman@ituring.ai",
    "girdhar.s@ituring.ai",
    "valsan@ituring.ai",
    "bemnet.tesfaye@ituring.ai",
    "tarika@ituring.ai",
]

DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")
SHEET_LINKS_FILE = os.path.join(DOCS_DIR, "sheet_links.json")
DATA_FILE = os.path.join(DOCS_DIR, "data.json")


def api_get(path, params=None):
    resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_post(path, body=None):
    resp = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=body or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_all_us_campaigns():
    """
    ALL campaigns whose name starts with 'US_', regardless of status --
    same reasoning as fetch_data.py: a completed campaign can still get
    late opens/clicks/replies, so its Sheet should keep updating rather
    than freezing the moment the campaign finishes sending.
    """
    campaigns = []
    starting_after = None
    while True:
        params = {"limit": 100, "exclude_status": 0}  # 0 = Draft, never actually launched
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


def get_all_leads(campaign_id):
    """
    NOTE: unlike /campaigns and /campaigns/analytics/daily (which are GET
    with query params), leads listing is a POST endpoint with a JSON body --
    Instantly's own docs explain this is a deliberate deviation because the
    filter options are too complex for a query string. Confirmed via
    developer.instantly.ai/api-reference/lead/list-leads.
    """
    leads = []
    starting_after = None
    while True:
        body = {"campaign": campaign_id, "limit": 100}
        if starting_after:
            body["starting_after"] = starting_after
        page = api_post("/leads/list", body)
        items = page.get("items", [])
        leads.extend(items)
        # Defensive: handle either a top-level cursor or one nested under
        # "pagination", since this wasn't 100% confirmed from docs alone.
        starting_after = page.get("next_starting_after") or (page.get("pagination") or {}).get("next_starting_after")
        if not starting_after or not items:
            break
    return leads


def load_sheet_links():
    if os.path.exists(SHEET_LINKS_FILE):
        with open(SHEET_LINKS_FILE) as f:
            return json.load(f)
    return {}


def save_sheet_links(links):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(SHEET_LINKS_FILE, "w") as f:
        json.dump(links, f, indent=2, sort_keys=True)


def create_sheet_for_campaign(campaign_name):
    """
    IMPORTANT: service accounts have a permanent 0 GB personal Drive quota
    and cannot own files directly -- Google's own docs confirm this. Files
    must be created inside a Shared Drive instead, which has its own pooled
    storage. Hence: create via the DRIVE API (not Sheets API's own create
    endpoint) with the shared drive as parent, and supportsAllDrives=True on
    every Drive API call that touches this file (create AND sharing).
    """
    file_metadata = {
        "name": f"Leads — {campaign_name}",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [SHARED_DRIVE_FOLDER_ID],
    }
    created = drive_svc.files().create(
        body=file_metadata, fields="id", supportsAllDrives=True
    ).execute()
    sheet_id = created["id"]
    for email in SHARE_WITH:
        drive_svc.permissions().create(
            fileId=sheet_id,
            body={"type": "user", "role": "reader", "emailAddress": email},
            sendNotificationEmail=False,
            supportsAllDrives=True,
        ).execute()
    return sheet_id


def sync_leads_to_sheet(sheet_id, leads):
    """
    Every meaningfully distinct field from the lead record. Deliberately
    excluded: `organization` (same internal workspace ID on every row, no
    information value), `assigned_to` (an internal Instantly user ID with no
    human-readable resolution available via this API), and `payload` (an
    exact duplicate of first_name/company_name/job_title/email already
    shown separately). `status` is included as Instantly's raw internal
    code -- there's no publicly documented mapping for these integers, so
    we show the number as-is rather than guess a label.
    """
    header = [
        "Lead ID", "Email", "First name", "Company name", "Company domain",
        "Job title", "Phone", "Status (raw code)",
        "Opens", "Clicks", "Replies",
        "Created (UTC)", "Last contacted (UTC)", "Last touch (UTC)",
        "Last step - from", "Last step - ID", "Last step - executed (UTC)",
        "Upload method", "ESP code", "ESG code",
    ]
    rows = [header]
    # Sort by total engagement (opens+clicks+replies), not opens alone --
    # matches the master pivot's definition, so someone who clicked/replied
    # without a tracked open doesn't rank artificially low.
    leads_sorted = sorted(
        leads,
        key=lambda l: l.get("email_open_count", 0) + l.get("email_click_count", 0) + l.get("email_reply_count", 0),
        reverse=True,
    )
    for lead in leads_sorted:
        last_step = (lead.get("status_summary") or {}).get("lastStep") or {}
        rows.append([
            lead.get("id", ""),
            lead.get("email", ""),
            lead.get("first_name", ""),
            lead.get("company_name", ""),
            lead.get("company_domain", ""),
            lead.get("job_title", ""),
            lead.get("phone", ""),
            lead.get("status", ""),
            lead.get("email_open_count", 0),
            lead.get("email_click_count", 0),
            lead.get("email_reply_count", 0),
            lead.get("timestamp_created", ""),
            lead.get("timestamp_last_contact", ""),
            lead.get("timestamp_last_touch", ""),
            last_step.get("from", ""),
            last_step.get("stepID", ""),
            last_step.get("timestamp_executed", ""),
            lead.get("upload_method", ""),
            lead.get("esp_code", ""),
            lead.get("esg_code", ""),
        ])

    sheets_svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range="A1:Z10000", body={}
    ).execute()
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def get_or_create_master_pivot_sheet(links):
    key = "_master_pivot"
    if key not in links:
        sheet_id = create_sheet_for_campaign("US Campaigns — Master Engagement Pivot")
        links[key] = {
            "sheet_id": sheet_id,
            "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
        }
        print(f"  Created master pivot sheet: {links[key]['url']}")
    return links[key]


def sync_master_pivot(sheet_id, all_leads_by_campaign):
    """
    One row per unique person (by email), aggregated across every active US
    campaign they appear in. 'Total interactions' = opens + clicks + replies
    summed across all their campaigns, sorted descending -- this answers
    'who has engaged with us the most, everywhere, all time'.
    """
    by_email = {}
    for campaign_name, leads in all_leads_by_campaign.items():
        for lead in leads:
            email = lead.get("email", "")
            if not email:
                continue
            entry = by_email.setdefault(email, {
                "first_name": lead.get("first_name", ""),
                "company_name": lead.get("company_name", ""),
                "job_title": lead.get("job_title", ""),
                "phone": lead.get("phone", ""),
                "opens": 0, "clicks": 0, "replies": 0,
                "campaigns": set(),
            })
            entry["opens"] += lead.get("email_open_count", 0)
            entry["clicks"] += lead.get("email_click_count", 0)
            entry["replies"] += lead.get("email_reply_count", 0)
            entry["campaigns"].add(campaign_name)
            # Fill in name/company/phone if this campaign's record has it
            # and an earlier one didn't (leads can have sparse fields).
            for field in ("first_name", "company_name", "job_title", "phone"):
                if not entry[field] and lead.get(field):
                    entry[field] = lead.get(field)

    header = ["Email", "First name", "Company", "Job title", "Phone",
              "Total interactions", "Total opens", "Total clicks", "Total replies", "Campaigns"]
    rows = [header]
    ranked = sorted(by_email.items(), key=lambda kv: kv[1]["opens"] + kv[1]["clicks"] + kv[1]["replies"], reverse=True)
    for email, e in ranked:
        total = e["opens"] + e["clicks"] + e["replies"]
        rows.append([
            email, e["first_name"], e["company_name"], e["job_title"], e["phone"],
            total, e["opens"], e["clicks"], e["replies"],
            ", ".join(sorted(e["campaigns"])),
        ])

    sheets_svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range="A1:Z10000", body={}
    ).execute()
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def main():
    links = load_sheet_links()
    campaigns = get_all_us_campaigns()

    with open(DATA_FILE) as f:
        data = json.load(f)

    all_leads_by_campaign = {}

    for c in campaigns:
        name, cid = c["name"], c["id"]
        print(f"Syncing leads sheet: {name}")

        if name not in links:
            sheet_id = create_sheet_for_campaign(name)
            links[name] = {
                "sheet_id": sheet_id,
                "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
            }
            print(f"  Created new sheet: {links[name]['url']}")

        leads = get_all_leads(cid)
        all_leads_by_campaign[name] = leads
        sync_leads_to_sheet(links[name]["sheet_id"], leads)

        # Make the link available to the dashboard/email report.
        bucket = data.get("campaigns", {}).get(name)
        if bucket is not None:
            bucket.setdefault("current", {})["leads_sheet_url"] = links[name]["url"]

    # Cross-campaign master pivot: who has engaged the most, everywhere.
    print("Syncing master engagement pivot")
    master = get_or_create_master_pivot_sheet(links)
    sync_master_pivot(master["sheet_id"], all_leads_by_campaign)
    data["master_pivot_url"] = master["url"]

    save_sheet_links(links)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)

    print(f"Done. {len(campaigns)} campaign sheet(s) + 1 master pivot synced.")


if __name__ == "__main__":
    main()
