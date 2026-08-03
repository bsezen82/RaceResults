"""Shared Streamlit plumbing (DB connection) for app.py and pages/*.py.

Defined once here so both the main page and the summary page reference the
exact same @st.cache_resource-wrapped function - Streamlit keys that cache
by the function's identity, so importing it from one shared place (rather
than redefining it per page) means every page reuses the same open SQLite
connection instead of each opening its own.
"""
import os

import streamlit as st

from raceresults import store

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "raceresults.db")


@st.cache_resource
def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return store.connect(DB_PATH)
