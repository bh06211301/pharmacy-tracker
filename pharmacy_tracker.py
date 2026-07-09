"""
pharmacy_tracker.py
===================
藥局異動追蹤系統 — GitHub Actions 版 v3
覆蓋範圍：台北市、新北市、基隆市、桃園市

比對邏輯（全新改版）：
  以 place_id 為唯一識別，比對本次與上次快照
  🆕 新出現：place_id 上次沒有、這次有 → 交叉比對健保名單
  🚪 消失中：place_id 上次有、這次沒有 → 可能關閉
  👤 改名了：place_id 相同但名稱不一樣 → 可能換老闆

健保 CSV 角色（輔助驗證）：
  新出現的藥局若不在健保名單 → 最新、最有價值的開發對象
  新出現的藥局若已在健保名單 → 可能只是剛上 Google Maps

排除品牌：杏一、大樹、丁丁、維康、專品、立赫、光點、
         康是美、屈臣氏、健康人生、富康活力、優嘉、快樂鳥
執行排程：週一、三、五 08:00（GitHub Actions 自動觸發）
"""

import os
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta

import gspread
import requests
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

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

def _is_transient_sheets_error(exc) -> bool:
    return isinstance(exc, gspread.exceptions.APIError) and any(
        code in str(exc) for code in ("[503]", "[500]", "[429]")
    )

_sheets_retry = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=5, max=60),
    retry=retry_if_exception(_is_transient_sheets_error),
    reraise=True,
)


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
#  自動產生六角形網格座標（2.5km 間距）
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
    reraise=True,
)
def _call_places_api(url: str, params: dict) -> dict:
    res = requests.get(url, params=params, timeout=15).json()
    status = res.get("status", "")
    if status not in ("OK", "ZERO_RESULTS"):
        raise Exception(f"Places API status={status}")
    return res

def fetch_pharmacies(city, location, radius):
    url     = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    results = []
    params  = {
        "location": location, "radius": radius,
        "type": "pharmacy", "language": "zh-TW", "key": PLACES_API_KEY,
    }
    while True:
        try:
            res = _call_places_api(url, params)
        except Exception as e:
            print(f"    ⚠️  API 錯誤（已重試3次）：{e}")
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

def fetch_all() -> dict:
    """抓取所有區域藥局，以 place_id 去重，排除連鎖品牌"""
    all_data, excluded, total = {}, 0, len(LOCATIONS)
    for i, (city, location, radius) in enumerate(LOCATIONS, 1):
        if i == 1 or i % 20 == 0:
            print(f"  進度：{i}/{total}")
        for p in fetch_pharmacies(city, location, radius):
            if not p["place_id"]:
                continue
            if is_excluded_chain(p["名稱"]):
                excluded += 1
                continue
            if p["place_id"] not in all_data:
                all_data[p["place_id"]] = p
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

HEADERS_SNAPSHOT = ["place_id", "名稱", "地址", "評分", "評論數", "縣市"]
HEADERS_NEW      = ["發現日期", "place_id", "名稱", "地址", "縣市", "健保狀態"]
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
                     p["地址"], p["縣市"], p["健保狀態"]])
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
    print(f"  藥局異動追蹤  v3  |  {TODAY}")
    print(f"  網格座標點數：{len(LOCATIONS)}")
    print("=" * 50)

    ss = get_spreadsheet()

    # 1. 載入上次快照
    previous = load_previous_snapshot(ss)

    # 2. 載入健保基準（輔助驗證）
    baseline_addrs = load_baseline_addresses(ss)

    # 3. 抓取今日 Google Places 資料
    print(f"\n📡 抓取 Google Places（{len(LOCATIONS)} 個座標點）...")
    today = fetch_all()
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
