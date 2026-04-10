# AI System v3.2 — 架構與程式功能說明

> **版本紀錄**
> | 版本 | 日期 | 主要改動 |
> |------|------|---------|
> | v3.0 | 2026-04-08 | 初版：五層記憶 + 狀態機 + DreamCycle |
> | v3.1 | 2026-04-09 | L2 單一真相、Snapshot Commit、Cost Governor |
> | v3.2 | 2026-04-10 | 雙模型分工（Governor / Researcher）、Bridge Schema 驗證、--sleep/--deep 雙階段 |

---

## 目錄

1. [系統概覽](#1-系統概覽)
2. [整體架構圖](#2-整體架構圖)
3. [雙模型分工](#3-雙模型分工)
4. [記憶層級說明](#4-記憶層級說明)
5. [程式模組說明](#5-程式模組說明)
6. [資料流說明](#6-資料流說明)
7. [狀態機邏輯](#7-狀態機邏輯)
8. [記憶生命週期](#8-記憶生命週期)
9. [時間排程](#9-時間排程)
10. [設定參數總覽](#10-設定參數總覽)
11. [快速上手](#11-快速上手)
12. [檔案結構](#12-檔案結構)

---

## 1. 系統概覽

AI System v3.2 是一套**自主學習型 AI 程式開發助理**，設計思想是：

> 每一次執行任務的成敗，都應該被記錄、篩選、萃取成可重用的知識，並在下一次執行時自動注入到工作 context 中。

### 五大設計原則

| 原則 | 說明 |
|------|------|
| **五層記憶架構** | 事件 → 驗證池 → 知識庫 → 策略庫，多層門檻過濾，防止錯誤知識固化 |
| **記憶自然衰減** | 30 天未驗證的知識自動降低 confidence，180 天退休，保持知識庫新鮮度 |
| **Git Diff 智能學習** | 從每次程式碼改動的成敗中，萃取「哪類改動容易 pass / fail」的模式 |
| **L2 單一真相** | L2_working.md 是記憶的唯一真相，CLAUDE.md 只是展示層 mirror |
| **雙模型分工** | Governor（穩定主控）負責判斷與治理；Researcher（高服從執行者）負責生成與萃取 |

---

## 2. 整體架構圖

```
使用者輸入任務
      │
      ▼
 PROMPT.md ──────────────────────────────────────────────────────┐
 角色設定（通用軟體開發）                                           │
 L4 知識注入（今日規律）                                           │  DreamCycle
 L5 策略注入（今日執行策略）                                        │  --wake 注入
 任務 context + 迭代狀態                                           │
      │                                                          │
      ▼                                                          │
┌─────────────────────────────────────────────────────────────┐  │
│              main_loop.py  主控迴圈                          │  │
│                                                             │  │
│   ┌─────────────────────────────────────────────────────┐   │  │
│   │              Loop State Machine                      │   │  │
│   │   continue → switch_tool → rollback → stop          │   │  │
│   └───────────────────┬─────────────────────────────────┘   │  │
│                       │                                     │  │
│         ┌─────────────┼──────────────┐                      │  │
│         ▼             ▼              ▼                      │  │
│      Aider          Harness     Autoresearch                │  │
│      執行層          驗證層       研究層                      │  │
│      改程式碼        跑測試        Researcher                 │  │
│                                  200字 brief                │  │
│         │             │              │                      │  │
│         ▼             ▼              │                      │  │
│   Git Snapshot    pass/fail          │                      │  │
│   iter_N_pre_harness  delta          │                      │  │
│         │             │              │                      │  │
│         └─────────────┴──────────────┘                      │  │
│                       │                                     │  │
│     Episode Scoring → L3   Git Diff Intel 記錄              │  │
│     MemPalace 四區壓縮（L2 唯一真相 → mirror CLAUDE.md）      │  │
│                                                             │  │
└───────────────────────┬─────────────────────────────────────┘  │
                        │                                        │
              任務結束 Bridge（Researcher 轉換 + Schema 驗證）     │
                        │                                        │
              memory/L3_episodes.jsonl                           │
                        │                                        │
       DreamCycle --sleep（15:00）                               │
       Phase 1 Researcher 產候選 L4                              │
       Phase 2 Governor  複核 L4                                 │
                        ▼                                        │
              memory/L4_knowledge.json                           │
                        │                                        │
       DreamCycle --deep（15:30）                                │
       Phase 1 Researcher 產 L5 草稿                             │
       Phase 2 Governor  審核 + Conflict Resolver                │
                        ▼                                        │
              memory/L5_strategies.json ─────────────────────────┘
```

---

## 3. 雙模型分工

v3.2 核心升級：根據各環節的風險等級與任務性質，將兩個模型放在對應職位。

### 模型角色定位

| 角色 | 模型 | 定位 | 適合任務 |
|------|------|------|---------|
| **Governor** | `qwen3.5:27b` | 技術主管 / 調度者 | 判斷、審核、治理、複核、衝突仲裁 |
| **Researcher** | `prutser/gemma-4-26B-A4B-it-ara-abliterated:q5_k_m` | 高效率研究助理 | 生成、萃取、草稿、格式轉換 |

### 完整分工對照表

| 模組 / 函式 | 使用模型 | 理由 |
|------------|---------|------|
| `cmd_update_mempalace()` | **Governor** | 工作記憶壓縮，需規則感與結構穩定性 |
| `cmd_sleep()` Phase 1：產候選 L4 | **Researcher** | 高頻生成，格式固定，邊界清晰 |
| `cmd_sleep()` Phase 2：複核 L4 | **Governor** | 過濾重複 / 步驟混入 / confidence 過高 / scope 過泛 |
| `cmd_deep()` Phase 1：產 L5 草稿 | **Researcher** | 快速產出多個候選策略 |
| `cmd_deep()` Phase 2：審核草稿 | **Governor** | 格式校正 + confidence 上限 + needs_review 標記 |
| Conflict Resolver | **Python**（不呼叫模型） | 純規則仲裁，最穩定，不受模型輸出漂移影響 |
| `cmd_bridge()` | **Researcher** | 格式轉換 + 摘要，輸出短且結構固定 |
| `bridge_schema_validate()` | **Python**（不呼叫模型） | 攔截格式問題於進 L3 之前 |
| `autoresearch()` | **Researcher** | 200 字 brief，邊界明確，不直接影響 rollback 決策 |
| `--wake` L4/L5 挑選注入 | **Python**（不呼叫模型） | confidence 排序取前 5，規則即可 |
| `git_diff_intel --analyze` | **Python**（不呼叫模型） | 純統計分析，無需語意判斷 |

### 雙模型優於單模型的前提

```
不是所有步驟都雙跑，
而是只在高風險節點雙層把關：

  Researcher 提出候選
       ↓
  Governor 審核核准
       ↓
  Python Conflict Resolver 最終把關（L5）
```

---

## 4. 記憶層級說明

系統採用五層記憶架構，每一層都有嚴格的升級門檻：

### L1 — 即時緩衝（L1_buffer.txt）
- **性質**：原始對話與工具輸出的暫存
- **壽命**：單次對話，不跨任務
- **管理**：自動覆蓋

### L2 — 工作記憶唯一真相（L2_working.md）
- **性質**：MemPalace 四區壓縮後的結構化狀態
- **壽命**：單次任務
- **格式**：`[GOAL] [DONE] [PENDING] [CONSTRAINTS]` 四區
- **v3.1 重要**：L2 是唯一真相，所有讀取必須來自此檔案；CLAUDE.md 只是展示層 mirror

### L3 — 事件池（L3_episodes.jsonl）
- **性質**：每次 Aider 執行的事件記錄，附 Episode Importance Score
- **升級門檻**：score >= 6 進入「驗證池」，同類事件 >= 3 次才升 L4

```json
{
  "id": "ep_001",
  "time": "2026-04-10T09:15:00",
  "task": "修正 RSI 計算邊界條件",
  "status": 1,
  "diff_summary": "files=signals.py lines=12",
  "harness_delta": 2.5,
  "score": 9,
  "reasons": ["第一次出現這類問題", "Harness 分數提升 2.5"],
  "priority": "high",
  "pool": "validated"
}
```

### L4 — 知識庫（L4_knowledge.json）
- **性質**：可泛化的規律描述，**只記觀察，不含執行步驟**
- **升級門檻**：同類 L3 事件 >= 3 次 + Researcher 產出 + Governor 複核通過
- **衰減**：30 天未驗證 confidence -= 0.05/週；首次 confidence 上限 0.75

```json
{
  "id": "k_001",
  "pattern": "同時修改 core.py 和 signals.py 時 regression 率高",
  "scope": "Python 量化策略專案",
  "confidence": 0.72,
  "evidence_count": 4,
  "last_verified": "2026-04-10",
  "decay_timer": 30,
  "status": "active"
}
```

### L5 — 策略庫（L5_strategies.json）
- **性質**：可直接執行的操作指令，**condition → action → avoid 格式**
- **升級門檻**：來源 L4 confidence >= 0.75 + Researcher 草稿 + Governor 審核 + Conflict Resolver 無衝突
- **降級條件**：fail_count / total > 40%（且 total >= 3）→ 退回 L4 重新驗證

```json
{
  "id": "s_001",
  "condition": "需要同時修改 core.py 和 signals.py 時",
  "action": "先建立獨立測試，逐一修改並驗證，最後整合",
  "avoid": "不要一次 commit 同時改兩個核心模組",
  "scope": "Python 專案重構",
  "confidence": 0.82,
  "success_count": 5,
  "fail_count": 1,
  "last_verified": "2026-04-10",
  "decay_timer": 30,
  "status": "active",
  "conflict_check": "無衝突"
}
```

---

## 5. 程式模組說明

### dream_cycle.py — 記憶引擎

系統核心，負責記憶的全生命週期管理。

| 指令 | 模型 | 功能 | 呼叫時機 |
|------|------|------|---------|
| `--init` | — | 初始化 memory/ 目錄與所有記憶檔案 | 第一次使用 |
| `--record` | — | 記錄 L3 事件並執行 Episode Importance Scoring | 每次 Aider 執行後 |
| `--update-mempalace` | Governor | 壓縮 context → MemPalace 四區，寫入 L2（唯一真相）再 mirror 至 CLAUDE.md | 每次迭代 |
| `--sleep` | Researcher + Governor | L3 驗證池 → L4：Phase 1 Researcher 產候選，Phase 2 Governor 複核 | 每日 15:00 |
| `--deep` | Researcher + Governor | L4 → L5：Phase 1 Researcher 產草稿，Phase 2 Governor 審核 + Conflict Resolver | 每日 15:30 |
| `--wake` | Python | L4+L5 注入 CLAUDE.md 和 PROMPT.md（按 confidence 排序，不呼叫模型） | 每日 09:00 |
| `--decay` | Python | 執行 Memory Decay 衰減、降級、退休 | 每日 09:00 + 16:00 |
| `--bridge` | Researcher + Python | MemPalace 輸出 → L3：Researcher 轉換，Python Schema 驗證後寫入 | 任務結束後 |
| `--status` | — | 顯示各層記憶數量與健康狀態 | 隨時 |

#### Episode Importance Scoring 計分規則

| 條件 | 加分 |
|------|------|
| 第一次出現這類問題 | +3 |
| 導致 rollback | +5 |
| Harness 分數大幅提升（delta >= 1.5） | +4 |
| diff 涉及可重用模組（function/class/util 等） | +3 |
| 任何失敗事件 | +2 |

- score >= 6 → 進「驗證池」，優先升 L4
- score 3-5 → 進「候選池」，累積後再考慮
- score < 3 → 低價值，不升級

#### Cost Governor

```python
TOKEN_BUDGET      = 500_000   # 單次 session 所有 Ollama 呼叫 token 上限
MAX_DEEP_EPISODES = 20        # --deep 最多處理幾條 L4（取 confidence 最高）
MAX_SLEEP_GROUPS  = 10        # --sleep 最多處理幾個事件群組（取成員數最多）
```

每個 Ollama 呼叫前執行 `budget_check()`，超出則跳過並印警告。
Phase 2 budget 超出時：使用 Phase 1 結果並安全調降 confidence，不直接廢棄。

#### bridge_schema_validate()（純 Python）

Bridge 輸出的 JSON 格式在進入 L3 前先做純 Python 校驗：

| 欄位 | 驗證規則 |
|------|---------|
| `L3_events` | list of str，每條 <= 100 字 |
| `L4_candidates` | list of str，每條 <= 150 字 |
| `L5_candidates` | list of dict，必含 condition / action / avoid / confidence |
| confidence | float in [0, 1] |

驗證失敗 → 拒絕寫入 L3 + 記錄一筆低分失敗 episode 供後續追蹤。

---

### main_loop.py — 主控迴圈

系統的執行引擎，協調 Aider、Harness、Autoresearch 三個執行層。

#### 核心流程（每次迭代）

```
1.  更新 PROMPT.md context（任務狀態、迭代進度）
2.  判斷狀態機 → 決定本輪動作
3.  從 L2_working.md 讀取 MemPalace（唯一真相）
4.  執行 Aider（帶入 MemPalace + Research Brief + 上次失敗訊息）
5.  取得 Git Diff（changed_files、lines_changed）
6.  建立 Snapshot：git tag iter_{N}_pre_harness         ← v3.1
7.  執行 Harness 驗證（pass/fail + score delta）
8.  若 PASS → 更新 last_good_snapshot                  ← v3.1
9.  記錄 Git Diff Intel
10. 記錄 Episode → L3
11. 壓縮 MemPalace（Governor）→ 寫 L2 → mirror CLAUDE.md ← v3.1
12. 狀態機更新 → 決定下一輪動作
13. 若 stop → Bridge（Researcher + Schema 驗證），結束迴圈
```

#### Autoresearch

使用 **Researcher**（Gemma），輸出嚴格 <= 200 字。
邊界條件已固定（輸出短、格式固定、不直接影響 rollback 決策），適合高服從模型快速產出。

#### 主要 CLI 參數

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--task` | 任務描述（必填） | — |
| `--harness` | 驗證指令（必填），e.g. `pytest tests/` | — |
| `--aider-model` | Aider 使用的 LLM | claude-3-5-sonnet-20241022 |
| `--aider-files` | 要讓 Aider 編輯的檔案，空格分隔 | （空） |
| `--work-dir` | 目標專案的工作目錄 | 當前目錄 |
| `--max-iter` | 最大迭代次數 | 20 |
| `--score-pattern` | 從 Harness 輸出擷取分數的 regex | （無） |
| `--ollama-governor` | Governor 模型 | qwen3.5:27b |
| `--ollama-researcher` | Researcher 模型 | gemma-4-26B-A4B... |
| `--dry-run` | 略過 Aider 實際執行（測試用） | False |

---

### git_diff_intel.py — Git Diff Intelligence

追蹤每次程式碼改動的模式，學習哪類改動容易 pass / fail。

| 指令 | 功能 |
|------|------|
| `--record` | 記錄一筆 diff（由 main_loop.py 自動呼叫） |
| `--analyze` | 分析累積 records，萃取高風險 / 高勝率 pattern（純 Python） |
| `--status` | 顯示 diff 統計（pass rate、高風險組合） |

#### 記錄格式

```json
{
  "time": "2026-04-10T09:20:00",
  "patch_type": "bugfix",
  "files_changed": ["src/signals.py"],
  "core_files": ["signals.py"],
  "harness_result": "pass",
  "harness_delta": 1.5,
  "lines_changed": 18,
  "rollback": false
}
```

#### Pattern 萃取規則（純 Python，不呼叫模型）

| 模式類型 | 觸發條件 | 輸出 |
|---------|---------|------|
| 高風險改動 | 同檔案組合 fail_rate >= 50%，累積 >= 4 筆 | L5 candidate（avoid 規則） |
| 高勝率改動 | 單一檔案 pass_rate >= 75%，累積 >= 4 筆 | L5 candidate（action 規則） |

---

### CLAUDE.md — 展示層（唯讀）

**v3.1 重要**：CLAUDE.md 現在只是展示層，供 Claude Code IDE 顯示。
任何讀取 MemPalace 的程式必須讀 `memory/L2_working.md`。

包含三個注入區塊（由系統自動維護）：

| 區塊標籤 | 內容 | 更新來源 |
|---------|------|---------|
| `<!-- MEMPALACE_START/END -->` | MemPalace 四區 mirror | `dream_cycle.py --update-mempalace` |
| `<!-- L4_INJECT_START/END -->` | 今日規律（active L4，conf >= 0.70） | `dream_cycle.py --wake` |
| `<!-- L5_INJECT_START/END -->` | 今日策略（active L5，conf >= 0.75） | `dream_cycle.py --wake` |

---

### PROMPT.md — 系統提示詞

通用軟體開發代理角色設定，不限語言與領域（後端、前端、腳本、API、資料處理等）。

包含四個注入區塊：

| 區塊標籤 | 內容 | 更新來源 |
|---------|------|---------|
| `<!-- GOAL_INJECT_START/END -->` | 當前任務 + 迭代狀態 | `main_loop.py` 每輪更新 |
| `<!-- L4_INJECT_START/END -->` | 知識注入 | `dream_cycle.py --wake` |
| `<!-- L5_INJECT_START/END -->` | 策略注入 | `dream_cycle.py --wake` |
| `<!-- WARN_INJECT_START/END -->` | decay 警告 / needs_review 提示 | `dream_cycle.py --wake` |

---

### startup.sh — 排程腳本

| 指令 | 動作 | 建議時間 |
|------|------|---------|
| `./startup.sh init` | 初始化記憶目錄 | 一次性 |
| `./startup.sh morning` | decay check → wake 注入 | 09:00 |
| `./startup.sh evening` | sleep → deep → decay → analyze | 15:00 |
| `./startup.sh status` | L3/L4/L5 + DiffIntel 狀態 | 隨時 |
| `./startup.sh analyze` | 手動觸發 Diff Intel 分析 | 隨時 |

---

## 6. 資料流說明

### 白天工作流（任務執行期間）

```
main_loop.py
  │
  ├─ 讀 L2_working.md（唯一真相）→ 組合 Aider message
  ├─ Aider 執行 → 程式碼改動
  ├─ get_git_diff()
  ├─ git_snapshot() → tag iter_{N}_pre_harness
  ├─ run_harness() → pass/fail + score_delta
  │    └─ PASS → last_good_snapshot = iter_{N}_pre_harness
  ├─ git_diff_intel.py --record
  ├─ dream_cycle.py --record（Episode Scoring → L3）
  └─ dream_cycle.py --update-mempalace
       └─ Governor 壓縮 → 寫 L2（唯一真相）→ mirror CLAUDE.md

任務結束
  └─ dream_cycle.py --bridge
       ├─ Researcher 轉換（MemPalace → 結構化 JSON）
       ├─ bridge_schema_validate()（純 Python，格式把關）
       └─ 通過 → 寫入 L3
```

### 盤後整合流（15:00-16:00）

```
L3_episodes.jsonl（score >= 6，同類 >= 3次）
  │
  ├─[--sleep Phase 1] Researcher 產候選 L4
  ├─[--sleep Phase 2] Governor 複核（刪重複 / 修格式 / 調 confidence）
  └─► L4_knowledge.json（confidence <= 0.75）
                          │
  ├─[--deep Phase 1]  Researcher 產 L5 草稿
  ├─[--deep Phase 2]  Governor 審核（格式 / confidence 上限）
  ├─[Conflict Resolver] Python 仲裁衝突
  └─► L5_strategies.json
                          │
  └─[--decay] 衰減 / 降級 / 退休
```

### 記憶注入流（09:00）

```
L4_knowledge.json（active，conf >= 0.70）  ┐
L5_strategies.json（active，conf >= 0.75） ├─[--wake Python 規則]──► PROMPT.md
decay_timer < 7天的警告項目               ┘                         CLAUDE.md（展示層）
```

---

## 7. 狀態機邏輯

`LoopStateMachine` 控制每輪迭代的行為：

```
初始狀態：continue
               │
    ┌──────────┴──────────┐
    │                     │
harness PASS          harness FAIL
    │                     │
重置 consecutive_fails    consecutive_fails += 1
last_good_snapshot 更新   │
    │              ┌──────┴──────┐
    ▼              │             │
  continue        1次           2次
                   │             │
                continue    switch_tool
                                 │
                             Researcher
                             Autoresearch
                             （200字 brief）
                                 │
                             回到 continue（帶 brief）
                                 │
                         若再次 fail → 3次 → rollback
                                             │
                                   git reset --hard last_good_snapshot
                                   （無 snapshot 則 fallback HEAD^）
                                             │
                                       rollback_count += 1
                                       consecutive_fails = 0
                                             │
                                   若 rollback_count >= 2 且仍 fail
                                             │
                                           stop（升級人工介入）

特殊情況：
  Aider 輸出含 TASK_COMPLETE → stop（任務完成）
  iteration >= max_iter       → stop（達到上限）
```

---

## 8. 記憶生命週期

### 升級路徑

```
原始事件
  └─[Episode Scoring]──► L3 驗證池（score >= 6）
                           │
         [同類 >= 3次]
                           │
         ┌─────────────────┘
         │
         ├─ Phase 1：Researcher 產候選 L4
         └─ Phase 2：Governor 複核
                           │
                    L4 知識庫（confidence <= 0.75）
                           │
         [confidence >= 0.75]
                           │
         ┌─────────────────┘
         │
         ├─ Phase 1：Researcher 產 L5 草稿
         ├─ Phase 2：Governor 審核
         └─ Conflict Resolver（Python）
                           │
                    L5 策略庫（可執行指令）
```

### 衰減路徑

```
L4 / L5 active
  │
  ├─ 30天未驗證 → confidence -= 0.05/週
  ├─ confidence < 0.50 → status = inactive
  └─ inactive 180天 → status = retired

L5 額外降級條件：
  └─ fail_count / (success_count + fail_count) > 40%（且 total >= 3）
       → status = inactive → 退回 L4 重新驗證
```

### Conflict Resolver（Python，不呼叫模型）

當新 L5 策略的 condition 與現有策略的 condition 詞彙重疊 > 50% 時觸發仲裁：

| 優先順序 | 說明 |
|---------|------|
| 1 | scope 更小者優先（日線 > 週線 > 通用） |
| 2 | last_verified 更近者優先 |
| 3 | success_count 更高者優先 |
| 4 | confidence 更高者優先 |

- 勝出者 → 保持 active
- 落敗者 → status = inactive + conflict_note
- 無法仲裁 → 兩者標記 `needs_review`，需人工確認

---

## 9. 時間排程

### 建議 crontab 設定

```bash
crontab -e

# 加入以下排程（請替換路徑）
0  9 * * 1-5  /path/to/AI_system_v3/startup.sh morning >> /tmp/ai_morning.log 2>&1
0 15 * * 1-5  /path/to/AI_system_v3/startup.sh evening >> /tmp/ai_evening.log 2>&1
```

### 完整時間流

| 時間 | 動作 | 說明 |
|------|------|------|
| 09:00 | `startup.sh morning` | decay check → wake（Python 規則注入 L4/L5） |
| 09:01+ | `main_loop.py` | 主迴圈執行任務（Aider + Harness + Snapshot） |
| 任務結束 | `--bridge` | Researcher 轉換 + Python Schema 驗證 → L3 |
| 15:00 | `startup.sh evening (1/4)` | `--sleep`：L3 → L4（雙階段） |
| 15:30 | `startup.sh evening (2/4)` | `--deep`：L4 → L5（雙階段 + Conflict Resolver） |
| 16:00 | `startup.sh evening (3/4)` | decay 衰減 / 降級 / 退休 |
| 16:05 | `startup.sh evening (4/4)` | Diff Intel pattern 分析（純 Python） |

---

## 10. 設定參數總覽

### 模型設定

| 環境變數 | 預設值 | 說明 |
|---------|--------|------|
| `OLLAMA_MODEL_GOVERNOR` | `qwen3.5:27b` | 判斷、審核、治理 |
| `OLLAMA_MODEL_RESEARCHER` | `prutser/gemma-4-26B-A4B-it-ara-abliterated:q5_k_m` | 生成、萃取、草稿 |

### Cost Governor

| 常數 | 預設值 | 說明 |
|------|--------|------|
| `TOKEN_BUDGET` | 500,000 | 單次 session 所有 Ollama 呼叫 token 上限 |
| `MAX_DEEP_EPISODES` | 20 | `--deep` 最多處理幾條 L4（取 confidence 最高） |
| `MAX_SLEEP_GROUPS` | 10 | `--sleep` 最多處理幾個事件群組（取成員數最多） |

### 記憶門檻（dream_cycle.py THRESHOLDS）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `episode_min_score` | 6 | L3 進驗證池的最低分數 |
| `l3_to_l4_min_count` | 3 | 同類事件升 L4 需出現幾次 |
| `l4_to_l5_min_conf` | 0.75 | L4 升 L5 的最低 confidence |
| `wake_l4_min_conf` | 0.70 | 注入 PROMPT 的 L4 最低 confidence |
| `wake_l5_min_conf` | 0.75 | 注入 PROMPT 的 L5 最低 confidence |
| `decay_days` | 30 | 幾天未驗證開始衰減 |
| `decay_amount` | 0.05 | 每週衰減量 |
| `inactive_threshold` | 0.50 | confidence 低於此值 → inactive |
| `retire_days` | 180 | inactive 幾天後 retire |
| `fail_ratio_threshold` | 0.40 | fail/total 超過此值 → L5 降級 |
| `conflict_overlap` | 0.50 | condition 重疊度超過此值視為衝突 |
| `max_wake_tokens` | 500 | 注入內容最大字數 |
| `decay_warning_days` | 7 | decay_timer 剩餘天數警告門檻 |

---

## 11. 快速上手

### 第一次使用

```bash
cd /path/to/AI_system_v3

# 1. 初始化記憶系統
./startup.sh init

# 2. 確認 Ollama 已啟動並拉取兩個模型
ollama serve &
ollama pull qwen3.5:27b
ollama pull prutser/gemma-4-26B-A4B-it-ara-abliterated:q5_k_m

# 3. Morning 啟動
./startup.sh morning
```

### 執行任務

```bash
# 基本用法（Python 專案）
python main_loop.py \
  --task "實作使用者登入 API，含 JWT 驗證" \
  --harness "pytest tests/ -q" \
  --aider-files src/auth.py src/models.py \
  --work-dir /path/to/your/project

# 指定 Aider 模型 + 量化分數追蹤
python main_loop.py \
  --task "優化模型推論速度" \
  --harness "python benchmark.py" \
  --aider-model gpt-4o \
  --score-pattern "throughput=(\d+\.\d+)" \
  --work-dir /path/to/project \
  --max-iter 15

# 覆蓋雙模型設定
OLLAMA_MODEL_GOVERNOR=qwen3.5:27b \
OLLAMA_MODEL_RESEARCHER=prutser/gemma-4-26B-A4B-it-ara-abliterated:q5_k_m \
python main_loop.py \
  --task "修正 DataFrame merge 記憶體洩漏" \
  --harness "pytest tests/ -q" \
  --aider-files src/data_loader.py \
  --work-dir /path/to/project

# 測試模式（不執行 Aider）
python main_loop.py \
  --task "測試流程" \
  --harness "echo ok" \
  --work-dir /path/to/project \
  --dry-run
```

### 查看狀態

```bash
./startup.sh status          # L3/L4/L5 + DiffIntel 全部狀態
python dream_cycle.py --status
python git_diff_intel.py --status
```

### 手動操作記憶

```bash
# 手動記錄事件
python dream_cycle.py --record \
  --task "修正 pandas deprecated warning" \
  --status-code 0 \
  --diff-summary "files=utils.py lines=5" \
  --harness-delta 0.0

# 手動觸發盤後整合
python dream_cycle.py --sleep    # L3 → L4（雙階段）
python dream_cycle.py --deep     # L4 → L5（雙階段）
python dream_cycle.py --wake     # 注入 PROMPT.md / CLAUDE.md

# 手動觸發 Diff Intel 分析
python git_diff_intel.py --analyze
```

### Snapshot Rollback 操作

```bash
# 查看所有 snapshot tags
git tag | grep pre_harness

# 手動 rollback 到任意一輪
git reset --hard iter_3_pre_harness

# 查看哪輪是最後一次 PASS（從 main_loop 輸出的 log 確認）
```

---

## 12. 檔案結構

```
AI_system_v3/
│
├── dream_cycle.py        記憶引擎
│                         ├─ 雙模型常數（Governor / Researcher）
│                         ├─ Cost Governor（TOKEN_BUDGET / MAX_DEEP / MAX_SLEEP）
│                         ├─ bridge_schema_validate()（純 Python）
│                         ├─ --sleep：雙階段（Researcher → Governor 複核）
│                         ├─ --deep：雙階段（Researcher → Governor 審核）
│                         └─ --bridge：Researcher 轉換 + Schema 驗證
│
├── main_loop.py          主控迴圈
│                         ├─ 雙模型常數（Governor / Researcher）
│                         ├─ LoopStateMachine（continue/switch_tool/rollback/stop）
│                         ├─ git_snapshot()：iter_{N}_pre_harness tag
│                         ├─ git_rollback()：精確回到 last_good_snapshot
│                         └─ autoresearch()：Researcher 200 字 brief
│
├── git_diff_intel.py     Git Diff Intelligence
│                         ├─ --record：記錄 diff
│                         ├─ --analyze：純 Python pattern 萃取
│                         └─ --status：統計報表
│
├── startup.sh            時間排程（morning / evening / init / status）
│
├── CLAUDE.md             展示層（v3.1：唯讀 mirror，讀取必須用 L2）
├── PROMPT.md             系統提示詞（通用軟體開發角色 + L4/L5 注入）
│
└── memory/
    ├── L1_buffer.txt             即時緩衝
    ├── L2_working.md             工作記憶唯一真相（MemPalace 四區）
    ├── L3_episodes.jsonl         事件池（含 Episode Importance Score）
    ├── L4_knowledge.json         知識庫（可泛化規律）
    ├── L5_strategies.json        策略庫（可執行指令）
    └── git_diff_records.jsonl    Git Diff 改動記錄
```
