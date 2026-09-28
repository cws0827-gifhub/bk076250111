# Mac mini ＋ Synology 部署指南

```
客人 LINE → LINE 平台 → Cloudflare Tunnel → 家裡的 Mac mini :8765 /callback
                                              │  （程式＋資料庫都在 Mac mini）
 診所同仁 → crm.beauty-keys.com → Cloudflare Access（email 驗證碼）─┘
                                              │ 每天 03:15 備份
                                              ▼
                                   Synology /backup/line-crm（保留 30 天）
                                              │ Hyper Backup
                                              ▼
                                   異地：C2 雲端 或 外接 USB 硬碟
```

**分工原則**：Mac mini 負責執行程式，Synology 只放備份。

## 0. Mac mini 放在家裡要注意的事

客人的 LINE 對話（包含術後狀況描述）屬於個人資料，存放在診所以外的地方要多一層保護：
- **開啟 FileVault**（系統設定 → 隱私權與安全性 → FileVault）：電腦遺失或被偷時，硬碟內容無法被讀取。
- Mac 登入密碼要夠強，並開啟螢幕保護程式密碼；家人如果也會用這台電腦，**另外開一個使用者帳號給他們**。
- 家裡 Wi-Fi 使用 WPA2／WPA3 和強密碼，路由器的管理密碼也要改掉預設值。
- **家裡斷電或斷網時**，這段期間的訊息不會進到看板。若使用分流站，領健不受影響，只是我們的看板會漏掉這段時間的訊息。建議 Mac mini 和路由器一起接不斷電系統。
⚠️ 不要把資料庫直接放在 NAS 共用資料夾、讓程式透過網路讀寫：SQLite 經由 SMB 存取很容易損毀。

---

## 1. Synology 準備（DSM 控制台）

1. **控制台 → 共用資料夾 → 新增**：名稱 `backup`（已有備份用資料夾就沿用），在裡面建立 `line-crm` 資料夾。
2. **控制台 → 使用者帳號**：建一個專用帳號，例如 `macmini-backup`，只給 `backup` 的讀寫權限。
3. **控制台 → 檔案服務 → SMB**：確認已啟用。
4. **必做：異地備份**。Mac mini 和 NAS 都在家裡，火災、竊盜、淹水時可能兩份一起不見，所以一定要有一份存在家以外的地方：
   - 用 **Hyper Backup** 把 `backup/line-crm` 每天備份到 **Synology C2**（雲端，這份資料很小，最低方案就夠用）
   - 建立備份任務時**勾選「啟用用戶端加密」**，並把加密密碼另外存好（例如密碼管理器，或密封後放在診所）。密碼遺失的話，備份就無法還原。
   - 不想用雲端的話，也可以備份到外接 USB 硬碟，並**定期把硬碟帶到診所存放**
   - 每季挑一份備份實際還原一次，確認真的救得回來
5. 選配：**Snapshot Replication** 對 `backup` 開每日快照，可防止勒索病毒加密備份檔。

## 2. Mac mini 掛載 NAS 共用資料夾（只需做一次）

1. Finder → 前往 → 連接伺服器（⌘K）→ 輸入 `smb://DiskStation.local/backup`
   （`DiskStation` 換成你的 NAS 名稱，或直接輸入 IP，例如 `smb://192.168.1.10/backup`）
2. 用 `macmini-backup` 帳號登入，**勾選「在我的鑰匙圈中記住此密碼」**。
3. 系統設定 → 一般 → 登入項目：把掛載後的 `backup` 拖進去，開機就會自動掛載。
4. 萬一沒有掛上，備份腳本也會用 `.env` 裡的 `NAS_SMB_URL` 自動嘗試掛載。

## 3. Mac mini 系統設定

你的 Mac mini 已經在跑排程，下面幾項應該都設好了，確認一下即可：
- **系統設定 → 能源**：關閉「顯示器關閉時自動睡眠」，並開啟「停電後自動啟動」。
- **系統設定 → 使用者與群組 → 自動登入**：要開啟。本程式屬於「使用者層級」的排程，要登入後才會執行，和你現有的排程一樣。
- 確認 8765 埠沒被佔用：`lsof -i :8765` 沒有輸出就代表可以用。
- 備份時間預設在凌晨 **03:15**。若和現有排程撞時段，改 `install.sh` 裡的 `Hour`／`Minute` 後重跑即可。

## 4. 安裝程式

```bash
cd ~          # 或你想放的位置
git clone https://github.com/cws0827-gifhub/bk076250111.git
cd bk076250111/line-crm
cp .env.example .env
open -e .env  # 填入 LINE 金鑰、管理頁密碼、NAS_BACKUP_DIR
bash deploy/macos/install.sh
```

`.env` 裡的 `NAS_BACKUP_DIR` 要填 **Finder 掛載後的實際路徑**，通常是 `/Volumes/backup/line-crm`。

完成後：
- 在家裡測試管理頁：`http://Mac名稱.local:8765`
- 診所同仁使用的網址是 `https://crm.beauty-keys.com`，要先完成第 6 步
- 先手動跑一次備份，確認 NAS 上出現檔案：`bash deploy/macos/backup.sh`

## 5. 讓 LINE 連得進來：Cloudflare Tunnel（建議）

不用在路由器開 port，自動提供 HTTPS，而且**只開放 `/callback`，管理頁不會出現在網路上**。
網域使用 **beauty-keys.com**，DNS 需要交給 Cloudflare 管理（免費方案就夠用），做法見 [`../cloudflare-relay/README.md`](../cloudflare-relay/README.md) 第 1 步。

```bash
brew install cloudflared
cloudflared tunnel login                       # 瀏覽器選 beauty-keys.com
cloudflared tunnel create line-crm             # 記下 Tunnel ID
cloudflared tunnel route dns line-crm line.beauty-keys.com
cloudflared tunnel route dns line-crm crm.beauty-keys.com
cp deploy/macos/cloudflared-config.example.yml ~/.cloudflared/config.yml
open -e ~/.cloudflared/config.yml              # 填入 Tunnel ID、使用者名稱
cloudflared tunnel ingress validate            # 檢查設定檔
cloudflared service install                    # 登入後自動啟動（不要加 sudo：加了會改讀 /etc/cloudflared 的設定）
```

⚠️ **macOS 上 `cloudflared service install` 產生的排程少了啟動參數**，裝完後連線通道會每隔幾秒就退出一次
（紀錄裡會出現 `use cloudflared tunnel run`）。裝完請補上參數，再重新載入：

```bash
PLIST=~/Library/LaunchAgents/com.cloudflare.cloudflared.plist
cp "$PLIST" ~/.cloudflared/com.cloudflare.cloudflared.plist.bak
/usr/libexec/PlistBuddy -c "Add :ProgramArguments: string tunnel" \
                        -c "Add :ProgramArguments: string run" \
                        -c "Add :ProgramArguments: string line-crm" "$PLIST"
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null; launchctl bootstrap gui/$(id -u) "$PLIST"
cloudflared tunnel info line-crm                # 應該看到連線（CONNECTOR）
```

以後如果重跑 `cloudflared service install`，這些參數會被覆蓋掉，要再補一次。

⚠️ **LINE 的 Webhook 目前接在領健，不要直接改成 `https://line.beauty-keys.com/callback`**，否則領健會收不到訊息。
要讓兩邊都收到，請看 [`../cloudflare-relay/README.md`](../cloudflare-relay/README.md)。

## 6. 診所同仁打開管理頁：Cloudflare Access（必做）

Mac mini 在家裡，診所的電腦和手機要透過網路連進來。用 Cloudflare Access 在管理頁前面加一道門：
**只有你指定的 email 能進來，每次登入都會寄一組驗證碼到信箱**，同仁不用安裝任何 App。

⚠️ **一定要先設定 Access，再啟動 Tunnel**，否則 `crm.beauty-keys.com` 會有一段時間只靠密碼保護。

1. Cloudflare 後台 → **Zero Trust**（第一次使用時選 Free 方案，50 人以內免費）
2. Access → Applications → **Add an application → Self-hosted**
   - Application domain：`crm.beauty-keys.com`
   - Session duration：建議 `24 hours`
3. 新增 Policy：
   - Action：**Allow**
   - Include → **Emails**：逐一填入可以看管理頁的同仁 email
4. Login methods 勾選 **One-time PIN**（寄驗證碼到信箱）
5. 儲存後，用無痕視窗打開 `https://crm.beauty-keys.com`：應該先看到 Cloudflare 的登入頁，驗證 email 後才會出現管理頁的帳號密碼視窗。

同仁離職時，從 Policy 移除他的 email 即可，不需要改密碼。

**讓同仁通過 email 驗證後直接進入看板（不用再輸入密碼）**：在 `.env` 加上

```
CF_ACCESS_TEAM_DOMAIN=你的團隊名稱.cloudflareaccess.com
CF_ACCESS_AUD=Application Audience (AUD) Tag
```

兩個值都在 Zero Trust → Access → Applications → 美之耀客服看板 → **Overview**。
程式會向 Cloudflare 驗證登入憑證的簽章，偽造的憑證會被擋下；按「已回覆」時也會記下是哪位同仁處理的。
改完執行 `bash deploy/macos/install.sh` 重新載入。

---

## 日常維護與疑難排解

| 狀況 | 怎麼做 |
|---|---|
| 看程式紀錄 | `tail -f ~/Library/Logs/line-crm/com.meizhiyao.line-crm.log` |
| 看備份紀錄 | `tail ~/Library/Logs/line-crm/com.meizhiyao.line-crm-backup.log` |
| 更新程式 | `git pull && bash deploy/macos/install.sh` |
| 重新啟動 | `launchctl kickstart -k gui/$(id -u)/com.meizhiyao.line-crm` |
| 備份出現 `Operation not permitted` | 系統設定 → 隱私權與安全性 → 完整磁碟取用權限，加入 `/bin/bash` |
| 備份出現「找不到 NAS 資料夾」 | NAS 沒開機，或共用資料夾沒掛載，回到第 2 步 |

**還原資料**（Mac mini 壞掉、換新機時）：

```bash
launchctl bootout gui/$(id -u)/com.meizhiyao.line-crm
gunzip -c /Volumes/backup/line-crm/line_crm-日期.db.gz > line_crm.db
bash deploy/macos/install.sh
```
