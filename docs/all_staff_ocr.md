# 全員工代號 OCR（V2，獨立實驗）

這條管線不會取代既有的 `丁／戊／己／庚` ResNet，也沒有接入 LINE、Discord 或
正式 API。V1 checkpoint 保持在 `models/roster-resnet18*.pt`；V2 只會讀寫
`data/all_staff/` 與 `models/all_staff/`。

V2 先以輕量分割模型找出每格中的人員代號區域，再由 CRNN＋CTC 讀取代號字串。
因此 `57` 是一個框、兩個字元，而不是第 57 個分類類別。可接受的代號為
`1–84`、`A–N` 與 `甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉`。辨識後再查本機 roster JSON 取得姓名。

V2 掃描完整勤務主表，不沿用 V1 的 14×16 範圍：時段為 `05-06` 至 `23-24`，勤務
格為 `c02–c36`，實際範圍為每張照片 19×35＝665 格（2026-10-04 起納入最右四欄 `c33–c36`：A1A3站、內機、內部管理、全區帶勤，
見「最後四欄與浮動視窗」一節；先前只到 `c32`＝589 格）。（早期曾把 `c28` 當成雙框線間隔而排除；
實際上它是粗線右側的一般勤務格，2026-09-29 起納入，見「格線追蹤」一節。）V1 template 不會被修改；`prepare` 會另外產生
`configs/all_staff_roster_template.local.json`。

## V2.1：context 格子＋行切分＋合成資料辨識器（2026-09-29，建議使用）

班表是電腦排版印字（Times 類襯線字＋明體干支），所以改成：

1. **context 格子**（`all_staff/context.py`）：每格左右多切 22%、上下多切 12%，避免跨格線的字（如 `67` 的 `7`）被切斷。
   標註頁會以藍色虛線畫出本格範圍；文字群中心落在虛線內才屬於本格。
2. **行切分**（`all_staff/segment.py`）：以背景正規化＋形態學去格線＋連通元件分行，取代 U-Net detector。
   上下兩行（`79/68`）與左右並排（`IB`、`甲戊`）不再黏成一框。
3. **line recognizer**（`all_staff/synth.py`、`all_staff/lines.py`）：整行 CTC 讀出 `72`、`#72`、`IB`、`甲戊`，
   再依規則拆成代號（連續數字＝一碼，字母／干支一字一碼，`#` 忽略）。先用合成資料涵蓋全部 108 個代號預訓練，
   再以人工框＋切分器實際輸出的行（含格線殘段、`備`、箭頭等空白負樣本）微調。輸入保持長寬比（32×128）。

### 使用

```bash
# 1. 產生 context 資料集並搬移既有人工標註（template matching 對位；比對不佳的格子會退回待審並列在報告中）
python -m all_staff.cli prepare-context

# 2. 評估（dataset 與 recognizer 預設自動選 data/all_staff_ctx 與 models/all_staff/line-v2/recognizer.pt）
python -m all_staff.cli evaluate --split val,test

# 3. 用新模型重做未複核建議，然後在標註頁只審紅色／低信心
python -m all_staff.cli model-label
python -m all_staff.cli label

# 4. 重新訓練（有新標註後）
python -m all_staff.cli train-lines                       # CPU 可跑，較慢
python -m all_staff.cli train-lines --device cuda --workers 8 --synth-samples 100000
```

`evaluate` 另外輸出 `cell_level`：每格「有哪些人」的 precision／recall 與整格全對比例，這才是 Bot 需要的指標。

合成字型自動尋找：Windows／WSL 的 `times.ttf`、`mingliu.ttc`（新細明體），macOS 的 Times New Roman、Songti，
Linux 的 Liberation Serif、Noto Serif CJK。可用 `ALL_STAFF_LATIN_FONTS`、`ALL_STAFF_CJK_FONTS` 覆寫。

### 格線追蹤與逐格校正（`all_staff/grid.py`，prepare-context 預設）

V1 的切格方式有三個問題：全圖偵測格線後依「第幾條」對應欄位，漏一條或多一條線，後面整排就錯位
（9/27 二版的 c10–c11、c30–c32）；每格用水平矩形裁切，紙張歪斜或彎曲時字會跨出格子；
另外有幾張照片靠寫死 hash 的補丁修正，換一張照片就不適用。

V2 改成：

1. 沿用 V1 的透視校正。
2. 以形態學分離長直線與長橫線。
3. 定錨：欄以「時段欄右側的第一條線」為基準（時段欄約 2 格寬；照片把時段欄切掉時改用最左邊那條線），
   列以「代號列」（窄列，上方是沒有橫線的表頭）為基準；接著依實體順序逐條往外走。
4. 以中間的帶狀區為起點，逐帶往外追蹤每一條線，每帶最多只能移動 0.35 格；
   每條線擬合成二次曲線，所以彎曲的紙也能跟上。
5. 每格用四條曲線的交點形成四邊形，連同 context 邊界一起透視拉正成直立矩形。

debug 圖（`debug/<group>.jpg`，網頁上的「原圖」）中，藍線是有實際線段支撐的格線，紅線是內插補上的。

驗證：以舊人工標註和現行 line model 比對 21 張照片。欄位對應在 15 張照片與舊標註一致。
其餘不一致的經目視確認都是 V1 切錯，舊標註跟著錯：9/12 整排左移一欄、9/22 二版上移一列、
9/23 前幾列跨格、9/19 右側 c29 以後錯位。9/25 二版的時段欄被照片切掉，V1 無法處理，V2 可以。

`prepare-context` 會先把舊的 `data/all_staff_ctx` 移到 `.bak-時間` 資料夾，再重新切格並搬移標註。
搬移時會在同格及上下左右相鄰格尋找舊格的影像：V1 錯位的標註會搬到真正對應的格子（報告中的 `moved`），
無法確定的格子退回待審（`weak_matches`）。

### 最後四欄與浮動視窗（2026-10-04）

10/5 班表的測試找出兩個問題：

1. **最右四欄從沒被讀過**。勤務格原本只到 `c32`，A1A3站（c33）、內機（c34，常是 80／81 上下兩行）、
   內部管理（c35）、全區帶勤（c36）整欄沒有格子，B 與 80 的勤務因此漏了大半。
2. **浮動視窗與備註文字被讀成勤務**。截圖上蓋著的對話框，或合併儲存格裡的備註（「72.73 防爆訓練」、
   「81.28.K.F.L 專案」），其中的 `A`、`72`、`卯` 都被當成人員代號，A 在 10/5 因此多出幾個小時的勤務。

**最後四欄**

- `dataset.py`：`LAST_DUTY_COLUMN` 改為 36（`FULL_DUTY_COLUMNS` ＝ `c02–c36`，每張 19×35＝665 格）；
  `configs/all_staff_columns.json` 加上 33 `A1A3站`、34 `內機`、35 `內部管理`、36 `全區帶勤`。
- 格線範本只有 37 條直線，第 28 條之後不可靠（`reliable_until=27`），追蹤原本只做到 c32 的右緣。
  `grid.register` 現在以固定欄距從可信的那條線外推到第 37 條線（c36 的右緣），再逐帶追蹤實際線段；
  `configs/all_staff_roster_template.local.json` 不必重做。
- 回歸：30 張照片（`data/schedules` 共 32 張，含 10/5；9/11、9/20 本來就在排除清單，改版前後都不處理）以正式的 `DemoApp.analyze`
  重跑，與改版前逐格比對 `c02–c32` 共 17,670 格：
  扣掉被視窗過濾的格子，只有 10 格的代號組合不同——多讀到 3 格（`82`×2、`1`）、少讀 2 格（`82`、一個信心 0.34 的候選），
  改讀 5 格（`1`→`11`、`2`→`22`、`72`→`2`＋`7` 與兩格低信心候選）。這些都是原本就在邊緣的讀值（`82`／`72` 等字形接近的代號，
  或字被切成兩行的臨界情形）隨格線外推造成的次像素位移而跳動，兩個方向都有，沒有系統性偏差；它們與格線追蹤本身的雜訊同量級。

**浮動視窗**（`all_staff/overlays.py`，`grid.ingest_tracked` 在格線登錄後執行）

視窗蓋在表格上，底下的格線被擋住，所以「格線看得到嗎」是最可靠的線索：

1. 沿每條已登錄的曲線取樣（±3 px），用形態學長筆畫遮罩算出每一段格線（每格的四邊）的可見度。
2. 找出**方塊**：矩形、至少 4 格且跨 2 欄以上、最多 40 格，內部的格線全都不到 0.85 可見、且至少一半幾乎看不到（< 0.5），
   而四周的外框還看得見（≥ 50% 可見；外框也看不見代表是對位問題，不是視窗）。方塊內所有讀值整格丟棄，
   移到該格的 `ignored`，並標 `overlay="box"`。
3. **尖角**：從方塊往外沿著受損的格線走（深度 ≤ 6、總長 ≤ 18 格），這些格子加上尖端指到的那一格標 `overlay="pointer"`，
   只丟掉信心低於 0.8 的讀值——尖角下仍有清楚的真實代號（信心高）就保留。
4. **不可靠就關閉**：方塊以外有超過 5% 的內部格線看不到（照片太糊或對位失敗），或判定的方塊合計超過 80 格，
   整個過濾關閉（`unreliable`），保留全部讀值——偵測失誤的代價是多一個誤讀，不能是漏掉真實代號。
   只蓋住 1–3 格的小標籤（子丙、乙寅這類真的有代號的）與單一欄的細長條都不算視窗。

結果存在 `cells/overlays/<照片雜湊前 16 碼>.json`；`viewer.sheet_payload` 讀回後套到每一格（`ignored`、`overlay`），
並把摘要放進 `payload["overlays"]`；整張總覽圖（debug 圖）上方塊畫紅框、尖角格畫橘框。`DemoApp.ensure_duties`
不會對標了 `overlay` 的格子（方塊與尖角格）做 `1/11/2/22/3/33` 字形複核。

驗證（30 張照片）：

- 9 張偵測到視窗（共 10 個、120 格），丟掉 147 個讀值（72 格有讀值被丟），逐一目視核對全是備註或對話框裡的文字；
  其餘 21 張沒偵測到任何視窗，讀值與沒有過濾時完全相同。同一個位置（`列19–20 × 欄5–9` 一帶的備註框）在不少張照片都出現。
- 10/5：兩個視窗——「丁帶領替代役新生認識環境 17-19」（列 19×欄 5–8）與「14-18 支援二廠：9（停休 4HR）」（列 19×欄 21–24），
  另有 12 個尖角格；第一個視窗裡被讀成 A 的幻影讀值（`A` 0.89，連同 `癸`、`寅`、`未` 等）被丟棄，A 只剩原表右下角的「輪休」。
- 劣化測試（30 張 × 模糊三級、縮小兩級、雜訊兩級、褪色、橫／直向動態模糊、JPEG 品質 25 共 330 組）：
  誤判的視窗 1 次（雜訊 σ=24，沒有字的角落冒出一個 4 格的假視窗），漏掉視窗 2 次（σ=1.6 的模糊，退回舊行為）；
  中度以上的模糊或動態模糊時格線看不清楚，過濾自動關閉。

**已存班表的重讀**

格線規則以 `dataset.GRID_VERSION` 標記（1＝`c02–c32`、視窗文字被當成勤務；2＝`c02–c36`＋視窗過濾），新辨識的結果寫進 `result.json["grid_version"]`。
`DemoApp.ensure_grid(run_id)` 用保存的原始照片重新切格並讀取，保留姓名對照表、休假表與班表日期；整個替換是原子的
（暫存在 `cells.next`，成功後換掉 `cells`，失敗就回復，原結果完全不動），並清掉舊的個人／時段標示圖。
`DemoApp.upgrade_runs()`（最新的先，已是最新版的略過，失敗的留下 `regrid-v2.failed` 標記、不會每次啟動重試）有兩個入口：
LINE 機器人啟動時的背景執行緒（`LINE_V2_REGRID_LIMIT`，預設 20，`0` 關閉），以及 `python -m all_staff.cli reanalyze`
（`--run`、`--limit`、`--force`）。重讀後的個人標示圖與時段圖檔名多一個 `-g2`，LINE 才不會繼續顯示快取的舊圖。

**限制**

- 真正合併成 ≥4 格、≥2 欄的正式勤務格（沒在 30 張照片中出現過）會被當成備註忽略。
- 備註文字本身（「72.73 防爆訓練」之類）被忽略，不會出現在任何查詢結果；若要顯示，需要另外 OCR 那個方塊。
- `9_23` 最上面幾列的最右欄（c36）超出校正後的畫布，最右邊那條線的支撐也不足，這張照片的 c36 不可靠。
- `查看時段` 的區域仍只有 c02–c16（國際）、c17–c30（國內）。

### 上傳模擬（輸入一張班表 → 輸出辨識結果）

```bash
python -m all_staff.cli demo          # 開啟 http://127.0.0.1:8776
```

可以拖曳、貼上或選擇一張班表照片。流程跟正式上傳相同：透視校正、格線追蹤、切出 19×35 context 格（逐格透視拉正）、行切分、line OCR。
一張照片在 CPU 上大約 13–20 秒。不會送出 LINE、Discord 或 OpenAI 請求，也不影響 V1。

- 格子：整張表的辨識結果。點一格可以看原圖、文字行和信心。
- 人員班表：列出每個代號的時段和勤務名稱，可以複製成文字。全部時段都低信心的人（多半是雜訊）排在最後並淡化。
- 格線警告：某些時段的格高跟其他時段差很多時，會提示格線可能定位錯了（例如 9_24.jpg 的 20–24 點）。
- JSON：下載完整結果，內含 `people` 欄位，可以直接接 Bot 的格式。
- 歷史紀錄：每次上傳都存在 `reports/all_staff_demo/runs/<id>/`，可以從下拉選單重新開啟。

勤務名稱取自 `configs/phase1_roster_template.local.json` 的 `column_map`。缺少的欄位可以寫在
`configs/all_staff_columns.local.json`，格式是 `{"11": "國際三線", ...}`。

### LINE 班表機器人（V2）

```bash
python -m all_staff.cli line-sim     # 本機 LINE 模擬器 http://127.0.0.1:8779
python -m all_staff.cli line-bot     # 正式 webhook（需要 LINE_V2_CHANNEL_ACCESS_TOKEN / SECRET）
```

邏輯寫在 `all_staff/bot.py`，模擬器和正式 webhook 共用同一份。模型預設使用最新訓練的 `models/all_staff/line-*/recognizer.pt`。

| 指令 | 作用 |
|---|---|
| `設定 72` | 綁定自己的代號（全形也可以；一人一個代號，在各個聊天室共用） |
| `我的班表` | 一張卡片：下個小時（與現在）的勤務、今天每一段勤務、明天的班表（還沒上傳也會說）；同一勤務的連續時段會合併。`今天班表`、`明天班表`、`下個時段` 也是這張；輸入日期（`9/30`、`今天`）看那天的個人卡片 |
| `查 72`（1 對 1 也可以只打 `72`） | 查某個代號的勤務 |
| 傳班表照片 | 辨識整張表（約 15 秒）。私訊傳的直接回覆結果；群組裡傳的不在群組回覆，結果回報給管理員（`LINE_V2_ADMIN_USER_IDS`；預設放進管理員的收件匣），開了通知的人另外私訊自己的勤務（只在推播開啟時） |
| `通知 開／關` | 群組有新班表時，私訊自己的班表。整個功能預設停用（`LINE_V2_PUSH=1` 和 `LINE_V2_NOTIFY=1` 才開）：私訊是 push，會用掉官方帳號的每月額度；停用時 `通知 開` 只回覆說明 |
| `收件匣`（`辨識回報`、`我的訊息`） | 預設不推播：要給你的訊息（辨識回報、姓名申請的結果、給上傳者的提醒）放在收件匣，跟著下一次回覆送出；這個指令直接看。管理員選單的「辨識回報」按鈕就是它 |
| `本月統計` | 用圖表看自己這個月排定的時數、上班天數與各地點時數（只看得到這個月） |
| `人員管理` | 幹部和管理員：增刪人員名冊、固定代號（役男的中文代號、英文代號）與幹部；管理員的卡片另有「姓名申請」「意見列表」兩個方塊（README「人員管理（幹部）」） |
| `意見 內容` | 把問題或建議連同姓名交給管理員（`LINE_V2_ADMIN_USER_IDS`） |
| `日期 月/日` | 更正最新這張班表的日期（讀錯或讀不到時，「月/日」換成班表上印的日期）；設定管理員後，群組班表的日期只有管理員能改，在那天的班表卡片點「📅 改這張的日期」 |
| `解除`、`說明` | 取消綁定、顯示說明 |

- 回覆班表時會附一張照片：校正後的整張班表（全寬，裁到此人最後一個勤務的下一列），此人的每一格都用藍框 `(37, 99, 235)` 標出，和 V1 四人班表的標示圖相同。低信心的格子也會框出，對照文字中的「？」確認。
  - 格線位置在切格時存成 `cells/corrected/<group>.jpg`、`cells/geometry/<group>.json`；照片第一次被要求時才產生，存在 `runs/<id>/people/<代號>.jpg`（原圖 ≤2400 px）與 `-preview.jpg`（≤1000 px）。
  - 需要 `LINE_PUBLIC_BASE_URL`（LINE 只接受 https 圖片網址）；沒設定時只回文字。這個功能加入前的舊紀錄沒有格線資料，也只回文字。
- **班表日期**：從表頭「2026年10月1日星期四」讀取（`all_staff/header_date.py`，用原圖同一個透視矩陣往上多取一段表頭，Tesseract `chi_tra`；星期幾要和日期相符才採用）。9/2–9/27 共 24 張照片全部讀對。讀不到時：沒有設定管理員就照常通知（顯示「日期未辨識」），並私訊上傳者一個「📅 選這張的日期」日期選擇器，只改那一張；設定 `LINE_V2_ADMIN_USER_IDS` 後先不通知，只私訊管理員確認日期，確認後才通知（README「管理員：日期讀不到時先確認再通知」）。
- 群組裡的一般聊天，機器人不會回應。
- 1 對 1 聊天時，使用的是你所在群組的最新班表；沒有的話就用最新上傳的班表。
- 勤務名稱依表頭整理在 `configs/all_staff_columns.json`。
- 綁定資料存在 SQLite：模擬器在 `reports/line_sim/`，正式版在 `data/line_v2/`。

**模擬器**

- 左側可以切換使用者、建立群組、邀請成員。
- 右側開發者面板會顯示送給機器人的 LINE webhook 事件，以及機器人呼叫的 reply／push API。
- 回覆裡的「整張表」連結會開啟該張班表的檢視頁；藍框標示圖點開可看原圖。

**訊息額度**：辨識結果用 Reply API 回覆（免費，reply token 一分鐘內有效；辨識約 15 秒）。
push 以「收件人數」計：推到 30 人的群組算 30 則，免費的輕用量方案每月只有 200 則，所以機器人預設完全不推播
（`LINE_V2_PUSH`，README「不推播」）：辨識超過時限時先回一句「還在辨識」，結果放進收件匣，下次回覆時一起送出；
給管理員的辨識回報、姓名申請等也一樣。自動通知預設停用，而且要推播開啟才能用（`LINE_V2_NOTIFY`，README「自動通知」）。1 對 1 傳照片時會顯示 LINE 的「輸入中」動畫；群組沒有這個 API，大約 15 秒後直接收到結果。

#### 正式部署（Docker Compose + 既有的 Cloudflare Tunnel）

V2 使用**另一個 LINE 官方帳號**（另一個 Messaging API channel）。原因：一個 channel 只能設一個 webhook，
V1 四人班表機器人維持不變；V2 另有自己的好友名單與訊息額度。

1. 在 [LINE Official Account Manager](https://manager.line.biz/) 建立新的官方帳號（例如「班表小幫手」），
   然後到 **設定 → Messaging API** 啟用，Provider 選和 V1 同一個。
   - **回應設定**：開啟 Webhook，關閉「自動回應訊息」和「加入好友的歡迎訊息」（由機器人自己回覆）。
   - **帳號設定**：開啟「允許被加入群組」，否則無法邀請進群組。
2. 在 [LINE Developers Console](https://developers.line.biz/console/) 找到這個 channel：
   - Basic settings → **Channel secret**
   - Messaging API → **Channel access token (long-lived)** → Issue
3. 在 Cloudflare Zero Trust → Networks → Tunnels，打開 V1 正在用的同一個 tunnel，新增 **Public Hostname**：
   例如 `roster.example.com`，Service：`HTTP`，URL：`work-schedule-line-v2:8081`。
4. 伺服器的 `.env` 加上（見 `.env.example`；名稱都有 `V2`，不會和 V1 衝突）：
   ```
   LINE_V2_CHANNEL_ACCESS_TOKEN=…
   LINE_V2_CHANNEL_SECRET=…
   LINE_V2_PUBLIC_BASE_URL=https://roster.example.com
   LINE_V2_RECOGNIZER=/app/models/all_staff/line-v4/recognizer.pt
   ```
5. 模型不在 git 裡，要先複製到伺服器同一路徑：
   ```bash
   scp models/all_staff/line-v4/recognizer.pt <伺服器>:<專案>/models/all_staff/line-v4/
   ```
6. 在伺服器上：
   ```bash
   git pull
   ./linebot_v2_restart.sh
   docker compose logs --follow work-schedule-line-v2   # 看到「使用模型：…」和 Uvicorn running
   curl https://roster.example.com/line/health
   ```
7. 回到 LINE Developers Console → Messaging API → Webhook URL 填 `https://roster.example.com/line/callback`，
   按 **Verify**，並開啟 **Use webhook**。
8. 用手機加好友，輸入 `設定 你的代號`，傳一張班表照片測試。

- 綁定資料與每次辨識的照片、格子在伺服器的 `data/line_v2/`（`bot.sqlite3`、`runs/`）。
- 換模型：把新的 `recognizer.pt` 放進 `models/all_staff/`，改 `LINE_V2_RECOGNIZER`，再執行 `./linebot_v2_restart.sh`。
- V1 的 `linebot_restart.sh` 不會啟動 V2；兩個機器人可以分開重啟。

**上線前待辦**

- 設定 `LINE_V2_PUBLIC_BASE_URL`（https）才會附「整張表」連結和藍框標示圖。注意：拿到連結的人都能看到整張班表。
- 1 對 1 聊天中，如果使用者不在任何有班表的群組，會用「所有聊天室中最新的一張班表」。若機器人會給不同單位使用，要改成只看自己群組的班表。
- 辨識不到日期，所以回覆中顯示的是上傳時間。
- `#` 兼服記號目前不區分。
- 浮動視窗與備註文字（例如 10/5 的「丁帶領替代役新生認識環境 17-19」）會被忽略，不出現在任何查詢結果；
  跨 4 格以上、2 欄以上的合併勤務格也會被當成備註（30 張照片沒出現過，見「最後四欄與浮動視窗」）。

### 訓練監控（在 Mac M1 上訓練）

```bash
python -m all_staff.cli prepare-context      # 先建 context 資料集（train-lines 會檢查）
# 快速版：從 line-v2 接續，約 20–40 分鐘
python -m all_staff.cli train-lines --monitor --init models/all_staff/line-v2/recognizer.pt \
  --pretrain-epochs 3 --finetune-epochs 8 --synth-samples 40000 --output models/all_staff/line-v3/recognizer.pt
# 從頭訓練（預設 60000 合成樣本 × 10 輪＋微調 8 輪）
python -m all_staff.cli train-lines --monitor --output models/all_staff/line-v3/recognizer.pt
```

`--monitor` 會開啟 http://127.0.0.1:8778，畫面包含：
- 整體進度與預估剩餘時間
- 每一步的 loss（對數尺度，含平滑線）、每輪驗證正確率、學習率
- 每輪紀錄、最近一次驗證的錯誤
- 設定與資料量

「停止訓練」會在當前這一步結束後停止，並保留最佳 checkpoint。訓練結束後網頁仍會開著，按 Ctrl+C 離開。
另開的訓練也可以用 `python -m all_staff.cli monitor --run models/all_staff/line-v3/recognizer.json` 監控。

- 裝置：預設 CPU。可以試 `--device mps`，完成時網頁會顯示「CPU 重新載入驗證」是否一致；不一致就改用 CPU 重訓（2026-09-28 曾發生 MPS 存檔重載不一致）。
- 輸出檔已存在時，會先備份成 `.bak-時間`。

### 推論檢視網頁

```bash
python -m all_staff.cli viewer                 # 全部班表；--split val,test 或 --sheet 820e94ac 可縮小範圍
open reports/all_staff_viewer/index.html       # 靜態頁，直接開即可，不需伺服器
```

每格顯示本格縮圖與辨識出的代號。綠框表示與人工標註一致，橘底表示不符，紅色虛線表示需複核。
點格子會在右側顯示 context 原圖、本格範圍、文字行框、各代號框與信心。
可以搜尋代號或姓名，列出此人在這張表的所有時段；也可以拖動信心門檻，即時重算需複核的格子。
圖片以相對路徑引用 `data/all_staff_ctx/images`，所以報告只有 `index.html` 和 `data.js`。
`reports/` 已加入 .gitignore。

### 第一版結果（`models/all_staff/line-v2/`，雲端 CPU 訓練約 25 分鐘）

同一批 val／test 格子（只涵蓋 05–14 點的已複核格）：

| 模型 | val 格子 P／R | val 整格全對 | test 格子 P／R | test 整格全對 |
|---|---|---|---|---|
| 舊 best-checkpoint-jsw0h38j | 0.44／0.53 | 68% | 0.54／0.58 | 75% |
| line-v2 | 0.97／0.98 | 98% | 1.00／1.00 | 100% |

val 剩下 4 個錯誤中 3 個是標註錯誤：`78c0047a6ecf7f81_r05_c17` 標成 H，實際是 4；
`e70f4c5fde1802f8_r05_c04` 標成巳，實際是己；`78c0047a6ecf7f81_r06_c13` 標成空白，實際有 57。
14–24 點尚無標註，只做過整張 test 照片目視抽查。仍建議先補 3–5 張完整照片作為固定測試集，再考慮上線。

### 10 月驗證與 line-v4（2026-10-18）

**怎麼驗證的**

- `data/schedules` 的 46 張照片，用正式環境同一條路徑（`prepare_context` → `sheet_payload`）重讀，比較 line-v2、line-v3。
- 27 張照片的每一格都建立了正確答案（17,955 格）：10/1–10/19 全部 20 張、9/27、9/29、9/30，加上舊 test／val 的 9/13、9/15、9/19、9/21。
  - 先由多個 agent 逐格盲讀，看不到模型讀值。
  - 盲讀和模型不同的格子，再由兩位裁判各自判定；兩人意見不同時找第三位。
- 盲讀的準確度：和 4 張舊照片的 372 個人工複核格比對，6 處不同全是舊標註錯了。
  - 3 處是上面記過的。
  - 另外 3 處是 `78c0047a6ecf7f81_r04_c29`、`_r05_c29`、`_r09_c29`：標成空白，實際是 2、2、11。
- 9/11、9/20 在 `EXCLUDED_SOURCE_GROUPS`，不切格。

**line-v3（當時的正式模型）錯在哪裡**

10 月 20 張共 13,299 格：整格全對 99.60%，53 格錯，平均每張 2–3 格，錯誤會出現在辨識回報的「沒有這個人」「信心較低」。

- **10 月才常見的代號。** 丑、子、癸、辛、壬讀錯，例如丑 → 丁L、癸丑 → 未子乙。字母組合也會錯，例如 EF → F。9 月的人工標註完全沒有 E、G、A、N、辛、辰⋯，丑 5 個、癸 1 個。
- **不是代號的記號。** `*L`（半點起班）的 `*` 讀成丁；`#H` 讀成空白；最後一列的格線殘段讀成 I／1。
- **小型備註讀成代號。** 例如 10/14 的「程式設計」「線上待命」。大型浮動視窗則有被正確濾掉。
- **跨格印的代號切行錯。** 例如 10/16 13–14 的庚丑橫跨兩格，讀成 575、辰丁H。這是切行的問題，重訓救不了。

**line-v4**

- 從 line-v3 接續，只做真實資料微調；最佳在第 3 輪，4 核 CPU 約 1 小時：
  ```bash
  python -m all_staff.cli train-lines --dataset <資料集> --init models/all_staff/line-v3/recognizer.pt \
    --pretrain-epochs 0 --finetune-epochs 8 --synth-samples 100000 --real-repeat 3 --batch-size 128 --workers 2 \
    --output models/all_staff/line-v4/recognizer.pt
  ```
- 資料集 = `data/all_staff_ctx`（扣掉上面 6 個錯誤標註）＋ 23 張新照片已驗證的格子。
  - 每條驗證過的文字行，對到切行結果同位置的那一行，再用 `split_line` 給每個代號一個框。
  - 訓練行從 2,043 條增加到 19,761 條。
- 先用同樣做法只拿 9/27–10/9 訓練（候選 A），在完全沒看過的 10/10–10/19 比較，確認做法有效：

| 10/10–10/19（6,650 格） | line-v2 | line-v3 | 候選 A |
|---|---|---|---|
| 錯的格子 | 119 | 26 | 17 |
| 信心較低（每張） | 37.7 | 6.2 | 2.4 |
| 不可能的代號（辰、575⋯） | 1 | 4 | 0 |

- 候選 A 和 line-v3 不同的格子：A 對 11 格、v3 對 2 格（McNemar p ≈ 0.013）。
- 正式的 line-v4 再加上 10/10–10/19 一起訓練。依雜湊，10/11、10/15 屬 test 不會被訓練，所以仍可和舊 test 的 9/13、9/21 一起當乾淨的測試集：

| 沒訓練過的 4 張（2,660 格） | line-v3 | line-v4 |
|---|---|---|
| 錯的格子 | 7 | 2 |
| 信心較低（每張） | 6.0 | 0.8 |

- line-v4 也通過 `RealDigitPhotoTests`。剩下的錯誤大多是跨格代號與備註（切行），以及一個手寫的筆劃。

**不是辨識模型的問題**

- **日期未辨識。** 46 張照片的表頭日期全部讀得出來，每張約 2 秒。表頭 OCR 和格子、姓名 OCR 同時跑，伺服器忙的時候會超過原本的 10 秒上限，然後默默放棄。現在改成單執行緒、20 秒，逾時會記在 log。
- **姓名。** 名冊完整時，23 張照片 98.1% 的姓名配對正確，沒有配錯人；其餘只是沒配到。`configs/staff_names.txt` 少了 10 月表上的 10 個姓名，名冊不完整時只剩 94.3%。
- **10/16 12:32 那張（168 格信心較低、姓名 22/41）。** 那張照片的表格定位錯了，格子讀到表上別的區域，換模型也沒用。

## 1. 建立隔離資料集

原始照片放在 `data/schedules/`。這一步重用既有透視校正與固定格線切格，但輸出到
獨立目錄：

```bash
python -m all_staff.cli prepare
```

它會建立全表 V2 template、`data/all_staff/images/` 與 `annotations.jsonl`。重新執行會保留已存在的
框選標註。資料切分以完整原始照片的 hash 決定，單一照片的格子不會洩漏到不同 split。

## 2. 框選及複核

先讓本機 Tesseract 去除格線、尋找代號框並產生文字建議：

```bash
python -m all_staff.cli auto-label --workers 8
```

這會直接完成沒有墨跡的高信心空白格；有內容的格子保存為未複核建議。若調整了
自動標註規則，可用 `--replace-proposals` 重建機器建議，人工來源不會被覆寫：

```bash
python -m all_staff.cli auto-label --workers 8 --replace-proposals
```

```bash
python -m all_staff.cli label
```

瀏覽器預設開啟 `http://127.0.0.1:8775`。右側會列出全部 108 個人員代號；可搜尋
代號或 `all_staff_roster.local.json` 中的姓名。先選員工，再於每格拖曳一個緊密但完整
的框，代號會自動帶入。同格多人就建立多個框。非人員數字標成 `other`；模糊內容標成
`unknown`；被切斷的字標成 `partial`。確認沒有代號的格子使用「複核為空白」，空白格
也是偵測器必要的負樣本。

網站預設只載入「待審核」佇列；接受後該格會立即消失。可切換「低信心」、
「已複核」或「全部」。快捷鍵為 `A` 接受／儲存、`E` 空白、`S` 跳過及左右方向鍵。
先抽查機器建議品質後，可按「接受全部 ≥95%」批次核准有效且高信心的建議。

每一格載入後會自動聚焦並全選「直接輸入人員代號」。看到 `72` 時直接鍵入 `72`
再按 Enter，即可修正目前框並接受；不必先點右側代號按鈕。若自動框漏掉代號，先輸入
代號、再於圖片拖曳框選。待審佇列會交錯不同日期，讓第一批種子標籤涵蓋獨立照片。

自動建議會保存 `source=tesseract` 與 `confidence`；人工修改或逐格接受後保存為
`source=human`。訓練仍只讀取 `reviewed=true`，低信心建議不會自行混入訓練資料。

查看完成度與各代號數量：

```bash
python -m all_staff.cli status
```

## 3. 設定姓名字典

```bash
cp configs/all_staff_roster.example.json configs/all_staff_roster.local.json
```

將值補成經人工確認的姓名。此 local 檔已被 `.gitignore` 排除，避免把人員資料提交。
更換姓名或新增人員時只修改字典；只要新代號由現有字元構成，辨識器輸出層不需新增
「姓名類別」。

## 4. 訓練

建議採反覆的主動學習流程，而不是一次手標全部：

1. 先人工複核約 200–500 個有代號的格子，並涵蓋多個日期及常見數字。
2. 確認 `status` 的 train 與 val 都有人工正樣本。
3. 訓練第一版 detector 與 recognizer。
4. 用新模型取代未複核的 Tesseract 建議。
5. 只審低信心／錯誤結果，再重新訓練。

```bash
python -m all_staff.cli train-detector
python -m all_staff.cli train-recognizer
```

以剛訓練完成的模型重做未複核建議；人工與已複核資料不會被覆蓋：

```bash
python -m all_staff.cli model-label
```

`model-label` 預設清除舊的非人工建議，再寫入 `source=model` 與模型信心。早期模型未
偵測到內容的格子仍保持待審，不會自動當成空白。若只想試跑，可加 `--limit 200`。

若要在預標註時繼續人工審核，使用線上合併工具（標註服務須已啟動）：

```bash
python -m all_staff.model_label_online --dataset data/all_staff \
  --models models/all_staff/best-checkpoint-jsw0h38j
```

此工具使用 CPU，每 50 格透過 `/api/model-proposals` 與人工儲存共用鎖写入。
寫入前逐格核對原始內容，只更新尚未複核且不含人工標註的格子；並行修改會跳過。
模型框一律為 `source=model`、`reviewed=false`，空預測也不視為已複核空白。
會先備份完整 manifest，並在模型目錄產生 `prelabel-*.json` 進度報告。

輸出分別為 `models/all_staff/detector.pt` 及 `recognizer.pt`。訓練會拒絕缺少已複核
train/val 標註的資料集。兩個 checkpoint 都帶有 V2 kind 標記，推論器會拒絕載入既有
四人模型，避免誤覆蓋或混用。

以完整代號框 IoU ≥ 0.5 且文字完全相同計算端到端 precision／recall：

```bash
python -m all_staff.cli evaluate --split test
```

## 5. 離線推論

### 人工種子資料與第一版模型（2026-09-28）

本次保留「已複核」清單依 `(row, column, source_group)` 排序的前 1,620 格，
其中含未知標註的 1 格退回待審，得到 1,619 格種子資料。
低信心沿用網站定義：`unknown` 或非空 confidence 小於 0.8。
其他資料只更改 reviewed 狀態，保留所有框與文字；原始資料備份於
`data/all_staff/backups/before-seed-20260928-172917-903371.jsonl`。

固定訓練快照為 `data/all_staff/training_snapshots/seed-20260928-172917-903371/`，
訓練、驗證、測試分別為 1,295／160／164 格；代號框為 745／78／83 個。
獨立模型輸出為 `models/all_staff/seed-20260928-172917/recognizer.pt` 與 `detector.pt`，
同目錄 JSON 包含訓練進度與驗證指標。不變更現有四人模型，也不自動接受預標註。

初次 MPS 辨識器雖回報驗證 76.9%，重新載入 checkpoint 後驗證僅 6.4%，不可使用。
另訓練 `recognizer_cpu.pt`，並由 `all_staff.verify_seed_run` 在訓練完成後驗證
存檔重載一致性及獨立測試集，產生 `verification.json`。報告完成前不可將模型
當成已驗證可用，也不應大量回標；初版推論驗證使用 CPU。

重分類工具（執行前暫停標註服務，避免檔案寫入競爭）：

```bash
python -m all_staff.prepare_seed --dataset data/all_staff --keep 1620
```

未出現或樣本很少的代號不應被視為已有可靠辨識能力；第一版只供預標註及人工校正。

```bash
python -m all_staff.cli analyze /完整路徑/班表.jpg \
  --output /tmp/all-staff-result.json
```

低於信心門檻、代號不在允許範圍或字典查不到姓名時，結果會標成
`needs_review: true`。在 V2 通過跨月份測試以前，不應把結果接入 Bot 的正式回覆。

## 建議上線程序

1. 至少蒐集 30–50 個不同日期；每個活躍代號至少 50 個正樣本。
2. 以完整照片分組的 test split 評估框偵測 recall、代號 exact match 與每人班表結果。
3. 先以 shadow mode 執行，V1 照常回覆，V2 只保存比較結果。
4. 達到每人 recall 95%、precision 98% 且跨月份穩定後，再新增獨立 V2 API。
