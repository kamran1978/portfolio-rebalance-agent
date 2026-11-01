import os
import requests
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
WATI_ACCESS_TOKEN = os.environ.get("WATI_ACCESS_TOKEN")
RECIPIENT_NUMBER = os.environ.get("RECIPIENT_WHATSAPP_NUMBER")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TARGET_WEIGHTS = {
    "SPY": 0.25,
    "QQQ": 0.15,
    "BND": 0.30,
    "TLT": 0.18,
    "AGG": 0.12
}

def run_portfolio_rebalancer():
    response = supabase.table("portfolio").select("*").execute()
    holdings = response.data

    total_portfolio_value = sum(float(item["current_value"]) for item in holdings)
    report_lines = ["*Daily Autonomous Rebalance Report*", f"Total Value: ${total_portfolio_value:,.2f}\n"]

    for item in holdings:
        ticker = item["ticker"]
        old_val = float(item["current_value"])
        target_val = round(total_portfolio_value * TARGET_WEIGHTS[ticker], 2)
        diff = round(target_val - old_val, 2)

        supabase.table("portfolio").update({"current_value": target_val}).eq("ticker", ticker).execute()
        
        status_symbol = "📈 +" if diff >= 0 else "📉 "
        report_lines.append(f"{ticker}: ${old_val:,.2f} ➔ ${target_val:,.2f} ({status_symbol}${diff:,.2f})")

    report_text = "\n".join(report_lines)

    # Clean phone number (digits only, no + sign)
    clean_number = "".join(filter(str.isdigit, str(RECIPIENT_NUMBER)))

    wati_url = "https://live-mt-server.wati.io/api/ext/v3/conversations/messages/text"
    headers = {
        "Authorization": f"Bearer {WATI_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "target": clean_number,
        "text": report_text
    }

    print(f"Sending message to target: {clean_number}")
    resp = requests.post(wati_url, headers=headers, json=payload)
    print(f"WATI Status Code: {resp.status_code}")
    print(f"WATI Response Body: {resp.text}")

    if resp.status_code not in (200, 201):
        raise Exception(f"WATI dispatch failed with status {resp.status_code}: {resp.text}")

    print("Rebalance complete and WhatsApp notification delivered successfully.")

if __name__ == "__main__":
    run_portfolio_rebalancer()
