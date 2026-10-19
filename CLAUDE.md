# 專案定位

這是一個「IG 食譜帳號圖片批次處理」專案：把手上的料理照片批次裁切成統一的**橫向 4:3**比例（不管原圖是直幅還是橫幅拍的，一律輸出橫幅 4:3），並套用統一的食物攝影風格濾鏡，讓整個 IG 版面風格一致。

> 原本的需求是裁成 1:1 正方形，但考慮到 IG 動態牆/個人頁的格狀總覽本來就會把任何比例的貼文自動裁成正方形縮圖，使用者後來決定改成輸出 4:3（貼文本身完整呈現，不用額外裁切或加留白邊框妥協）。中間一度做成「橫幅→4:3、直幅→3:4」依原圖自動判斷，但使用者後來明確要求**全部統一橫向 4:3**，不要有 3:4 直幅混在裡面。

核心流程寫在 `.claude/skills/ig-photo-batch/SKILL.md`：裁切範圍一定要經過使用者確認，濾鏡風格是固定參數、套用在每一張照片上。

## 目錄慣例

- `input/` — 使用者放原始照片的地方，**正常流程下**處理時不會被修改或刪除。
  > 已知事件：2026-10-16 使用者手動把 `output/` 的菜名改回去對應 `input/` 時，不小心把 `input/` 裡原本的 35 張生圖也覆蓋成處理過的成品圖（目前 `input/` 裡的 34 個檔案其實都是 1440x1080 的成品尺寸，不是真正的原始照片）。使用者確認目前已定案的 34 張成品沒問題、不需要找回原圖，所以沒有回復動作。但這表示：**之後若要重跑這 34 張的 `auto-detect` 或改裁切，等於是拿成品再裁一次，容易裁過頭**——只對「真正新放進來、還沒處理過」的照片用 `auto-detect`/`preview`/`finalize`，不要對 `output/manifest.json` 裡已經有記錄的檔案重跑。
- 雲端匯入：使用者用 Android 手機把照片分享到 Google Drive 的資料夾（不再經過 Messenger，Messenger 會把照片壓成 2048px）。電腦上 Google Drive 桌面版會把那個資料夾掛在 G: 槽，路徑寫在專案根目錄的 `input_import_dir.txt`（這台電腦專屬設定，不進版控）。`scripts/cloud_import.py` 會在 `list-new` 和每週自動預覽時，先把雲端資料夾裡 `input/` 還沒有的照片（依檔名判斷）複製進 `input/`，不會刪改雲端上的檔案。
- `output/` — 確認後的最終成品（統一橫向 4:3、已套濾鏡），檔名是菜名（例如 `三杯雞.jpg`），不是 `input/` 的原始檔名。
- `output/manifest.json` — 記錄 `input` 檔名（不含副檔名）對應到 `output` 用的菜名，`list-new` 靠這個判斷哪些 input 已經處理過，不是直接比對檔名（因為兩邊檔名現在不一樣）。
- `.cache/previews/` — 裁切+濾鏡後的「預覽圖」，給使用者確認用，不是最終交付成果，不進版控。
- `scripts/` — 圖片處理工具，依賴套件列在 `scripts/requirements.txt`。
- `.claude/skills/` — 可重複呼叫的封裝工作流程。
- `.claude/settings.json` — 工具權限設定。
- `.venv/` — 專案的 Python 虛擬環境，不進版控。

## 執行環境

- Python 3.12 + 專案內的 `.venv`（`python -m venv .venv`，再 `.venv\Scripts\pip install -r scripts\requirements.txt`）。
- 主要依賴 `Pillow`（裁切/濾鏡）、`numpy`（暗角運算）與 `opencv-python-headless`（`auto-detect` 的圓形/邊緣偵測）；若照片是 iPhone 的 HEIC 格式，需要額外裝 `pillow-heif`。

## 核心工具：scripts/process_photo.py

- `list-new` — 列出 `input/` 裡還沒出現在 `output/manifest.json` 的新照片。
- `auto-detect <路徑>` — 用電腦視覺自動偵測畫面主體（優先找圓盤/碗的邊緣，找不到就用飽和度+邊緣對比），印出建議的 `offset_x`/`offset_y`/`zoom`/`rotate`（JSON，`detected: false` 代表偵測失敗，要退回手動模式）。目的是讓裁切流程更自動化，減少每張都要手動試 offset/zoom/rotate 的次數；只對**真正的原始照片**有意義，拿 `output/` 的成品圖重跑會裁過頭（見上面「已知事件」）。
  - `rotate` 的判斷分兩層：先用 Hough 直線偵測找畫面裡的水平參考物（桌緣、木紋等）算出鏡頭本身歪了幾度（`deskew_degrees`，找不到夠可信的參考線就是 0，不會亂猜）；接著在「不轉」「+90」「-90（即 270）」三個方向都各自試算需要的 zoom，選 zoom 最小的方向——**不是固定往同一個方向轉**，因為固定方向在某些照片會轉錯邊、讓結果不穩定。最終 `rotate` 是「微幅校正角度 + 90 的倍數」的合併結果，所以現在 `--rotate` 可以是任意角度，不是只有 0/90/180/270 四選一。
- `preview <路徑> [--offset-x F] [--offset-y F] [--zoom F]` — 產生裁切+濾鏡後的預覽圖到 `.cache/previews/`。
- `finalize <路徑> --name <菜名> [--offset-x F] [--offset-y F] [--zoom F]` — 同樣的處理邏輯，輸出到 `output/<菜名>.jpg`，並把 `input 檔名 -> 菜名` 寫進 `manifest.json`。沒給 `--name` 的話會退回用 input 原始檔名（不建議，新照片都應該給 `--name`）。
- 單張例外參數：`--warmth`/`--sharpen`/`--brightness` 倍率，只影響那一張、不碰全域濾鏡設定；`--rotate {0,90,180,270}` 在裁切前先轉正直幅照片，常能保留更完整的原圖範圍。`auto-detect` 算出的 `rotate` 建議也是這個參數。
- 裁切與濾鏡邏輯共用同一個 `process()` 函式，確保 preview 看到的畫面跟最終輸出完全一致。
- 濾鏡參數（對比/飽和度/暖色調/暗角/銳化/顆粒感）以常數形式寫在 `process_photo.py` 開頭，參考一般食物攝影 IG 帳號常見做法：飽和度其實是略降而非推高、對比與清晰度放輕、暖色調幅度很小，走真實自然路線而非厚重濾鏡。
- 裁切邏輯預設一律輸出橫向 4:3（`--orientation` 預設是 `landscape`），直幅原圖也會被裁成橫幅；原圖已經是橫幅 4:3 時通常只需要裁掉一點點多餘的邊。只有使用者明確要求 `--zoom > 1` 時才會額外裁緊、放大主體。
- **IG Crema 濾鏡（100%）**：使用者要求發文時套 IG 的 Crema 濾鏡，但發文 API 不能套 app 內建濾鏡，所以 `apply_style()` 最後一步用 `apply_crema()` 自己做。參數（`CREMA_SATURATION`、`CREMA_CURVES`）是拿同一張照片的原圖與 IG 網頁版套 Crema 100% 的截圖逐像素擬合出來的，不是目測；強度 `CREMA_STRENGTH = 1.0` 是使用者選定的。2026-10-16 已對 `output/` 全部 34 張成品補套 Crema（套用前的版本備份在 `.cache/output_backup_before_crema/`）。**`output/` 裡的成品都已經含 Crema，不要再對它們套一次**，新照片走 `process()` 會自動套到。
- 以上都是固定參數/邏輯，所有照片套用完全相同的值，才能保證 IG 版面風格一致，不是每張個別調整。

## 自動預覽：scripts/auto_preview.py

- 使用者要求**每週檢查一次**：由 `.claude/settings.json` 的 `SessionStart` 與 `UserPromptSubmit` hook 呼叫，但腳本會看 `.cache/auto_preview_last_check` 的時間戳，距離上次檢查不滿 7 天就直接結束；滿 7 天後的第一次開對話/送訊息才真正檢查。Claude Code 沒開時不會檢查。使用者隨時也可以手動叫我處理，不受這個週期限制。檢查時會看 `input/` 有沒有還沒記錄在 manifest 的新照片，有的話用 `auto-detect` 的參數先產生預覽到 `.cache/previews/<檔名>.jpg`，旁邊存一份同名 `.json` 記錄參數，並把結果注入對話脈絡。
- 只做到「產生預覽」為止，**不會 finalize**——定案仍然要使用者看過預覽、明確同意。看到 `[auto_preview]` 的提示時，照 `ig-photo-batch` skill 把預覽顯示給使用者確認即可，`.json` 裡的參數就是 finalize 要帶的參數（若之後手動調整過，以最後一次 `preview` 的參數為準）。
- 一次最多 5 張；已經定案（進 manifest）的照片，殘留的預覽檔會被自動清掉。沒有新照片時幾乎不花時間、也不輸出任何東西。

## 自動發 IG：scripts/publish_ig.py + GitHub Actions

- 使用者要求**完全自動發文**（發出前不再經過人工確認）：GitHub Actions（`.github/workflows/ig-publish.yml`）每天跑 `publish_ig.py publish`，每 3 天在**美東時間 19:00**（使用者在美東；`America/New_York`，自動處理夏令/冬令時間）發一篇；貼文文字**只有菜名**（撞名用的尾數字會去掉）。
- 發文順序 = `manifest.json` 的定案順序，新 finalize 的照片自動排到最後。進度記在 `output/publish_state.json`，Actions 發完會自己 commit 回 repo。
- 使用者 2026-10-16 起**不再用 Meta Business Suite 排程，發文全部交給這套自動流程**。`set-priority` 可指定插隊優先發的菜名；`schedule` 列出接下來的預計發文日；在別處手動發過的用 `mark-posted <菜名>` 標記避免重發；`set-not-before` 設定最早自動發文時間。
- 用的是 Instagram 官方 Content Publishing API，它要從公開網址抓圖，所以這個 repo 是**公開的** GitHub repo，圖片透過 GitHub Pages 提供（`IMAGE_BASE_URL`）。`input/`、`.cache/`、`.venv/` 不進版控（見 `.gitignore`）。token 放在 GitHub Secrets（`IG_USER_ID`、`IG_ACCESS_TOKEN`），不能寫進任何檔案。
- **finalize 之後要 commit + push `output/`**，新成品才會進到雲端的發文佇列。

## 工作慣例（軟規則）

- 使用者說「不用這張」時，跑 `process_photo.py discard <檔名>`：會刪 `input/` 的照片、預覽圖、**Google Drive「IG照片」裡的原檔**（使用者明確要求雲端一起刪；會進雲端硬碟垃圾桶，30 天內可救回），已定案但還沒發的也會刪成品與 manifest 紀錄。若刪到成品，記得 commit + push。已經發到 IG 的照片 `discard` 會拒絕，請使用者自己在 IG app 刪貼文。

- 裁切範圍一定要讓使用者看過預覽圖、明確同意後才輸出到 `output/`，不能自動判斷就直接定案。
- 濾鏡參數是全域固定值，不要因為單一張照片好看與否就臨時修改 `apply_style()`；如果真的要調整風格，要跟使用者確認這是「以後全部照片都要改」的決定。
- `output/` 的檔案視為交付成果；`.cache/previews/` 只是暫存，可以隨時清空重建。
- 曾經做過「對焦/銳利度檢測」功能（`check-focus`），但後來拿掉了：失焦是拍攝當下造成的，後製救不回來，使用者也明確說不會重拍，偵測出來沒有任何能採取的行動，純粹是噪音。之後不要因為「可以偵測」就重新加類似功能，先想清楚偵測出來之後實際能做什麼。

## 如何擴充

之後如果需要支援其他輸出比例（例如 4:5）或不同風格（例如換一套濾鏡給不同系列的貼文），可以在 `process_photo.py` 加新的子命令或參數，並在這份文件補充說明。
