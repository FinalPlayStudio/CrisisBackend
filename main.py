import os
import json
import hashlib
import feedparser
from google import genai
import firebase_admin
from firebase_admin import credentials, firestore, messaging
import trafilatura
from geopy.geocoders import Nominatim
import time

# --- ORTAM DEĞİŞKENLERİNDEN ALINACAK ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")

if not firebase_creds_json:
    raise Exception("Firebase Credentials bulunamadı!")

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
        "Global": ["http://feeds.bbci.co.uk/news/world/rss.xml", "https://www.aljazeera.com/xml/rss/all.xml", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"],
        "Turkey": ["https://www.trthaber.com/sondakika.rss", "https://www.haberturk.com/rss/manset.xml", "https://www.ntv.com.tr/son-dakika.rss"],
        "Germany": ["https://www.tagesschau.de/xml/rss2/", "https://www.spiegel.de/schlagzeilen/index.rss"],
        "USA": ["http://rss.cnn.com/rss/cnn_topstories.rss", "https://feeds.npr.org/1001/rss.xml"]
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
    topic_rules = {
        "Gundem": "Focus on MAJOR events, politics, disasters.",
        "Futbol": "Focus strictly on Football matches, transfers.",
        "Basketbol": "Focus strictly on Basketball matches.",
        "Muzik": "Focus on Music, Album releases, Concerts."
    }
    
    current_rule = topic_rules.get(topic, "Focus on significant news.")

    prompt = f"""
    Analyze this news for topic '{topic}' in region '{target_category}'.
    Rule: {current_rule}
    
    1. Create short SUMMARY (max 3 sentences) in English.
    2. Extract Location Name.
    3. Is significant? (true/false)
    4. Severity (1-10).
    5. Translate to Turkish.
    
    Title: {title}
    Text: {full_text[:500]}
    
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
            model='gemini-2.5-flash-lite', 
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        
        # --- DÜZELTME BURADA YAPILDI ---
        # Gemini bazen ```json ile sarar, temizleyelim
        cleaned_text = response.text.strip()
        if cleaned_text.startswith("```"):
            cleaned_text = cleaned_text.split("\n", 1)[-1].rsplit("\n", 1)[0]

        parsed_json = json.loads(cleaned_text)

        # Eğer liste dönerse ([{...}]), ilk elemanı al
        if isinstance(parsed_json, list):
            if len(parsed_json) > 0:
                return parsed_json[0]
            else:
                return None
        
        return parsed_json
        # -------------------------------

    except Exception as e:
        print(f"Gemini Hatası: {e}")
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
        print("📲 Bildirim Gönderildi.")
    except Exception as e:
        print(f"Bildirim Hatası: {e}")

def main():
    print("🚀 GitHub Actions - Crisis Monitor Başlatılıyor...")
    
    for topic, countries in CATEGORY_FEEDS.items():
        print(f"\n📂 {topic.upper()} Taranıyor...")
        for target_country, urls in countries.items():
            for url in urls:
                try:
                    feed = feedparser.parse(url)
                    # Sadece en yeni 1 habere bak (API kotasını ve süreyi korumak için)
                    for entry in feed.entries[:1]:
                        doc_id = hashlib.md5((topic + entry.link).encode('utf-8')).hexdigest()
                        
                        doc_ref = db.collection("crises").document(doc_id)
                        if doc_ref.get().exists:
                            print(f"♻️ Zaten var: {entry.title[:30]}")
                            continue 
                        
                        print(f"🔥 İşleniyor: {entry.title[:30]}")
                        full_text = get_full_news_content(entry.link)
                        
                        if full_text:
                            # Gemini'yi yavaşlatmamak için kısa bir bekleme (Opsiyonel)
                            time.sleep(1) 
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
                                
                                # Sadece önemli global haberlerde bildirim at
                                if topic == "Gundem" and target_country == "Global" and res.get('severity', 0) >= 7:
                                    send_push_notification(res.get('title_tr'), loc_name, doc_id)

                except Exception as e:
                    print(f"RSS Hatası ({url}): {e}")

if __name__ == "__main__":
    main()
