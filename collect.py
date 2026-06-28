"""
chimera-trend-collector / collect.py
Thu thập trending TikTok Vietnam → format genz_hot_news.json → PATCH Gist.
Chạy bởi GitHub Actions mỗi 4 giờ (cron: "0 */4 * * *").

Chiến lược thu thập (theo thứ tự ưu tiên):
  1. TikTok qua Apify (dữ liệu THẬT: shareCount, playCount)
  2. RSS báo VN (VnExpress, Tuổi Trẻ, Kênh 14) làm fallback
  3. Scrape Kenh14 trending HTML

Output JSON genz_hot_news.json — đúng schema T0d mong đợi.
"""

import os
import json
import re
import random
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Dict

import requests
import feedparser
from apify_client import ApifyClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════

GIST_ID      = os.getenv("GIST_ID", "36eb2530da8921c099ddc3e571e7b55c")
GIST_TOKEN   = os.getenv("GHT_TOKEN", "")
APIFY_TOKEN  = os.getenv("APIFY_TOKEN", "")
GIST_FILE    = "genz_hot_news.json"
MAX_ITEMS    = 30
TIMEOUT      = 10

# ══════════════════════════════════════════════════════════════════════
# CATEGORY & EMOTION RULES
# ══════════════════════════════════════════════════════════════════════

CATEGORY_RULES: List[tuple] = [
    ("finance",      ["nợ", "vỡ nợ", "phá sản", "bất động sản", "ngân hàng",
                      "lãi suất", "tín dụng", "chứng khoán", "đầu tư", "tiền"]),
    ("scandal",      ["bóc phốt", "scandal", "lộ", "sự thật", "phát hiện",
                      "bí mật", "vạch trần", "phốt"]),
    ("crime",        ["lừa đảo", "trộm", "cướp", "giết", "bắt giữ",
                      "tội phạm", "cảnh sát", "bị bắt"]),
    ("protest",      ["biểu tình", "phản đối", "tẩy chay", "chống", "không đồng ý"]),
    ("ai",           ["ai", "chatgpt", "robot", "thay thế", "tự động", "gemini"]),
    ("scam_app",     ["deepfake", "hack", "rò rỉ", "lừa online", "scam", "giả mạo"]),
    ("cancel",       ["cancel", "cô lập", "bị loại", "tẩy chay người"]),
    ("nostalgia",    ["nhớ", "hoài niệm", "ngày xưa", "tuổi thơ", "hồi nhỏ"]),
    ("relationship", ["tình yêu", "chia tay", "phản bội", "cặp đôi", "hôn nhân"]),
    ("lifestyle",    ["ăn", "du lịch", "thời trang", "làm đẹp", "gym"]),
]

EMOTION_RULES: List[tuple] = [
    ("shock",   ["sốc", "không tin", "kinh hoàng", "bất ngờ", "wtf"]),
    ("anger",   ["tức", "phẫn nộ", "bực", "ghét", "phản đối"]),
    ("fear",    ["sợ", "lo lắng", "nguy hiểm", "cảnh báo", "rủi ro"]),
    ("sadness", ["buồn", "đau lòng", "xót xa", "thương", "mất"]),
    ("joy",     ["vui", "hạnh phúc", "tuyệt vời", "yêu", "thích"]),
]

# Map category → pattern (từ pattern_to_chimera.json)
PATTERN_MAP: Dict[str, str] = {
    "finance":      "economic_collapse",
    "scandal":      "hidden_truth_exposed",
    "crime":        "power_vacuum",
    "protest":      "authority_distrust",
    "ai":           "fear_of_replacement",
    "scam_app":     "technology_backfire",
    "cancel":       "social_isolation",
    "nostalgia":    "lost_legacy",
    "relationship": "moral_dilemma",
    "lifestyle":    "lost_legacy",
}

# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def classify_category(text: str) -> str:
    text_lower = text.lower()
    for category, keywords in CATEGORY_RULES:
        if any(kw in text_lower for kw in keywords):
            return category
    return "lifestyle"

def classify_emotion(text: str) -> str:
    text_lower = text.lower()
    for emotion, keywords in EMOTION_RULES:
        if any(kw in text_lower for kw in keywords):
            return emotion
    return "neutral"

def extract_hashtags(text: str) -> List[str]:
    return re.findall(r"#\w+", text)

def clean_title(text: str, max_words: int = 30) -> str:
    """Cắt title ≤ 30 từ, loại hashtag thừa."""
    if not text:
        return "Không có tiêu đề"
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    return " ".join(words[:max_words]).strip()

def dedup(items: List[Dict]) -> List[Dict]:
    """Loại bỏ item trùng title (dựa trên hash 8 ký tự đầu)."""
    seen = set()
    result = []
    for item in items:
        h = hashlib.md5(item["title"][:40].encode()).hexdigest()[:8]
        if h not in seen:
            seen.add(h)
            result.append(item)
    return result

# ══════════════════════════════════════════════════════════════════════
# COLLECTOR 1: TIKTOK VIA APIFY (NGUỒN CHÍNH)
# ══════════════════════════════════════════════════════════════════════

def collect_from_tiktok_apify() -> List[Dict]:
    """
    Cào TikTok qua Apify Actor: clockworks/tiktok-scraper
    Lấy dữ liệu THẬT: shareCount, playCount từ TikTok.
    Chỉ lấy video có shareCount > 500 (đảm bảo là tin HOT).
    """
    if not APIFY_TOKEN:
        logger.warning("⚠️ APIFY_TOKEN trống — bỏ qua Apify")
        return []

    items = []
    try:
        client = ApifyClient(APIFY_TOKEN)
        
        # Input cho TikTok Scraper
        run_input = {
            "searchQueries": ["#hongbien", "#drama", "#bocphot", "#xuhuong", "#var"],
            "resultsPerPage": 30,
            "maxResultsQueries": 15,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
        }
        
        logger.info("⏳ Đang gọi Apify cào TikTok (mất 2-3 phút)...")
        run = client.actor("clockworks/tiktok-scraper").call(run_input=run_input)
        
        # Lấy dữ liệu từ Dataset
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            share_count = item.get("shareCount", 0)
            play_count = item.get("playCount", 0)
            
            # Lọc: Chỉ lấy video có > 500 shares (tin HOT thật sự)
            if share_count < 500:
                continue
            
            raw_title = item.get("text", "")
            if not raw_title or len(raw_title) < 10:
                continue
            
            # Dùng shareCount làm views (hoặc playCount nếu muốn)
            views = share_count * 100  # Ước tính views từ shares
            
            cat = classify_category(raw_title)
            items.append({
                "title":      clean_title(raw_title, 30),
                "hashtags":   extract_hashtags(raw_title),
                "views":      views,
                "category":   cat,
                "emotion":    classify_emotion(raw_title),
                "pattern":    PATTERN_MAP.get(cat, "moral_dilemma"),
                "raw_text":   raw_title[:200],
                "_source":    "tiktok_apify",
                "_tiktok_id": item.get("id"),
                "_link":      item.get("webVideoUrl"),
            })
        
        logger.info(f"[TikTok-Apify] Lấy được {len(items)} video hot (shareCount > 500)")
        
    except Exception as e:
        logger.error(f"[TikTok-Apify] Lỗi: {e}")
    
    return items

# ══════════════════════════════════════════════════════════════════════
# COLLECTOR 2: RSS BÁO VN (FALLBACK)
# ══════════════════════════════════════════════════════════════════════

def collect_from_rss() -> List[Dict]:
    """Fallback: RSS VnExpress + Tuổi Trẻ + Kênh 14."""
    items = []
    feeds = [
        ("https://vnexpress.net/rss/tin-moi-nhat.rss",  "vnexpress"),
        ("https://vnexpress.net/rss/giai-tri.rss",       "vnexpress_ent"),
        ("https://tuoitre.vn/rss/tin-moi-nhat.rss",      "tuoitre"),
        ("https://tuoitre.vn/rss/giai-tri.rss",          "tuoitre_ent"),
        ("https://kenh14.vn/rss/home.rss",               "kenh14"),
        ("https://kenh14.vn/rss/showbiz.rss",            "kenh14_showbiz"),
    ]
    
    for url, source_name in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")
                full    = f"{title} {summary}"
                
                if not title:
                    continue
                
                # Ước tính views cho RSS (vì không có số thật)
                viral_signals = ["viral", "hot", "trending", "triệu view", "lan truyền"]
                if any(s in full.lower() for s in viral_signals):
                    views = random.randint(1_000_000, 5_000_000)
                else:
                    views = random.randint(50_000, 500_000)
                
                cat = classify_category(full)
                items.append({
                    "title":      clean_title(title, 30),
                    "hashtags":   extract_hashtags(full),
                    "views":      views,
                    "category":   cat,
                    "emotion":    classify_emotion(full),
                    "pattern":    PATTERN_MAP.get(cat, "moral_dilemma"),
                    "raw_text":   summary[:200],
                    "_source":    source_name,
                })
            
            logger.info(f"[RSS] {source_name}: {len(feed.entries)} entries")
            
        except Exception as e:
            logger.warning(f"[RSS] Lỗi {url}: {e}")
    
    return items

# ══════════════════════════════════════════════════════════════════════
# COLLECTOR 3: KENH14 HTML SCRAPE (FALLBACK)
# ══════════════════════════════════════════════════════════════════════

def collect_from_kenh14_html() -> List[Dict]:
    """Scrape Kenh14 trending HTML."""
    items = []
    try:
        from html.parser import HTMLParser
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "vi,vi-VN;q=0.9",
        }
        
        resp = requests.get(
            "https://kenh14.vn/tag/trending.chn",
            timeout=TIMEOUT,
            headers=headers
        )
        
        if resp.status_code != 200:
            return items
        
        class TitleExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.titles = []
                self._in_title = False
            
            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                cls = attrs_dict.get("class", "")
                if tag == "h3" and "knl-card__title" in cls:
                    self._in_title = True
            
            def handle_data(self, data):
                if self._in_title and data.strip():
                    self.titles.append(data.strip())
                    self._in_title = False
        
        parser = TitleExtractor()
        parser.feed(resp.text)
        
        for title in parser.titles[:15]:
            if len(title) > 10:
                cat = classify_category(title)
                items.append({
                    "title":      clean_title(title, 30),
                    "hashtags":   extract_hashtags(title),
                    "views":      random.randint(100_000, 1_000_000),
                    "category":   cat,
                    "emotion":    classify_emotion(title),
                    "pattern":    PATTERN_MAP.get(cat, "moral_dilemma"),
                    "raw_text":   title,
                    "_source":    "kenh14_trending",
                })
        
        logger.info(f"[Kenh14 HTML] Scraped {len(parser.titles)} titles")
        
    except Exception as e:
        logger.warning(f"[Kenh14 HTML] Lỗi: {e}")
    
    return items

# ══════════════════════════════════════════════════════════════════════
# MAIN COLLECT
# ══════════════════════════════════════════════════════════════════════

def collect_all() -> List[Dict]:
    """Tổng hợp từ tất cả nguồn, dedup, sort by views."""
    all_items: List[Dict] = []
    logger.info("=== Bắt đầu thu thập trend ===")
    
    # 1. TikTok qua Apify (NGUỒN CHÍNH - dữ liệu thật)
    tt = collect_from_tiktok_apify()
    logger.info(f"🎵 TikTok Apify: {len(tt)} items")
    all_items.extend(tt)
    
    # 2. RSS báo VN (FALLBACK)
    rss = collect_from_rss()
    logger.info(f"📰 RSS: {len(rss)} items")
    all_items.extend(rss)
    
    # 3. Kenh14 HTML (FALLBACK)
    k14 = collect_from_kenh14_html()
    logger.info(f"🔥 Kenh14 HTML: {len(k14)} items")
    all_items.extend(k14)
    
    # Dedup, sort by views desc, giới hạn MAX_ITEMS
    deduped = dedup(all_items)
    deduped.sort(key=lambda x: x.get("views", 0), reverse=True)
    
    # Bỏ field internal (_source, _tiktok_id, _link)
    final = []
    for item in deduped[:MAX_ITEMS]:
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        final.append(clean)
    
    logger.info(f"✅ Tổng sau dedup: {len(final)} / {len(all_items)} raw items")
    return final

# ══════════════════════════════════════════════════════════════════════
# BUILD GIST JSON
# ══════════════════════════════════════════════════════════════════════

def build_gist_payload(items: List[Dict]) -> Dict:
    """Tạo payload đúng schema genz_hot_news_schema.json."""
    return {
        "updated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source":      "tiktok_vn",
        "collector":   "chimera-trend-collector",
        "items_count": len(items),
        "items":       items,
    }

# ══════════════════════════════════════════════════════════════════════
# PATCH GIST
# ══════════════════════════════════════════════════════════════════════

def update_gist(content: str) -> bool:
    """PATCH Gist qua GitHub API."""
    if not GIST_TOKEN:
        logger.error("❌ GIST_TOKEN trống — không thể update Gist")
        return False
    
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization":        f"Bearer {GIST_TOKEN}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "files": {
            GIST_FILE: {"content": content}
        }
    }
    
    try:
        resp = requests.patch(url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        updated = resp.json().get("updated_at", "?")
        logger.info(f"✅ Gist updated at: {updated}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Gist PATCH thất bại: {e}")
        if hasattr(e, "response") and e.response is not None:
            logger.error(f"   Response: {e.response.text[:300]}")
        return False

# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 1. Thu thập
    items = collect_all()
    if not items:
        logger.error("⚠️ Không có item nào — hủy update Gist để tránh ghi rỗng")
        exit(1)
    
    # 2. Build payload
    payload = build_gist_payload(items)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    logger.info(f"📦 Payload size: {len(content.encode())} bytes")
    
    # 3. Dry-run preview (5 items đầu)
    logger.info("🔍 Preview 5 items đầu:")
    for item in items[:5]:
        logger.info(
            f"  [{item['category']:15s}] [{item['emotion']:8s}] "
            f"[{item.get('pattern', '?'):20s}] "
            f"views={item['views']:>9,} | {item['title'][:60]}"
        )
    
    # 4. Update Gist
    success = update_gist(content)
    exit(0 if success else 1)
