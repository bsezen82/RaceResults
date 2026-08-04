"""RaceResults: yıl seç (opsiyonel) → yarış seç (opsiyonel) → ad-soyad yaz → sonucunu gör."""

from datetime import date

import streamlit as st

from raceresults import store
from raceresults.timeutils import format_seconds
from webapp_common import get_connection

ALL_YEARS = "Tüm yıllar"

conn = get_connection()

st.title("🏁 RaceResults")
st.caption("İstersen yıl ve yarış seç, ad-soyad yaz, sonucunu gör. Toplanan veri özeti için soldaki **Analizler** sayfasına bak.")

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
    results = store.search_runners(
        conn, name_query, race_slug=selected_slug, status="finished", limit=200
    )
else:
    year_filter = None if year_choice == ALL_YEARS else year_choice
    results = store.search_runners(
        conn, name_query, year=year_filter, status="finished", limit=200
    )

    def _days_from_today(race_date: str | None) -> float:
        if not race_date:
            return float("inf")
        try:
            return abs((date.fromisoformat(race_date) - date.today()).days)
        except ValueError:
            return float("inf")

    results = sorted(results, key=lambda row: _days_from_today(row["race_date"]))

if not results:
    st.warning("Bu isimle eşleşen bir sonuç bulunamadı.")
    st.stop()

# --- Sonuçların özeti: kaç yarış, toplam/en uzun mesafe, en uzun süre, en iyi sıralamalar ---
def _race_year_sub(row) -> str | None:
    if not row:
        return None
    year = row["race_date"][:4] if row["race_date"] else None
    return f"{row['race_name']} ({year})" if year else row["race_name"]


distance_rows = [row for row in results if row["course_distance_m"]]
total_km = sum(row["course_distance_m"] / 1000 for row in distance_rows)
longest_row = max(distance_rows, key=lambda row: row["course_distance_m"]) if distance_rows else None
longest_time_row = max(results, key=lambda row: row["finish_seconds"] or 0)

course_ranks = [row for row in results if row["rank_course"]]
best_course_row = min(course_ranks, key=lambda row: row["rank_course"]) if course_ranks else None
gender_ranks = [row for row in results if row["rank_gender"]]
best_gender_row = min(gender_ranks, key=lambda row: row["rank_gender"]) if gender_ranks else None
category_ranks = [row for row in results if row["rank_category"]]
best_category_row = min(category_ranks, key=lambda row: row["rank_category"]) if category_ranks else None

summary_row1 = _tile_row(
    [
        _tile("Koştuğu Yarış Sayısı", str(len(results)), None, "race"),
        _tile("Toplam Mesafe", f"{total_km:.1f} km" if distance_rows else "-", "mesafesi bilinenler", "distance"),
        _tile(
            "En Uzun Mesafe",
            f"{longest_row['course_distance_m'] / 1000:.1f} km" if longest_row else "-",
            _race_year_sub(longest_row),
            "participants",
        ),
        _tile("En Uzun Süre", format_seconds(longest_time_row["finish_seconds"]), _race_year_sub(longest_time_row), "time"),
    ]
)
summary_row2 = _tile_row(
    [
        _tile(
            "En İyi Genel Sıra",
            f"{best_course_row['rank_course']}." if best_course_row else "-",
            _race_year_sub(best_course_row),
            "overall",
        ),
        _tile(
            "En İyi Cinsiyet Sırası",
            f"{best_gender_row['rank_gender']}." if best_gender_row else "-",
            _race_year_sub(best_gender_row),
            "gender",
        ),
        _tile(
            "En İyi Kategori Sırası",
            f"{best_category_row['rank_category']}." if best_category_row else "-",
            _race_year_sub(best_category_row),
            "category",
        ),
    ]
)
st.markdown(summary_row1, unsafe_allow_html=True)
st.markdown(summary_row2, unsafe_allow_html=True)

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
