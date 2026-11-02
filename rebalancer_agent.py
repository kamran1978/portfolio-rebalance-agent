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
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 80% stocks / 20% bonds
TARGET_WEIGHTS = {
    "SPY": 0.50,
    "QQQ": 0.30,
    "BND": 0.10,
    "TLT": 0.05,
    "AGG": 0.05,
}
STOCKS = {"SPY", "QQQ"}
DRIFT_LIMIT = 0.05

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
FINNHUB_URL = "https://finnhub.io/api/v1/quote"

PROMPT = """You are a friendly portfolio teacher.
Write a short morning email for a beginner investor.
Always write in English only.
Use very simple English and short sentences.
Plain text only. No markdown, no asterisks.

Use exactly these 3 sections:

1. DECISION: Start with "REBALANCE TODAY" or
"WAIT - NO REBALANCE NEEDED". Then give the reason
in 2-3 sentences. Mention drift, trading costs,
and the 5% rule.

2. WHAT THE NUMBERS MEAN: Explain Price, Value,
Now %, Target %, and the stocks/bonds split,
using today's real numbers.

3. LESSON OF THE DAY: Teach one simple investing
idea linked to today's numbers, with a tiny example.

Keep it under 300 words.
End with: "This is education, not financial advice."

Today's portfolio data:
"""


def get_price(ticker):
    params = {"symbol": ticker, "token": FINNHUB_KEY}
    r = requests.get(FINNHUB_URL, params=params, timeout=20)
    r.raise_for_status()
    price = float(r.json().get("c") or 0)
    if price <= 0:
        raise Exception("Finnhub gave no price for " + ticker)
    return price


def pick_gemini_models():
    names = ["gemini-flash-latest"]
    try:
        params = {"key": GEMINI_KEY, "pageSize": 200}
        url = GEMINI_BASE + "/models"
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        found = []
        for m in r.json().get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods:
                found.append(m["name"].replace("models/", ""))
        flash = []
        for n in found:
            if "flash" not in n:
                continue
            if "lite" in n or "image" in n or "tts" in n:
                continue
            flash.append(n)
        names += sorted(flash, reverse=True) + found
    except Exception as e:
        print("Could not list Gemini models:", e)
    ordered = []
    for n in names:
        if n not in ordered:
            ordered.append(n)
    return ordered


def ask_gemini(numbers_report):
    if not GEMINI_KEY:
        return "(AI analyst skipped: GEMINI_API_KEY missing.)"
    prompt = PROMPT + numbers_report
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    for model in pick_gemini_models()[:6]:
        url = GEMINI_BASE + "/models/" + model + ":generateContent"
        try:
            r = requests.post(
                url,
                params={"key": GEMINI_KEY},
                json=body,
                timeout=90,
            )
            if r.status_code == 404:
                print("Model not available:", model)
                continue
            r.raise_for_status()
            data = r.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = parts[0]["text"].strip()
            print("Gemini model used:", model)
            return text
        except Exception as e:
            print("Gemini error with", model, ":", e)
    return "(AI analyst could not run today. Numbers are still correct.)"


def send_email(subject, body):
    to_list = []
    for e in RECIPIENTS.split(","):
        if e.strip():
            to_list.append(e.strip())
    address = (GMAIL_ADDRESS or "").strip()
    password = "".join((GMAIL_APP_PASSWORD or "").split())
    if not to_list:
        raise Exception("No recipients. Set RECIPIENT_EMAILS.")
    if len(password) != 16:
        raise Exception("GMAIL_APP_PASSWORD is not 16 letters.")

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
    print("Email sent to", len(to_list), "people.")


def run_portfolio_rebalancer():
    if not FINNHUB_KEY:
        raise Exception("FINNHUB_API_KEY secret is missing.")

    table = supabase.table("portfolio")
    holdings = table.select("*").execute().data

    rows = []
    for item in holdings:
        ticker = item["ticker"]
        price = get_price(ticker)
        shares = item.get("shares")
        if shares is None:
            shares = float(item["current_value"]) / price
            supabase.table("portfolio").update(
                {"shares": shares}
            ).eq("ticker", ticker).execute()
        shares = float(shares)
        value = round(shares * price, 2)
        supabase.table("portfolio").update(
            {"current_value": value}
        ).eq("ticker", ticker).execute()
        rows.append({"ticker": ticker, "price": price, "value": value})

    total = 0
    stock_total = 0
    for r in rows:
        total += r["value"]
        if r["ticker"] in STOCKS:
            stock_total += r["value"]
    stock_pct = stock_total / total * 100
    bond_pct = 100 - stock_pct

    lines = []
    lines.append(f"Total value: ${total:,.2f}")
    lines.append(f"Stocks now: {stock_pct:.1f}%")
    lines.append(f"Bonds now: {bond_pct:.1f}%")
    lines.append("Plan: 80% stocks / 20% bonds")
    lines.append("")
    lines.append("Fund | Price | Value | Now % | Target %")

    trades = []
    for r in rows:
        ticker = r["ticker"]
        now_w = r["value"] / total
        target_w = TARGET_WEIGHTS[ticker]
        price_txt = f"${r['price']:,.2f}"
        value_txt = f"${r['value']:,.2f}"
        now_txt = f"{now_w * 100:.1f}%"
        target_txt = f"{target_w * 100:.0f}%"
        row = [ticker, price_txt, value_txt, now_txt, target_txt]
        lines.append(" | ".join(row))
        if abs(now_w - target_w) > DRIFT_LIMIT:
            diff = round(total * target_w - r["value"], 2)
            action = "BUY" if diff > 0 else "SELL"
            trades.append(f"{action} ${abs(diff):,.2f} of {ticker}")

    lines.append("")
    if trades:
        lines.append("Rule check: a fund is over 5% off plan.")
        lines.append("Suggested trades:")
        for t in trades:
            lines.append("  - " + t)
    else:
        lines.append("Rule check: everything is within 5% of plan.")

    numbers = "\n".join(lines)
    ai_text = ask_gemini(numbers)

    body = "DAILY PORTFOLIO REPORT\n\n"
    body += "=== YOUR AI ANALYST ===\n"
    body += ai_text
    body += "\n\n=== THE NUMBERS ===\n"
    body += numbers
    body += "\n\nNo real trades were made."

    print(body)
    subject = "Portfolio Report - " + date.today().strftime("%d %b %Y")
    send_email(subject, body)


if __name__ == "__main__":
    run_portfolio_rebalancer()
