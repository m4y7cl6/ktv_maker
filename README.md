# 🎤 KTV Maker — YouTube 轉 KTV 去人聲工具

YouTube 影片 → **AI 去人聲 + 繁體字幕燒入 → MP4 1080p**

```
YouTube URL
    │
    ├─ [yt-dlp]          下載 1080p 視訊 + 原始音訊 WAV
    │
    ├─ [Demucs htdemucs] AI 人聲分離 → 伴奏 WAV
    │
    ├─ [yt-dlp 字幕]     抓 YouTube 內嵌字幕（繁中）
    │   └─ 失敗時 ──→ [faster-whisper large-v3] AI 語音識別 + 繁體轉換
    │
    └─ [FFmpeg]          合成：視訊 + 伴奏 + ASS 字幕燒入 → output/*.mp4
```

---

## 環境需求

| 項目 | 最低 | 建議 |
|------|------|------|
| GPU  | NVIDIA 8GB VRAM | RTX 3080 / A10 以上 |
| RAM  | 16 GB | 32 GB |
| 硬碟 | 20 GB 可用 | SSD 50 GB |
| OS   | Ubuntu 22.04 | 同左 |
| CUDA | 11.8 | 12.1 |

---

## 快速啟動（Docker，推薦）

```bash
# 1. Clone 並進入目錄
git clone <your-repo> ktv-maker && cd ktv-maker

# 2. 啟動（首次會下載模型，約 5-10 GB，需等待）
docker compose up -d

# 3. 開啟 Web 介面
open http://localhost:8000
```

---

## 本機安裝（不用 Docker）

```bash
# 1. 系統套件
sudo apt update
sudo apt install -y ffmpeg libass-dev fonts-noto-cjk

# 2. Python 環境
python3.11 -m venv venv
source venv/bin/activate

# 3. PyTorch（CUDA 12.1）
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

# 4. 其他套件
pip install -r requirements.txt

# 5. 啟動 API Server
uvicorn src.server:app --host 0.0.0.0 --port 8000

# 或直接命令列使用（不需 Server）
python src/pipeline.py "https://www.youtube.com/watch?v=XXXX"
```

---

## 命令列使用範例

```bash
# 基本使用
python src/pipeline.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# 保留暫存檔（debug 用）
python src/pipeline.py "https://..." --keep-temp

# 指定 job ID
python src/pipeline.py "https://..." --job-id my_song_01
```

輸出檔案位於：`output/<影片標題>_KTV.mp4`

---

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/process` | 批次啟動處理，回傳 `job_ids` 列表 |
| GET  | `/api/progress/{job_id}` | SSE 進度串流 |
| GET  | `/api/download/{job_id}` | 下載完成的 MP4 |
| GET  | `/auth/status` | 查詢 YouTube 授權狀態 |
| GET  | `/auth/youtube` | 開始 YouTube OAuth 授權流程 |
| GET  | `/auth/callback` | OAuth 回呼（Google 自動呼叫） |

### 呼叫範例

```bash
# 啟動（支援批次，vocal_mix: 0.0 純伴奏 ~ 1.0 原唱）
curl -X POST http://localhost:8000/api/process \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://www.youtube.com/watch?v=XXXX"],
    "vocal_mix": 0.0,
    "auto_upload": false,
    "yt_privacy": "private"
  }'
# → {"job_ids": ["a1b2c3d4"]}

# 監聽進度
curl -N http://localhost:8000/api/progress/a1b2c3d4

# 下載
wget http://localhost:8000/api/download/a1b2c3d4 -O ktv_output.mp4
```

---

## YouTube 自動上傳設定

處理完成後可自動上傳至你的 YouTube 頻道，需先完成以下一次性設定：

### 1. 建立 Google Cloud 專案

1. 前往 [console.cloud.google.com](https://console.cloud.google.com)
2. 建立新專案（名稱隨意，例如 `ktv-maker`）
3. 左側選單 → **API 和服務** → **啟用 API**
4. 搜尋並啟用 **YouTube Data API v3**

### 2. 建立 OAuth 憑證

1. 左側選單 → **API 和服務** → **憑證**
2. 點選 **建立憑證** → **OAuth 用戶端 ID**
3. 應用程式類型選擇：**網頁應用程式**
4. 在「已授權的重新導向 URI」加入：
   ```
   http://localhost:8000/auth/callback
   ```
5. 點選建立，下載 JSON 檔案

### 3. 放置憑證檔

```bash
# 將下載的 JSON 改名，放到專案 credentials/ 目錄
mv ~/Downloads/client_secret_xxx.json credentials/client_secrets.json
```

> ⚠️ `client_secrets.json` 已列入 `.gitignore`，**不會被 commit 到 Git**。

### 4. 連結帳號

重啟容器後，在網頁點選「**連結帳號**」按鈕，完成 Google 授權即可。

授權 token 會儲存在 `credentials/youtube_token.json`（同樣不會上傳到 Git）。

---

## 字幕樣式說明

字幕採用 **ASS 格式**燒入，KTV 風格設定：

| 屬性 | 設定值 |
|------|--------|
| 字型 | 微軟正黑體 52pt |
| 顏色 | 白字 + 黑邊框（3pt）|
| 位置 | 畫面底部置中 |
| 陰影 | 2pt 半透明 |

若需自訂字幕樣式，修改 `pipeline.py` 的 `_patch_ass_style()` 函式。

---

## 處理時間參考（RTX 3080）

| 影片長度 | 下載 | Demucs | Whisper | FFmpeg | 總計 |
|----------|------|--------|---------|--------|------|
| 3 分鐘   | 30s  | 2min   | 40s     | 1min   | ~4min |
| 5 分鐘   | 45s  | 3.5min | 1min    | 1.5min | ~7min |
| 10 分鐘  | 1.5min| 7min  | 2min    | 3min   | ~14min |

---

## 常見問題

**Q: Demucs 找不到 GPU？**
```bash
python -c "import torch; print(torch.cuda.is_available())"
# 若輸出 False，重新安裝對應 CUDA 版本的 PyTorch
```

**Q: FFmpeg 字幕燒入失敗（libass 錯誤）？**
```bash
sudo apt install libass-dev
# 重新編譯或安裝支援 libass 的 ffmpeg
ffmpeg -filters | grep subtitles
```

**Q: Whisper 記憶體不足？**
在 `pipeline.py` 中將模型改為 `medium` 或 `small`：
```python
model = WhisperModel("medium", device="cuda", compute_type="float16")
```

**Q: 字幕亂碼（方塊字）？**
```bash
sudo apt install fonts-noto-cjk
fc-cache -f -v
```
