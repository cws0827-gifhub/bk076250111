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
cd bk076250111/line-crm/deploy/cloudflare-relay
open -e wrangler.toml     # VENDOR_URL 填入領健原本的 Webhook 網址
npx wrangler login
npx wrangler deploy
```

**領健原本的網址**：LINE Developers → 你的 channel → Messaging API → Webhook URL。
**切換前先抄下來、存好**，這也是還原用的網址。

## 3. 切換前先測試

1. 用瀏覽器打開 `https://hook.beauty-keys.com/`，應顯示 `OK`。
2. 建議先開一個**測試用的 LINE 官方帳號**，把 Webhook 指向分流站，跑一輪領健有用到的功能，確認沒問題。
3. **告知領健**：Webhook 前面多了一個轉發點，他們排查問題時才知道。

## 4. 正式切換

1. LINE Developers → Messaging API → Webhook URL 改為 `https://hook.beauty-keys.com/` → **Verify**。
2. 用自己的手機傳幾則訊息給官方帳號，確認：
   - 領健後台有收到，相關功能正常
   - 美之耀看板也出現這幾則訊息
3. 觀察一兩天。Cloudflare 後台 → Workers → line-webhook-relay → Logs，可以看到有沒有轉發錯誤。

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
