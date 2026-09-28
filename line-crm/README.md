# 美之耀 LINE 官方帳號・客戶狀況追蹤

客人傳 LINE 給官方帳號 → 系統自動記錄並分類 → 諮詢師打開管理頁，一眼看到誰要先處理。

| 分類 | 什麼時候會出現 | 建議動作 |
|---|---|---|
| 🔴 術後關懷・立即回覆 | 客人提到紅腫、疼痛、過敏、頭暈、「正常嗎」「怎麼辦」 | 醫師／護理師優先確認 |
| 🟠 需要回覆 | 有提問、想預約、傳照片、新加好友 | 先了解膚況與歷史療程，再給方案 |
| 🔵 今天該追蹤 | 之前「考慮中」的客人，追蹤日到了 | 主動關心、補衛教、邀約諮詢 |
| 🔵 追蹤中 | 客人說「再考慮」「問老公」「等發薪」 | 系統自動排 3 天後追蹤 |
| ⚪ 無需處理 | 「謝謝」「好的」、貼圖，或已按「已回覆」 | — |

同時會自動貼上**療程標籤**（皮秒、音波、電波、玻尿酸、肉毒、熊貓針、猛健樂…）與**階段標籤**（詢價、預約、術後），方便日後分眾經營。

設計原則：
- **不自動回覆**。系統只負責「記錄、分類、提醒」，回覆仍由諮詢師用顧問式的方式親自處理。
- **不會漏掉緊急狀況**：客人先說「好腫」再說「謝謝」，仍然維持在最高優先，並顯示那則關鍵訊息。
- **個資不外流**：分類用關鍵字判斷，訊息只存在你自己的主機（SQLite），管理頁需帳號密碼。

---

## 一、LINE 後台設定（約 10 分鐘）

1. 到 [LINE Developers](https://developers.line.biz/console/) 用管理官方帳號的 LINE 登入。
   若官方帳號還沒開通 Messaging API：到 [LINE Official Account Manager](https://manager.line.biz/) → 設定 → Messaging API → 啟用。
2. 在 channel 的 **Basic settings** 複製 `Channel secret`。
3. 在 **Messaging API** 頁籤最下方發行 `Channel access token (long-lived)` 並複製。
4. 同一頁的 **Webhook settings**：
   - Webhook URL 填 `https://你的網址/callback`（部署後才有，見第三步）
   - 開啟 **Use webhook**，按 **Verify** 應顯示 Success
5. 到 Official Account Manager → 設定 → **回應設定**：
   - 「聊天」**開啟**（諮詢師繼續在聊天室手動回覆）
   - 「Webhook」**開啟**
   - 「自動回應訊息」依需求，建議關閉，避免罐頭訊息破壞品牌感

> ⚠️ 重要：在官方帳號聊天室**手動回覆的訊息不會傳到 webhook**，系統無法自動得知你已經回了。
> 所以回完客人後，請在管理頁按「已回覆」或「回覆了，3 天後追蹤」。

## 二、本機試跑

```bash
cd line-crm
pip install -r requirements.txt
cp .env.example .env        # 填入上面拿到的值
set -a; source .env; set +a
python app.py               # http://localhost:8765
```

LINE 需要 https 公開網址才能呼叫 webhook，本機測試可用 [ngrok](https://ngrok.com/)：
`ngrok http 8765`，把它給的 `https://xxxx.ngrok-free.app/callback` 貼到 Webhook URL。

## 三、正式部署

- **Mac mini ＋ Synology**：照 [`deploy/macos/README.md`](deploy/macos/README.md)，一個指令就能安裝完成，含開機自動啟動、每晚備份到 NAS、Cloudflare Tunnel。
- 其他主機（Render、Railway、Zeabur…）：啟動指令為 `gunicorn -w 1 -b 0.0.0.0:$PORT app:app`，環境變數照 `.env.example` 設定。主機要有永久磁碟，並定期備份 `line_crm.db`。

## 四、日常使用

- `/` 看板：依優先順序分區，手機也好操作。上方可搜尋暱稱、療程、備註。
- 點客人名字：看完整訊息紀錄、寫**諮詢備註**（膚況、在意的點、歷史療程、預算），設定追蹤日。
- `匯出 CSV`：用 Excel 開啟，可做月報或 VIP 分層。

## 五、調整分類規則

全部規則在 `classifier.py`，都是中文關鍵字清單：

- `URGENT_WORDS` 術後不適（最高優先）
- `FOLLOW_WORDS` 猶豫、延後
- `QUESTION_WORDS` 提問、預約
- `CLOSING_WORDS` 客套收尾
- `TREATMENT_TAGS`／`STAGE_TAGS` 標籤

加完關鍵字跑一次測試確認沒有改壞：

```bash
python -m unittest discover -s tests
```

## 六、個資提醒

LINE 對話屬於個人資料，醫美的術後描述更可能涉及健康資訊：
- 管理頁務必設定強密碼，只給需要的同仁。
- 資料庫檔案不要放進 Git、不要傳到共用雲端資料夾。
- 客人要求刪除資料時，可直接刪除 `customers` 與 `messages` 中該 `user_id` 的資料。
