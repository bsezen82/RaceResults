# RaceResults

Yıl ve yarış seç (ya da hiç seçme), ad-soyad yaz, sonucunu gör.

Türkiye'deki koşu yarışlarının sonuçlarını üç farklı zamanlama sağlayıcısından
(Argeus Timing / G-Live, PassTiming, PlusTiming - "Racetec") tek bir SQLite
veritabanında topluyor.

## Çalıştırma

```
pip install -r requirements.txt
streamlit run app.py
```

## Veri toplama (CLI)

```
python -m raceresults.cli discover                      # bilinen tüm yarışları listele
python -m raceresults.cli scrape-all                     # hepsini veritabanına çek
python -m raceresults.cli scrape <url> --slug <slug>     # tek bir yarış
python -m raceresults.cli import-html --slug ... --name ... file1.html file2.html
                                                          # Cloudflare korumalı Racetec
                                                          # sitelerinden manuel kaydedilmiş
                                                          # sayfaları içe aktar
python -m raceresults.cli search "Ad Soyad"
```
