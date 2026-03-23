"""
sheets_client.py
----------------
Handles the connection to Google Sheets.

Works in two modes automatically:
  - Local (your laptop): reads credentials.json from disk
  - Streamlit Cloud (deployed): reads credentials from st.secrets, so
    you never need to upload your credentials.json to GitHub
"""

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_client(credentials_path="credentials.json"):
    """
    Returns an authenticated gspread client.

    First tries Streamlit secrets (used when running on Streamlit Cloud).
    Falls back to reading credentials.json from disk (used locally).
    """
    # ── Try Streamlit secrets first (cloud deployment) ───────────────────────
    try:
        import streamlit as st
        if "gcp_service_account" in st.secrets:
            creds = Credentials.from_service_account_info(
                dict(st.secrets["gcp_service_account"]),
                scopes=SCOPES,
            )
            return gspread.authorize(creds)
    except Exception:
        pass  # Not running in Streamlit or no secrets set — fall through

    # ── Fall back to local credentials.json ──────────────────────────────────
    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    return gspread.authorize(creds)


def get_spreadsheet(client, spreadsheet_id):
    """
    Opens a Google Spreadsheet by its ID.
    The ID is the long string in the URL:
      https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit
    """
    return client.open_by_key(spreadsheet_id)
