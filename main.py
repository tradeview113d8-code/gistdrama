"""
chimera-trend-collector / collect.py
Thu thập trending TikTok Vietnam → format genz_hot_news.json → PATCH Gist.
Chạy bởi GitHub Actions mỗi 4 giờ (cron: "0 */4 * * *").
Không cần MongoDB, không cần secrets CHIMERA — chỉ cần GIST_TOKEN + GIST_ID.

Chiến lược thu thập (fallback theo thứ tự):
  1. RSS báo VN (VnExpress, Tuổi Trẻ, Kênh 14, Zing News)
  2. Scrape Kenh14 trending HTML
  3. TikTok RSS Bridge (qua RSSHub public)
  4. TikTok tracking aggregator (placeholder mở rộng)

Output JSON genz_hot_news.json — đúng schema T0d mong đợi.
"""

import os
import json
import re
import random
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

import requests
import feedparser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════

GIST_ID    = os.getenv("GIST_ID", "36eb2530da8921c099ddc3e571e7b55c")
GIST_TOKEN = os.getenv("GHT_TOKEN", "")       # GitHub PAT với scope gist
GIST_FILE  = "genz_hot_news.json"
MAX_ITEMS  = 30                                  # Số items tối đa trong Gist
TIMEOUT    = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# ── Category keywords ─────────────────────────────────────────────────

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

# ── Pattern mapping (urban_drama column từ pattern_to_chimera) ────────

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

def estimate_views(text: str, source: str) -> int:
    """Ước tính views dựa trên từ khóa mức độ viral."""
    viral_signals = ["viral", "hot", "trending", "triệu view", "lan truyền", "bùng nổ"]
    high_signals  = ["nổi tiếng", "được chia sẻ", "nhiều người", "cộng đồng mạng"]
    text_lower = text.lower()
    if any(s in text_lower for s in viral_signals):
        return random.randint(3_000_000, 10_000_000)
    if any(s in text_lower for s in high_signals):
        return random.randint(500_000, 3_000_000)
    if source == "tiktok":
        return random.randint(100_000, 500_000)
    return random.randint(10_000, 100_000)

def extract_hashtags(text: str) -> List[str]:
    return re.findall(r"#\w+", text)

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

def clean_title(text: str, max_words: int = 30) -> str:
    """Cắt title ≤ 30 từ, loại hashtag thừa."""
    if not text:
        return "Không có tiêu đề"
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    return " ".join(words[:max_words]).strip()

# ══════════════════════════════════════════════════════════════════════
# COLLECTORS
# ══════════════════════════════════════════════════════════════════════

def _rss_to_items(url: str, source_name: str, limit: int = 10) -> List[Dict]:
    """Helper chung: parse RSS → list items."""
    items = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit]:
            title   = entry.get("title", "")
            summary = entry.get("summary", "")
            full    = f"{title} {summary}"
            if not title:
                continue
            cat = classify_category(full)
            items.append({
                "title":          clean_title(title),
                "hashtags":       extract_hashtags(full),
                "views":          estimate_views(full, "news"),
                "category":       cat,
                "emotion":        classify_emotion(full),
                "pattern":        PATTERN_MAP.get(cat, "moral_dilemma"),
                "raw_text":       summary[:200],
                "_source":        source_name,
                "_published":     entry.get("published", ""),
            })
        logger.info(f"[RSS] {source_name}: {len(feed.entries)} entries → {len(items)} items")
    except Exception as e:
        logger.warning(f"[RSS] Lỗi {url}: {e}")
    return items


def collect_from_rss() -> List[Dict]:
    """Nguồn 1: RSS báo VN chính thống."""
    feeds = [
        ("https://vnexpress.net/rss/tin-moi-nhat.rss",  "vnexpress"),
        ("https://vnexpress.net/rss/giai-tri.rss",       "vnexpress_ent"),
        ("https://tuoitre.vn/rss/tin-moi-nhat.rss",      "tuoitre"),
        ("https://tuoitre.vn/rss/giai-tri.rss",          "tuoitre_ent"),
        ("https://kenh14.vn/rss/home.rss",               "kenh14"),
        ("https://kenh14.vn/rss/showbiz.rss",            "kenh14_showbiz"),
        ("https://kenh14.vn/rss/star.rss",               "kenh14_star"),
        ("https://zingnews.vn/rss/giai-tri.rss",         "zingnews_ent"),
    ]
    items = []
    for url, name in feeds:
        items.extend(_rss_to_items(url, name))
    return items


def collect_from_kenh14_html() -> List[Dict]:
    """Nguồn 2: Scrape Kenh14 trending HTML."""
    items = []
    try:
        resp = requests.get(
            "https://kenh14.vn/tag/trending.chn",
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "vi,vi-VN;q=0.9"},
        )
        if resp.status_code != 200:
            return items

        from html.parser import HTMLParser

        class TitleExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.titles: List[str] = []
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
                    "title":      clean_title(title),
                    "hashtags":   extract_hashtags(title),
                    "views":      estimate_views(title, "social"),
                    "category":   cat,
                    "emotion":    classify_emotion(title),
                    "pattern":    PATTERN_MAP.get(cat, "moral_dilemma"),
                    "raw_text":   title,
                    "_source":    "kenh14_trending",
                    "_published": "",
                })
        logger.info(f"[Kenh14 HTML] Scraped {len(parser.titles)} titles → {len(items)} items")
    except Exception as e:
        logger.warning(f"[Kenh14 HTML] Lỗi: {e}")
    return items


def collect_from_tiktok_rsshub() -> List[Dict]:
    """Nguồn 3: TikTok qua RSSHub public bridge."""
    items = []
    # Các kênh TikTok GenZ VN phổ biến
    channels = [
        "beatvn_official",
        "welax",
        "yeah1com",
        "yan.vn",
        "tintuc24h",
    ]
    rsshub_base = "https://rsshub.app/tiktok/user"

    for channel in channels:
        url = f"{rsshub_base}/@{channel}"
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                title = entry.get("title", "")
                if title:
                    cat = classify_category(title)
                    items.append({
                        "title":      clean_title(title),
                        "hashtags":   extract_hashtags(title),
                        "views":      estimate_views(title, "tiktok"),
                        "category":   cat,
                        "emotion":    classify_emotion(title),
                        "pattern":    PATTERN_MAP.get(cat, "moral_dilemma"),
                        "raw_text":   entry.get("summary", "")[:200],
                        "_source":    f"tiktok_{channel}",
                        "_published": entry.get("published", ""),
                    })
            logger.info(f"[TikTok-RSSHub] @{channel}: {len(feed.entries)} entries")
        except Exception as e:
            logger.warning(f"[TikTok-RSSHub] @{channel} lỗi: {e}")
    return items


# ══════════════════════════════════════════════════════════════════════
# MAIN COLLECT
# ══════════════════════════════════════════════════════════════════════

def collect_all() -> List[Dict]:
    """Tổng hợp từ tất cả nguồn, dedup, sort by views."""
    all_items: List[Dict] = []
    logger.info("=== Bắt đầu thu thập trend ===")

    # 1. RSS báo VN (luôn chạy, ổn định nhất)
    rss = collect_from_rss()
    logger.info(f"📰 RSS: {len(rss)} items")
    all_items.extend(rss)

    # 2. Kenh14 HTML trending
    k14 = collect_from_kenh14_html()
    logger.info(f"🔥 Kenh14 HTML: {len(k14)} items")
    all_items.extend(k14)

    # 3. TikTok qua RSSHub
    tt = collect_from_tiktok_rsshub()
    logger.info(f"🎵 TikTok RSSHub: {len(tt)} items")
    all_items.extend(tt)

    # Dedup, sort by views desc, giới hạn MAX_ITEMS
    deduped = dedup(all_items)
    deduped.sort(key=lambda x: x.get("views", 0), reverse=True)

    # Bỏ field internal (_source, _published)
    final = []
    for item in deduped[:MAX_ITEMS]:
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        final.append(clean)

    logger.info(f"✅ Tổng sau dedup: {len(final)} / {len(all_items)} raw items")
    return final

# ══════════════════════════════════════════════════════════════════════
# BUILD GIST JSON (đúng schema genz_hot_news_schema.json)
# ══════════════════════════════════════════════════════════════════════

def build_gist_payload(items: List[Dict]) -> Dict:
    """Tạo payload đúng schema T0d mong đợi."""
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
