"""
Daily email report for the US campaign dashboard.

Runs after fetch_data.py in the same workflow. Reads the just-updated
docs/data.json and sends a self-contained HTML email (plain inline-styled
tables, no JavaScript/charts -- email clients don't run JS) to the
recipients list below.

This is a separate, permanent, archivable snapshot of that day's numbers --
unlike the live website, which only ever shows "now", each email is a fixed
record of what things looked like on that date. That's the point of it.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

DATA_FILE = os.path.join(os.path.dirname(__file__), "docs", "data.json")

# Edit this list directly to add/remove recipients -- not a secret, just names.
RECIPIENTS = [
    "ray.millman@ituring.ai",
    "girdhar.s@ituring.ai",
    "valsan@ituring.ai",
    "bemnet.tesfaye@ituring.ai",
    "tarika@ituring.ai",
]

DASHBOARD_URL = "https://mikua1617.github.io/us-campaign-dashboard/"

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
    sys.exit("GMAIL_ADDRESS and/or GMAIL_APP_PASSWORD environment variables are not set.")


def pct(n, d):
    return round((n / d) * 1000) / 10 if d else 0


def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def aggregate_totals(campaigns):
    """Same aggregation logic as the website: sum per-date across campaigns."""
    all_dates = sorted({d for c in campaigns.values() for d in c.get("days", {})})
    totals = []
    for date in all_dates:
        sent = opened = clicks = replies = 0
        for c in campaigns.values():
            row = c.get("days", {}).get(date)
            if row:
                sent += row.get("sent", 0)
                opened += row.get("opened", 0)
                clicks += row.get("clicks", 0)
                replies += row.get("replies", 0)
        totals.append({"date": date, "sent": sent, "opened": opened, "clicks": clicks, "replies": replies})
    return totals


def build_html(data):
    campaigns = data.get("campaigns", {})
    generated_at = data.get("generated_at")
    totals = aggregate_totals(campaigns)
    today = totals[-1] if totals else {}
    yesterday = totals[-2] if len(totals) > 1 else {}

    sent_24h = sum(c.get("current", {}).get("sent_24h", 0) for c in campaigns.values())
    replies_24h = sum(c.get("current", {}).get("replies_24h", 0) for c in campaigns.values())
    bounced_lifetime = sum(c.get("current", {}).get("bounced_lifetime", 0) for c in campaigns.values())

    def delta_text(t, y):
        if not y:
            return ""
        diff = t - y
        if diff == 0:
            return "flat vs yesterday"
        arrow = "&uarr;" if diff > 0 else "&darr;"
        change = abs(round((diff / y) * 100)) if y else 0
        return f"{arrow} {change}% vs yesterday"

    kpi_cells = [
        ("Active campaigns", len(campaigns), ""),
        ("Sent (24h)", sent_24h, ""),
        ("Replies (24h)", replies_24h, ""),
        ("Opens (today)", today.get("opened", 0), delta_text(today.get("opened", 0), yesterday.get("opened", 0))),
        ("Clicks (today)", today.get("clicks", 0), delta_text(today.get("clicks", 0), yesterday.get("clicks", 0))),
        ("Bounces (lifetime)", bounced_lifetime, ""),
    ]

    kpi_html = "".join(
        f"""<td style="padding:12px 16px; background:#f7f7f5; border-radius:8px;">
              <div style="font-size:12px; color:#767671;">{label}</div>
              <div style="font-size:20px; font-weight:600; color:#1a1a19;">{value}</div>
              <div style="font-size:11px; color:#767671; margin-top:2px;">{delta}</div>
            </td><td style="width:8px;"></td>"""
        for label, value, delta in kpi_cells
    )

    rows_html = ""
    for name, c in campaigns.items():
        last_date = max(c.get("days", {}).keys(), default=None)
        last = c.get("days", {}).get(last_date, {}) if last_date else {}
        cur = c.get("current", {})
        open_pct = pct(cur.get("opens_lifetime", 0), cur.get("sent_lifetime", 0))
        click_pct = pct(cur.get("clicks_lifetime", 0), cur.get("sent_lifetime", 0))
        bounce_high = cur.get("bounced_lifetime", 0) > 5
        bounce_style = "color:#b23b3b; font-weight:600;" if bounce_high else ""
        rows_html += f"""
        <tr>
          <td style="padding:6px 8px; border-bottom:1px solid #eee;">{name}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{cur.get('sent_24h', 0)}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{last.get('opened', 0)}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{last.get('clicks', 0)}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{last.get('replies', 0)}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{open_pct}%</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{click_pct}%</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{cur.get('sent_lifetime', 0)}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{cur.get('opens_lifetime', 0)}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{cur.get('clicks_lifetime', 0)}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right;">{cur.get('replies_lifetime', 0)}</td>
          <td style="padding:6px 8px; border-bottom:1px solid #eee; text-align:right; {bounce_style}">{cur.get('bounced_lifetime', 0)}</td>
        </tr>"""

    generated_str = (
        datetime.fromisoformat(generated_at).strftime("%d %b %Y, %I:%M %p UTC")
        if generated_at else "unknown"
    )

    return f"""
    <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; color:#1a1a19; max-width:900px;">
      <h2 style="margin-bottom:4px;">US campaign dashboard — daily report</h2>
      <p style="font-size:13px; color:#767671; margin-top:0;">
        Generated {generated_str} &middot;
        <a href="{DASHBOARD_URL}" style="color:#2a78d6;">View live dashboard &rarr;</a>
      </p>

      <table cellspacing="0" cellpadding="0" style="margin: 16px 0;"><tr>{kpi_html}</tr></table>

      <table cellspacing="0" cellpadding="0" style="width:100%; border-collapse:collapse; font-size:13px;">
        <thead>
          <tr style="text-align:right;">
            <th style="text-align:left; padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Campaign</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Sent 24h</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Opens today</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Clicks today</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Replies today</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Open %</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Click %</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Total sent</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Total opens</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Total clicks</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Total replies</th>
            <th style="padding:6px 8px; color:#767671; border-bottom:1px solid #ccc;">Bounces (lifetime)</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """


def send_email(html_body):
    today_str = datetime.now(timezone.utc).strftime("%d %b %Y")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"US campaign dashboard — {today_str}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENTS, msg.as_string())


def main():
    data = load_data()
    if not data.get("campaigns"):
        print("No campaign data yet — skipping email (nothing to report).")
        return
    html = build_html(data)
    send_email(html)
    print(f"Report sent to: {', '.join(RECIPIENTS)}")


if __name__ == "__main__":
    main()
