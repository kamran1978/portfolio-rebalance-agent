import os
import smtplib
import requests
from datetime import date
from email.message import EmailMessage
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECIPIENTS = os.environ.get("RECIPIENT_EMAILS", "")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 80% stocks / 20% bonds
TARGET_WEIGHTS = {
    "SPY": 0.50,
    "QQQ": 0.30,
    "BND": 0.10,
    "TLT": 0.05,
    "AGG": 0.05
}
STOCKS = {"SPY", "QQQ"}
DRIFT_LIMIT = 0.05   # only suggest trades if a fund is more than 5% off target


def get_price(ticker):
    r = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": ticker, "token": FINNHUB_API_KEY},
        timeout=20,
    )
    r.raise_for_status()
    price = float(r.json().get("c") or 0)
    if price <= 0:
        raise Exception(f"Finnhub gave no price for {ticker}. Check FINNHUB_API_KEY.")
    return price


def send_email(subject, body):
    to_list = [e.strip() for e in RECIPIENTS.split(",") if e.strip()]
    address = (GMAIL_ADDRESS or "").strip()
    password = "".join((GMAIL_APP_PASSWORD or "").split())

    print(f"Checks -> address has @: {'@' in address} | password length: {len(password)} (must be 16) | recipients: {len(to_list)}")
    if not to_list:
        raise Exception("No recipients. Set RECIPIENT_EMAILS secret.")
    if len(password) != 16:
        raise Exception("GMAIL_APP_PASSWORD is not 16 letters. Re-enter it with no spaces.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(address, password)
        smtp.send_message(msg)
    print(f"Email sent to {len(to_list)} people.")


def run_portfolio_rebalancer():
    if not FINNHUB_API_KEY:
        raise Exception("FINNHUB_API_KEY secret is missing.")

    holdings = supabase.table("portfolio").select("*").execute().data

    # 1. Get today's price and work out what each fund is worth
    rows = []
    for item in holdings:
        ticker = item["ticker"]
        price = get_price(ticker)
        shares = item.get("shares")

        if shares is None:
            # First run only: turn the dollar amount into a number of shares
            shares = float(item["current_value"]) / price
            supabase.table("portfolio").update({"shares": shares}).eq("ticker", ticker).execute()
        shares = float(shares)

        value = round(shares * price, 2)
        supabase.table("portfolio").update({"current_value": value}).eq("ticker", ticker).execute()
        rows.append({"ticker": ticker, "price": price, "shares": shares, "value": value})

    total = sum(r["value"] for r in rows)
    stock_pct = sum(r["value"] for r in rows if r["ticker"] in STOCKS) / total * 100

    # 2. Compare with the 80/20 plan
    lines = [
        "Daily Portfolio Report",
        f"Total value: ${total:,.2f}",
        f"Split now: {stock_pct:.1f}% stocks / {100 - stock_pct:.1f}% bonds  (plan: 80 / 20)",
        "",
        "Fund | Price | Value | Now % | Target %",
    ]
    trades = []
    for r in rows:
        now_w = r["value"] / total
        target_w = TARGET_WEIGHTS[r["ticker"]]
        lines.append(f"{r['ticker']} | ${r['price']:,.2f} | ${r['value']:,.2f} | {now_w*100:.1f}% | {target_w*100:.0f}%")
        if abs(now_w - target_w) > DRIFT_LIMIT:
            diff = round(total * target_w - r["value"], 2)
            action = "BUY" if diff > 0 else "SELL"
            trades.append(f"{action} ${abs(diff):,.2f} of {r['ticker']}")

    lines.append("")
    if trades:
        lines.append("Rebalance suggested (a fund is more than 5% off plan):")
        lines += [f"  - {t}" for t in trades]
    else:
        lines.append("No action needed today. Everything is within 5% of plan.")
    lines.append("")
    lines.append("This is a suggestion only. No real trades were made.")

    report = "\n".join(lines)
    print(report)
    send_email(f"Portfolio Report – {date.today():%d %b %Y}", report)


if __name__ == "__main__":
    run_portfolio_rebalancer()
