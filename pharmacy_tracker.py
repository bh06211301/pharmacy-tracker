"""
pharmacy_tracker.py
===================
藥局異動追蹤系統 — GitHub Actions 版 v5
覆蓋範圍：台北市、新北市、基隆市、桃園市（47 個行政區，密度加權座標點）

比對邏輯：
  以 place_id 為唯一識別，比對本次與上次快照
  🆕 新出現：place_id 上次沒有、這次有 → 交叉比對健保名單
  🚪 消失中：place_id 上次有、這次沒有 → 可能關閉
  👤 改名了：place_id 相同但名稱不一樣 → 可能換老闆

健保 CSV 角色（輔助驗證）：
  新出現的藥局若不在健保名單 → 最新、最有價值的開發對象
  新出現的藥局若已在健保名單 → 可能只是剛上 Google Maps

排除品牌：杏一、大樹、丁丁、維康、專品、立赫、光點、
         康是美、屈臣氏、健康人生、富康活力、優嘉、快樂鳥
執行排程：每月 1 號 08:00（GitHub Actions 自動觸發）

v4 變更（2026-07-30 修正 Places API 帳單事件）：
  - 舊版 legacy Nearby Search（含 rating/評論數）落在 Enterprise 計費層，
    免費額度僅 1,000 次/月，585 點 × 週三次 = 每月上萬次呼叫，單月被
    收費超過 $1,000 美金
  - 改用新版 Places API（searchNearby v1），FieldMask 只要 Pro 層欄位
    （id/名稱/地址/座標），免費額度提高到 5,000 次/月、單價也更低
  - 新版 API 不支援分頁（單次最多 20 筆），故不再有一個座標點打 2-3
    次的狀況，呼叫次數等於座標點數

v5 變更（座標點模型改版）：
  - 移除楊梅/龍潭/大溪/觀音/新屋：實際健保藥局密度太低（22-102家 分散
    在 56-81 個網格點），且使用者現有客戶完全沒有涵蓋到這幾區
  - 拿掉「矩形硬鋪」設計，改成以**行政區**為單位、依實際健保藥局密度
    （家數 ÷ 行政區面積）反推每個行政區該用多大搜尋半徑：
      目標讓每點預期回傳筆數落在 20 筆上限的七八成（約15筆），留緩衝
      空間，避免密集區未來成長超過 20 筆時被新版 API 悄悄截斷漏抓
      （新版 API 沒有分頁，超過 20 筆的部分不會有任何警訊）
    半徑上限 4000m、下限 800m。密集區（板橋/大安/信義等）半徑會被壓
    到 800m 左右；空曠山區/海邊（石碇/平溪/萬里/金山）半徑封頂在
    4000m、只放 1-3 個點意思意思
  - 行政區面積與中心座標為政府公開統計值的近似整理，非逐筆地籍測量
  - 總座標點數從 585 降到 325（依 2026-07 健保藥局分布資料計算）
"""

import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta

import gspread
import requests
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception

# ════════════════════════════════════════════
#  設定（從 GitHub Secrets 環境變數讀取）
# ════════════════════════════════════════════
PLACES_API_KEY      = os.environ["PLACES_API_KEY"]
SPREADSHEET_ID      = os.environ["SPREADSHEET_ID"]
LINE_TOKEN          = os.environ["LINE_TOKEN"]
LINE_USER_ID        = os.environ["LINE_USER_ID"]
CREDENTIALS_FILE    = "credentials.json"
SMART_BOARD_URL     = "https://still-meadow-0efd.bh06211301.workers.dev"

TAIWAN_TZ = timezone(timedelta(hours=8))
TODAY     = datetime.now(tz=TAIWAN_TZ).strftime("%Y-%m-%d")
TODAY_INT = datetime.now(tz=TAIWAN_TZ).strftime("%Y%m%d")

MIN_SNAPSHOT_SIZE = 100   # 抓到筆數低於此值 → 視為異常，不覆蓋快照


# ════════════════════════════════════════════
#  Sheets API 重試（503/500/429 暫時性錯誤）
# ════════════════════════════════════════════

_sheets_retry = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=5, max=60),
    retry=retry_if_exception_type(gspread.exceptions.APIError),
    reraise=True,
)

# Places API 永久性錯誤：KEY 無效、請求格式錯誤 → 不重試
class PlacesApiPermanentError(RuntimeError):
    pass

# 只要求 Pro 層欄位（不含 rating/評論數等 Enterprise 層欄位），
# 免費額度較高（5,000 次/月）、單價也較低
PLACES_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"


# ════════════════════════════════════════════
#  不拜訪的連鎖品牌（直接排除）
# ════════════════════════════════════════════
EXCLUDE_CHAINS = [
    "杏一", "大樹", "丁丁", "維康", "專品", "立赫", "光點",
    "康是美", "屈臣氏", "健康人生", "富康活力", "優嘉", "快樂鳥",
]

def is_excluded_chain(name: str) -> bool:
    return any(chain in name for chain in EXCLUDE_CHAINS)


# ════════════════════════════════════════════
#  依行政區密度反推搜尋半徑，產生座標點
# ════════════════════════════════════════════

# 每點目標回傳筆數（新版 API 上限 20 筆，抓七八成留緩衝，避免未來
# 該區藥局數成長後悄悄被截斷漏抓）
TARGET_RESULTS_PER_POINT = 15
MIN_RADIUS_M, MAX_RADIUS_M = 800, 4000

# (縣市, 行政區, 中心緯度, 中心經度, 面積km², 2026-07 健保有效藥局家數)
# 面積/中心座標為政府公開統計值的近似整理，用於決定搜尋半徑，非地籍測量
DISTRICTS = [
    ("台北市","中正",25.032,121.519,7.61,52),
    ("台北市","大同",25.063,121.513,5.68,32),
    ("台北市","中山",25.064,121.533,13.68,80),
    ("台北市","松山",25.050,121.558,9.29,69),
    ("台北市","大安",25.026,121.543,11.36,88),
    ("台北市","萬華",25.032,121.500,8.85,46),
    ("台北市","信義",25.033,121.570,11.21,82),
    ("台北市","士林",25.092,121.525,62.37,84),
    ("台北市","北投",25.132,121.499,56.82,60),
    ("台北市","內湖",25.083,121.594,31.58,66),
    ("台北市","南港",25.055,121.606,21.84,36),
    ("台北市","文山",24.989,121.570,31.51,61),

    ("新北市","板橋",25.013,121.459,23.14,192),
    ("新北市","三重",25.061,121.487,16.32,149),
    ("新北市","中和",24.998,121.500,20.14,123),
    ("新北市","永和",25.008,121.516,5.71,78),
    ("新北市","新莊",25.037,121.433,19.74,134),
    ("新北市","土城",24.973,121.443,29.55,84),
    ("新北市","蘆洲",25.086,121.473,7.34,67),
    ("新北市","五股",25.083,121.437,35.20,20),
    ("新北市","泰山",25.056,121.430,19.24,18),
    ("新北市","林口",25.077,121.392,34.24,42),
    ("新北市","樹林",24.990,121.423,33.13,55),
    ("新北市","汐止",25.064,121.658,71.24,55),
    ("新北市","淡水",25.170,121.440,70.65,46),
    ("新北市","萬里",25.179,121.689,60.79,2),
    ("新北市","金山",25.222,121.637,49.03,4),
    ("新北市","深坑",25.002,121.616,20.50,6),
    ("新北市","石碇",24.991,121.657,79.11,1),
    ("新北市","平溪",25.026,121.740,57.19,0),
    ("新北市","新店",24.958,121.541,120.23,73),
    ("新北市","鶯歌",24.954,121.353,21.66,22),
    ("新北市","三峽",24.934,121.369,191.40,38),

    ("基隆市","仁愛",25.128,121.741,1.82,18),
    ("基隆市","信義",25.120,121.734,6.94,9),
    ("基隆市","中正",25.133,121.747,19.75,22),
    ("基隆市","中山",25.138,121.751,9.24,16),
    ("基隆市","安樂",25.150,121.730,20.44,12),
    ("基隆市","暖暖",25.106,121.771,22.98,10),
    ("基隆市","七堵",25.088,121.729,35.19,11),

    ("桃園市","桃園",24.994,121.301,34.79,169),
    ("桃園市","中壢",24.970,121.224,76.53,158),
    ("桃園市","平鎮",24.945,121.213,30.40,56),
    ("桃園市","龜山",25.006,121.343,65.90,64),
    ("桃園市","八德",24.933,121.290,33.70,62),
    ("桃園市","大園",25.061,121.207,84.87,21),
    ("桃園市","蘆竹",25.058,121.291,76.75,67),
]
# 楊梅/龍潭/大溪/觀音/新屋 已移除：密度太低（22-102家分散在56-81個網格點），
# 使用者現有客戶也完全沒有涵蓋到這幾區


def _district_radius_m(density_per_km2: float) -> float:
    """依密度反推半徑，讓預期回傳筆數落在 TARGET_RESULTS_PER_POINT 附近"""
    if density_per_km2 <= 0.02:
        return MAX_RADIUS_M
    r_km = math.sqrt(TARGET_RESULTS_PER_POINT / (density_per_km2 * math.pi))
    return max(MIN_RADIUS_M, min(MAX_RADIUS_M, r_km * 1000))


def _generate_local_grid(center_lat, center_lon, side_km, spacing_km):
    """在行政區中心點附近鋪一個小網格，範圍約等於該區面積開根號的正方形"""
    half_lat = (side_km / 2) / 111.0
    half_lon = (side_km / 2) / 101.0
    step_lat = spacing_km / 111.0
    step_lon = spacing_km / 101.0
    min_lat, max_lat = center_lat - half_lat, center_lat + half_lat
    min_lon, max_lon = center_lon - half_lon, center_lon + half_lon

    points, row = [], 0
    lat = min_lat
    while lat <= max_lat + step_lat * 0.1:
        lon = min_lon + (step_lon / 2 if row % 2 else 0)
        while lon <= max_lon + step_lon * 0.1:
            points.append((lat, lon))
            lon += step_lon
        lat += step_lat
        row += 1
    return points


def build_locations():
    seen, locs = set(), []
    for city, _dist, clat, clon, area_km2, count in DISTRICTS:
        density = count / area_km2 if area_km2 > 0 else 0.0001
        radius_m = _district_radius_m(density)
        spacing_km = (radius_m / 1000) * 1.6
        side_km = math.sqrt(area_km2)
        for lat, lon in _generate_local_grid(clat, clon, side_km, spacing_km):
            key = f"{lat:.4f},{lon:.4f}"
            if key not in seen:
                seen.add(key)
                locs.append((city, key, round(radius_m)))
    return locs

LOCATIONS = build_locations()


# ════════════════════════════════════════════
#  Google Sheets 連線
# ════════════════════════════════════════════

@_sheets_retry
def get_spreadsheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

@_sheets_retry
def get_or_create_sheet(ss, title, rows=3000, cols=12):
    try:
        ws = ss.worksheet(title)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=rows, cols=cols)
    return ws

@_sheets_retry
def _worksheets(ss):
    return ss.worksheets()

@_sheets_retry
def _get_all_values(ws):
    return ws.get_all_values()


# ════════════════════════════════════════════
#  載入上次快照（從最近一個日期分頁）
# ════════════════════════════════════════════

def load_previous_snapshot(ss) -> dict:
    """
    找出 Sheets 裡最近一個日期分頁（格式 YYYY-MM-DD）
    回傳 dict：{ place_id: { 名稱, 地址, 縣市 } }
    """
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    all_sheets   = _worksheets(ss)

    date_sheets = sorted(
        [ws for ws in all_sheets if date_pattern.match(ws.title) and ws.title != TODAY],
        key=lambda ws: ws.title,
        reverse=True,
    )

    if not date_sheets:
        print("⚠️  找不到上次快照，本次只建立基準，下次才能比對")
        return {}

    prev_ws    = date_sheets[0]
    prev_date  = prev_ws.title
    print(f"📂 上次快照：{prev_date}")

    data = _get_all_values(prev_ws)
    if not data or len(data) < 2:
        return {}

    headers = data[0]
    result  = {}
    for row in data[1:]:
        r = dict(zip(headers, row))
        pid = r.get("place_id", "").strip()
        if pid:
            result[pid] = {
                "名稱": r.get("名稱", ""),
                "地址": r.get("地址", ""),
                "縣市": r.get("縣市", ""),
            }

    print(f"   載入 {len(result)} 筆上次資料")
    return result


# ════════════════════════════════════════════
#  載入健保基準（輔助驗證用）
# ════════════════════════════════════════════

def load_baseline_addresses(ss) -> set:
    """
    從健保基準資料分頁載入有效藥局地址集合。
    原始 CSV 欄位：0代碼 1名稱 2種類 3電話 4地址 ... 9終止日 12縣市代碼
    """
    TARGET = {"63000", "65000", "10017", "68000"}
    try:
        ws = ss.worksheet("健保基準資料")
    except gspread.exceptions.WorksheetNotFound:
        print("⚠️  找不到健保基準資料，跳過健保驗證")
        return set()

    addrs = set()
    for row in _get_all_values(ws)[1:]:
        if len(row) < 13:
            continue
        if str(row[12]).strip() in TARGET and str(row[9]).strip() >= TODAY_INT:
            addr = str(row[4]).strip()
            if addr:
                addrs.add(normalize_addr(addr))

    print(f"📋 健保基準：{len(addrs)} 筆有效地址")
    return addrs

def normalize_addr(addr: str) -> str:
    """簡單正規化地址供健保交叉比對"""
    addr = unicodedata.normalize("NFKC", addr)
    addr = addr.replace("臺", "台")
    addr = re.sub(r"[\s　]", "", addr)
    addr = re.sub(r"[（(][^）)]*[）)]", "", addr)
    addr = re.sub(r"\d+[、,，\d]*[樓層].*$", "", addr)
    return addr.strip()

def is_in_health_insurance(pharmacy: dict, baseline_addrs: set) -> bool:
    """判斷此藥局是否已在健保名單"""
    g_addr = normalize_addr(pharmacy.get("地址", ""))
    if not g_addr:
        return False
    # 用包含比對（因格式可能不完全一致）
    return any(g_addr in b or b in g_addr for b in baseline_addrs if len(b) > 4)


# ════════════════════════════════════════════
#  Google Places API
# ════════════════════════════════════════════

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(lambda e: not isinstance(e, PlacesApiPermanentError)),
    reraise=True,
)
def _call_places_api(body: dict) -> dict:
    resp = requests.post(
        "https://places.googleapis.com/v1/places:searchNearby",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": PLACES_API_KEY,
            "X-Goog-FieldMask": PLACES_FIELD_MASK,
        },
        json=body,
        timeout=15,
    )
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code in (429, 500, 502, 503):
        raise Exception(f"Places API 暫時性錯誤 HTTP {resp.status_code}")
    # 400/401/403 等 → key 無效或請求格式錯誤，屬永久性錯誤，直接中止不重試
    raise PlacesApiPermanentError(
        f"Places API 永久錯誤: HTTP {resp.status_code} {resp.text[:200]}（請檢查 API Key 是否有效）"
    )

def fetch_pharmacies(city, location, radius):
    lat, lng = location.split(",")
    body = {
        "includedTypes": ["pharmacy"],
        "maxResultCount": 20,
        "rankPreference": "DISTANCE",
        "languageCode": "zh-TW",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(lat), "longitude": float(lng)},
                "radius": float(radius),
            }
        },
    }
    try:
        res = _call_places_api(body)
    except PlacesApiPermanentError:
        raise  # 往上傳遞，讓 main() 處理
    except Exception as e:
        print(f"    ⚠️  API 錯誤（已重試3次）：{e}")
        return []

    results = []
    for p in res.get("places", []):
        loc = p.get("location", {})
        results.append({
            "place_id": p.get("id", ""),
            "名稱":     p.get("displayName", {}).get("text", ""),
            "地址":     p.get("formattedAddress", ""),
            "縣市":     city,
            "緯度":     str(loc.get("latitude", "")),
            "經度":     str(loc.get("longitude", "")),
        })
    return results

def fetch_all() -> dict:
    """抓取所有區域藥局，以 place_id 去重，排除連鎖品牌"""
    all_data, excluded, total = {}, 0, len(LOCATIONS)
    for i, (city, location, radius) in enumerate(LOCATIONS, 1):
        if i == 1 or i % 20 == 0:
            print(f"  進度：{i}/{total}")
        try:
            for p in fetch_pharmacies(city, location, radius):
                if not p["place_id"]:
                    continue
                if is_excluded_chain(p["名稱"]):
                    excluded += 1
                    continue
                if p["place_id"] not in all_data:
                    all_data[p["place_id"]] = p
        except PlacesApiPermanentError:
            raise  # 立即中止整個搜尋
        time.sleep(0.5)
    print(f"  已排除連鎖品牌：{excluded} 筆（重複計算）")
    return all_data


# ════════════════════════════════════════════
#  核心比對：place_id 快照比對
# ════════════════════════════════════════════

def compare_snapshots(today: dict, previous: dict, baseline_addrs: set):
    """
    以 place_id 比對今日與上次快照，分三類：
      new_with_insurance    🆕 新出現 + 已在健保
      new_without_insurance 🆕 新出現 + 未在健保（最高優先）
      disappeared           🚪 消失（可能關閉）
      renamed               👤 改名（可能換老闆）
    """
    today_ids    = set(today.keys())
    previous_ids = set(previous.keys())

    # 新出現的 place_id
    new_ids = today_ids - previous_ids
    new_with    = []   # 已在健保
    new_without = []   # 未在健保（全新！）

    for pid in new_ids:
        p = today[pid]
        if is_in_health_insurance(p, baseline_addrs):
            p["健保狀態"] = "✅ 已有健保"
            new_with.append(p)
        else:
            p["健保狀態"] = "❗ 尚未健保"
            new_without.append(p)

    # 消失的 place_id
    disappeared_ids = previous_ids - today_ids
    disappeared = [previous[pid] | {"place_id": pid} for pid in disappeared_ids]

    # 改名（place_id 相同，名稱不同）
    renamed = []
    for pid in today_ids & previous_ids:
        t_name = today[pid]["名稱"]
        p_name = previous[pid]["名稱"]
        if t_name != p_name:
            renamed.append({
                **today[pid],
                "原名稱": p_name,
            })

    return new_without, new_with, disappeared, renamed


# ════════════════════════════════════════════
#  寫入 Google Sheets
# ════════════════════════════════════════════

HEADERS_SNAPSHOT = ["place_id", "名稱", "地址", "縣市", "緯度", "經度"]
HEADERS_NEW      = ["發現日期", "place_id", "名稱", "地址", "縣市", "健保狀態", "緯度", "經度"]
HEADERS_GONE     = ["發現日期", "place_id", "名稱", "地址", "縣市"]
HEADERS_RENAMED  = ["發現日期", "place_id", "現名稱", "原名稱", "地址", "縣市"]

def write_snapshot(ss, all_data):
    ws = get_or_create_sheet(ss, TODAY, rows=5000)
    ws.append_row(HEADERS_SNAPSHOT)
    ws.append_rows([[p[h] for h in HEADERS_SNAPSHOT] for p in all_data.values()])
    print(f"✅ 快照寫入「{TODAY}」：{len(all_data)} 筆")

def write_new_sheet(ss, new_without, new_with):
    ws = get_or_create_sheet(ss, "🆕 新出現藥局", rows=500)
    ws.append_row(HEADERS_NEW)
    rows = []
    # 未有健保的排在前面（優先級最高）
    for p in new_without + new_with:
        rows.append([TODAY, p["place_id"], p["名稱"],
                     p["地址"], p["縣市"], p["健保狀態"],
                     p.get("緯度", ""), p.get("經度", "")])
    if rows:
        ws.append_rows(rows)

def write_disappeared_sheet(ss, disappeared):
    ws = get_or_create_sheet(ss, "🚪 消失藥局", rows=500)
    ws.append_row(HEADERS_GONE)
    if disappeared:
        ws.append_rows([[TODAY, p["place_id"], p["名稱"],
                         p["地址"], p["縣市"]] for p in disappeared])

def write_renamed_sheet(ss, renamed):
    ws = get_or_create_sheet(ss, "👤 改名藥局", rows=500)
    ws.append_row(HEADERS_RENAMED)
    if renamed:
        ws.append_rows([[TODAY, p["place_id"], p["名稱"],
                         p["原名稱"], p["地址"], p["縣市"]] for p in renamed])


# ════════════════════════════════════════════
#  LINE 通知
# ════════════════════════════════════════════

def send_line(text):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}",
        },
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


# ════════════════════════════════════════════
#  推送新藥局到業務智能規劃系統待辦清單
# ════════════════════════════════════════════

def post_new_pharmacies_to_smart_board(new_without: list, new_with: list) -> tuple[int, int]:
    """將新發現藥局自動寫入 pharmacy-smart-board 的新開藥局待辦池"""
    all_new = new_without + new_with
    if not all_new:
        return 0, 0

    url = f"{SMART_BOARD_URL}/todos-v2"
    success, failed = 0, 0

    for p in all_new:
        payload = {
            "task":           p["名稱"],
            "quadrant":       "pending_newph",
            "pharmacyId":     p.get("place_id", ""),
            "pharmacyName":   p["名稱"],
            "newPhAddress":   p.get("地址", ""),
            "newPhCity":      p.get("縣市", ""),
            "healthInsurance": p.get("健保狀態", ""),
            "source":         "tracker",
            "type":           "other",
            "lat":            p.get("緯度", ""),
            "lng":            p.get("經度", ""),
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                success += 1
            else:
                failed += 1
                print(f"    ⚠️ 寫入失敗：{p['名稱']} (HTTP {resp.status_code})")
        except Exception as e:
            failed += 1
            print(f"    ⚠️ 寫入失敗：{p['名稱']} ({e})")

    print(f"📋 已推送至業務系統：成功 {success} 筆，失敗 {failed} 筆")
    return success, failed

def build_message(new_without, new_with, disappeared, renamed, total):
    lines = [
        "🏥 藥局異動報告",
        f"📅 {TODAY}",
        f"本次掃描 {total} 間",
        "─" * 22,
    ]

    def section(icon, label, items, name_key="名稱", extra_key=None, extra_label=""):
        if not items:
            return
        lines.append(f"\n{icon} {label}：{len(items)} 間")
        for p in items[:5]:
            lines.append(f"  • {p[name_key]}")
            lines.append(f"    📍 {p['地址']}")
            if extra_key and p.get(extra_key):
                lines.append(f"    {extra_label}{p[extra_key]}")
        if len(items) > 5:
            lines.append(f"    ...還有 {len(items)-5} 間，見 Sheets")

    # 未有健保的新藥局優先顯示
    if new_without:
        lines.append(f"\n🆕 全新藥局（尚未健保）：{len(new_without)} 間  ← 最優先！")
        for p in new_without[:5]:
            lines.append(f"  • {p['名稱']}")
            lines.append(f"    📍 {p['地址']}  {p['健保狀態']}")
        if len(new_without) > 5:
            lines.append(f"    ...還有 {len(new_without)-5} 間，見 Sheets")

    if new_with:
        lines.append(f"\n🆕 新出現藥局（已有健保）：{len(new_with)} 間")
        for p in new_with[:3]:
            lines.append(f"  • {p['名稱']}")
            lines.append(f"    📍 {p['地址']}")
        if len(new_with) > 3:
            lines.append(f"    ...還有 {len(new_with)-3} 間，見 Sheets")

    section("🚪", "消失藥局",  disappeared)
    section("👤", "改名藥局",  renamed, extra_key="原名稱", extra_label="原名：")

    if not any([new_without, new_with, disappeared, renamed]):
        lines.append("\n本次無異動紀錄")

    return "\n".join(lines)


# ════════════════════════════════════════════
#  主程式
# ════════════════════════════════════════════

def main():
    print("=" * 50)
    print(f"  藥局異動追蹤  v5  |  {TODAY}")
    print(f"  網格座標點數：{len(LOCATIONS)}")
    print("=" * 50)

    ss = get_spreadsheet()

    # 1. 載入上次快照
    previous = load_previous_snapshot(ss)

    # 2. 載入健保基準（輔助驗證）
    baseline_addrs = load_baseline_addresses(ss)

    # 3. 抓取今日 Google Places 資料
    print(f"\n📡 抓取 Google Places（{len(LOCATIONS)} 個座標點）...")
    try:
        today = fetch_all()
    except PlacesApiPermanentError as e:
        msg = (f"⚠️ 藥局追蹤緊急中止\n📅 {TODAY}\n\n"
               f"{e}\n\n請至 GitHub Secrets 更新 PLACES_API_KEY")
        send_line(msg)
        print(f"❌ {e}")
        return
    print(f"✅ 共抓到 {len(today)} 間（去重複、排除連鎖後）")

    # 4. 存今日快照（筆數異常時不覆蓋，保護對照基準）
    if len(today) < MIN_SNAPSHOT_SIZE:
        msg = (f"⚠️ 藥局追蹤異常\n📅 {TODAY}\n\n"
               f"只抓到 {len(today)} 間（門檻 {MIN_SNAPSHOT_SIZE}）\n"
               f"API 可能有問題，快照未寫入\n請至 GitHub Actions 查看 log")
        send_line(msg)
        print(f"❌ 筆數 {len(today)} < {MIN_SNAPSHOT_SIZE}，中止以保護對照基準")
        return
    write_snapshot(ss, today)

    # 5. 與上次比對
    if not previous:
        print("\n⚠️  無上次快照可比對，下次執行才會有異動報告")
        send_line(f"🏥 藥局追蹤系統\n📅 {TODAY}\n\n首次執行完成！\n共建立 {len(today)} 間藥局基準\n下次執行將開始比對異動")
        return

    print("\n🔍 比對 place_id 異動...")
    new_without, new_with, disappeared, renamed = compare_snapshots(
        today, previous, baseline_addrs
    )

    # 6. 寫入結果分頁
    write_new_sheet(ss, new_without, new_with)
    write_disappeared_sheet(ss, disappeared)
    write_renamed_sheet(ss, renamed)

    print(f"   🆕 新出現（未健保）：{len(new_without)} 間")
    print(f"   🆕 新出現（已健保）：{len(new_with)} 間")
    print(f"   🚪 消失：           {len(disappeared)} 間")
    print(f"   👤 改名：           {len(renamed)} 間")

    # 7. LINE 通知
    msg = build_message(new_without, new_with, disappeared, renamed, len(today))
    send_line(msg)
    print("\n📲 LINE 通知已發送")

    # 8. 推送新藥局到業務智能規劃系統
    if new_without or new_with:
        print("\n📤 推送新藥局至業務系統...")
        post_new_pharmacies_to_smart_board(new_without, new_with)

    # 清理舊快照（只保留最近 2 個）
    cleanup_old_snapshots(ss)
    print("🎉 完成！")

# ════════════════════════════════════════════
#  快照自動清理（只保留最近 2 個）
# ════════════════════════════════════════════

def cleanup_old_snapshots(ss, keep: int = 2):
    """刪除舊的日期快照，只保留最近 keep 個"""
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    date_sheets  = sorted(
        [ws for ws in _worksheets(ss) if date_pattern.match(ws.title)],
        key=lambda ws: ws.title, reverse=True,
    )
    for ws in date_sheets[keep:]:
        print(f"🗑️  刪除舊快照：{ws.title}")
        ss.del_worksheet(ws)


if __name__ == "__main__":
    main()
