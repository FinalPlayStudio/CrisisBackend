import os
import json
import hashlib
import time
import itertools
from datetime import datetime, timedelta
from time import mktime

import feedparser
from google import genai
import firebase_admin
from firebase_admin import credentials, firestore, messaging
import trafilatura
from geopy.geocoders import Nominatim

# --- 1. AYARLAR VE KİMLİK DOĞRULAMA ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")

if not firebase_creds_json:
    raise Exception("Firebase Credentials bulunamadı! (GitHub Secrets kontrol et)")

cred_dict = json.loads(firebase_creds_json)
cred = credentials.Certificate(cred_dict)

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()
client = genai.Client(api_key=GEMINI_API_KEY)
geolocator = Nominatim(user_agent="CrisisMonitorApp_Cloud_v2")

# --- 2. SABİT VERİLER VE GÜNCEL KAYNAKLAR ---
DEFAULT_LOCATIONS = {
    "Global": "New York, USA",
    "Germany": "Berlin, Germany",
    "Turkey": "Ankara, Turkey",
    "USA": "Washington D.C., USA"
}

CATEGORY_FEEDS = {
    "Gundem": {
        "Global": [
            "http://feeds.bbci.co.uk/news/world/rss.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"
        ],
        "Turkey": [
            "https://www.trthaber.com/sondakika.rss",
            "https://www.haberturk.com/rss/manset.xml",
            "https://www.ntv.com.tr/son-dakika.rss"
        ],
        "Germany": [
            "https://www.tagesschau.de/xml/rss2/",
            "https://www.spiegel.de/schlagzeilen/index.rss"
        ],
        "USA": [
            "http://rss.cnn.com/rss/cnn_topstories.rss",
            "https://feeds.npr.org/1001/rss.xml"
        ]
    },
    "Futbol": {
        "Global": ["http://feeds.bbci.co.uk/sport/football/rss.xml"],
        "Turkey": [
            "https://www.ntvspor.net/rss/kategori/futbol"           # Güncel
        ],
        "Germany": ["https://www.sportschau.de/fussball/index~rss2.xml"],
        "USA": ["https://www.espn.com/espn/rss/soccer/news"]
    },
    "Basketbol": {
        "Global": ["https://www.espn.com/espn/rss/nba/news", "https://www.eurohoops.net/en/feed/"],
        "Turkey": ["https://www.ntvspor.net/rss/kategori/basketbol"],
        "Germany": ["https://www.kicker.de/basketball/startseite/rss"],
        "USA": ["https://www.espn.com/espn/rss/nba/news"]
    },
    "Muzik": {
        "Global": ["https://www.billboard.com/feed/", "https://www.rollingstone.com/music/music-news/feed/"],
        "Turkey": ["https://www.hurriyet.com.tr/rss/kelebek"],
        "Germany": ["https://www.rollingstone.de/feed/"],
        "USA": ["https://pitchfork.com/feed/feed-news/rss"]
    }
}

# --- 3. YARDIMCI FONKSİYONLAR ---

def get_full_news_content(url):
    """Verilen URL'den haber metnini çeker."""
    try:
        downloaded = trafilatura.fetch_url(url)
        content = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return content if content else ""
    except:
        return ""

def get_precise_coords(location_name):
    """Şehir ismini koordinata çevirir."""
    try:
        if not location_name: return 0.0, 0.0
        location = geolocator.geocode(location_name, timeout=10)
        if location: return location.latitude, location.longitude
    except: pass
    return 0.0, 0.0

def analyze_with_gemini(full_text, title, target_category, topic):
    """Haber metnini Gemini AI ile analiz eder."""
    topic_rules = {
        "Gundem": "Focus on MAJOR events, politics, disasters.",
        "Futbol": "Focus strictly on Football matches, transfers, scores.",
        "Basketbol": "Focus strictly on Basketball matches, transfers.",
        "Muzik": "Focus on Music, Album releases, Concerts."
    }
    
    current_rule = topic_rules.get(topic, "Focus on significant news.")

    prompt = f"""
    Analyze this news for topic '{topic}' in region '{target_category}'.
    Rule: {current_rule}
    
    1. Create short SUMMARY (max 3 sentences) in English.
    2. Extract Location Name (City, Country format).
    3. Is significant? (true/false)
    4. Severity (1-10) based on urgency.
    5. Translate Title and Summary to Turkish.
    
    News Title: {title}
    News Text: {full_text[:600]}
    
    Respond strictly in JSON format:
    {{
        "is_relevant": true,
        "title_en": "Title in English",
        "summary_en": "Summary in English",
        "title_tr": "Başlık Türkçe",
        "summary_tr": "Özet Türkçe",
        "location_name": "City, Country",
        "severity": 8
    }}
    """
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite', 
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        
        # Temizlik (Gemini bazen ```json etiketi ekler)
        cleaned_text = response.text.strip()
        if cleaned_text.startswith("```"):
            cleaned_text = cleaned_text.split("\n", 1)[-1].rsplit("\n", 1)[0]

        parsed_json = json.loads(cleaned_text)

        # Liste dönerse ilkini al
        if isinstance(parsed_json, list):
            return parsed_json[0] if parsed_json else None
        
        return parsed_json

    except Exception as e:
        print(f"Gemini Hatası: {e}")
        return None

def send_push_notification(title, location, crisis_id):
    """FCM üzerinden bildirim gönderir."""
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title="🚨 Yeni Gelişme!",
                body=f"{location}: {title}"
            ),
            data={"crisisId": str(crisis_id)},
            topic='global_alerts'
        )
        messaging.send(message)
        print("📲 Bildirim Gönderildi.")
    except Exception as e:
        print(f"Bildirim Hatası: {e}")

# --- 4. ANA PROGRAM (MAIN) ---

def main():
    print("🚀 GitHub Actions - Crisis Monitor (v3 Final) Başlatılıyor...")
    
    TARGET_NEWS_COUNT = 10  # Her kategori için hedeflenen YENİ haber sayısı
    MAX_SCAN_LIMIT = 60     # Sonsuz döngüden kaçmak için maksimum tarama sayısı
    
    # Şimdiki zaman (Tarih karşılaştırması için)
    now = datetime.now()

    for topic, countries in CATEGORY_FEEDS.items():
        print(f"\n📂 KATEGORİ: {topic.upper()}")
        
        for target_country, urls in countries.items():
            print(f"  👉 Bölge: {target_country} (Hedef: {TARGET_NEWS_COUNT} Yeni Haber)")
            
            # 1. ADIM: Feedleri Çek
            all_feed_entries = []
            for url in urls:
                try:
                    feed = feedparser.parse(url)
                    # Her kaynaktan en fazla 15 haber al (Havuzu oluştur)
                    if feed.entries:
                        all_feed_entries.append(feed.entries[:15])
                except Exception as e:
                    print(f"    ❌ RSS Hatası ({url}): {e}")

            # 2. ADIM: Haberleri Karıştır (Interleaving)
            # [SiteA_1, SiteB_1, SiteC_1, SiteA_2...] şeklinde sıralar.
            mixed_entries = [
                entry for entry in itertools.chain.from_iterable(itertools.zip_longest(*all_feed_entries))
                if entry is not None
            ]

            processed_count = 0  # Eklenen başarılı haber sayısı
            scanned_count = 0    # Toplam bakılan haber sayısı

            # 3. ADIM: İşleme Döngüsü
            for entry in mixed_entries:
                # Hedefe ulaştık mı?
                if processed_count >= TARGET_NEWS_COUNT:
                    print(f"    ✅ Hedef ({TARGET_NEWS_COUNT}) tamamlandı. Sonraki bölgeye geçiliyor.")
                    break
                
                # Güvenlik Limiti
                if scanned_count >= MAX_SCAN_LIMIT:
                    print("    ⚠️ Çok fazla haber tarandı, limit doldu. Geçiliyor.")
                    break

                scanned_count += 1
                
                # --- TARİH FİLTRESİ (24 SAAT KURALI) ---
                try:
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        pub_date = datetime.fromtimestamp(mktime(entry.published_parsed))
                        # 24 saatten eskiyse atla
                        if (now - pub_date) > timedelta(hours=24):
                            # print(f"    ⏳ Eski haber atlandı ({pub_date}): {entry.title[:20]}")
                            continue
                except:
                    pass # Tarih yoksa, şansa bırak ve devam et
                
                try:
                    # ID Oluştur (Başlık + Link bazlı)
                    doc_id = hashlib.md5((topic + entry.link).encode('utf-8')).hexdigest()
                    doc_ref = db.collection("crises").document(doc_id)
                    
                    # Veritabanında VARSA, Gemini'ye sorma, geç
                    if doc_ref.get().exists:
                        # print(f"    ♻️ Zaten var: {entry.title[:30]}")
                        continue
                    
                    print(f"    🔥 Analiz ediliyor ({processed_count + 1}/{TARGET_NEWS_COUNT}): {entry.title[:40]}...")
                    full_text = get_full_news_content(entry.link)
                    
                    if full_text:
                        time.sleep(1) # API nezaketi
                        res = analyze_with_gemini(full_text, entry.title, target_country, topic)
                        
                        if res and res.get('is_relevant'):
                            # Konum belirleme
                            loc_name = res.get('location_name') or ""
                            if target_country != "Global" and target_country not in loc_name:
                                loc_name = DEFAULT_LOCATIONS.get(target_country, loc_name)
                            
                            # Koordinat bulma
                            real_lat, real_lng = get_precise_coords(loc_name)
                            if real_lat == 0.0:
                                loc_name = DEFAULT_LOCATIONS.get(target_country, "New York, USA")
                                real_lat, real_lng = get_precise_coords(loc_name)

                            # Veri Hazırlama
                            doc_data = {
                                "category": topic,
                                "country": target_country,
                                "title_en": res.get('title_en'),
                                "summary_en": res.get('summary_en'),
                                "title_tr": res.get('title_tr'),
                                "summary_tr": res.get('summary_tr'),
                                "locationName": loc_name,
                                "latitude": real_lat,
                                "longitude": real_lng,
                                "severity": res.get('severity', 5),
                                "sourceLink": entry.link,
                                "date": firestore.SERVER_TIMESTAMP
                            }
                            
                            # Kaydet
                            doc_ref.set(doc_data)
                            print(f"    ✅ Eklendi: {res.get('title_tr')}")
                            processed_count += 1
                            
                            # Bildirim (Sadece Önemli Global Gündem)
                            if topic == "Gundem" and target_country == "Global" and res.get('severity', 0) >= 8:
                                send_push_notification(res.get('title_tr'), loc_name, doc_id)
                        else:
                            print("    ❌ İlgisiz içerik.")
                
                except Exception as e:
                    print(f"    ⚠️ İşleme Hatası: {e}")

if __name__ == "__main__":
    main()
