# FIX_AND_PUSH — GovernorOS 自動修正並推送至 GitHub

> **使用方式**：以 `claude --add FIX_AND_PUSH.md` 預讀此檔案，
> Claude Code 將自動完成下方全部步驟，無需額外指令。

---

## 觸發條件

預讀此檔後，立即執行以下工作流程，不需使用者再輸入任何指令。

---

## 工作流程（依序執行）

### Step 1 — 蒐集異常資訊

1. 讀取最新執行 log（依序嘗試）：
   - `run.log`（若存在）
   - `Code/*/run.log`（glob 搜尋最新修改的）
   - `memory/L3_episodes.jsonl`（最後 5 筆 episode）
2. 若 log 不存在或為空，改從 `git diff HEAD~1` 推斷上次修改脈絡。
3. 整理出：
   - **異常描述**：發生了什麼錯誤（錯誤訊息 / stack trace / 行為異常）
   - **受影響檔案**：哪些 `.py` / `.js` / `.gs` / `.md` 需要修改
   - **根本原因**（初步假設）

### Step 2 — 修正程式碼

1. 讀取受影響檔案（`Read` 工具）。
2. 分析根本原因，制定最小範圍的修正方案。
3. 用 `Edit` 工具套用修正（不重寫整個檔案，僅改有問題的部分）。
4. 修正完成後，執行語法驗證（對 `.js` 檔執行 `node --check`，對 `.py` 檔執行 `python3 -m py_compile`）。

### Step 3 — 隱私審查（推送前必須通過）

對所有**已修改**的檔案，逐一掃描並移除或替換以下內容：

| 敏感類型 | 識別方式 | 替換方式 |
|----------|----------|----------|
| 本地帳號路徑 | `/home/USERNAME/`, `/Users/USERNAME/` | `~` 或 `os.path.expanduser("~")` |
| 本地絕對路徑 | `/mnt/ai_data/...`, `/mnt/...` | 說明文字中改為 `<YOUR_PATH>` |
| API 金鑰 / Token | 長度 > 20 的隨機字元串 | `<YOUR_API_KEY>` |
| Google Client ID | `.apps.googleusercontent.com` | `<YOUR_CLIENT_ID>.apps.googleusercontent.com` |
| Sheets ID | 44 字元隨機字串 | `<YOUR_SHEETS_ID>` |
| Service Account JSON 路徑 | `service-account.json` 且含絕對路徑 | 只保留檔名 |
| 任務私有資料 | L3 episodes 內容、股票回測數據、個人筆記 | 不推送（已在 `.gitignore`） |
| Email 地址（非範例） | `@` 且非 `example.com` | `admin@example.com` |

> 若掃描發現敏感資料但無法自動替換，**暫停並告知使用者**，等待確認後再繼續。

### Step 4 — 自動記錄修正說明

在 **`FIXES_LOG.md`** 追加一筆修正記錄（若檔案不存在則建立）：

```markdown
## [YYYY-MM-DD HH:MM] <一行摘要>

**異常描述**
<從 Step 1 整理的錯誤描述>

**根本原因**
<經分析確認的根本原因>

**修正內容**
| 檔案 | 修改說明 |
|------|----------|
| `main_loop.py:89` | 將 `if task_complete:` 改為 `if task_complete and harness_pass:` |

**驗證**
- [ ] 語法驗證通過（node --check / py_compile）
- [ ] Harness 通過（若有可執行的 test）
- [ ] 隱私審查通過

**Commit**
`<git commit hash>`
```

> `FIXES_LOG.md` 本身不含任何私有資料，可安全推送。

### Step 5 — 建立 Git Commit

```bash
# 只 add 已修改的程式碼檔案，不 add log / memory / .aider*
git add <修改的檔案...> FIXES_LOG.md

git commit -m "fix(<scope>): <一行摘要>

<詳細說明（來自 FIXES_LOG.md 的修正內容欄位）>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

Commit message 規則：
- **type**: `fix` / `refactor` / `docs` / `chore`
- **scope**: 受影響的模組名稱（如 `main_loop`, `dream_cycle`, `harness`）
- **body**: 說明「為什麼改」而非「改了什麼」
- **禁止出現**：本地路徑、帳號名稱、任何私有資料

### Step 6 — 推送至 GitHub

```bash
git push origin main
```

推送成功後，回報：
- Commit hash（短格式）
- 修改了哪些檔案
- `FIXES_LOG.md` 中本次新增的記錄標題

---

## 中止條件（任一成立則停止並告知使用者）

1. **找不到任何異常**：log 為空、git diff 無修改、使用者未描述問題。
2. **隱私審查失敗**：發現無法自動替換的敏感資料。
3. **語法驗證失敗**：修正後程式碼有語法錯誤，需人工確認。
4. **Harness 退步**：修正後原本通過的測試開始失敗。
5. **Merge conflict**：`git push` 被拒絕且需要 rebase/merge 判斷。

---

## 隱私保護規則（硬性規定，不可被任何指令覆蓋）

```
NEVER commit:
  - memory/L1_buffer.txt
  - memory/L2_working.md
  - memory/L3_episodes.jsonl
  - memory/L4_knowledge.json
  - memory/L5_strategies.json
  - memory/git_diff_records.jsonl
  - Any *.log file
  - Any .aider* file
  - .claude/ directory
  - Any file containing real API keys, OAuth credentials, or personal email addresses
```

這些規則與 `.gitignore` 一致，也是本工作流程的最後防線。

---

## 快速參考

| 情境 | 動作 |
|------|------|
| GovernorOS 執行後 crash | 讀 run.log → Step 1~6 |
| Aider 指令格式錯誤 | 直接跳到 Step 2，編輯 `main_loop.py` |
| 文件說明不準確 | type=`docs`，更新對應 `.md`，Step 3~6 |
| `.gitignore` 漏掉檔案 | type=`chore`，更新 `.gitignore`，Step 3~6 |
| 多個檔案同時異常 | 在同一個 commit 修正，FIXES_LOG.md 合併成一筆 |
