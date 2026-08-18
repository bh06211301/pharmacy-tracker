"""
pharmacy_tracker.py
===================
藥局異動追蹤系統 — GitHub Actions 版 v8
覆蓋範圍：台北市、新北市、基隆市、桃園市（47 個行政區，密度加權座標點）

比對邏輯：
  以 place_id 為唯一識別，比對本次與累積總表「📇 藥局總表」（v7 起不再
  只比對上一次快照，見下方 v7 變更）
  🆕 新出現：place_id 有史以來第一次出現
  🚪 消失中：place_id 連續 DISAPPEAR_THRESHOLD 次沒出現 → 可能關閉
  👤 改名了：place_id 相同但名稱不一樣 → 可能換老闆

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

v6 變更（實測發現漏抓問題後的修正）：
  - 實測發現：行政區「平均密度」低估了商圈熱點的真實密度，導致部分
    密集角落單點實際藥局數超過 20 筆被新版 API 悄悄截斷，不少真實
    存在的藥局被誤判成「消失」
  - 新增「熱點自動偵測 + 校正」機制，取代單純調低目標筆數（調低目標
    筆數雖然能降低超量機率，但需要更多座標點、平常就要多付新版API
    的錢；改成下面這套機制可以維持原本 325 點的低成本，只在真的需要
    時才多花一點點錢）：
      1. 每點正常用新版 API（便宜）查詢
      2. 若剛好回傳 20 筆（頂到上限，疑似超量）→ 當場改用**舊版**
         Nearby Search API 翻頁補查（最多 3 頁 60 筆），拿到完整清單
      3. 用這個點「實際觀測到的密度」（不是行政區平均密度）反推一個
         更小、更精準的校正半徑，寫入 Google Sheet 的「⚙️ 熱點校正
         清單」分頁**永久保存**
      4. 下次執行時，這個座標點會直接套用校正後的半徑，正常情況下
         就不會再超量、也不需要再查舊版 API 了——只有第一次發現、
         校正的那一次需要多付舊版 API 的錢，之後就回到用便宜的新版
      5. 每次執行都會照樣檢查所有點（含已校正過的），如果密度持續
         成長導致校正後的半徑未來又不夠用，會自動再校正一次
  - 新增店名正面過濾：名稱必須含有「藥局」「藥房」「藥行」其中之一，
    濾掉 Google 誤標成 pharmacy 類型的其他商家（例如路名、地標）以及
    藥品批發/製造商（例如「XX藥業股份有限公司」，這種是公司行號不是
    門市藥局，Google 一樣會因為它賣藥而標成 pharmacy 類型）
  - 需要在 Google Cloud Console 額外啟用「Places API」（舊版，跟
    「Places API (New)」是兩個要分別啟用的項目），同一組 PLACES_API_KEY
    才能同時呼叫兩個版本的 API

v7 變更（修正「新出現/消失」清單失真問題）：
  - 實測發現：只跟「上一次」快照比對，會把「這次剛好被網格/超量校正
    涵蓋到、但其實早就存在」的舊藥局，誤判成「新開幕」——3天內曾經
    一次冒出515間假新出現，幾乎等於總筆數的增量，不是真的新開幕潮
  - 改用永久累積總表「📇 藥局總表」取代單純比對上一次快照：
      「新出現」= 有史以來第一次出現在任何快照，不會因為涵蓋範圍
      今天多涵蓋、明天又縮回去而重複誤判成新開幕
  - 「消失」加上防抖動：連續 DISAPPEAR_THRESHOLD（預設2）次都沒看到
    才回報一次，避免同樣的涵蓋範圍抖動造成忽有忽無的誤判
  - 移除健保 CSV 交叉比對：這層比對原本只影響「新出現」清單裡的排序
    /分類顯示，不影響是否推送到業務系統（不管有沒有健保，新出現的一
    律推送），但地址字串比對是造成上面誤判的原因之一（英文地址比對
    必然失敗），實際帶來的價值（排序提示）配不上它的維護成本與誤導
    風險，評估後直接移除。`load_baseline_addresses`／`normalize_addr`／
    `is_in_health_insurance` 三個函式與「健保基準資料」分頁的讀取都已
    拿掉；Google Sheet 上的「健保基準資料」分頁本身沒有動，只是程式
    不再讀取，之後若要恢復可以參考 git 歷史

v8 變更（修正熱點校正永遠無法收斂、每次執行都重打舊版API的問題）：
  - 實測發現（2026-08-13 執行 log）：168 個已校正熱點裡，113 個
    （67%）每次執行都重新判定「疑似超量」、重打一次舊版API——檢查
    校正紀錄發現這些點幾乎全部「校正半徑」都卡在 MIN_RADIUS_M（800m）
    這個下限，代表就算縮到系統允許的最小半徑，範圍內實際藥局數還是
    超過 20 筆（實測看到 21～60 筆不等），校正機制沒辦法再往下縮，
    導致這些點的「疑似超量」判定永遠不會消失，形成無限迴圈式的重複
    計費——這就是 8/12 那筆 $30.82 帳單的主因
  - 修正：新增 `_split_hot_point()`，對「連 MIN_RADIUS_M 都塞不下
    TARGET_RESULTS_PER_POINT」的熱點，不再單純把半徑夾在 800m，改成
    沿用既有的 `_generate_local_grid()` 鋪點邏輯，在原本這個點涵蓋的
    範圍內鋪一個更細的小網格，每個子點半徑縮到這個點實際密度反推出
    的理想值（可以小於 800m），子點數量依實際筆數抓（最多
    MAX_SPLIT_POINTS=4 個，避免點數失控吃掉新版API的免費額度）
  - 熱點校正清單改成「原始座標→多個子點座標＋半徑」的一對多結構
    （原本是一對一），`load_calibrations`/`save_calibrations` 對應
    改寫；儲存時機也從「每個熱點各自即時寫入」改成「整次執行結束後
    一次寫回」，避免一次執行上百個熱點時打爆 Sheets API 呼叫次數
  - 已知取捨：拆點後座標點數會從 325 增加（估計到 ~550-650 之間，
    取決於每次實際超量的熱點數與密度），仍在新版 API 5,000次/月免費
    額度內，但margin不像原本325點時那麼寬，之後如果密度持續成長、
    超量熱點變多，需要重新檢視是否要降低排程頻率或調整
    TARGET_RESULTS_PER_POINT
"""

import math
import os
import re
import time
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

MIN_SNAPSHOT_SIZE = 100   # 抓到筆數低於此值 → 視為異常，不覆蓋快照
DISAPPEAR_THRESHOLD = 2  # 消失防抖動：連續幾次沒看到才回報「消失」，避免網格覆蓋範圍變動造成誤判


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


# 店名必須含有以下其中之一才算數，濾掉 Google 誤標成 pharmacy 類型的
# 其他商家（路名/地標等）以及藥品批發、製造商（例如「XX藥業股份有限
# 公司」，是公司行號不是門市藥局，但 Google 一樣會因為賣藥而標成
# pharmacy 類型）
PHARMACY_NAME_INDICATORS = ["藥局", "藥房", "藥行"]

def is_valid_pharmacy_name(name: str) -> bool:
    return any(ind in name for ind in PHARMACY_NAME_INDICATORS)


# ════════════════════════════════════════════
#  依行政區密度反推搜尋半徑，產生座標點
# ════════════════════════════════════════════

# 每點目標回傳筆數（新版 API 上限 20 筆，抓七八成留緩衝，避免未來
# 該區藥局數成長後悄悄被截斷漏抓）
TARGET_RESULTS_PER_POINT = 15
MIN_RADIUS_M, MAX_RADIUS_M = 800, 4000

# 熱點校正時，如果連 MIN_RADIUS_M 都塞不下 TARGET_RESULTS_PER_POINT，代表
# 這個角落真實密度太高，改成拆成多個小點涵蓋同一塊區域（見 v8 變更）
SUB_POINT_MIN_RADIUS_M = 200   # 子點半徑的絕對下限，避免密度極端值算出過小的半徑
MAX_SPLIT_POINTS = 4           # 單一熱點最多拆成幾個子點，避免點數失控增加免費額度壓力

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


def build_locations(calib_records: dict = None):
    """
    產生座標點清單，每筆為 (city, location, radius, parent_key)。

    calib_records 是 {原始座標: {"city":..., "sub_points": [(子點座標, 半徑), ...],
    "first_found":...}}，來自 Google Sheet 的「熱點校正清單」——曾經偵測到超量、
    校正過的點，這裡會直接套用校正結果，取代原本依行政區平均密度算出來的
    預設值。一般情況 sub_points 只有一筆（子點座標＝原始座標，只是半徑變小）；
    如果連 MIN_RADIUS_M 都塞不下目標筆數（見 v8 變更），sub_points 會是拆分後
    的多個小點，取代原本這一個點。

    parent_key 用於：這個點如果又疑似超量，要往哪一筆熱點校正紀錄回寫
    （一般情況 parent_key == location；拆點後的子點，parent_key 會是原本
    那個超密集熱點的座標）。
    """
    calib_records = calib_records or {}
    seen, locs = set(), []
    for city, _dist, clat, clon, area_km2, count in DISTRICTS:
        density = count / area_km2 if area_km2 > 0 else 0.0001
        radius_m = _district_radius_m(density)
        spacing_km = (radius_m / 1000) * 1.6
        side_km = math.sqrt(area_km2)
        for lat, lon in _generate_local_grid(clat, clon, side_km, spacing_km):
            key = f"{lat:.4f},{lon:.4f}"
            if key in seen:
                continue
            seen.add(key)
            rec = calib_records.get(key)
            if rec:
                for sub_coord, sub_radius in rec["sub_points"]:
                    locs.append((city, sub_coord, sub_radius, key))
            else:
                locs.append((city, key, round(radius_m), key))
    return locs


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

@_sheets_retry
def _get_or_create_persistent_sheet(ss, title, headers, rows=1000, cols=10):
    """跟 get_or_create_sheet 不同：不會清空既有內容，只有完全沒有這個分頁
    時才建立並寫入表頭。用於需要跨執行持續累積的資料（例如熱點校正清單）"""
    try:
        return ss.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=rows, cols=cols)
        ws.append_row(headers)
        return ws


# ════════════════════════════════════════════
#  熱點校正清單（跨執行持久保存，偵測到超量的座標點會校正半徑存在這裡）
# ════════════════════════════════════════════

CALIBRATION_SHEET_NAME = "⚙️ 熱點校正清單"
CALIBRATION_HEADERS = ["原始座標", "子點座標", "縣市", "半徑", "首次發現日期", "最近校正日期", "校正時實際筆數"]

# v7 以前的舊版熱點校正清單格式（一個座標對一個半徑，沒有拆點的概念）。
# v8 把結構改成一對多後，欄位順序整個變了，如果不特別處理，既有的 168 筆
# 校正紀錄會被新的解析邏輯直接判讀失敗、當成不存在——下次執行會把所有已知
# 熱點當成全新的重新查一次舊版API，反而製造一次額外的費用高峰。所以
# load_calibrations 會自動偵測舊格式並轉換，不會遺失既有校正進度。
OLD_CALIBRATION_HEADERS = ["座標", "縣市", "校正後半徑", "首次發現日期", "最近校正日期", "校正時實際筆數"]

def load_calibrations(ss) -> dict:
    """
    讀取已校正過的熱點清單。回傳：
      {原始座標: {"city":..., "sub_points": [(子點座標, 半徑), ...], "first_found":...}}
    一般情況（沒有拆點）子點座標會等於原始座標，sub_points 只有一筆；
    v8 起若熱點連 MIN_RADIUS_M 都塞不下目標筆數，會拆成多個子點（見
    _split_hot_point），sub_points 會有多筆。
    """
    ws = _get_or_create_persistent_sheet(ss, CALIBRATION_SHEET_NAME, CALIBRATION_HEADERS)
    data = _get_all_values(ws)
    records = {}

    if data and data[0][:1] == ["座標"]:
        print("⚙️  偵測到 v7 以前的舊版熱點校正清單格式，自動轉換（不會遺失既有校正進度）")
        for row in data[1:]:
            if len(row) < 3 or not row[0] or not row[2]:
                continue
            try:
                radius = int(float(row[2]))
            except ValueError:
                continue
            records[row[0]] = {
                "city": row[1] if len(row) > 1 else "",
                "sub_points": [(row[0], radius)],
                "first_found": row[3] if len(row) > 3 and row[3] else TODAY,
            }
    else:
        for row in data[1:]:
            if len(row) < 5 or not row[0] or not row[1] or not row[3]:
                continue
            try:
                radius = int(float(row[3]))
            except ValueError:
                continue
            rec = records.setdefault(row[0], {"city": row[2], "sub_points": [], "first_found": row[4] or TODAY})
            rec["sub_points"].append((row[1], radius))

    total_sub = sum(len(r["sub_points"]) for r in records.values())
    print(f"⚙️  載入 {len(records)} 個已校正的熱點（共 {total_sub} 個實際子點）")
    return records

@_sheets_retry
def save_calibrations(ss, records: dict):
    """
    把整份熱點校正清單（記憶體中已更新好的版本）覆寫回 Google Sheet。
    改成整批一次寫入（而不是每個熱點各自呼叫一次API）：一次執行可能有
    上百個熱點需要更新（實測看過 113 個），逐筆寫入會打爆 Sheets API
    呼叫次數，改成 fetch_all() 只在記憶體裡更新這份 dict，執行結束後
    一次寫回。
    """
    ws = _get_or_create_persistent_sheet(ss, CALIBRATION_SHEET_NAME, CALIBRATION_HEADERS)
    ws.clear()
    ws.append_row(CALIBRATION_HEADERS)
    rows = [
        [parent, sub_coord, rec["city"], radius, rec["first_found"], TODAY, rec.get("real_count", "")]
        for parent, rec in records.items()
        for sub_coord, radius in rec["sub_points"]
    ]
    if rows:
        ws.append_rows(rows)
    print(f"⚙️  熱點校正清單已更新：{len(records)} 個熱點、{len(rows)} 個子點")


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
#  藥局總表（跨執行永久累積，取代單純比對上一次快照）
# ════════════════════════════════════════════
#
#  取代「只跟上一次快照比對」的原因：座標網格的涵蓋範圍會因為熱點校正、
#  超量補查等機制而每次執行都有些微不同，同一間早就存在的藥局可能「這次
#  剛好被涵蓋到、上次沒被涵蓋到」，若只跟上一次比對，會被誤判成「新開幕」；
#  改成跟「有史以來看過的全部藥局」比對，只有真正第一次出現才算新出現。
#  「消失」則加上 DISAPPEAR_THRESHOLD 次防抖動，同一種涵蓋範圍抖動不會
#  造成忽有忽無的誤判。

REGISTRY_SHEET_NAME = "📇 藥局總表"
REGISTRY_HEADERS = ["place_id", "名稱", "地址", "縣市", "緯度", "經度",
                     "首次發現日期", "最近出現日期", "未出現次數", "狀態"]

def load_registry(ss):
    """
    讀取累積總表，回傳 (registry, is_first_ever)。
    registry: {place_id: {名稱,地址,縣市,緯度,經度,首次發現日期,最近出現日期,未出現次數,狀態}}
    is_first_ever：True 代表系統從來沒有任何資料（總表是空的、也找不到既有
    快照可以 bootstrap）——這時不適合跑異動比對，只能先建立基準。
    """
    ws = _get_or_create_persistent_sheet(ss, REGISTRY_SHEET_NAME, REGISTRY_HEADERS, rows=6000, cols=10)
    data = _get_all_values(ws)
    registry = {}
    for row in data[1:]:
        if len(row) < 10 or not row[0]:
            continue
        try:
            miss_count = int(row[8])
        except ValueError:
            miss_count = 0
        registry[row[0]] = {
            "名稱": row[1], "地址": row[2], "縣市": row[3],
            "緯度": row[4], "經度": row[5],
            "首次發現日期": row[6], "最近出現日期": row[7],
            "未出現次數": miss_count,
            "狀態": row[9] or "現存",
        }

    is_first_ever = False
    if not registry:
        # 總表剛建立、是空的：優先從既有的最新快照 bootstrap，避免遷移當天
        # 把所有藥局都當成「新出現」誤發一輪警報
        prev = load_previous_snapshot(ss)
        if prev:
            for pid, p in prev.items():
                registry[pid] = {
                    "名稱": p["名稱"], "地址": p["地址"], "縣市": p["縣市"],
                    "緯度": "", "經度": "",
                    "首次發現日期": "遷移前", "最近出現日期": "遷移前",
                    "未出現次數": 0, "狀態": "現存",
                }
            print(f"🗂  首次啟用藥局總表，從既有快照 bootstrap {len(registry)} 筆為已知藥局")
        else:
            is_first_ever = True
            print("🗂  藥局總表是空的，且找不到既有快照，視為系統第一次執行")

    print(f"🗂  藥局總表：{len(registry)} 筆在案")
    return registry, is_first_ever

@_sheets_retry
def save_registry(ss, registry: dict):
    """把整份累積總表（記憶體中已更新好的版本）覆寫回 Google Sheet"""
    ws = _get_or_create_persistent_sheet(ss, REGISTRY_SHEET_NAME, REGISTRY_HEADERS, rows=6000, cols=10)
    ws.clear()
    ws.append_row(REGISTRY_HEADERS)
    rows = [
        [pid, r["名稱"], r["地址"], r["縣市"], r["緯度"], r["經度"],
         r["首次發現日期"], r["最近出現日期"], r["未出現次數"], r["狀態"]]
        for pid, r in registry.items()
    ]
    if rows:
        ws.append_rows(rows)
    print(f"🗂  藥局總表已更新：{len(registry)} 筆")


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


# ════════════════════════════════════════════
#  舊版 Places API 補查（只在新版剛好回傳20筆/疑似超量時才呼叫）
# ════════════════════════════════════════════

def _call_legacy_places_api(params: dict) -> dict:
    """
    舊版 Nearby Search，支援分頁最多抓 60 筆。只在新版 API 疑似超量時
    當備援用，失敗時直接回空清單、不中止整個流程（這只是補查，不是
    主要資料來源，失敗了就沿用新版的20筆結果，不影響其他座標點）。
    """
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params=params, timeout=15,
        )
        data = resp.json()
    except Exception as e:
        print(f"    ⚠️  舊版API請求失敗：{e}")
        return {"results": []}
    status = data.get("status", "")
    if status not in ("OK", "ZERO_RESULTS"):
        print(f"    ⚠️  舊版API錯誤：{status}（可能尚未在 Console 啟用「Places API」舊版）")
        return {"results": []}
    return data

def fetch_legacy_overflow(city: str, location: str, radius) -> list:
    """疑似超量點的補查：翻頁抓取，最多3頁(60筆)，拿到完整清單"""
    results = []
    params = {
        "location": location, "radius": radius,
        "type": "pharmacy", "language": "zh-TW", "key": PLACES_API_KEY,
    }
    for _ in range(3):
        data = _call_legacy_places_api(params)
        for p in data.get("results", []):
            loc = p.get("geometry", {}).get("location", {})
            results.append({
                "place_id": p.get("place_id", ""),
                "名稱":     p.get("name", ""),
                "地址":     p.get("vicinity", ""),
                "縣市":     city,
                "緯度":     str(loc.get("lat", "")),
                "經度":     str(loc.get("lng", "")),
            })
        token = data.get("next_page_token")
        if not token:
            break
        time.sleep(2)  # next_page_token 需等待生效
        params = {"pagetoken": token, "key": PLACES_API_KEY}
    return results

def _ideal_radius_m(current_radius_m: float, real_count: int) -> float:
    """
    依這個點實際觀測到的密度（不是行政區平均密度），反推「理想」半徑——
    不套用 MIN_RADIUS_M 下限，單純用來判斷連最小半徑都塞不下目標筆數、
    需要拆點（見 _split_hot_point），而不是單純縮半徑。
    """
    if real_count <= TARGET_RESULTS_PER_POINT:
        return current_radius_m  # 沒有真的超量，維持原半徑
    real_density = real_count / (math.pi * (current_radius_m / 1000) ** 2)  # 家/km²
    return math.sqrt(TARGET_RESULTS_PER_POINT / (real_density * math.pi)) * 1000

def _calibrate_radius(current_radius_m: float, real_count: int) -> float:
    """
    依實際密度反推更小、更精準的校正半徑（套用 MIN_RADIUS_M 下限）。
    用於「本身已經是子點」或「還沒密到需要拆點」的一般校正情況；真正連
    MIN_RADIUS_M 都塞不下目標筆數的超密集點改走 _split_hot_point。
    """
    ideal = _ideal_radius_m(current_radius_m, real_count)
    return max(MIN_RADIUS_M, min(current_radius_m, ideal))

def _split_hot_point(lat: float, lon: float, current_radius_m: float, real_count: int) -> list:
    """
    連 MIN_RADIUS_M 都塞不下 TARGET_RESULTS_PER_POINT 的超密集點，代表這個
    角落真實密度太高，單一點不管怎麼把半徑夾在 MIN_RADIUS_M，範圍內藥局
    數還是超過 20 筆，每次執行都會被新版 API 悄悄截斷、判定疑似超量、
    重打一次舊版 API，永遠不會收斂。

    改成沿用既有的 _generate_local_grid() 鋪點邏輯，在原本這個點涵蓋的
    範圍內鋪一個更細的小網格，每個子點半徑縮到這個點實際密度反推出的
    理想值（可以小於 MIN_RADIUS_M），子點數量依實際筆數抓、上限
    MAX_SPLIT_POINTS 個，避免點數失控吃掉新版 API 的免費額度。

    回傳 [(子點座標, 子點半徑), ...]，取代原本這一個點。

    子點數量直接鎖在 n_target（最多 MAX_SPLIT_POINTS 個）：_generate_local_grid
    是兩端都含頭尾的網格，實測發現用 spacing 反推張目標點數會系統性地多出
    快一倍（例如目標4個點，實際鋪出8個），如果不修正，113個超密集熱點全部
    套用下去，新版API每月呼叫量會從免費額度內暴衝到快兩倍——所以改成鋪一個
    偏密的候選網格，再直接取離中心最近的 n_target 個，把子點數量的上限
    鎖死，不透過 spacing 間接控制。
    """
    ideal_radius_m = max(SUB_POINT_MIN_RADIUS_M, round(_ideal_radius_m(current_radius_m, real_count)))
    n_target = min(MAX_SPLIT_POINTS, max(2, math.ceil(real_count / TARGET_RESULTS_PER_POINT)))
    side_km = 2 * current_radius_m / 1000
    spacing_km = side_km / math.sqrt(n_target)
    candidates = _generate_local_grid(lat, lon, side_km, spacing_km)
    if len(candidates) > n_target:
        candidates.sort(key=lambda p: (p[0] - lat) ** 2 + (p[1] - lon) ** 2)
        candidates = candidates[:n_target]
    return [(f"{sub_lat:.4f},{sub_lon:.4f}", ideal_radius_m) for sub_lat, sub_lon in candidates]


def fetch_all(ss, locations, calib_records: dict) -> dict:
    """
    抓取所有區域藥局，以 place_id 去重，排除連鎖品牌與非藥局名稱。
    剛好回傳20筆(疑似超量)的點，會用舊版API補查完整清單，並依實際密度
    重新校正——如果連 MIN_RADIUS_M 都塞不下目標筆數，改拆成多個小點
    （見 _split_hot_point），否則單純縮小半徑（見 _calibrate_radius）。

    calib_records 是 load_calibrations() 讀到的既有校正紀錄，這裡會直接
    就地更新（新增/覆蓋這次疑似超量的熱點），呼叫端負責在抓取完後把整份
    calib_records 存回 Google Sheet（見 save_calibrations，改成整批寫入
    而不是每個熱點各自呼叫一次 API）。
    """
    all_data, excluded, invalid_name, overflow_count, total = {}, 0, 0, 0, len(locations)
    for i, (city, location, radius, parent_key) in enumerate(locations, 1):
        if i == 1 or i % 20 == 0:
            print(f"  進度：{i}/{total}")
        try:
            pharmacies = fetch_pharmacies(city, location, radius)

            if len(pharmacies) == 20:
                overflow_count += 1
                print(f"    ⚠️  疑似超量：{location}（{city}，半徑{radius}m）→ 用舊版API補查")
                legacy_pharmacies = fetch_legacy_overflow(city, location, radius)
                if len(legacy_pharmacies) > len(pharmacies):
                    real_count = len(legacy_pharmacies)
                    is_subpoint = (location != parent_key)

                    if not is_subpoint and _ideal_radius_m(radius, real_count) < MIN_RADIUS_M:
                        lat, lon = (float(v) for v in location.split(","))
                        sub_points = _split_hot_point(lat, lon, radius, real_count)
                        print(f"       實際{real_count}筆，最小半徑仍塞不下 → 拆成 {len(sub_points)} 個子點（已存檔，下次直接套用）")
                    else:
                        new_radius = _calibrate_radius(radius, real_count)
                        sub_points = [(location, round(new_radius))]
                        print(f"       實際{real_count}筆 → 校正半徑為{round(new_radius)}m（已存檔，下次直接套用）")

                    existing = calib_records.get(parent_key)
                    calib_records[parent_key] = {
                        "city": city, "sub_points": sub_points,
                        "first_found": existing["first_found"] if existing else TODAY,
                        "real_count": real_count,
                    }
                    pharmacies = legacy_pharmacies

            for p in pharmacies:
                if not p["place_id"]:
                    continue
                if not is_valid_pharmacy_name(p["名稱"]):
                    invalid_name += 1
                    continue
                if is_excluded_chain(p["名稱"]):
                    excluded += 1
                    continue
                if p["place_id"] not in all_data:
                    all_data[p["place_id"]] = p
        except PlacesApiPermanentError:
            raise  # 立即中止整個搜尋
        time.sleep(0.5)
    print(f"  已排除非藥局名稱：{invalid_name} 筆（重複計算）")
    print(f"  已排除連鎖品牌：{excluded} 筆（重複計算）")
    if overflow_count:
        print(f"  ⚠️  本次共 {overflow_count} 個疑似超量點，已用舊版API補查並校正半徑")
    return all_data


# ════════════════════════════════════════════
#  核心比對：place_id 快照比對
# ════════════════════════════════════════════

def compare_and_update_registry(today: dict, registry: dict,
                                 disappear_threshold: int = DISAPPEAR_THRESHOLD):
    """
    以累積總表 registry 比對今日資料（取代單純比對上一次快照），分三類：
      new          🆕 有史以來第一次出現
      disappeared  🚪 連續 disappear_threshold 次沒出現（可能關閉）
      renamed      👤 place_id 相同、名稱不同（可能換老闆）

    這個函式會直接就地更新 registry（新增新藥局、刷新已知藥局的最新資訊、
    累加缺席次數），呼叫端負責在比對完後把 registry 存回 Google Sheet。
    """
    today_ids = set(today.keys())

    new = []
    renamed = []

    for pid, p in today.items():
        r = registry.get(pid)
        if r is None:
            # 有史以來第一次看到，才算真正的新出現
            new.append(p)
            registry[pid] = {
                "名稱": p["名稱"], "地址": p["地址"], "縣市": p["縣市"],
                "緯度": p.get("緯度", ""), "經度": p.get("經度", ""),
                "首次發現日期": TODAY, "最近出現日期": TODAY,
                "未出現次數": 0, "狀態": "現存",
            }
        else:
            if p["名稱"] != r["名稱"]:
                renamed.append({**p, "原名稱": r["名稱"]})
            r.update({
                "名稱": p["名稱"], "地址": p["地址"], "縣市": p["縣市"],
                "緯度": p.get("緯度", ""), "經度": p.get("經度", ""),
                "最近出現日期": TODAY, "未出現次數": 0, "狀態": "現存",
            })

    # 這次沒出現的：累加缺席次數，連續達到門檻才回報一次消失
    disappeared = []
    for pid, r in registry.items():
        if pid in today_ids:
            continue
        r["未出現次數"] = r.get("未出現次數", 0) + 1
        if r["未出現次數"] >= disappear_threshold:
            if r["狀態"] != "已回報消失":
                disappeared.append({**r, "place_id": pid})
                r["狀態"] = "已回報消失"
            # 已經回報過的維持「已回報消失」，不重複回報
        else:
            r["狀態"] = "疑似消失"

    return new, disappeared, renamed


# ════════════════════════════════════════════
#  寫入 Google Sheets
# ════════════════════════════════════════════

HEADERS_SNAPSHOT = ["place_id", "名稱", "地址", "縣市", "緯度", "經度"]
HEADERS_NEW      = ["發現日期", "place_id", "名稱", "地址", "縣市", "緯度", "經度"]
HEADERS_GONE     = ["發現日期", "place_id", "名稱", "地址", "縣市"]
HEADERS_RENAMED  = ["發現日期", "place_id", "現名稱", "原名稱", "地址", "縣市"]

def write_snapshot(ss, all_data):
    ws = get_or_create_sheet(ss, TODAY, rows=5000)
    ws.append_row(HEADERS_SNAPSHOT)
    ws.append_rows([[p[h] for h in HEADERS_SNAPSHOT] for p in all_data.values()])
    print(f"✅ 快照寫入「{TODAY}」：{len(all_data)} 筆")

def write_new_sheet(ss, new):
    ws = get_or_create_sheet(ss, "🆕 新出現藥局", rows=500)
    ws.append_row(HEADERS_NEW)
    rows = [[TODAY, p["place_id"], p["名稱"], p["地址"], p["縣市"],
             p.get("緯度", ""), p.get("經度", "")] for p in new]
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

def post_new_pharmacies_to_smart_board(new: list) -> tuple[int, int]:
    """將新發現藥局自動寫入 pharmacy-smart-board 的新開藥局待辦池"""
    if not new:
        return 0, 0

    url = f"{SMART_BOARD_URL}/todos-v2"
    success, failed = 0, 0

    for p in new:
        payload = {
            "task":           p["名稱"],
            "quadrant":       "pending_newph",
            "pharmacyId":     p.get("place_id", ""),
            "pharmacyName":   p["名稱"],
            "newPhAddress":   p.get("地址", ""),
            "newPhCity":      p.get("縣市", ""),
            "healthInsurance": "",
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

def build_message(new, disappeared, renamed, total):
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

    section("🆕", "新出現藥局", new)
    section("🚪", f"消失藥局（連續{DISAPPEAR_THRESHOLD}次未見）", disappeared)
    section("👤", "改名藥局",  renamed, extra_key="原名稱", extra_label="原名：")

    if not any([new, disappeared, renamed]):
        lines.append("\n本次無異動紀錄")

    return "\n".join(lines)


# ════════════════════════════════════════════
#  主程式
# ════════════════════════════════════════════

def main():
    print("=" * 50)
    print(f"  藥局異動追蹤  v8  |  {TODAY}")
    print("=" * 50)

    ss = get_spreadsheet()

    # 0. 載入已校正的熱點座標，套用到座標點產生邏輯
    calibrations = load_calibrations(ss)
    locations = build_locations(calibrations)
    print(f"  網格座標點數：{len(locations)}")

    # 1. 載入累積藥局總表（取代單純比對上一次快照）
    registry, is_first_ever = load_registry(ss)

    # 2. 抓取今日 Google Places 資料
    #    fetch_all() 會就地更新 calibrations（新增/覆蓋這次疑似超量的熱點），
    #    不管成功與否都要把 calibrations 存回去，避免這次抓到的校正進度遺失
    print(f"\n📡 抓取 Google Places（{len(locations)} 個座標點）...")
    try:
        today = fetch_all(ss, locations, calibrations)
    except PlacesApiPermanentError as e:
        save_calibrations(ss, calibrations)
        msg = (f"⚠️ 藥局追蹤緊急中止\n📅 {TODAY}\n\n"
               f"{e}\n\n請至 GitHub Secrets 更新 PLACES_API_KEY")
        send_line(msg)
        print(f"❌ {e}")
        return
    print(f"✅ 共抓到 {len(today)} 間（去重複、排除連鎖後）")
    save_calibrations(ss, calibrations)

    # 3. 存今日快照（筆數異常時不覆蓋，保護對照基準）
    if len(today) < MIN_SNAPSHOT_SIZE:
        msg = (f"⚠️ 藥局追蹤異常\n📅 {TODAY}\n\n"
               f"只抓到 {len(today)} 間（門檻 {MIN_SNAPSHOT_SIZE}）\n"
               f"API 可能有問題，快照未寫入\n請至 GitHub Actions 查看 log")
        send_line(msg)
        print(f"❌ 筆數 {len(today)} < {MIN_SNAPSHOT_SIZE}，中止以保護對照基準")
        return
    write_snapshot(ss, today)

    # 4. 與累積總表比對
    if is_first_ever:
        for pid, p in today.items():
            registry[pid] = {
                "名稱": p["名稱"], "地址": p["地址"], "縣市": p["縣市"],
                "緯度": p.get("緯度", ""), "經度": p.get("經度", ""),
                "首次發現日期": TODAY, "最近出現日期": TODAY,
                "未出現次數": 0, "狀態": "現存",
            }
        save_registry(ss, registry)
        print("\n⚠️  無既有資料可比對，本次只建立基準，下次執行才會有異動報告")
        send_line(f"🏥 藥局追蹤系統\n📅 {TODAY}\n\n首次執行完成！\n共建立 {len(today)} 間藥局基準\n下次執行將開始比對異動")
        cleanup_old_snapshots(ss)
        return

    print("\n🔍 比對 place_id 異動（累積總表 + 消失防抖動）...")
    new, disappeared, renamed = compare_and_update_registry(today, registry)
    save_registry(ss, registry)

    # 5. 寫入結果分頁
    write_new_sheet(ss, new)
    write_disappeared_sheet(ss, disappeared)
    write_renamed_sheet(ss, renamed)

    print(f"   🆕 新出現：           {len(new)} 間")
    print(f"   🚪 消失（連續{DISAPPEAR_THRESHOLD}次未見）：{len(disappeared)} 間")
    print(f"   👤 改名：           {len(renamed)} 間")

    # 6. LINE 通知
    msg = build_message(new, disappeared, renamed, len(today))
    send_line(msg)
    print("\n📲 LINE 通知已發送")

    # 7. 推送新藥局到業務智能規劃系統
    if new:
        print("\n📤 推送新藥局至業務系統...")
        post_new_pharmacies_to_smart_board(new)

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
