import os
import smtplib
from datetime import date
from email.message import EmailMessage
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECIPIENTS = os.environ.get("RECIPIENT_EMAILS", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TARGET_WEIGHTS = {
    "SPY": 0.25,
    "QQQ": 0.15,
    "BND": 0.30,
    "TLT": 0.18,
    "AGG": 0.12
}

def send_email(subject, body):
    to_list = [e.strip() for e in RECIPIENTS.split(",") if e.strip()]
    if not to_list:
        raise Exception("No recipients. Set RECIPIENT_EMAILS secret.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Email sent to {len(to_list)} people.")

def run_portfolio_rebalancer():
    holdings = supabase.table("portfolio").select("*").execute().data
    total = sum(float(i["current_value"]) for i in holdings)
    lines = ["Daily Autonomous Rebalance Report", f"Total Value: ${total:,.2f}", ""]

    for item in holdings:
        ticker = item["ticker"]
        old_val = float(item["current_value"])
        target_val = round(total * TARGET_WEIGHTS[ticker], 2)
        diff = round(target_val - old_val, 2)
        supabase.table("portfolio").update({"current_value": target_val}).eq("ticker", ticker).execute()
        sign = "📈 +" if diff >= 0 else "📉 "
        lines.append(f"{ticker}: ${old_val:,.2f} ➔ ${target_val:,.2f} ({sign}${diff:,.2f})")

    print("Supabase portfolio successfully rebalanced.")
    send_email(f"Portfolio Report – {date.today():%d %b %Y}", "\n".join(lines))

if __name__ == "__main__":
    run_portfolio_rebalancer()
