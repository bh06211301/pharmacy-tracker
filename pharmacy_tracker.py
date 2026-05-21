"""
pharmacy_tracker.py
===================
藥局異動追蹤系統 — GitHub Actions 版
覆蓋範圍：台北市、新北市、基隆市、桃園市
偵測類型：全新開幕 / 疑似搬遷 / 疑似換老闆
執行排程：週一、三、五 08:00（GitHub Actions 自動觸發）
"""

import os
import re
import time
import unicodedata
from datetime import datetime

import gspread
import requests
from google.oauth2.service_account import Credentials

# ════════════════════════════════════════════
#  設定（從 GitHub Secrets 環境變數讀取）
# ════════════════════════════════════════════
PLACES_API_KEY   = os.environ["PLACES_API_KEY"]
SPREADSHEET_ID   = os.environ["SPREADSHEET_ID"]
LINE_TOKEN       = os.environ["LINE_TOKEN"]
LINE_USER_ID     = os.environ["LINE_USER_ID"]
CREDENTIALS_FILE = "credentials.json"

TODAY     = datetime.today().strftime("%Y-%m-%d")
TODAY_INT = datetime.today().strftime("%Y%m%d")

CITY_CODES = {
    "63000": "台北市",
    "65000": "新北市",
    "10017": "基隆市",
    "68000": "桃園市",
}

# ════════════════════════════════════════════
#  自動產生六角形網格座標（2.5km 間距，無死角）
# ════════════════════════════════════════════

def generate_grid(city, min_lat, max_lat, min_lon, max_lon, spacing_km=2.5):
    step_lat = spacing_km / 111.0
    step_lon = spacing_km / 101.0
    points, row = [], 0
    lat = min_lat
    while lat <= max_lat + step_lat * 0.1:
        lon = min_lon + (step_lon / 2 if row % 2 else 0)
        while lon <= max_lon + step_lon * 0.1:
            points.append((city, f"{lat:.4f},{lon:.4f}", 1500))
            lon += step_lon
        lat += step_lat
        row += 1
    return points

REGION_BOUNDS = [
    ("台北市", 24.985, 25.210, 121.445, 121.650),
    ("新北市", 24.920, 25.110, 121.370, 121.560),
    ("新北市", 25.060, 25.185, 121.440, 121.700),
    ("新北市", 24.930, 25.020, 121.540, 121.820),
    ("基隆市", 25.080, 25.200, 121.680, 121.810),
    ("桃園市", 24.930, 25.070, 121.130, 121.370),
    ("桃園市", 25.010, 25.100, 121.310, 121.430),
]

def build_locations():
    seen, locs = set(), []
    for (city, *bounds) in REGION_BOUNDS:
        for pt in generate_grid(city, *bounds):
            if pt[1] not in seen:
                seen.add(pt[1])
                locs.append(pt)
    return locs

LOCATIONS = build_locations()


# ════════════════════════════════════════════
#  地址正規化
# ════════════════════════════════════════════

def normalize_address(addr):
    if not addr:
        return ""
    addr = unicodedata.normalize("NFKC", addr)
    addr = addr.replace("臺", "台")
    addr = re.sub(r"[\s　]", "", addr)
    addr = re.sub(r"[（(][^）)]*[）)]", "", addr)
    addr = re.sub(r"\d+[、,，\d]*[樓層].*$", "", addr)
    return addr.strip()

def extract_key(addr):
    addr = normalize_address(addr)
    addr = re.sub(r"^(台|新)北市", "", addr)
    addr = re.sub(r"^(新北|基隆|桃園)市", "", addr)
    addr = re.sub(r"^[^路街巷弄號]*[區鄉鎮]", "", addr)
    return addr.strip()

def is_same_location(google_addr, official_addr):
    g = extract_key(google_addr)
    o = extract_key(official_addr)
    if not g or not o or len(g) < 4:
        return False
    shorter, longer = (g, o) if len(g) <= len(o) else (o, g)
    return shorter in longer


# ════════════════════════════════════════════
#  名稱正規化與比對
# ════════════════════════════════════════════

def clean_name(name):
    """移除常見後綴，取出核心名稱"""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"(大?藥局|藥房|藥行|中西藥局|健保藥局|社區藥局"
                  r"|連鎖藥局|藥師藥局|藥劑生藥局)$", "", name)
    # 移除連鎖品牌前綴（例：躍獅、杏一、大樹）
    name = re.sub(r"^(躍獅|杏一|大樹|丁丁|維康|專品|立赫|光點"
                  r"|康是美|屈臣氏|健康人生|富康活力)", "", name)
    return name.strip()

def is_similar_name(google_name, official_name):
    """判斷兩個名稱是否相似（含簡稱、分店等情況）"""
    g = clean_name(google_name)
    o = clean_name(official_name)
    if not g or not o or len(g) < 2:
        return False
    return g in o or o in g


# ════════════════════════════════════════════
#  Google Sheets 連線
# ════════════════════════════════════════════

def get_spreadsheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

def get_or_create_sheet(ss, title, rows=2000, cols=10):
    try:
        ws = ss.worksheet(title)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=rows, cols=cols)
    return ws


# ════════════════════════════════════════════
#  健保基準讀取
# ════════════════════════════════════════════

def load_baseline(ss):
    """
    回傳 list of dict，每筆含：
      地址、醫事機構名稱
    只保留四縣市且合約有效的藥局。
    原始 CSV 欄位：0代碼 1名稱 2種類 3電話 4地址 ... 9終止日 12縣市代碼
    """
    try:
        ws = ss.worksheet("健保基準資料")
    except gspread.exceptions.WorksheetNotFound:
        print("⚠️  找不到健保基準資料分頁")
        return []

    TARGET = {"63000", "65000", "10017", "68000"}
    rows   = []
    for row in ws.get_all_values()[1:]:
        if len(row) < 13:
            continue
        if str(row[12]).strip() in TARGET and str(row[9]).strip() >= TODAY_INT:
            rows.append({
                "地址": str(row[4]).strip(),
                "醫事機構名稱": str(row[1]).strip(),
            })

    print(f"📋 健保基準：{len(rows)} 筆有效藥局")
    return rows


# ════════════════════════════════════════════
#  Google Places API
# ════════════════════════════════════════════

def fetch_pharmacies(city, location, radius):
    url, results = "https://maps.googleapis.com/maps/api/place/nearbysearch/json", []
    params = {"location": location, "radius": radius,
              "type": "pharmacy", "language": "zh-TW", "key": PLACES_API_KEY}

    while True:
        try:
            res = requests.get(url, params=params, timeout=10).json()
        except Exception as e:
            print(f"    ⚠️  API 錯誤：{e}")
            break
        for p in res.get("results", []):
            results.append({
                "place_id": p.get("place_id", ""),
                "名稱":     p.get("name", ""),
                "地址":     p.get("vicinity", ""),
                "評分":     str(p.get("rating", "")),
                "評論數":   str(p.get("user_ratings_total", "")),
                "縣市":     city,
            })
        token = res.get("next_page_token")
        if not token:
            break
        time.sleep(2)
        params = {"pagetoken": token, "key": PLACES_API_KEY}
    return results

def fetch_all():
    all_data, total = {}, len(LOCATIONS)
    for i, (city, location, radius) in enumerate(LOCATIONS, 1):
        if i == 1 or i % 20 == 0:
            print(f"  進度：{i}/{total}")
        for p in fetch_pharmacies(city, location, radius):
            if p["place_id"] and p["place_id"] not in all_data:
                all_data[p["place_id"]] = p
        time.sleep(0.5)
    return all_data


# ════════════════════════════════════════════
#  核心比對：四種情況
# ════════════════════════════════════════════

def classify_pharmacies(all_data, baseline):
    """
    逐筆比對，分成四類：
      new_open   🆕 全新開幕：地址不在名單，名稱也沒有
      relocated  🔄 疑似搬遷：地址不在名單，但有同名在別處
      new_owner  👤 疑似換老闆：地址相同，但名稱不一樣
      normal     （略過）：地址和名稱都吻合
    """
    baseline_addrs = [b["地址"] for b in baseline if b["地址"]]
    baseline_names = [b["醫事機構名稱"] for b in baseline if b["醫事機構名稱"]]

    # 建立地址 → 官方名稱的對照表（供換老闆判斷用）
    addr_to_name = {}
    for b in baseline:
        key = extract_key(b["地址"])
        if key:
            addr_to_name[key] = b["醫事機構名稱"]

    new_open, relocated, new_owner = [], [], []

    for p in all_data.values():
        google_addr = p["地址"]
        google_name = p["名稱"]

        # 找是否有吻合的官方地址
        matched_base_addr = None
        for base_addr in baseline_addrs:
            if is_same_location(google_addr, base_addr):
                matched_base_addr = base_addr
                break

        if matched_base_addr:
            # 地址相同 → 再比名稱
            official_name = addr_to_name.get(extract_key(matched_base_addr), "")
            if official_name and not is_similar_name(google_name, official_name):
                # 地址相同，名稱不同 → 疑似換老闆
                p["備註"]    = "👤 疑似換老闆／改名"
                p["原名稱"] = official_name
                new_owner.append(p)
            # else: 正常藥局，略過
        else:
            # 地址不在名單 → 再比名稱
            name_matched = any(is_similar_name(google_name, o) for o in baseline_names)
            if name_matched:
                p["備註"]   = "🔄 疑似搬遷／二代接班"
                p["原名稱"] = ""
                relocated.append(p)
            else:
                p["備註"]   = "🆕 疑似全新開幕"
                p["原名稱"] = ""
                new_open.append(p)

    return new_open, relocated, new_owner


# ════════════════════════════════════════════
#  寫入 Google Sheets
# ════════════════════════════════════════════

HEADERS_SNAPSHOT = ["place_id", "名稱", "地址", "評分", "評論數", "縣市"]
HEADERS_RESULT   = ["發現日期", "place_id", "名稱", "Google地址", "縣市", "原名稱", "備註"]

def write_snapshot(ss, all_data):
    ws = get_or_create_sheet(ss, TODAY, rows=5000)
    ws.append_row(HEADERS_SNAPSHOT)
    ws.append_rows([[p[h] for h in HEADERS_SNAPSHOT] for p in all_data.values()])
    print(f"✅ 快照寫入「{TODAY}」：{len(all_data)} 筆")

def write_result_sheet(ss, title, pharmacies):
    ws = get_or_create_sheet(ss, title, rows=500)
    ws.append_row(HEADERS_RESULT)
    if pharmacies:
        ws.append_rows([[
            TODAY,
            p["place_id"],
            p["名稱"],
            p["地址"],
            p["縣市"],
            p.get("原名稱", ""),
            p["備註"],
        ] for p in pharmacies])


# ════════════════════════════════════════════
#  LINE 通知
# ════════════════════════════════════════════

def send_line(text):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_TOKEN}"},
        json={"to": LINE_USER_ID,
              "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )

def build_message(new_open, relocated, new_owner, total):
    has_result = any([new_open, relocated, new_owner])
    lines = [f"🏥 藥局異動報告", f"📅 {TODAY}",
             f"共比對 {total} 間", "─" * 22]

    if not has_result:
        lines.append("\n本次無異動紀錄")
        return "\n".join(lines)

    def section(icon, label, items, show_original=False):
        if not items:
            return
        lines.append(f"\n{icon} {label}：{len(items)} 間")
        for p in items[:5]:
            lines.append(f"  • {p['名稱']}")
            lines.append(f"    📍 {p['地址']}")
            if show_original and p.get("原名稱"):
                lines.append(f"    原名：{p['原名稱']}")
        if len(items) > 5:
            lines.append(f"    ...還有 {len(items)-5} 間，見 Sheets")

    section("🆕", "全新開幕", new_open)
    section("🔄", "疑似搬遷", relocated)
    section("👤", "疑似換老闆", new_owner, show_original=True)

    return "\n".join(lines)


# ════════════════════════════════════════════
#  主程式
# ════════════════════════════════════════════

def main():
    print("=" * 50)
    print(f"  藥局異動追蹤  |  {TODAY}")
    print(f"  網格座標點數：{len(LOCATIONS)}")
    print("=" * 50)

    ss       = get_spreadsheet()
    baseline = load_baseline(ss)

    print(f"\n📡 抓取 Google Places（{len(LOCATIONS)} 個座標點）...")
    all_data = fetch_all()
    print(f"✅ 共抓到 {len(all_data)} 間（去重複後）")

    write_snapshot(ss, all_data)

    print("\n🔍 比對中...")
    new_open, relocated, new_owner = classify_pharmacies(all_data, baseline)

    write_result_sheet(ss, "🆕 全新開幕", new_open)
    write_result_sheet(ss, "🔄 疑似搬遷", relocated)
    write_result_sheet(ss, "👤 疑似換老闆", new_owner)

    print(f"   全新開幕：{len(new_open)} 間")
    print(f"   疑似搬遷：{len(relocated)} 間")
    print(f"   疑似換老闆：{len(new_owner)} 間")

    msg = build_message(new_open, relocated, new_owner, len(all_data))
    send_line(msg)
    print("\n📲 LINE 通知已發送")
    print("🎉 完成！")

if __name__ == "__main__":
    main()
