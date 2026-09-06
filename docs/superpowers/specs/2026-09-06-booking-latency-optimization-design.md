# 搶場地延遲優化設計

日期：2026-09-06
狀態：待實作

## 1. 背景

目前每週四 00:00 搶中正運動中心羽球 7-4（`QPid=1199`）的 20:00 與 21:00 兩個時段，
近期持續失敗，log 顯示「預約失敗」，也就是回應含 `PT=1&X=2`。

`X=2` 代表請求**有送到、也被伺服器受理**，只是那格在我們之前就被訂走。因此問題不是
程式壞掉，也不是時間差到觸發「尚未開放」，而是**送達得不夠早**。

使用者已確認：只要羽球 7-4，不接受其他場地。因此擴大目標場地（多 QPid 並發）不在
本設計範圍內，唯一的槓桿是純粹的送達時間。

## 2. 目標與非目標

### 目標

1. 消除開搶瞬間所有非必要的延遲（DNS、TCP、TLS 握手、Python 準備工作）。
2. 把「本機時鐘 vs 預約伺服器時鐘」的偏差從**未知**變成**量測值**。
3. 讓請求**抵達伺服器**的瞬間剛過開放時刻，而不是**送出**的瞬間等於開放時刻。
4. 產出儀表數據，讓往後的調整有依據，不再盲調。
5. 降低（而非提高）帳號被網管注意到的風險。

### 非目標

- 多場地並發（使用者只要 7-4）。
- 失敗後重試（那格已經沒了，重試無用且像在敲門）。
- 部署到雲端 VM（來源 IP 從住宅變機房，反而更顯眼）。
- 更換執行環境或網路（家用 Wi-Fi 的抖動屬於使用者可自行處理的範圍，程式不介入）。

## 3. 延遲來源與可消除性

| 來源 | 估計 | 處理方式 |
| --- | --- | --- |
| 本機與伺服器時鐘偏差 θ | 未知，可能達數百 ms | 5.2 量測 |
| DNS + TCP + TLS 握手（冷連線） | 50～150 ms | 5.4 預熱 |
| Python 組 URL / 建 coroutine / gather | 1～5 ms | 5.4 預先建立 task |
| 送出到抵達的單程飛行時間 | RTT/2，約 10～15 ms | 5.3 提前送出補償 |
| Wi-Fi 抖動 | 10～50 ms，每次不同 | 不處理（非目標） |
| 實際傳輸 RTT | 10～30 ms | 物理下限，不可消除 |

## 4. 時序總覽

```
啟動
  ├─ 讀取使用者輸入（沿用現況）
  └─ NTP 校時（time.stdtime.gov.tw，不碰預約網站）→ θ_ntp

count_down 至 T-3min
  └─ Selenium 登入 → 取得 cookies

建立 aiohttp.ClientSession（明確 connector + 瀏覽器 headers + cookies）

T-3min ~ T-30s：伺服器時鐘量測
  └─ 約 8 次唯讀列表頁探測，間隔隨機 12~25 秒
     每次用 Date header 對 θ 做二分逼近
     副產品：RTT 樣本、keep-alive 行為觀察
  → θ_srv 與其不確定度 u

T-10s：連線預熱
  └─ 併發 2 個唯讀 GET（列表頁 + 登入頁）→ pool 中留下 2 條熱連線
  └─ 順便解析列表頁，記錄目標格子在開放前的狀態（前提驗證）
  └─ 預先組好 2 個 booking URL，建立 2 個 task，各自 await 同一個 asyncio.Event

count_down 至 t_send（見 5.3）
  └─ event.set() → 2 個 task 立刻送出

T+2s：事後偵察
  └─ 再取一次列表頁，記錄 7-4 整列的最終狀態

登出（沿用現況）
```

## 5. 元件設計

### 5.1 `badminton_bot/utils/ntp_client.py`（新檔）

與 `time.stdtime.gov.tw` 對時，取得 `θ_ntp = 本機時鐘 − 標準時間`（秒，正值代表本機快）。

- 自行實作約 30 行的 SNTP client（`socket` + `struct`），**不引入 `ntplib`**，
  以免動到 `requirements.txt` 與 PyInstaller 打包設定。
- 取樣 3 次，取往返延遲最小的那次（NTP 標準做法）。
- 每次 socket timeout 3 秒；整體必須在進入 T-3min 倒數之前完成。
- 失敗（逾時、DNS 失敗、封包格式錯）→ 記 warning、回傳 `None`，**絕不中斷搶場地**。

純函式（可離線測試）：

```python
def parse_ntp_response(packet: bytes, t0: float, t3: float) -> tuple[float, float]:
    """回傳 (offset, delay)，皆為秒。offset = 本機 − 伺服器。"""

def pick_best_sample(samples: list[tuple[float, float]]) -> float:
    """從 (offset, delay) 樣本中取 delay 最小者的 offset。"""
```

### 5.2 `badminton_bot/utils/server_clock.py`（新檔）── 量測預約伺服器的時鐘

這是本設計的核心。定義 `θ = 本機 epoch − 伺服器 epoch`（正值代表本機快）。

#### 單次探測能推出什麼

在本機時間 `t0` 送出、`t3` 收到，回應的 `Date` header 為整數秒 `D`（epoch）。
`Date` 是在 `[t0, t3]` 之間某一刻蓋章的，蓋章當下伺服器時鐘讀值落在 `[D, D+1)`：

```
θ ∈ ( t0 − D − 1 ,  t3 − D ]
```

區間寬度為 `1 + RTT`。單次探測不夠用。

#### 用相位選擇做二分逼近

設目前已知 `θ ∈ (lo, hi]`，`mid = (lo + hi) / 2`。
探測在本機時間 `t` 送出時，伺服器時鐘讀 `t − θ`，因此 `D = floor(t − θ)`。

若挑選 `t` 使得 **`t − mid` 恰為整數 `n`**（亦即 `t ≡ mid (mod 1)`），則：

- `θ < mid` → `t − θ > n` → `D ≥ n`
- `θ > mid` → `t − θ < n` → `D ≤ n − 1`

回應落在哪一邊，就把區間對半砍。送出時刻只受**小數相位**約束，落在第幾秒完全自由，
因此可以和「間隔隨機 12～25 秒」的排程自由組合。

下一次探測時刻取 `t = ceil(t_earliest − mid) + mid`。

#### 收斂

因為 RTT 的存在，切點只精確到 ±RTT，所以每次砍半是近似的，下限就是 RTT：

```
探測 1 → 1.00 s    探測 5 → 0.063 s
探測 2 → 0.50 s    探測 6 → 0.031 s
探測 3 → 0.25 s    探測 7 → 0.016 s ← 已觸及 RTT 地板
探測 4 → 0.125 s
```

**7～8 次即收斂到約一個 RTT（20～30 ms），再多打沒有任何資訊增益。**
請求數量的上限是數學性質，不是自訂的節制。

停止條件：`hi − lo ≤ max(2 × rtt_median, 0.02)` 或探測次數用盡。
最終 `θ̂ = (lo + hi) / 2`，不確定度 `u = (hi − lo) / 2`。

#### 樣本有效性

- 回應含 `Age > 0` 或 `X-Cache: HIT` → 讀到快取，`Date` 是舊的，**丟棄該樣本**。
- 請求帶 `Cache-Control: no-cache`（等同人類按重新整理）。
- 記錄 `Server` / `Via` / `X-Powered-By`，判斷前方是否有反向代理。有的話 `Date`
  可能來自代理而非應用伺服器 —— **不阻斷流程，但把判斷結果印進 log**。

#### 交集為空

若新約束與目前區間交集為空（伺服器時鐘被 step、RTT 暴衝、讀到快取），
代表某個前提破了：捨棄累積區間，改以這次探測自身的區間重新開始，記 warning。
發生 2 次以上 → 放棄伺服器量測，退回 `θ_ntp`。

#### 純函式（可離線測試）

```python
def constrain(t0: float, t3: float, server_date_epoch: float) -> tuple[float, float]:
    """單次探測推出的 θ 區間 (lo, hi]。"""

def intersect(current: tuple[float, float], new: tuple[float, float]) -> tuple[float, float] | None:
    """區間交集；為空回傳 None。"""

def next_probe_phase(interval: tuple[float, float], earliest: float) -> float:
    """回傳下一次探測應該送出的本機時刻。"""

def parse_http_date(value: str) -> float:
    """RFC 7231 HTTP-date → epoch 秒（用 email.utils.parsedate_to_datetime）。"""
```

### 5.3 送出時刻計算 ── `badminton_bot/utils/timing.py`（新檔）

**要讓請求抵達伺服器的瞬間剛過開放時刻，而不是送出的瞬間等於開放時刻。**

```
t_send = T_nominal + θ − rtt_median / 2 + margin + manual_offset
```

- `T_nominal`：名目開放時刻的 epoch（由 `datetime.timestamp()` 取得）。
- `θ`：優先序 **θ_srv（量測） > θ_ntp（NTP） > 0**。
- `rtt_median`：探測階段收集到的 RTT 中位數。
- `margin`：**預設等於量測不確定度 `u`**。理由：送早是硬失敗（伺服器回「尚未開放」），
  送晚只是可能輸 —— 兩邊不對稱，所以偏晚。
- `manual_offset`：使用者輸入的毫秒偏移，沿用現況，作為人工微調旋鈕。

**安全 clamp**：自動修正量 `|θ − rtt/2 + margin|` 必須 ≤ 2.0 秒，超出則拒絕套用、
退回 `θ_ntp`，並大聲記 log。防止量測爆掉導致整場歪掉。

所有時鐘運算一律走 epoch float（`time.time()`），避免時區與 naive datetime 的坑。
假設：量測期間（約 3 分鐘）本機不會發生 NTP step。

```python
def compute_send_time(
    nominal_epoch: float,
    theta: float,
    rtt_median: float,
    margin: float,
    manual_offset_ms: int,
) -> tuple[float, bool]:
    """回傳 (送出時刻 epoch, 自動修正是否通過 clamp)。"""
```

### 5.4 連線預熱與預先掛載

#### connector 設定

```python
aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, keepalive_timeout=60, ssl=False)
```

**`keepalive_timeout` 必須調大**：aiohttp 預設 15 秒，短於探測階段的隨機間隔（12～25 秒）。
沿用預設值的話，連線會在兩次探測之間被回收，每次探測都得重新握手 ——
量到的 RTT 會被握手成本汙染，無法代表開搶時那條熱連線的真實往返時間，
連帶讓 5.3 的 `rtt_median / 2` 補償失準。

`ssl=False` 沿用現況（關閉憑證驗證），順便省下 TLS 握手時的驗證成本。

#### 預熱

T-10s 併發 2 個唯讀 GET（列表頁 + 登入頁），使 pool 中留下 2 條熱連線，
供 2 個 booking 請求各用一條。兩個不同頁面併發載入是一般瀏覽行為。

選 T-10s 而非更早：預熱本身只需約 200 ms，而間隔越短，
連線變成 half-open（伺服器已單方面關閉、本機尚未察覺）的機會越低。

記錄回應的 `Connection` 與 `Keep-Alive: timeout=N` header 並印進 log。
若伺服器回 `Connection: close`，代表預熱無效 —— **要讓使用者直接看見，不能被蒙在鼓裡**。

#### 預先掛載

在預熱之後、倒數之前：

1. 兩個 booking URL 事先組好（純字串）。
2. 建立 2 個 task，各自 `await ready_event.wait()` 後立刻 `session.get(prebuilt_url)`。
3. 倒數結束後只做 `ready_event.set()`，再 `await asyncio.gather(...)`。

如此 task 建立、URL 組裝、coroutine 配置全都發生在截止時刻之前。
誠實說明：這一項省下的只有 1～5 ms，是整個設計中最小的一項，但成本趨近於零。

### 5.5 前提驗證與事後偵察

**這關係到整個方案的前提是否成立。**

`X=2` 無法區分「被別人搶先 20 ms」與「那格在開放前就已經不是可訂狀態」。
若羽球 7-4 的 20:00 早就被月租、固定團體或內部管道佔走，所有優化的收益是零。

- **開放前**（併在 T-10s 的預熱請求裡，**零額外請求**）：解析列表頁，記錄目標格子狀態
  （`img/place01.png` 可訂 / `img/place02.png` 已訂 / 其他）。
  註：目標日期在開放前尚未進入預約窗口，列表頁可能不渲染該日格子。
  **不對結果做任何分支**，原始判讀寫進 log 供事後檢視即可。
- **開放後**（T+2s，1 個額外請求）：再取一次列表頁，記錄羽球 7-4 整列的最終狀態。
  這是「輸幾毫秒」與「根本沒開放給你」之間唯一的判別依據。
  一個人搶完刷新看結果也是完全自然的行為。

### 5.6 瀏覽器 headers

目前 aiohttp 送出的 User-Agent 是 `Python/3.11 aiohttp/3.11.13`，
網管掃 log 一眼就看得出是機器人。

- User-Agent 抽成**單一共用常數**，Selenium 與 aiohttp 共用，避免兩邊漂掉。
- `ClientSession` 帶上 `User-Agent`、`Referer`（即列表頁，語意上請求真的是從那裡點過來的）、
  `Accept`、`Accept-Language: zh-TW,zh;q=0.9`。

**必須在 spec 裡記錄的取捨**：宣稱是 Chrome 但 TLS 指紋（JA3）、header 順序、
HTTP/1.1 全都是 Python —— 在有指紋偵測的系統眼裡，「說謊被抓到」比「誠實的 aiohttp UA」
更可疑。對一個地方運動中心的 ASP.NET 系統，有這類偵測的機率很低，
故仍選擇偽裝，但這是有意識的取捨而非疏忽。

### 5.7 儀表

啟動與執行過程中以 INFO 等級印出：

- `θ_ntp`、`θ_srv`、不確定度 `u`、兩者是否一致（一致代表對方有做 NTP，可信度高）。
- 探測階段的 RTT 分布（min / median / max）。
- 預熱回應的 `Connection`、`Keep-Alive`、`Server` / `Via`。
- 每一發 booking 請求：**實際送出時刻相對名目開放時刻差幾毫秒**、RTT、
  回應的 `Date`、是否成功。
- 開放前與開放後的目標格子狀態。

用 `time.perf_counter()` 量持續時間，`time.time()` 比對牆鐘。

**沒有這一節，下週的調整還是在盲調。**

### 5.8 倒數改寫

`count_down` 保留在 `main.py`（既有測試在 `tests/test_main.py::TestCountDown`），
但改以 `utils/timing.py` 的 `sleep_then_spin()` 實作：

- 距目標尚遠時以較粗的 `sleep` 等待，最後 5 秒才進 busy-wait。
  現行寫法讓一顆核心滿載 3 分鐘，Mac 筆電在關鍵時刻前熱節流不是好事，
  且精度完全不受影響。留 5 秒而非 2 秒，是為了容忍 macOS `sleep` 的排程 overshoot。
- 修正 log bug：`current_time.microsecond == 0` 幾乎不成立，倒數訊息實際上印不出來。
  改為追蹤「整秒變動時印一次」。
- 修正 `(booking_date - current_time).seconds`：`timedelta.seconds` 遇負值會捲成 ~86400。
  改用 `total_seconds()`。

### 5.9 開發測試模式

dev mode 打的仍然是真實網站。若每次測試都跑完整流程，等於在非開搶時段對站方送出
約 11 個請求，直接違反 CLAUDE.md 的 live-site constraint。

因此 dev mode 預設**跳過** NTP 校時、伺服器時鐘探測與事後偵察，`θ` 取 0，
只保留預熱（2 個請求）與搶場地本身。量測邏輯的驗證一律走第 9 節的離線收斂模擬，
不靠實站測試。

## 6. 現有程式碼改動清單

### `badminton_bot/services/sports_center_webservice.py`

- 新增 class attribute `target_qpid`，加入 `__init_subclass__` 的 `required_attrs`。
  目前 QPid 寫死在 `_generate_booking_url` 字串裡（中正 1199、中山 84），
  提升為類別屬性後，「換場地」變成改一行，也讓列表頁解析能共用同一個值。
- 新增抽象方法：
  - `_generate_list_page_url(year, month, day) -> str` ── 唯讀列表頁（`StepFlag=2`）
  - `_generate_warm_up_urls(year, month, day) -> tuple[str, ...]` ── 預熱用的 2 個 URL
  - `_parse_slot_state(html, hour) -> str` ── 目標場地在該小時的狀態
- `booking_courts()` 改為接受預組 URL 與 `ready_event`，並回傳含時間戳的結果物件
  供儀表使用。

### `badminton_bot/services/zhongzheng_sports_center_webservice.py`

- `target_qpid = 1199`
- 實作新的三個 hook。列表頁 URL 依 memory 已驗證的格式：
  `wd27.aspx?module=net_booking&files=booking_place&StepFlag=2&PT=1&D=YYYY/MM/DD`
- QTime **不補零**（沿用現況）。

### `badminton_bot/services/zhongshan_sports_center_webservice.py`

- `target_qpid = 84`
- 實作新的三個 hook。列表頁 URL 依同平台格式**推論**，
  **未經驗證** —— 須在程式碼註解明確標明。
- QTime **補零**（沿用現況）。

### `badminton_bot/main.py`

- 新增啟動時的 NTP 校時。
- 新增 T-3min ~ T-30s 的伺服器時鐘量測階段。
- 新增 T-10s 預熱、前提驗證、task 預先掛載。
- 倒數目標改為 `compute_send_time()` 的結果。
- `asyncio.gather(..., return_exceptions=True)`（見第 7 節）。
- 新增 T+2s 事後偵察。

### 新檔

- `badminton_bot/utils/ntp_client.py`
- `badminton_bot/utils/server_clock.py`
- `badminton_bot/utils/timing.py`

## 7. 錯誤處理

**最重要的一項**：現行 `asyncio.gather` 預設會把例外往外傳，
所以 20:00 那發若丟 `RuntimeError`（伺服器回了認不得的內容，例如「尚未開放」），
**會連 21:00 那發一起收掉**。這是現有缺陷，但時鐘校正讓它更容易被觸發。

- `asyncio.gather(..., return_exceptions=True)`，逐一檢視結果後才記錄成敗。
- NTP 失敗 → `θ_ntp = None`，繼續。
- 伺服器時鐘量測失敗或交集連續為空 → 退回 `θ_ntp`，繼續。
- 自動修正量超出 ±2 秒 clamp → 退回 `θ_ntp`，大聲記 log，繼續。
- 預熱請求失敗 → 記 warning，繼續（等同回到冷連線，不比現況差）。
- 事後偵察失敗 → 記 warning，不影響已完成的搶場地結果。

原則：**除了登入失敗以外，任何新增環節的失敗都只降級、不中斷搶場地。**

## 8. 請求預算

每次執行對預約網站的請求（不含 Selenium 登入，該部分不變）：

| 階段 | 次數 | 時間分布 |
| --- | --- | --- |
| 時鐘探測 | ~8 | T-3min ~ T-30s，間隔隨機 12～25 秒 |
| 預熱 | 2 | T-10s，併發 |
| 搶場地 | 2 | T，併發（現況已有） |
| 事後偵察 | 1 | T+2s |
| **新增合計** | **~11** | 攤在約 3 分鐘 |

平均每 16 秒一次，全部指向唯讀頁面（`StepFlag=2`），只有原本就有的 2 發是 `StepFlag=25`。
比一個在午夜前反覆重新整理場地頁的人還慢。

**探測間隔必須隨機抖動（12～25 秒）**：固定 18 秒一次比人類規律太多，
規律性本身就是機器人簽名。抖動不影響二分搜尋的正確性 —— 相位是我們挑的，
落在第幾秒完全自由。

## 9. 測試策略

全部離線，遵守 CLAUDE.md 的 live-site constraint。服務物件沿用
`tests/test_sports_center_webservice.py` 的 `build_without_browser`（`object.__new__`）。

- `tests/test_ntp_client.py`：`parse_ntp_response` 用已知封包做 table-driven；
  `pick_best_sample`；socket 以 stub 取代，不發真實封包。
- `tests/test_server_clock.py`：
  - `constrain` 的區間邊界（含 θ = 0、θ 正、θ 負）
  - `intersect` 的一般情形與空交集
  - `next_probe_phase` 產出的時刻確實滿足 `t ≡ mid (mod 1)` 且 `≥ earliest`
  - **收斂模擬**：給定一個已知的 θ 與 RTT，餵模擬回應跑完整個二分流程，
    斷言 7～8 次後區間寬度收斂到 RTT 量級且包含真值
  - `parse_http_date`
- `tests/test_timing.py`：`compute_send_time` 的各優先序與 clamp 邊界；
  `sleep_then_spin` 的短區間精度。
- `tests/test_sports_center_webservice.py` 擴充：兩個中心的
  `_generate_list_page_url` / `_generate_warm_up_urls` 字串斷言；
  `_parse_slot_state` 餵固定 HTML fixture；
  `target_qpid` 納入 `__init_subclass__` 契約測試。
- User-Agent 常數在 Selenium options 與 aiohttp headers 中一致的斷言。
- 觸發路徑：以假 session 物件斷言 (a) task 在截止前已建立、
  (b) `session.get` 收到的是預組 URL、(c) 一發拋例外不影響另一發。

**沒有任何測試碰網路。** 真正的驗證是下週四那一次實跑，讀 5.7 的儀表輸出。

## 10. 風險與緩解

| 風險 | 影響 | 緩解 |
| --- | --- | --- |
| **前提可能不成立**：7-4 20:00 在開放前就已被佔 | 所有優化收益為零 | 5.5 的開放前/後偵察，零至一個額外請求即可判定 |
| 送太早 → 硬失敗，且連坐另一發 | 比現況更糟 | margin 偏晚、±2s clamp、`return_exceptions=True` |
| half-open 死連線 | 首次寫入失敗後重新握手，**比冷連線更慢** | 預熱移到 T-10s 縮短窗口；讀 `Keep-Alive: timeout=N` 並記 log。殘餘風險存在 |
| 判斷開放與否用的是**資料庫**時鐘（ASP.NET 常見的 `GETDATE()`） | θ_srv 量了也沒用 | 量不到，無法緩解。通常應用與資料庫同機房同 NTP。列為已知殘餘風險 |
| 反向代理蓋的 `Date` | θ_srv 反映代理而非應用伺服器 | 偵測 `Server`/`Via`/`X-Powered-By` 並記 log，不阻斷 |
| 規律流量成為機器人簽名 | 提高被注意到的機率 | 探測間隔隨機抖動 |
| UA 偽裝與 TLS 指紋不符 | 有指紋偵測時更可疑 | 已知取捨，見 5.6 |
| **00:00:00 正負數十毫秒送出本身就是機器人鐵證** | 無法規避 | 這是現況已承擔的風險，本設計不改變其性質，僅使特徵更清晰。除非放棄搶，否則無解 |
| 除錯迴圈每週僅一次 | 任何錯誤代價是再輸一週 | 儀表齊備 + 離線測試覆蓋所有純函式 |

## 11. 已否決的做法

- **繞過 aiohttp，直接對熱 socket 寫預先序列化的 HTTP bytes**：
  能再省 1～3 ms，但要自行管理 SSL 物件與回應解析，過於脆弱。YAGNI。
- **多 QPid 並發**：使用者只要羽球 7-4。
- **`X=2` 之後重試**：那格已經沒了，重試無資訊增益且像在敲門。
- **跨週快取 θ_srv**：若伺服器未做 NTP，晶振漂移每日可達 0.086～4.3 秒，
  隔週的量測值不可信。每次執行重新量測。
- **雲端 VM**：RTT 可壓到個位數 ms，但來源 IP 從穩定住宅 IP 變成機房 IP，
  在網管眼裡更顯眼，與「不要被 ban」的要求直接衝突。
- **兩階段上線（第一週只量測、第二週才套用修正）**：曾提議，使用者選擇一次到位。
