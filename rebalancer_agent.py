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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 80% stocks / 20% bonds
TARGET_WEIGHTS = {"SPY": 0.50, "QQQ": 0.30, "BND": 0.10, "TLT": 0.05, "AGG": 0.05}
STOCKS = {"SPY", "QQQ"}
DRIFT_LIMIT = 0.05

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def get_price(ticker):
    r = requests.get("https://finnhub.io/api/v1/quote",
                     params={"symbol": ticker, "token": FINNHUB_API_KEY}, timeout=20)
    r.raise_for_status()
    price = float(r.json().get("c") or 0)
    if price <= 0:
        raise Exception(f"Finnhub gave no price for {ticker}. Check FINNHUB_API_KEY.")
    return price


def pick_gemini_models():
    """Ask Google which models this key can use, best 'flash' ones first."""
    names = ["gemini-flash-latest"]
    try:
        r = requests.get(f"{GEMINI_BASE}/models", params={"key": GEMINI_API_KEY, "pageSize": 200}, timeout=20)
        r.raise_for_status()
        found = [
            m["name"].replace("models/", "")
            for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
        flash = [n for n in found if "flash" in n and "lite" not in n and "image" not in n and "tts" not in n]
        names += sorted(flash, reverse=True) + found
    except Exception as e:
        print(f"Could not list Gemini models: {e}")
    seen, ordered = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def ask_gemini(numbers_report):
    if not GEMINI_API_KEY:
        return "(AI analyst skipped: GEMINI_API_KEY secret is missing.)"
    prompt = f"""You are a friendly portfolio teacher writing a short morning email
for a beginner investor who is also learning day trading.

Here is today's portfolio data:

{numbers_report}

Write in very simple English, short sentences, no jargon without explaining it.
Plain text only: no markdown, no asterisks, no hashtags.
Use exactly these 3 sections:

1. DECISION: Start with either "REBALANCE TODAY" or "WAIT - NO REBALANCE NEEDED",
   then give the reason in 2-3 sentences (mention drift, trading costs, the 5% rule).

2. WHAT THE NUMBERS MEAN: Explain each column (Price, Value, Now %, Target %)
   and what the stocks/bonds split means, using today's real numbers.

3. LESSON OF THE DAY: Teach one simple investing idea connected to today's numbers,
   with a tiny example.

Keep the whole thing under 300 words. End with: "This is education, not financial advice."
"""
    for model in pick_gemini_models()[:6]:
        try:
            r = requests.post(
                f"{GEMINI_BASE}/models/{model}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=90,
            )
            if r.status_code == 404:
                print(f"Gemini model not available: {model}, trying next...")
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"Gemini model used: {model}")
            return text
        except Exception as e:
            print(f"Gemini error with {model}: {e}")
    return "(AI analyst could not run today. The numbers below are still correct.)"


def send_email(subject, body):
    to_list = [e.strip() for e in RECIPIENTS.split(",") if e.strip()]
    address = (GMAIL_ADDRESS or "").strip()
    password = "".join((GMAIL_APP_PASSWORD or "").split())
    if not to_list:
        raise Exception("No recipients. Set RECIPIENT_EMAILS secret.")
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
    print(f"Email sent to {len(to_list)} people.")


def run_portfolio_rebalancer():
    if not FINNHUB_API_KEY:
        raise Exception("FINNHUB_API_KEY secret is missing.")

    holdings = supabase.table("portfolio").select("*").execute().data

    rows = []
    for item in holdings:
        ticker = item["ticker"]
        price = get_price(ticker)
        shares = item.get("shares")
        if shares is None:
            shares = float(item["current_value"]) / price
            supabase.table("portfolio").update({"shares": shares}).eq("ticker", ticker).execute()
        shares = float(shares)
        value = round(shares * price, 2)
        supabase.table("portfolio").update({"current_value": value}).eq("ticker", ticker).execute()
        rows.append({"ticker": ticker, "price": price, "value": value})

    total = sum(r["value"] for r in rows)
    stock_pct = sum(r["value"] for r in rows if r["ticker"] in STOCKS) / total * 100

    lines = [
        f"Total value: ${total:,.2f}",
        f"Split now: {stock_pct:.1f}% stocks / {100 - stock_pct:.1f}% bonds
