"""
pharmacy_analyzer.py
====================
藥局輿情追蹤系統 v6（精簡版）
移除：健保名單比對、留言時間戳追蹤
保留：評論數變化偵測、五層訊號分析、本週摘要產生
排程：週一、三、五 09:00（tracker 跑完後一小時）
"""

import os
import re
import time
from datetime import datetime, timezone, timedelta

import gspread
import requests
from google.oauth2.service_account import Credentials

# ════════════════════════════════════════════
#  設定
# ════════════════════════════════════════════
PLACES_API_KEY   = os.environ["PLACES_API_KEY"]
SPREADSHEET_ID   = os.environ["SPREADSHEET_ID"]
LINE_TOKEN       = os.environ["LINE_TOKEN"]
LINE_USER_ID     = os.environ["LINE_USER_ID"]
CREDENTIALS_FILE = "credentials.json"

TODAY     = datetime.today().strftime("%Y-%m-%d")
TAIWAN_TZ = timezone(timedelta(hours=8))

MIN_REVIEW_CHANGE  = 3   # 評論數增加幾則以上才納入分析
NEW_SHOP_THRESHOLD = 10  # 總評論數 ≤ 此值 → 全新開幕/高潛力頂讓店
BURST_ABS          = 5   # 短期爆發：絕對增加 ≥ 此值
BURST_RATE         = 20  # 短期爆發：成長率 ≥ 此值（%）
NEW_OPEN_DAYS      = 30  # 最早留言距今 ≤ 此天數 → 本月新開幕
RECENT_OPEN_DAYS   = 90  # 最早留言距今 ≤ 此天數 → 近期開業


# ════════════════════════════════════════════
#  時間工具
# ════════════════════════════════════════════

def unix_to_taiwan(unix_ts) -> str:
    try:
        dt = datetime.fromtimestamp(int(unix_ts), tz=TAIWAN_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(unix_ts)

def days_since(unix_ts) -> int:
    try:
        dt  = datetime.fromtimestamp(int(unix_ts), tz=TAIWAN_TZ)
        now = datetime.now(tz=TAIWAN_TZ)
        return (now - dt).days
    except (ValueError, TypeError):
        return 9999


# ════════════════════════════════════════════
#  關鍵字定義
# ════════════════════════════════════════════
KEYWORDS = {
    "人員異動": {
        "words": ["換藥師", "新藥師", "換老闆", "新老闆", "換人", "接手", "新負責"],
        "priority": "高", "label": "👤 人員異動",
    },
    "藥師好評": {
        "words": ["藥師很專業", "藥師親切", "藥師好", "藥師耐心", "藥師認真", "藥師用心", "藥師推薦"],
        "priority": "高", "label": "⭐ 藥師好評",
    },
    "新開幕": {
        "words": ["新開幕", "剛開", "試營運", "開幕", "新店", "grand opening"],
        "priority": "高", "label": "🎉 新開幕",
    },
    "缺貨需求": {
        "words": ["缺貨", "沒有", "找不到", "缺少", "沒貨", "買不到"],
        "priority": "中", "label": "📦 缺貨需求",
    },
    "促銷活動": {
        "words": ["優惠", "活動", "折扣", "促銷", "特價", "免費", "便宜"],
        "priority": "中", "label": "🏷️ 促銷活動",
    },
    "負評警示": {
        "words": ["態度差", "很差", "失望", "不推薦", "爛", "騙", "差評"],
        "priority": "低", "label": "⚠️ 負評警示",
    },
}

def scan_keywords(text: str) -> tuple[list[str], str]:
    found, priority = [], "低"
    for _, config in KEYWORDS.items():
        for word in config["words"]:
            if word in text:
                found.append(config["label"])
                if config["priority"] == "高":
                    priority = "高"
                elif config["priority"] == "中" and priority != "高":
                    priority = "中"
                break
    return found, priority


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
#  載入快照（找評論數有變化的藥局）
# ════════════════════════════════════════════

def load_snapshots(ss):
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    date_sheets  = sorted(
        [ws for ws in ss.worksheets() if date_pattern.match(ws.title)],
        key=lambda ws: ws.title, reverse=True,
    )
    if len(date_sheets) < 2:
        print("⚠️  需要至少兩個日期快照才能分析")
        return None, None

    print(f"📂 快照比對：{date_sheets[1].title} → {date_sheets[0].title}")

    def parse(ws):
        data = ws.get_all_values()
        if not data:
            return {}
        headers = data[0]
        result  = {}
        for row in data[1:]:
            r   = dict(zip(headers, row))
            pid = r.get("place_id", "").strip()
            if pid:
                result[pid] = r
        return result

    return parse(date_sheets[0]), parse(date_sheets[1])


def find_targets(latest, previous) -> dict:
    """找出評論數增加 >= MIN_REVIEW_CHANGE 的藥局"""
    targets = {}
    for pid, p in latest.items():
        if pid not in previous:
            continue
        try:
            new_cnt = int(p.get("評論數", 0) or 0)
            old_cnt = int(previous[pid].get("評論數", 0) or 0)
            change  = new_cnt - old_cnt
        except ValueError:
            continue
        if change >= MIN_REVIEW_CHANGE:
            targets[pid] = {
                **p,
                "place_id":    pid,
                "異動類型":   "評論增加",
                "評論數變化": f"+{change}（{old_cnt}→{new_cnt}）",
                "健保狀態":   "",
            }
    return targets


def load_new_pharmacies(ss) -> list[dict]:
    """載入 tracker 產生的「🆕 新出現藥局」"""
    try:
        ws   = ss.worksheet("🆕 新出現藥局")
        data = ws.get_all_values()
        if len(data) < 2:
            return []
        headers = data[0]
        return [dict(zip(headers, row)) for row in data[1:] if any(row)]
    except gspread.exceptions.WorksheetNotFound:
        return []


# ════════════════════════════════════════════
#  Google Places API：抓取留言
# ════════════════════════════════════════════

def fetch_reviews(place_id: str) -> list[dict]:
    url    = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields":   "reviews",
        "language": "zh-TW",
        "key":      PLACES_API_KEY,
    }
    try:
        res = requests.get(url, params=params, timeout=10).json()
        return res.get("result", {}).get("reviews", [])
    except Exception as e:
        print(f"    ⚠️  取得留言失敗：{e}")
        return []


# ════════════════════════════════════════════
#  五層訊號分析
# ════════════════════════════════════════════

def analyze_pharmacy(pid: str, pharmacy: dict, reviews: list[dict]) -> dict | None:
    if not reviews and pharmacy.get("異動類型") != "新出現":
        return None

    # 關鍵字掃描（所有留言）
    all_text          = " ".join(r.get("text", "") for r in reviews)
    signals, priority = scan_keywords(all_text)

    # 總評論數與本次增加數
    try:
        total_reviews = int(pharmacy.get("評論數", 0) or 0)
    except ValueError:
        total_reviews = 0

    try:
        change_num = int(re.search(r"\+(\d+)", pharmacy.get("評論數變化", "")).group(1))
    except (AttributeError, ValueError):
        change_num = 0

    # ── 層1：短期爆發成長 ─────────────────────────────
    growth_rate = (change_num / total_reviews * 100) if total_reviews > 0 else 100
    if change_num >= BURST_ABS or growth_rate >= BURST_RATE:
        signals.insert(0, "🚀 短期爆發成長")
        if priority == "低":
            priority = "中"

    # ── 層2：總評論數個位數 ───────────────────────────
    if total_reviews <= NEW_SHOP_THRESHOLD:
        signals.insert(0, "🏪 全新開幕/高潛力頂讓店")
        priority = "高"

    # ── 層3：最早留言日期偵測 ─────────────────────────
    oldest_info = ""
    timestamps  = [r.get("time", 0) for r in reviews if r.get("time")]
    if timestamps:
        oldest_ts   = min(timestamps)
        oldest_age  = days_since(oldest_ts)
        oldest_date = unix_to_taiwan(oldest_ts)[:10]
        oldest_info = f"{oldest_date}（距今 {oldest_age} 天）"

        if oldest_age <= NEW_OPEN_DAYS and total_reviews <= 20:
            signals.insert(0, f"🆕 極可能本月新開（{oldest_info}）")
            priority = "高"
        elif oldest_age <= RECENT_OPEN_DAYS and total_reviews <= 30:
            signals.insert(0, f"📅 近期開業（{oldest_info}）")
            if priority == "低":
                priority = "中"

    # ── 層4：複合強訊號 ───────────────────────────────
    strong = [s for s in signals if s.startswith(("🆕", "🏪", "🚀"))]
    if len(strong) >= 2:
        signals.insert(0, "⚡ 強烈訊號（多重指標）")
        priority = "高"

    # ── 層5：新出現藥局調整 ───────────────────────────
    if pharmacy.get("異動類型") == "新出現":
        if priority == "低":
            priority = "中"
        if "尚未健保" in pharmacy.get("健保狀態", ""):
            priority = "高"

    # 格式化留言（含台灣時間）
    formatted = []
    for r in reviews[:5]:
        rating  = int(r.get("rating", 3))
        text    = r.get("text", "（無文字）").strip()
        tw_time = unix_to_taiwan(r.get("time", ""))
        formatted.append(
            f"  {'⭐'*rating}（{tw_time}）{text[:80]}{'...' if len(text)>80 else ''}"
        )

    return {
        "place_id":     pid,
        "名稱":         pharmacy.get("名稱", ""),
        "地址":         pharmacy.get("地址", ""),
        "縣市":         pharmacy.get("縣市", ""),
        "異動類型":    pharmacy.get("異動類型", "評論增加"),
        "總評論數":    str(total_reviews),
        "評論數變化":  pharmacy.get("評論數變化", ""),
        "成長率":      f"{growth_rate:.1f}%",
        "最早留言":    oldest_info,
        "健保狀態":    pharmacy.get("健保狀態", ""),
        "留言數":      len(reviews),
        "訊號":        "、".join(signals) if signals else "—",
        "優先等級":    priority,
        "留言列表":    formatted,
    }


def run_analysis(targets: dict) -> list[dict]:
    results = []
    total   = len(targets)

    for i, (pid, pharmacy) in enumerate(targets.items(), 1):
        if i == 1 or i % 10 == 0:
            print(f"  進度：{i}/{total}")
        reviews = fetch_reviews(pid)
        time.sleep(0.5)
        result  = analyze_pharmacy(pid, pharmacy, reviews)
        if result:
            results.append(result)

    order = {"高": 0, "中": 1, "低": 2}
    results.sort(key=lambda x: order.get(x["優先等級"], 1))
    return results


# ════════════════════════════════════════════
#  產生「本週摘要」（直接複製貼到 Claude.ai）
# ════════════════════════════════════════════

def generate_summary(ss, results: list[dict]):
    ws    = get_or_create_sheet(ss, "本週摘要", rows=500, cols=2)
    high  = [r for r in results if r["優先等級"] == "高"]
    mid   = [r for r in results if r["優先等級"] == "中"]
    low   = [r for r in results if r["優先等級"] == "低"]

    lines = [
        "以下是本次藥局新增留言資料，請幫我分析並給出拜訪建議：",
        f"（分析日期：{TODAY}，共 {len(results)} 間藥局有異動）",
        "",
        "【請回答】",
        "1. 最值得優先拜訪的前 5 間，說明原因",
        "2. 有哪些店家出現人員異動、全新開幕或爆發成長訊號",
        "3. 哪些區域最近比較活躍",
        "4. 給我本次的具體拜訪建議",
        "",
        "=" * 40,
    ]

    def add_section(label, items):
        if not items:
            return
        lines.append(f"\n{label}（{len(items)} 間）")
        lines.append("-" * 30)
        for r in items:
            lines.append(f"【{r['名稱']}】{r['縣市']} {r['地址']}")
            lines.append(
                f"  {r['異動類型']} ｜ {r['評論數變化']} ｜"
                f" 總評論數：{r['總評論數']} ｜ 成長率：{r['成長率']}"
            )
            if r.get("最早留言"):
                lines.append(f"  最早留言：{r['最早留言']}")
            if r["訊號"] != "—":
                lines.append(f"  偵測訊號：{r['訊號']}")
            if r.get("健保狀態"):
                lines.append(f"  健保狀態：{r['健保狀態']}")
            lines.append(f"  留言（{r['留言數']} 則）：")
            for line in r["留言列表"]:
                lines.append(line)
            lines.append("")

    add_section("🔥 高優先", high)
    add_section("📋 中優先", mid)
    add_section("📌 低優先", low)

    ws.append_rows([[line] for line in lines])
    print(f"✅ 本週摘要已寫入（{len(lines)} 行）")


# ════════════════════════════════════════════
#  LINE 通知
# ════════════════════════════════════════════

def send_line(text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_TOKEN}"},
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )

def build_line_message(results: list[dict]) -> str:
    high     = sum(1 for r in results if r["優先等級"] == "高")
    mid      = sum(1 for r in results if r["優先等級"] == "中")
    new_open = sum(1 for r in results if "極可能本月新開" in r["訊號"])
    burst    = sum(1 for r in results if "短期爆發成長" in r["訊號"])
    strong   = sum(1 for r in results if "強烈訊號" in r["訊號"])

    lines = [f"📊 藥局輿情報告", f"📅 {TODAY}",
             f"有異動藥局：{len(results)} 間", "─" * 20]
    if strong:   lines.append(f"⚡ 強烈訊號：{strong} 間")
    if new_open: lines.append(f"🆕 本月新開幕：{new_open} 間")
    if burst:    lines.append(f"🚀 短期爆發：{burst} 間")
    lines += [f"🔥 高優先：{high} 間", f"📋 中優先：{mid} 間",
              "", "請開啟 Sheets", "複製「本週摘要」貼到 Claude.ai"]
    return "\n".join(lines)


# ════════════════════════════════════════════
#  主程式
# ════════════════════════════════════════════

def main():
    print("=" * 50)
    print(f"  藥局輿情追蹤 v6  |  {TODAY}")
    print("=" * 50)

    ss = get_spreadsheet()

    # 1. 載入兩個快照
    latest, previous = load_snapshots(ss)
    if latest is None:
        send_line(f"📊 輿情系統\n{TODAY}\n\n⚠️ 快照不足，請等 tracker 累積兩次資料")
        return

    # 2. 找目標藥局（評論增加 + 新出現）
    targets = find_targets(latest, previous)
    for p in load_new_pharmacies(ss):
        pid = p.get("place_id", "").strip()
        if pid and pid not in targets:
            targets[pid] = {
                **latest.get(pid, {}),
                "place_id":   pid,
                "名稱":       p.get("名稱", ""),
                "地址":       p.get("Google地址", p.get("地址", "")),
                "縣市":       p.get("縣市", ""),
                "異動類型":  "新出現",
                "評論數變化": f"初次出現（{latest.get(pid, {}).get('評論數', 0)} 則）",
                "健保狀態":  p.get("健保狀態", ""),
            }

    print(f"🔍 分析目標：{len(targets)} 間藥局")

    if not targets:
        send_line(f"📊 藥局輿情\n📅 {TODAY}\n\n本次無評論異動")
        return

    # 3. 抓留言 + 分析
    print(f"\n📡 抓取留言並分析中...")
    results = run_analysis(targets)

    # 4. 產生摘要
    generate_summary(ss, results)

    # 5. LINE 通知
    msg = build_line_message(results)
    send_line(msg)

    strong = sum(1 for r in results if "強烈訊號" in r["訊號"])
    high   = sum(1 for r in results if r["優先等級"] == "高")
    print(f"\n   ⚡ 強烈訊號：{strong} 間")
    print(f"   🔥 高優先：  {high} 間")
    print(f"   📊 總分析：  {len(results)} 間")
    print("\n📲 LINE 通知已發送")
    print("🎉 完成！")


if __name__ == "__main__":
    main()
