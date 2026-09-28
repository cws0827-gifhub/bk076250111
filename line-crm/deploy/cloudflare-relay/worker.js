// LINE Webhook 分流站（Cloudflare Worker）
//
// LINE 一個官方帳號只能設定一個 Webhook 網址。這個 Worker 放在最前面：
//   1. 原封不動轉給廠商（領健）→ 等他們回應，並照實回報給 LINE
//   2. 同時複製一份給美之耀 Mac mini → 送不到也不影響廠商
//
// 內容與 X-Line-Signature 都不動，兩邊各自用 Channel secret 驗證簽章仍會通過。
// 本 Worker 不驗簽、不回覆客人、不儲存任何訊息內容。

const SKIP_HEADERS = new Set(["host", "content-length", "connection", "x-forwarded-proto", "x-real-ip"]);

function forwardHeaders(request) {
  const headers = new Headers();
  for (const [key, value] of request.headers) {
    const k = key.toLowerCase();
    if (SKIP_HEADERS.has(k) || k.startsWith("cf-")) continue;
    headers.set(key, value);
  }
  return headers;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("OK");
    }

    if (!env.VENDOR_URL) {
      // 尚未設定廠商網址：回 500 讓 LINE 稍後重送，不要默默吃掉訊息
      return new Response("VENDOR_URL not configured", { status: 500 });
    }

    const body = await request.arrayBuffer();
    const headers = forwardHeaders(request);

    // ② 美之耀：背景送出，失敗只記錄，不影響回應
    if (env.CRM_URL) {
      ctx.waitUntil(
        fetch(env.CRM_URL, { method: "POST", headers, body })
          .then((r) => { if (!r.ok) console.log(`CRM 回應 ${r.status}`); })
          .catch((e) => console.log(`CRM 無法連線：${e}`))
      );
    }

    // ① 廠商：一定要等到結果，把狀態碼原樣交還給 LINE，讓 LINE 的重送機制照常運作
    try {
      const res = await fetch(env.VENDOR_URL, { method: "POST", headers, body });
      return new Response(res.body, {
        status: res.status,
        headers: { "Content-Type": res.headers.get("Content-Type") || "text/plain" },
      });
    } catch (e) {
      console.log(`廠商無法連線：${e}`);
      return new Response("vendor unreachable", { status: 502 });
    }
  },
};
