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
def _top_runners_by_race_count():
    return [dict(r) for r in store.top_runners_by_race_count(conn, limit=20, status="finished")]


@st.cache_data(show_spinner=False)
def _top_runners_by_distance():
    return [dict(r) for r in store.top_runners_by_distance(conn, limit=20, status="finished")]


@st.cache_data(show_spinner=False)
def _top_races():
    return [dict(r) for r in store.top_races_by_participants(conn, limit=20)]


@st.cache_data(show_spinner=False)
def _top_race_courses():
    return [dict(r) for r in store.top_race_courses_by_participants(conn, limit=20)]


def _table(rows, height_rows=None):
    n = height_rows if height_rows is not None else len(rows)
    st.dataframe(rows, hide_index=True, width="stretch", height=35 * (n + 1) + 3)


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
    _table(summary_rows)
else:
    st.info("Henüz kayıtlı veri yok.")

col1, col2 = st.columns(2)

with col1:
    st.subheader("En çok yarış koşan 20 sporcu")
    st.caption("Bitirdiği yarış sayısına göre.")
    rows = _top_runners_by_race_count()
    if rows:
        _table([{"Ad Soyad": r["name"], "Bitirdiği Yarış Sayısı": r["race_count"]} for r in rows])
    else:
        st.info("Henüz kayıtlı veri yok.")

with col2:
    st.subheader("En çok km koşan 20 sporcu")
    st.caption("Bitirdiği, mesafesi bilinen yarışların toplamına göre.")
    rows = _top_runners_by_distance()
    if rows:
        _table(
            [
                {"Ad Soyad": r["name"], "Toplam Mesafe (km)": round(r["total_distance_m"] / 1000, 1)}
                for r in rows
            ]
        )
    else:
        st.info("Henüz kayıtlı veri yok.")

col3, col4 = st.columns(2)

with col3:
    st.subheader("En kalabalık 20 yarış")
    st.caption("Mesafe bağımsız - o yarışın (edisyonun) toplam katılımcı sayısı.")
    rows = _top_races()
    if rows:
        _table(
            [
                {"Yarış": r["name"], "Tarih": r["date"] or "-", "Katılımcı": r["runner_count"]}
                for r in rows
            ]
        )
    else:
        st.info("Henüz kayıtlı veri yok.")

with col4:
    st.subheader("En kalabalık 20 yarış/mesafe")
    st.caption("Tek bir mesafe/parkur bazında (örn. bir maratonun 42K'sı ayrı, 10K'sı ayrı sayılır).")
    rows = _top_race_courses()
    if rows:
        _table(
            [
                {
                    "Yarış": r["race_name"],
                    "Mesafe": r["course_code"] or "-",
                    "Tarih": r["date"] or "-",
                    "Katılımcı": r["participant_count"],
                }
                for r in rows
            ]
        )
    else:
        st.info("Henüz kayıtlı veri yok.")
