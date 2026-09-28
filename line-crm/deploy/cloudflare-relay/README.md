# LINE Webhook 分流：領健 ＋ 美之耀 同時收訊息

LINE 一個官方帳號只能設定**一個** Webhook 網址，而目前已經接在領健。

```
                    ┌─①→ 領健（原本的網址）  ← 一定等到回應，並照實回報給 LINE
LINE → hook.beauty-keys.com（Cloudflare Worker）
                    └─②→ line.beauty-keys.com/callback → Cloudflare Tunnel → Mac mini
                         （背景複製一份，Mac mini 離線也不影響領健）
```

| 網址 | 用途 |
|---|---|
| `hook.beauty-keys.com` | 分流站，LINE 的 Webhook 改填這個 |
| `line.beauty-keys.com/callback` | Mac mini，只開放這一個路徑，管理頁不對外 |

## 先試著走最簡單的路

**先問領健能不能把 Webhook 轉發一份到 `https://line.beauty-keys.com/callback`。**
可以的話，這個分流站就不需要了：只要完成下面第 1 步和 Mac mini 的 Cloudflare Tunnel，LINE 設定完全不用動。

---

## 1. 把 beauty-keys.com 的 DNS 交給 Cloudflare

1. 註冊／登入 [Cloudflare](https://dash.cloudflare.com/) → Add a site → `beauty-keys.com` → 選 Free 方案。
2. Cloudflare 會自動掃描現有的 DNS 紀錄。**逐筆核對**，特別是：
   - 官網的 `@`、`www` 紀錄
   - **收信用的 `MX` 和 `TXT`（SPF、DKIM）紀錄**：漏掉的話，公司信箱會收不到信
3. 到網域註冊商（買網域的地方）把 Nameserver 改成 Cloudflare 給的那兩組。
4. 等 Cloudflare 顯示 **Active**（通常幾分鐘到幾小時），再確認官網、信箱都正常。

> 如果官網或信箱是別的廠商在管，改 Nameserver 前先跟他們確認。

## 2. 部署分流站

先完成 Mac mini 的安裝和 Cloudflare Tunnel（見 [`../macos/README.md`](../macos/README.md)），然後在 Mac mini 上執行：

```bash
cd ~/bk076250111/line-crm/deploy/cloudflare-relay
npx wrangler login                 # 瀏覽器授權 Cloudflare
npx wrangler secret put VENDOR_URL  # 貼上領健原本的 Webhook 網址（不會顯示、不會存進 repo）
npx wrangler deploy
```

**領健原本的網址**：LINE Developers → 你的 channel → Messaging API → Webhook URL。
**切換前先抄下來，存在密碼管理器**，這也是還原用的網址。
⚠️ 這個 repo 是公開的，**不要把領健的網址寫進任何檔案或 commit**。

## 3. 切換前的驗證（不影響任何人）

用一則**沒有 LINE 簽章的假訊息**，分別直接打給領健、以及打給分流站，比較兩邊的回應。
兩者一致，代表分流站把請求原封不動送到了領健；假訊息沒有有效簽章，領健和 Mac mini 都會拒收，不會產生任何資料。

```bash
BODY='{"destination":"test","events":[]}'
H='X-Line-Signature: invalid-test-signature'
# ① 直接打領健（從密碼管理器貼上網址，不要存進檔案）
read -rs VENDOR; echo
curl -s -o /dev/null -w '直接打領健：%{http_code}\n' -X POST "$VENDOR" -H "$H" -H 'Content-Type: application/json' -d "$BODY"
# ② 透過分流站
curl -s -o /dev/null -w '透過分流站：%{http_code}\n' -X POST https://hook.beauty-keys.com/ -H "$H" -H 'Content-Type: application/json' -d "$BODY"
unset VENDOR
# ③ Mac mini 也收到了（應該看到一筆 POST /callback 400）
tail -5 ~/Library/Logs/line-crm/com.meizhiyao.line-crm.log
```

**①② 的狀態碼必須相同**才能繼續。不同的話，先不要切換。

## 4. 正式切換

建議挑**客人訊息少的時段**（例如晚上診所休息後）。

1. LINE Developers → Messaging API → Webhook URL 改為 `https://hook.beauty-keys.com/` → **Update** → **Verify**
   - Verify 顯示 **Success** → 繼續
   - Verify 失敗 → **立刻改回領健原本的網址**，把錯誤訊息記下來
2. 用自己的手機傳幾則訊息給官方帳號，例如「測試：請問皮秒多少錢」「測試：打完很腫怎麼辦」，確認：
   - **美之耀看板**出現這幾則訊息，並且分類正確
   - **領健**原本的功能照常運作：他們的後台看得到訊息；如果有自動回覆、預約或會員綁定，也各試一次
3. 觀察 24 小時：Cloudflare 後台 → Workers → line-webhook-relay → **Logs**，
   出現「廠商無法連線」或「CRM 無法連線」就要處理。
4. **通知領健**：Webhook 前面多了一個轉發點（`hook.beauty-keys.com`），轉送內容與簽章都沒有改動。之後他們排查問題時才知道。

## 出問題時立刻還原

LINE Developers → Webhook URL **改回領健原本的網址** → Verify。
一分鐘內就會恢復成原本的狀態，Mac mini 那邊只是暫時收不到新訊息。

## 這個分流站的設計

- **領健永遠優先**：LINE 收到的回應就是領健的回應；領健出錯時，LINE 的自動重送機制也照常運作。
- **Mac mini 出問題不影響領健**：Mac mini 那份在背景送出，送不到只會記在 Logs 裡。
- **內容原封不動**：不修改訊息，也保留 LINE 的簽章，兩邊都能各自驗證訊息來源。
- **不儲存、不回覆**：分流站不保存客人訊息，也不會回覆客人，不會和領健搶著回覆。
- Cloudflare Workers 免費方案每天有 10 萬次請求，診所的訊息量遠遠用不完。

測試：`node test.mjs`
