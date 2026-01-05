import os
import json
import hashlib
import feedparser
from google import genai
import firebase_admin
from firebase_admin import credentials, firestore, messaging
import trafilatura
from geopy.geocoders import Nominatim

# --- ORTAM DEĞİŞKENLERİNDEN ALINACAK ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Firebase Key'i GitHub Secret'tan JSON string olarak alıp dosyaya çevireceğiz
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")

if not firebase_creds_json:
    raise Exception("Firebase Credentials bulunamadı!")

# JSON stringi dict'e çevir
cred_dict = json.loads(firebase_creds_json)
cred = credentials.Certificate(cred_dict)

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()
client = genai.Client(api_key=GEMINI_API_KEY)
geolocator = Nominatim(user_agent="CrisisMonitorApp_Cloud")

# --- SABİT VERİLER ---
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
        "Turkey": ["https://www.fotomac.com.tr/rss/futbol.xml"],
        "Germany": ["https://www.sportschau.de/fussball/index~rss2.xml"],
        "USA": ["https://www.espn.com/espn/rss/soccer/news"]
    },
    "Basketbol": {
        "Global": ["https://www.espn.com/espn/rss/nba/news", "https://www.eurohoops.net/en/feed/"],
        "Turkey": ["https://www.fotomac.com.tr/rss/basketbol.xml"],
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


def get_full_news_content(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        content = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return content if content else ""
    except:
        return ""

def get_precise_coords(location_name):
    try:
        if not location_name: return 0.0, 0.0
        location = geolocator.geocode(location_name, timeout=10)
        if location: return location.latitude, location.longitude
    except: pass
    return 0.0, 0.0

def analyze_with_gemini(full_text, title, target_category, topic):
    prompt = f"""
    Analyze this news for topic '{topic}' in region '{target_category}'.
    
    1. Summarize in max 3 sentences.
    2. Extract Location.
    3. Assign severity (1-10).
    4. Translate to Turkish.
    
    Title: {title}
    Text: {full_text[:300]}
    
    Respond JSON:
    {{
        "is_relevant": true,
        "title_en": "Title",
        "summary_en": "Summary",
        "title_tr": "Başlık",
        "summary_tr": "Özet",
        "location_name": "City, Country",
        "severity": 8
    }}
    """
    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash', # Flash daha hızlı ve ucuz (free tier için)
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Gemini Error: {e}")
        return None

def send_push_notification(title, location, crisis_id):
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
    except Exception as e:
        print(f"Notification Error: {e}")

def main():
    print("🚀 GitHub Actions - Crisis Monitor Başlatılıyor...")
    
    # Firestore'dan son işlenen linkleri çekebilirdik ama maliyet artmasın diye
    # şimdilik sadece RSS'in en tepesindeki 1 habere bakacağız.
    
    for topic, countries in CATEGORY_FEEDS.items():
        for target_country, urls in countries.items():
            for url in urls:
                try:
                    feed = feedparser.parse(url)
                    # Sadece EN YENİ 2 haberi kontrol et (API kotasını korumak için)
                    for entry in feed.entries[:2]:
                        # ID oluştur
                        doc_id = hashlib.md5((topic + entry.link).encode('utf-8')).hexdigest()
                        
                        # ÖNCE DATABASE'E BAK: Bu haber zaten var mı?
                        # Bu okuma işlemi yapar ama Gemini API kotalarını korur.
                        doc_ref = db.collection("crises").document(doc_id)
                        doc = doc_ref.get()
                        
                        if doc.exists:
                            print(f"♻️ Zaten var: {entry.title[:30]}")
                            continue # Haber varsa geç
                        
                        # Haber yoksa işle
                        print(f"🔥 Yeni Haber: {entry.title[:30]}")
                        full_text = get_full_news_content(entry.link)
                        
                        if full_text:
                            res = analyze_with_gemini(full_text, entry.title, target_country, topic)
                            
                            if res and res.get('is_relevant'):
                                loc_name = res.get('location_name') or ""
                                if target_country != "Global" and target_country not in loc_name:
                                    loc_name = DEFAULT_LOCATIONS.get(target_country, loc_name)
                                
                                real_lat, real_lng = get_precise_coords(loc_name)
                                if real_lat == 0.0:
                                    loc_name = DEFAULT_LOCATIONS.get(target_country, "New York, USA")
                                    real_lat, real_lng = get_precise_coords(loc_name)

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
                                
                                doc_ref.set(doc_data)
                                print("✅ Veritabanına Yazıldı.")
                                
                                if topic == "Gundem" and target_country == "Global":
                                    send_push_notification(res.get('title_tr'), loc_name, doc_id)

                except Exception as e:
                    print(f"RSS Hatası ({url}): {e}")

if __name__ == "__main__":
    main()