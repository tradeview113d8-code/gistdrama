import os
import re
import json
import requests
from datetime import datetime, timedelta
from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

def truncate_title(text, max_words=30):
    """Cắt tiêu đề còn tối đa 30 từ"""
    if not text: return "Không có tiêu đề"
    text = re.sub(r'#\w+', '', text)
    words = text.split()
    return ' '.join(words[:max_words]).strip()

def get_gist_content():
    """Đọc nội dung hiện tại của Gist"""
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"❌ Lỗi đọc Gist: {response.text}")
        return {"batches": []}
    
    gist_data = response.json()
    file_content = gist_data["files"]["genz_hot_news.json"]["content"]
    return json.loads(file_content)

def update_gist(data):
    """Cập nhật nội dung Gist"""
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "files": {
            "genz_hot_news.json": {
                "content": json.dumps(data, ensure_ascii=False, indent=2)
            }
        }
    }
    response = requests.patch(url, headers=headers, json=payload)
    
    if response.status_code == 200:
        print("✅ Đã cập nhật Gist thành công!")
    else:
        print(f"❌ Lỗi cập nhật Gist: {response.text}")

def run_pipeline():
    print("🚀 Bắt đầu pipeline cào tin nóng...")
    
    # 1. Cào dữ liệu từ Apify
    client = ApifyClient(APIFY_TOKEN)
    run_input = {
        "searchQueries": ["#hongbien", "#drama", "#bocphot", "#xuhuong"],
        "resultsPerPage": 20,
        "maxResultsQueries": 10,
        "shouldDownloadVideos": False,
    }
    
    print("⏳ Đang chờ Apify cào dữ liệu...")
    run = client.actor("clockworks/tiktok-scraper").call(run_input=run_input)
    
    # 2. Lọc tin nóng
    hot_news = []
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        if item.get("shareCount", 0) > 500:
            raw_title = item.get("text", "")
            hot_news.append({
                "tiktok_id": item.get("id"),
                "title": truncate_title(raw_title, 30),
                "author": item.get("authorMeta", {}).get("name", "unknown"),
                "play_count": item.get("playCount", 0),
                "share_count": item.get("shareCount", 0),
                "link": item.get("webVideoUrl"),
            })
    
    print(f"✅ Lọc được {len(hot_news)} tin nóng hợp lệ.")
    
    if not hot_news:
        print("⚠️ Không có tin nào đủ điều kiện hot.")
        return
    
    # 3. Đọc Gist hiện tại
    gist_data = get_gist_content()
    
    # 4. Thêm batch mới
    current_time = datetime.utcnow()
    new_batch = {
        "timestamp": current_time.isoformat(),
        "news": hot_news
    }
    gist_data["batches"].append(new_batch)
    
    # 5. Lọc bỏ batch cũ (> 24h) và giữ tối đa 4 batch
    cutoff_time = current_time - timedelta(hours=24)
    gist_data["batches"] = [
        batch for batch in gist_data["batches"]
        if datetime.fromisoformat(batch["timestamp"]) > cutoff_time
    ]
    
    # Giữ tối đa 4 batch (phòng trường hợp chạy nhiều hơn 4 lần/ngày)
    gist_data["batches"] = gist_data["batches"][-4:]
    
    print(f"📊 Gist hiện có {len(gist_data['batches'])} batch (tối đa 4 batch / 24h)")
    
    # 6. Cập nhật Gist
    update_gist(gist_data)

if __name__ == "__main__":
    run_pipeline()
