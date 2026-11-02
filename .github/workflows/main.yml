name: Daily Autonomous Portfolio Rebalance

on:
  schedule:
    - cron: '0 3 * * *'   # 8:00 AM Pakistan time
  workflow_dispatch:

jobs:
  rebalance:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install supabase

      - name: Run python rebalancer_agent.py
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
          GMAIL_ADDRESS: ${{ secrets.GMAIL_ADDRESS }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          RECIPIENT_EMAILS: ${{ secrets.RECIPIENT_EMAILS }}
        run: |
          python rebalancer_agent.py
