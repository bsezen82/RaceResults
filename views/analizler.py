"""RaceResults: toplanan verinin analizleri - yıl yıl kırılım, en çok yarışan
sporcular, en kalabalık yarışlar.
"""
import streamlit as st

from raceresults import store
from webapp_common import get_connection

conn = get_connection()

st.title("📊 Analizler")
st.caption("Toplanan verinin genel görünümü.")


@st.cache_data(show_spinner=False)
def _yearly_stats():
    return store.yearly_stats(conn)


@st.cache_data(show_spinner=False)
def _top_runners():
    return [dict(r) for r in store.top_runners_by_race_count(conn, limit=10)]


@st.cache_data(show_spinner=False)
def _top_races():
    return [dict(r) for r in store.top_races_by_participants(conn, limit=10)]


year_rows, total_row = _yearly_stats()

st.subheader("Yıl yıl toplanan veri")
if year_rows:
    columns = {
        "year": "Yıl",
        "races": "Yarış",
        "courses": "Yarış-Mesafe Kombinasyonu",
        "results": "Toplam Sonuç",
        "unique_runners": "Tekil Koşucu",
    }
    summary_rows = [{label: row[key] for key, label in columns.items()} for row in year_rows]
    summary_rows.append({label: total_row[key] for key, label in columns.items()})
    st.dataframe(
        summary_rows,
        hide_index=True,
        width="stretch",
        height=35 * (len(summary_rows) + 1) + 3,
    )
else:
    st.info("Henüz kayıtlı veri yok.")

col1, col2 = st.columns(2)

with col1:
    st.subheader("En çok yarışan 10 sporcu")
    top_runners = _top_runners()
    if top_runners:
        st.dataframe(
            [
                {"Ad Soyad": r["name"], "Koştuğu Yarış Sayısı": r["race_count"]}
                for r in top_runners
            ],
            hide_index=True,
            width="stretch",
            height=35 * (len(top_runners) + 1) + 3,
        )
    else:
        st.info("Henüz kayıtlı veri yok.")

with col2:
    st.subheader("En kalabalık 10 yarış")
    st.caption("Mesafe bağımsız - o yarışın (edisyonun) toplam katılımcı sayısı.")
    top_races = _top_races()
    if top_races:
        st.dataframe(
            [
                {
                    "Yarış": r["name"],
                    "Tarih": r["date"] or "-",
                    "Katılımcı": r["runner_count"],
                }
                for r in top_races
            ],
            hide_index=True,
            width="stretch",
            height=35 * (len(top_races) + 1) + 3,
        )
    else:
        st.info("Henüz kayıtlı veri yok.")
