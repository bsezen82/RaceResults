"""RaceResults: yıl seç (opsiyonel) → yarış seç (opsiyonel) → ad-soyad yaz → sonucunu gör.

Çalıştırma:
    streamlit run app.py
"""

import os
from datetime import date

import streamlit as st

from raceresults import store
from raceresults.timeutils import format_seconds

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "raceresults.db")
ALL_YEARS = "Tüm yıllar"

st.set_page_config(page_title="RaceResults", page_icon="🏁", layout="wide")


@st.cache_resource
def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return store.connect(DB_PATH)


conn = get_connection()

st.title("🏁 RaceResults")
st.caption("İstersen yıl ve yarış seç, ad-soyad yaz, sonucunu gör.")


@st.cache_data(show_spinner=False)
def _yearly_stats():
    year_rows, total_row = store.yearly_stats(conn)
    return year_rows, total_row


_year_rows, _total_row = _yearly_stats()

if _year_rows:
    _summary_columns = {
        "year": "Yıl",
        "races": "Yarış",
        "courses": "Yarış-Mesafe Kombinasyonu",
        "results": "Toplam Sonuç",
        "unique_runners": "Tekil Koşucu",
    }
    _summary_rows = [
        {label: row[key] for key, label in _summary_columns.items()} for row in _year_rows
    ]
    _summary_rows.append({label: _total_row[key] for key, label in _summary_columns.items()})
    st.dataframe(
        _summary_rows,
        hide_index=True,
        width="stretch",
        height=35 * (len(_summary_rows) + 1) + 3,
    )

TILE_COLORS = {
    "race": ("#E3F2FD", "#0D47A1"),
    "date": ("#E0F7FA", "#00838F"),
    "distance": ("#E0F2F1", "#00695C"),
    "participants": ("#F1F8E9", "#33691E"),
    "time": ("#FFF3E0", "#E65100"),
    "overall": ("#F3E5F5", "#6A1B9A"),
    "gender": ("#FCE4EC", "#AD1457"),
    "category": ("#E8EAF6", "#283593"),
}


def _tile(label: str, value: str, sub: str | None, color_key: str) -> str:
    # Kept on one line deliberately: st.markdown runs content through a Markdown
    # parser first, and 4-space-indented lines are read as an indented code
    # block, which escapes the HTML instead of rendering it.
    bg, fg = TILE_COLORS[color_key]
    sub_html = f'<div style="font-size:11px;opacity:.75;margin-top:2px;">{sub}</div>' if sub else ""
    return (
        f'<div style="background:{bg};color:{fg};border-radius:10px;padding:10px 14px;text-align:center;flex:1;min-width:120px;">'
        f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;opacity:.75;">{label}</div>'
        f'<div style="font-size:19px;font-weight:700;line-height:1.3;">{value}</div>'
        f"{sub_html}"
        f"</div>"
    )


def _tile_row(tiles: list[str]) -> str:
    return '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;">' + "".join(tiles) + "</div>"


@st.cache_data(show_spinner=False)
def _races():
    return [dict(r) for r in store.list_races(conn)]


races = _races()

if not races:
    st.info("Henüz kayıtlı yarış yok.")
    st.stop()

years = sorted({r["date"][:4] for r in races if r["date"]}, reverse=True)
year_choice = st.selectbox("Yıl", [ALL_YEARS] + years)

if year_choice == ALL_YEARS:
    races_in_scope = races
else:
    races_in_scope = [r for r in races if (r["date"] or "").startswith(year_choice)]

races_in_scope = sorted(races_in_scope, key=lambda r: r["date"] or "", reverse=True)

if year_choice == ALL_YEARS:
    all_races_label = "Tüm yarışlarda ara"
else:
    all_races_label = f"{year_choice} yılındaki tüm yarışlarda ara"

race_options = {all_races_label: None}
race_options.update(
    {f"{r['name']} ({r['date'] or '-'}) — {r['runner_count']} sporcu": r["slug"] for r in races_in_scope}
)
race_label = st.selectbox("Yarış", list(race_options.keys()))
selected_slug = race_options[race_label]

name_query = st.text_input("Ad Soyad")

if not name_query:
    st.stop()

if selected_slug:
    results = store.search_runners(conn, name_query, race_slug=selected_slug, limit=50)
else:
    year_filter = None if year_choice == ALL_YEARS else year_choice
    results = store.search_runners(conn, name_query, year=year_filter, limit=50)

    def _days_from_today(race_date: str | None) -> float:
        if not race_date:
            return float("inf")
        try:
            return abs((date.fromisoformat(race_date) - date.today()).days)
        except ValueError:
            return float("inf")

    results = sorted(results, key=lambda row: _days_from_today(row["race_date"]))

results = [row for row in results if row["status"] == "finished"]

if not results:
    st.warning("Bu isimle eşleşen bir sonuç bulunamadı.")
    st.stop()

for row in results:
    time_str = format_seconds(row["finish_seconds"])

    distance_km = f"{row['course_distance_m'] / 1000:.1f} km" if row["course_distance_m"] else None

    with st.container(border=True):
        st.markdown(f"### {row['name']}")

        info_row = _tile_row(
            [
                _tile("Yarış", row["race_name"], None, "race"),
                _tile("Tarih", row["race_date"] or "-", None, "date"),
                _tile("Mesafe", distance_km or row["course_code"] or "-", row["course_code"], "distance"),
                _tile("Katılımcı", row["course_participant_count"] or "-", "bu mesafede", "participants"),
            ]
        )
        result_row = _tile_row(
            [
                _tile("Süre", time_str, None, "time"),
                _tile("Genel Sıra", f"{row['rank_course']}." if row["rank_course"] else "-", None, "overall"),
                _tile(
                    "Cinsiyet Sırası",
                    f"{row['rank_gender']}." if row["rank_gender"] else "-",
                    row["sex"],
                    "gender",
                ),
                _tile(
                    "Kategori Sırası",
                    f"{row['rank_category']}." if row["rank_category"] else "-",
                    row["category"],
                    "category",
                ),
            ]
        )
        st.markdown(info_row, unsafe_allow_html=True)
        st.markdown(result_row, unsafe_allow_html=True)

        splits = store.get_splits(conn, row["id"])
        if splits:
            with st.expander("Kontrol noktaları"):
                st.dataframe(
                    [
                        {
                            "Kontrol Noktası": s["checkpoint_name"],
                            "Bölüm Süresi": format_seconds(s["split_seconds"]),
                            "Toplam Süre": format_seconds(s["cumulative_seconds"]),
                        }
                        for s in splits
                    ],
                    width="stretch",
                    hide_index=True,
                )
