"""RaceResults entry point: routes to the two pages with explicit nav titles
(st.navigation/st.Page, not filename-derived - Turkish labels render cleanly
this way regardless of what the underlying view files are named).

Çalıştırma:
    streamlit run app.py
"""
import streamlit as st

st.set_page_config(page_title="RaceResults", page_icon="🏁", layout="wide")

pg = st.navigation(
    [
        st.Page("views/sonuc_arama.py", title="Sonuç Arama", icon="🔍", default=True),
        st.Page("views/analizler.py", title="Analizler", icon="📊"),
    ]
)
pg.run()
