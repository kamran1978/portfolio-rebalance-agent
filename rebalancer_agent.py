import os
import sys
import time
import json
import html
import smtplib
import requests
from datetime import date, timedelta
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
MAX_AGENT_STEPS = 5
AGENT_TIME_LIMIT = 150   # seconds for the whole agent
LESSON_TIME_LIMIT = 60   # seconds for the day trading lesson

# Show log lines on GitHub straight away
sys.stdout.reconfigure(line_buffering=True)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
FINNHUB_BASE = "https://finnhub.io/api/v1"


# ---------------- FINNHUB ----------------

def finnhub_get(path, params):
    params["token"] = FINNHUB_KEY
    url = FINNHUB_BASE + path
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_quote(ticker):
    q = finnhub_get("/quote", {"symbol": ticker})
    price = float(q.get("c") or 0)
    if price <= 0:
        raise Exception("Finnhub gave no price for " + ticker)
    change_pct = float(q.get("dp") or 0)
    return price, change_pct


def headlines(items, limit=5):
    out = []
    for n in items[:limit]:
        title = (n.get("headline") or "").strip()
        summary = (n.get("summary") or "").strip()[:200]
        if title:
            out.append({"headline": title, "summary": summary})
    return out


def tool_get_market_news():
    items = finnhub_get("/news", {"category": "general"})
    return {"news": headlines(items)}


def tool_get_fund_news(symbol):
    today = date.today()
    params = {
        "symbol": symbol,
        "from": str(today - timedelta(days=3)),
        "to": str(today),
    }
    items = finnhub_get("/company-news", params)
    found = headlines(items)
    if not found:
        return {"news": [], "note": "No fund news. Try market news."}
    return {"news": found}


TOOLS = [{
    "functionDeclarations": [
        {
            "name": "get_market_news",
            "description": "Latest general stock market headlines.",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "get_fund_news",
            "description": "Recent news for one ticker, e.g. SPY.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "symbol": {"type": "STRING"},
                },
                "required": ["symbol"],
            },
        },
    ]
}]


def run_tool(name, args):
    print("Agent is using tool:", name, args)
    try:
        if name == "get_market_news":
            return tool_get_market_news()
        if name == "get_fund_news":
            return tool_get_fund_news(str(args.get("symbol", "SPY")))
        return {"error": "Unknown tool"}
    except Exception as e:
        return {"error": str(e)}


# ---------------- GEMINI ----------------

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
        names += sorted(flash, reverse=True)
    except Exception as e:
        print("Could not list Gemini models:", e)
    ordered = []
    for n in names:
        if n not in ordered:
            ordered.append(n)
    return ordered[:3]


def gemini_call(model, body):
    url = GEMINI_BASE + "/models/" + model + ":generateContent"
    r = requests.post(
        url,
        params={"key": GEMINI_KEY},
        json=body,
        timeout=60,
    )
    if r.status_code == 429:
        print("Gemini says: too many requests (free limit).")
    return r


AGENT_PROMPT = """You are a friendly portfolio agent and teacher.
You write a short email for a beginner investor.
Always write in English only. Very simple English.
Plain text only. No markdown, no asterisks.

You have tools to read news. Use them when it helps:
- If any fund moved more than 1% today, look up why.
- You may check general market news once.
Do not invent news. Only use what the tools return.

When you are done, write exactly these 4 sections:

DECISION: Write "REBALANCE TODAY" or
"WAIT - NO REBALANCE NEEDED". Explain why in
2-3 sentences (drift, trading costs, the 5% rule).

WHY PRICES MOVED: 2-3 sentences using the news
you found. If you used no news, say the moves
were small.

WHAT THE NUMBERS MEAN: Explain Price, Today %,
Value, Now %, Target %, using today's numbers.

LESSON OF THE DAY: One simple investing idea
with a tiny example.

Keep it under 350 words.
End with: "This is education, not financial advice."

Today's portfolio data:
"""


def run_agent(numbers_report):
    if not GEMINI_KEY:
        return "(AI agent skipped: GEMINI_API_KEY missing.)"
    first = {"role": "user", "parts": [
        {"text": AGENT_PROMPT + numbers_report}
    ]}
    start = time.time()
    for model in pick_gemini_models():
        contents = [first]
        try:
            for step in range(MAX_AGENT_STEPS):
                if time.time() - start > AGENT_TIME_LIMIT:
                    print("Agent time limit reached.")
                    return "(AI agent ran out of time today.)"
                print("Agent step", step + 1, "with", model)
                body = {"contents": contents, "tools": TOOLS}
                r = gemini_call(model, body)
                if r.status_code == 404:
                    print("Model not available:", model)
                    break
                r.raise_for_status()
                content = r.json()["candidates"][0]["content"]
                parts = content.get("parts", [])
                calls = [p for p in parts if "functionCall" in p]
                if not calls:
                    texts = [p.get("text", "") for p in parts]
                    print("Agent model used:", model)
                    return "".join(texts).strip()
                contents.append(content)
                replies = []
                for p in calls:
                    fc = p["functionCall"]
                    result = run_tool(fc["name"], fc.get("args", {}))
                    replies.append({"functionResponse": {
                        "name": fc["name"],
                        "response": result,
                    }})
                contents.append({"role": "user", "parts": replies})
            else:
                print("Agent used too many steps.")
        except Exception as e:
            print("Gemini error with", model, ":", e)
    return "(AI agent could not run. Numbers are still correct.)"


DT_PROMPT = """You teach day trading to a beginner.
Today's date is {today}. Pick a NEW topic each day.
Write in very simple English. English only.
Return JSON only, in this exact shape:
{{"passage": "one paragraph, 80-120 words",
  "terms": [{{"term": "word", "meaning": "short simple meaning",
             "example": "tiny example"}}]}}
The terms list must have exactly 5 day trading terms.
Mention once that day trading is risky and most
beginners lose money.
"""

FALLBACK_DT = {
    "passage": (
        "Day trading means buying and selling within the "
        "same day. You never hold overnight. Traders try to "
        "profit from small price moves. It needs fast "
        "decisions, strict rules and a stop loss on every "
        "trade. It is risky, and most beginners lose money, "
        "so practise on a demo account first."
    ),
    "terms": [
        {"term": "Stop loss", "meaning": "An order that sells "
         "if price falls to a set level.",
         "example": "Buy at $100, stop at $98."},
        {"term": "Volume", "meaning": "How many shares "
         "traded.", "example": "High volume = busy stock."},
        {"term": "Spread", "meaning": "Gap between buy and "
         "sell price.", "example": "Bid $9.99, ask $10.01."},
        {"term": "Volatility", "meaning": "How much price "
         "jumps around.", "example": "Moves 5% a day = high."},
        {"term": "Support", "meaning": "A price where buyers "
         "often step in.", "example": "Stock bounces at $50."},
    ],
}


def get_day_trading_lesson():
    if not GEMINI_KEY:
        return FALLBACK_DT
    prompt = DT_PROMPT.format(today=str(date.today()))
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    start = time.time()
    for model in pick_gemini_models():
        if time.time() - start > LESSON_TIME_LIMIT:
            print("Lesson time limit reached.")
            break
        try:
            print("Day trading lesson with", model)
            r = gemini_call(model, body)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            data = json.loads(text)
            if data.get("passage") and len(data.get("terms", [])) >= 5:
                data["terms"] = data["terms"][:5]
                return data
        except Exception as e:
            print("Day trading lesson error:", e)
    return FALLBACK_DT


# ---------------- EMAIL ----------------

def to_html_text(text):
    safe = html.escape(text)
    return safe.replace("\n", "<br>")


def build_html(ai_text, rows, summary, trades, dt):
    td = 'style="border:1px solid #ccc;padding:6px 10px;"'
    th = ('style="border:1px solid #ccc;padding:6px 10px;'
          'background:#f2f2f2;text-align:left;"')
    tbl = 'style="border-collapse:collapse;font-size:14px;"'

    h = ['<div style="font-family:Arial,sans-serif;'
         'font-size:15px;line-height:1.5;color:#222;">']
    h.append("<h2>Daily Portfolio Report</h2>")

    h.append("<h3>Your AI Agent</h3>")
    h.append("<p>" + to_html_text(ai_text) + "</p>")

    h.append("<h3>The Numbers</h3>")
    h.append("<p>" + to_html_text(summary) + "</p>")
    h.append("<table " + tbl + "><tr>")
    for col in ["Fund", "Price", "Today", "Value", "Now %", "Target %"]:
        h.append("<th " + th + ">" + col + "</th>")
    h.append("</tr>")
    for r in rows:
        cells = [
            r["ticker"],
            f"${r['price']:,.2f}",
            f"{r['change']:+.2f}%",
            f"${r['value']:,.2f}",
            f"{r['now'] * 100:.1f}%",
            f"{r['target'] * 100:.0f}%",
        ]
        h.append("<tr>")
        for c in cells:
            h.append("<td " + td + ">" + html.escape(c) + "</td>")
        h.append("</tr>")
    h.append("</table>")
    if trades:
        h.append("<p><b>Rule check:</b> a fund is over 5% off plan."
                 "<br>Suggested trades: "
                 + html.escape(", ".join(trades)) + "</p>")
    else:
        h.append("<p><b>Rule check:</b> everything is within "
                 "5% of plan.</p>")

    h.append("<h3>Day Trading Corner</h3>")
    h.append("<p>" + to_html_text(dt["passage"]) + "</p>")
    h.append("<table " + tbl + "><tr>")
    for col in ["#", "Term", "Meaning", "Example"]:
        h.append("<th " + th + ">" + col + "</th>")
    h.append("</tr>")
    for i, t in enumerate(dt["terms"], 1):
        cells = [
            str(i),
            str(t.get("term", "")),
            str(t.get("meaning", "")),
            str(t.get("example", "")),
        ]
        h.append("<tr>")
        for c in cells:
            h.append("<td " + td + ">" + html.escape(c) + "</td>")
        h.append("</tr>")
    h.append("</table>")

    h.append('<p style="color:#777;font-size:12px;">'
             "No real trades were made. Education only, "
             "not financial advice.</p></div>")
    return "".join(h)


def send_email(subject, text_body, html_body):
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
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(address, password)
        smtp.send_message(msg)
    print("Email sent to", len(to_list), "people.")


# ---------------- MAIN ----------------

def run_portfolio_rebalancer():
    if not FINNHUB_KEY:
        raise Exception("FINNHUB_API_KEY secret is missing.")

    table = supabase.table("portfolio")
    holdings = table.select("*").execute().data

    rows = []
    for item in holdings:
        ticker = item["ticker"]
        price, change = get_quote(ticker)
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
        rows.append({
            "ticker": ticker,
            "price": price,
            "change": change,
            "value": value,
        })

    total = sum(r["value"] for r in rows)
    stock_total = 0
    for r in rows:
        if r["ticker"] in STOCKS:
            stock_total += r["value"]
    stock_pct = stock_total / total * 100
    bond_pct = 100 - stock_pct

    lines = []
    lines.append(f"Total value: ${total:,.2f}")
    lines.append(f"Stocks now: {stock_pct:.1f}%")
    lines.append(f"Bonds now: {bond_pct:.1f}%")
    lines.append("Plan: 80% stocks / 20% bonds")
    summary = "\n".join(lines)

    lines.append("")
    lines.append("Fund | Price | Today % | Value | Now % | Target %")
    trades = []
    for r in rows:
        r["now"] = r["value"] / total
        r["target"] = TARGET_WEIGHTS[r["ticker"]]
        row = [
            r["ticker"],
            f"${r['price']:,.2f}",
            f"{r['change']:+.2f}%",
            f"${r['value']:,.2f}",
            f"{r['now'] * 100:.1f}%",
            f"{r['target'] * 100:.0f}%",
        ]
        lines.append(" | ".join(row))
        if abs(r["now"] - r["target"]) > DRIFT_LIMIT:
            diff = round(total * r["target"] - r["value"], 2)
            action = "BUY" if diff > 0 else "SELL"
            trades.append(f"{action} ${abs(diff):,.2f} of {r['ticker']}")

    lines.append("")
    if trades:
        lines.append("Rule check: a fund is over 5% off plan.")
        lines.append("Suggested trades: " + ", ".join(trades))
    else:
        lines.append("Rule check: everything is within 5% of plan.")
    numbers = "\n".join(lines)

    ai_text = run_agent(numbers)
    dt = get_day_trading_lesson()

    # The agent's decision changes the email subject
    first_part = ai_text.upper()[:150]
    if "REBALANCE TODAY" in first_part:
        tag = "ACTION NEEDED"
    else:
        tag = "No action needed"
    today_txt = date.today().strftime("%d %b %Y")
    subject = "Portfolio Report - " + today_txt + " - " + tag

    text_body = "DAILY PORTFOLIO REPORT\n\n"
    text_body += "=== YOUR AI AGENT ===\n" + ai_text
    text_body += "\n\n=== THE NUMBERS ===\n" + numbers
    text_body += "\n\n=== DAY TRADING CORNER ===\n"
    text_body += dt["passage"] + "\n"
    for i, t in enumerate(dt["terms"], 1):
        text_body += f"{i}. {t.get('term')}: {t.get('meaning')}\n"
    text_body += "\nNo real trades were made."

    html_body = build_html(ai_text, rows, summary, trades, dt)

    print(text_body)
    send_email(subject, text_body, html_body)


if __name__ == "__main__":
    run_portfolio_rebalancer()
