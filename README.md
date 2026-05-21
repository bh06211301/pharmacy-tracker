# 藥局追蹤器

自動偵測台北、新北、基隆、桃園四縣市新開藥局，每週三次發送 LINE 通知。

## 檔案結構

```
pharmacy-tracker/
├── .github/
│   └── workflows/
│       └── run.yml          ← GitHub Actions 排程
├── pharmacy_tracker.py      ← 主程式
└── README.md
```

## 設定步驟

### 1. 上傳到 GitHub

建立一個新的 **private** repository，把這三個檔案上傳進去。

### 2. 設定 GitHub Secrets

前往 GitHub → 你的 repo → Settings → Secrets and variables → Actions → New repository secret

需要新增以下四個 Secret：

| Secret 名稱 | 內容 |
|------------|------|
| `PLACES_API_KEY` | Google Places API 金鑰 |
| `SPREADSHEET_ID` | Google Sheets ID |
| `LINE_TOKEN` | LINE Channel Access Token |
| `LINE_USER_ID` | LINE User ID（U 開頭） |
| `GOOGLE_CREDENTIALS_JSON` | Google 服務帳戶 JSON 完整內容 |

### 3. 取得 GOOGLE_CREDENTIALS_JSON

1. 前往 Google Cloud Console → IAM 與管理 → 服務帳戶
2. 建立服務帳戶（或使用既有的）
3. 建立金鑰 → 選 JSON → 下載
4. 用文字編輯器打開，複製全部內容
5. 貼到 GitHub Secret `GOOGLE_CREDENTIALS_JSON`

### 4. 把服務帳戶加入 Google Sheets

開啟你的 Google Sheets → 共用 → 加入服務帳戶的 email（在 JSON 裡的 client_email）→ 編輯者

### 5. 匯入健保基準資料

把健保署 CSV 匯入 Google Sheets，分頁命名為「健保基準資料」。

### 6. 手動測試

GitHub → Actions → 藥局追蹤器 → Run workflow

## 排程

每週一、三、五台灣時間早上 08:00 自動執行。

## 覆蓋範圍

使用 2.5 km 六角形網格自動產生座標點，確保無搜尋死角：
- 台北市：含主要市區，排除陽明山高山區
- 新北市：核心都市區、北區、東南區
- 基隆市：全市
- 桃園市：桃園區、中壢、蘆竹、龜山等主要區域

## 免費額度

- Google Places API：每月約 5,000-8,000 次，免費額度 10,000 次 ✅
- LINE Messaging API：每月約 12-15 則，免費額度 200 則 ✅
- GitHub Actions：每月 2,000 分鐘，免費額度充足 ✅
