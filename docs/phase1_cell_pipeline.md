# 第一階段：逐格班表辨識實驗

這是一條新管線，入口是 `backend/phase1_cell_pipeline.py`；它沒有修改或接管既有的 API、LINE/Discord Bot、`schedule_reader.py` 或 `backend/preprocessing.py`。

它的資料流是：照片 → 透視校正 → 依 `row_map` 先裁切時段窗口 → 固定 Grid 切格 → 移除 cell 邊緣後放大 5 倍 → 每格可辨識多個 `丁／戊／己／庚`（或 `OTHER／EMPTY／UNKNOWN`）→ Python 的 row/column mapping → 僅對候選字元做鄰格 context 複查。每一個代號都必須由兩次辨識共同看見才會採用；它不會補足 8 小時。當前 local template 只保留 `06-07` 至 `17-18`。

目前 local template 啟用透視 registration（`use_original_image: false`）：每張照片會先將勤務表對齊到固定的 `2400×1600` canonical grid，再沿用已由 9/16 驗證的 `grid_x`／`grid_y` 裁切。它不會每天重新推測欄列；照片的拍攝角度、距離與旋轉差異都在 registration 階段消除。`minimum_grid_alignment_score: 0.9` 會在任何模型請求前驗證長水平／垂直格線是否仍對齊 template；未通過時會中止並要求重新拍攝，避免回傳整片錯位卻看似合理的班表。LINE 的標註圖仍會映射回原始照片座標，因此使用者看到的證據圖不會被 warp。

## 一次校正

先用一張代表性的班表產生新的設定檔（輸出檔案必須不存在）：

```bash
python -m backend.phase1_cell_pipeline calibrate /path/to/roster.jpg \
  --output configs/phase1_roster_template.local.json
```

它會將校正後的格線寫入新檔。請將「主表時段格」的 interval index 填到 `row_map`，將需要掃描的勤務欄 interval index 填到 `column_map`。例如：

```json
{
  "row_map": {"5": "05-06", "6": "06-07"},
  "column_map": {"12": "國際線", "13": "國際線（二）"}
}
```

每個 index 表示 `grid_[index]` 到 `grid_[index + 1]` 的一格；未列入 mapping 的表頭、姓名表與其他欄位不會送進模型。若自動抓到整張照片邊界而非表格，手動填入 `table_corners`（來源圖片中的 TL、TR、BR、BL 四個像素座標）後，再以 `--seed-template` 重新校正。

## 執行

```bash
export OPENAI_API_KEY="…"
python -m backend.phase1_cell_pipeline analyze /path/to/roster.jpg \
  --template configs/phase1_roster_template.local.json
```

輸出只包含兩輪同意、且確實對應到 `row_map` 與 `column_map` 的觀察結果。`unconfirmed_candidates` 是第一輪看起來像目標字、但複查不一致的格；請把它當成「未確認」，不要當成排班。

若要直接用 Markdown 表格人工檢查每個 cell，改用：

```bash
python -m backend.phase1_cell_pipeline analyze /path/to/roster.jpg \
  --template configs/phase1_roster_template.local.json --markdown
```

表格中的 `丁／戊／己／庚` 或 `丁+戊` 代表兩輪一致，`X→Y` 是兩輪不一致，`O` 代表其他文字，`·` 代表空白，`?` 則是未確認。

## 匯出裁切過程

加上 `--crops-dir` 會建立一個**不存在的新資料夾**，其中包含校正圖、06–18 時段裁切圖、每一格實際送往模型的放大 PNG、contact sheet，以及對照索引。`02-cell-contact-sheet.jpg` 會依 Markdown 表格的欄序排列：上方為勤務 header（含 `cXX` grid index）、左側為時段、每個時段只佔一個水平列，方便直接目視比對。這可先搭配 `--dry-run` 使用，不會發出 API 請求：

```bash
python -m backend.phase1_cell_pipeline analyze data/9_16.jpg \
  --template configs/phase1_roster_template.local.json --dry-run \
  --crops-dir data/phase1-review-06-18
```

## 丁／庚的保守規則

兩輪 prompt 會要求模型將 `丁` 與 `庚` 視為完整字形的精確辨識：`I/H/M/B/F/D`、數字、字體襯線、`乙`、`丙` 和其他相似字都不能當成代號；不確定時回傳 `UNKNOWN`。同一格若上下分別有中文與數字，仍會分開判斷，因此真正的 `丁 23` 或 `庚 72` 不會因為另一行有數字而被排除。

## 獨立 multi-label 比較實驗

`backend/phase1_multilabel_compare.py` 是另一條實驗管線，不會修改第一階段的切格或既有兩輪結果。它只讀取先前輸出的 `cells/` PNG，因此輸入格子與 baseline 完全一致；第一輪直接以 `ding/wu/ji/geng` 四個布林值判定整個 cell，**僅在初步 `wu=true`、`geng=false` 時**才以專用 verifier 覆查這一對。輸出資料夾必須不存在，舊辨識檔也只讀不寫：

```bash
python -m backend.phase1_multilabel_compare \
  --cells-dir data/phase1-recognition-v6/cells \
  --baseline data/phase1-recognition-v6/recognition.json \
  --template configs/phase1_roster_template.local.json \
  --output data/phase1-multilabel-compare-v1
```

實驗會產生 `experimental-grid.md`（新的完整表格）、`comparison.md`（只有差異與 verifier 觸發的格）、`comparison.json`。它不會把結果寫回 `phase1-recognition-v6`，也不會重新校正或重切原始照片。

若只是調整 verifier 規則，可加上 `--reuse-primary data/phase1-multilabel-compare-v1/comparison.json` 重用已保存的第一輪布林結果，避免再次上傳相同的 192 張 crop；這仍會以新的輸出資料夾寫出另一份比較報告。

## LINE Bot 的文字式後段

正式入口 `recognize_source_multilabel` 保留每格 96% 的寬高（原為 78%），避免截掉靠近框線的完整字形，並直接使用第一輪多標籤結果產生 final grid。原圖模式也使用相同比例。正式流程不再呼叫舊戊／庚 verifier，亦不再用它覆寫第一輪；歷史比較 CLI 保留原實驗行為。回傳 metadata 包含 `recognition_policy=primary_only_full_glyph`、`cell_inner_fraction=0.96`，`wu_geng_verifier_count` 固定為 0。

9/18 的兩次 API 對照測試中，此輸入方式的第一輪皆與目視核對一致；這不代表所有日期都已驗證。格線定位、藍色 debug 框、個人紅色工作框／綠色休息框保持原樣。

LINE Bot 現在會先以本文件的固定 grid 產生 final multi-label Markdown，再將**文字表格本身**交給 Luna 整理四人的班表；後段不再傳送原圖、預處理裁切圖或 cell 圖。`?` 與 `X→Y` 一律會以未確認處理，不會被 Luna 猜成勤務。所有逐格辨識和文字整理的 API 用量會合併，並在 LINE 回覆的最後一則顯示本次美元估算總價。

每位員工的圖片回覆保留原始班表左側，從照片頂端裁到 `18-19` 時段的底線（19:00），右側裁到「國內工作管制門」（c26／立榮托運）欄位的右邊界；不再做另一種左右證據卡。該員工的勤務格會依 final grid 以長方形標出：紅色實線框代表上班，綠色實線框代表休息。圖片右上角也會顯示相同圖例；文字訊息會額外列出上班與休息時數摘要。

預設使用 `configs/phase1_roster_template.local.json`。部署到其他環境時，請在 `.env` 設定一個存在的絕對路徑：

```ini
LINE_PHASE1_TEMPLATE=/srv/work_schedule/configs/phase1_roster_template.local.json
```

## 格線 debug（9/18 定位修正）

LINE 原本的 `analyze_schedule_image(use_final_markdown=True)` 已使用最新欄線修正。回覆圖固定保留藍色 OCR 目標格框，再以紅色覆蓋該員工工作格、綠色覆蓋休息格，不需要 debug 環境變數。回覆圖不顯示黃色全表輔助線；CLI 匯出的 `01-grid-debug.jpg` 仍保留黃線與藍框供檢查。

9/18 的紙張外緣曾干擾表格側邊擬合，造成時間欄裁切及索引錯位。邊界偵測現在只使用最大的連通格線區域，並排除靠近照片頂端的水平邊緣；不需要為每天的照片手動指定水平位移。

本機檢視（不呼叫辨識 API）：

```bash
python -m backend.phase1_cell_pipeline analyze 'data/9:18.jpg' \
  --template configs/phase1_roster_template.local.json --dry-run \
  --crops-dir data/phase1-debug-9_18-new
```

輸出 `01-grid-debug.jpg` 為完整格線，`02-cell-contact-sheet.jpg` 為 192 格實際辨識輸入。

## 垂直欄線修正

校正後不再讓每個勤務欄沿用整張圖的固定 x 座標。`registered_column_models` 先從時段表本體的長線段定位，再以多個局部水平帶追蹤欄線，擬合 `x = a + b*y`。排除下方姓名表的不同格線；時間欄在主表合併的內部分隔線以兩側邊界內插。若欄線在主表範圍交叉，會拒絕定位。

逐格裁切會以水平與垂直線交點求出該格位置，在格子中心取得局部矩形並去除邊緣，保留原有影像像素／放大流程。Debug 藍框與 LINE 勤務框則沿四個交點繪製，黃色垂直線顯示擬合後的傾斜方向。第二輪鄰格裁切也使用同一組幾何座標。

`data/vertical-grid-final/` 保存 9/10、13、14、15、17、18 的最終 debug、逐格輸入與模型參數。此修正處理垂直欄界；9/13 因水平線索引缺失而整體錯一列的問題仍未修正，不能視為該張班表已通過辨識驗證。

9/14 國際線 #3／#4 的 C05 交界補強：當等寬欄位的單條分隔線偏離相鄰欄線中點時，重新搜尋中點附近的實際墨線；至少四個高度帶提供證據且擬合位置合理才替換。六張測試照片的前後欄線參數比較，只有 9/14 C05 改變。最新圖與 192 格輸入保存於 `data/vertical-divider-fix/`。
