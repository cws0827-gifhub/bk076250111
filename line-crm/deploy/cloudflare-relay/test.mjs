// node test.mjs —— 用假的 fetch 驗證分流行為
import assert from "node:assert/strict";
import worker from "./worker.js";

const env = { VENDOR_URL: "https://vendor.example/hook", CRM_URL: "https://line.beauty-keys.com/callback" };
const body = '{"events":[{"type":"message"}]}';

function makeRequest() {
  return new Request("https://hook.beauty-keys.com/", {
    method: "POST", body,
    headers: { "Content-Type": "application/json", "X-Line-Signature": "sig123", "User-Agent": "LineBotWebhook/2.0", "CF-Connecting-IP": "1.2.3.4" },
  });
}

async function run(fakeFetch) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, headers: init.headers, body: new TextDecoder().decode(init.body) });
    return fakeFetch(url);
  };
  const pending = [];
  const res = await worker.fetch(makeRequest(), env, { waitUntil: (p) => pending.push(p) });
  await Promise.all(pending);
  return { res, calls };
}

// 1. 兩邊都收到一模一樣的內容與簽章；LINE 拿到廠商的狀態碼
{
  const { res, calls } = await run(async (url) => new Response("vendor ok", { status: url.includes("vendor") ? 200 : 500 }));
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "vendor ok");
  assert.deepEqual(calls.map((c) => c.url).sort(), [env.CRM_URL, env.VENDOR_URL].sort());
  for (const c of calls) {
    assert.equal(c.body, body);
    assert.equal(c.headers.get("X-Line-Signature"), "sig123");
    assert.equal(c.headers.get("User-Agent"), "LineBotWebhook/2.0");
    assert.equal(c.headers.get("CF-Connecting-IP"), null);
  }
}

// 2. Mac mini 離線：廠商照常，LINE 收到 200
{
  const { res } = await run(async (url) => { if (url.includes("beauty-keys")) throw new Error("down"); return new Response("ok"); });
  assert.equal(res.status, 200);
}

// 3. 廠商回錯誤：原樣回報給 LINE（讓 LINE 重送）
{
  const { res } = await run(async (url) => new Response("err", { status: url.includes("vendor") ? 500 : 200 }));
  assert.equal(res.status, 500);
}

// 4. 廠商連不上：回 502
{
  const { res } = await run(async (url) => { if (url.includes("vendor")) throw new Error("down"); return new Response("ok"); });
  assert.equal(res.status, 502);
}

console.log("分流站測試全部通過");
