"""把 LINE 訊息分類成「需要處理的程度」。

規則式判斷（關鍵字），不呼叫外部 AI，好處是：
- 個資不出診所主機
- 判斷邏輯透明，可以隨時自己加關鍵字

狀態（優先度由高到低）：
    urgent  術後異常／不適 → 需立即回覆（醫療風險）
    reply   有提問、要預約、傳照片 → 需要回覆
    follow  考慮中、再想想 → 需要追蹤
    done    謝謝、收到、貼圖 → 無需處理
"""

import re

URGENT = "urgent"
REPLY = "reply"
FOLLOW = "follow"
DONE = "done"

PRIORITY = {DONE: 0, FOLLOW: 1, REPLY: 2, URGENT: 3}

STATUS_LABEL = {
    URGENT: "術後關懷・立即回覆",
    REPLY: "需要回覆",
    FOLLOW: "需要追蹤",
    DONE: "無需處理",
}

# 術後或療程中的不適描述 —— 醫療安全優先，一律最高優先
URGENT_WORDS = [
    "紅腫", "腫起來", "腫很大", "很腫", "過敏", "發燒", "化膿", "流膿", "起水泡", "水泡",
    "流血", "出血", "瘀青", "很痛", "好痛", "劇痛", "刺痛", "灼熱", "發熱", "癢到",
    "麻木", "不對稱", "硬塊", "結節", "發黑", "發白", "看不清", "視力", "呼吸",
    "噁心", "嘔吐", "吐了", "頭暈", "心悸", "拉肚子", "腹瀉", "胃痛",
    "急", "緊急", "怎麼辦", "正常嗎", "是正常", "會不會有事",
]

# 猶豫、延後決定 —— 不是現在要回，而是要記得回頭關心
FOLLOW_WORDS = [
    "考慮", "再想想", "想一下", "再看看", "評估一下", "再說", "下次", "之後再",
    "晚點", "改天", "下個月", "等發薪", "預算", "問一下老公", "問老公", "問男友",
    "問家人", "跟家人討論", "商量", "比較一下", "比價", "還在猶豫", "猶豫",
    "先不用", "暫時不", "有空再", "忙完",
]

# 提問、要預約 —— 需要諮詢師回覆
QUESTION_WORDS = [
    "?", "？", "嗎", "呢", "多少", "價格", "價錢", "費用", "報價", "優惠", "方案",
    "預約", "約診", "掛號", "改期", "改約", "取消", "時間", "有空", "營業",
    "可以", "能不能", "能否", "請問", "想問", "想了解", "想知道", "諮詢",
    "推薦", "適合", "效果", "維持多久", "副作用", "恢復期", "幾次", "多久",
    "地址", "停車", "在哪",
]

# 結束對話的客氣話 —— 單獨出現時不需要再回
CLOSING_WORDS = [
    "謝謝", "感謝", "3q", "thx", "thanks", "好的", "好喔", "好哦", "好唷", "ok", "okay",
    "收到", "了解", "知道了", "沒問題", "辛苦了", "掰掰", "拜拜", "晚安", "👍", "🙏",
]

# 療程興趣標籤：方便日後分眾、追蹤
TREATMENT_TAGS = {
    "皮秒": ["皮秒", "picosure", "pico", "蜂巢"],
    "音波": ["音波", "海芙", "ultherapy", "ulthera", "hifu"],
    "電波": ["電波", "鳳凰", "thermage", "塑顏"],
    "玻尿酸": ["玻尿酸", "填充", "蘋果肌", "淚溝", "法令紋", "隆鼻", "下巴"],
    "肉毒": ["肉毒", "瘦臉", "咬肌", "抬頭紋", "魚尾紋", "皺眉紋"],
    "熊貓針": ["熊貓針", "黑眼圈"],
    "猛健樂": ["猛健樂", "mounjaro", "減重", "瘦身針", "體重"],
    "痘痘/痘疤": ["痘痘", "粉刺", "痘疤", "凹疤", "毛孔"],
    "斑/膚色": ["斑", "肝斑", "暗沉", "美白", "膚色不均"],
    "鬆弛/抗老": ["鬆弛", "下垂", "拉提", "抗老", "細紋", "皺紋"],
}

# 對話階段：詢價 → 諮詢 → 預約 → 術後
STAGE_TAGS = {
    "詢價": ["多少", "價格", "價錢", "費用", "報價", "優惠"],
    "預約": ["預約", "約診", "掛號", "改期", "改約"],
    "術後": ["打完", "做完", "術後", "施打後", "雷完", "昨天做", "今天做", "回診"],
}


def _hits(text, words):
    return [w for w in words if w in text]


def _is_only_closing(text):
    """整句只是客套話（可能夾貼圖、標點）。"""
    stripped = text
    for w in sorted(CLOSING_WORDS, key=len, reverse=True):
        stripped = stripped.replace(w, "")
    stripped = re.sub(r"[\s!！~～.。,，、^_\-😊😄🥰❤️♥️]+", "", stripped)
    return len(stripped) <= 1


def detect_tags(text):
    lower = text.lower()
    tags = []
    for group in (TREATMENT_TAGS, STAGE_TAGS):
        for tag, words in group.items():
            if any(w in lower for w in words):
                tags.append(tag)
    return tags


def classify_text(text):
    """回傳 (status, reason, tags)。"""
    text = (text or "").strip()
    lower = text.lower()
    tags = detect_tags(text)

    hits = _hits(lower, URGENT_WORDS)
    if hits:
        return URGENT, f"可能有術後不適：{'、'.join(hits[:3])}", tags

    if _is_only_closing(lower):
        return DONE, "客套收尾", tags

    follow_hits = _hits(lower, FOLLOW_WORDS)
    question_hits = _hits(lower, QUESTION_WORDS)

    # 「我再考慮一下，那價格是多少？」→ 有問題就先回覆
    if question_hits and not (follow_hits and set(question_hits) <= {"嗎", "呢"}):
        return REPLY, f"客人提問：{'、'.join(question_hits[:3])}", tags

    if follow_hits:
        return FOLLOW, f"客人猶豫中：{'、'.join(follow_hits[:3])}", tags

    # 沒有明確訊號的一般陳述：保守起見仍列為需要回覆，避免漏接
    return REPLY, "一般訊息，待確認", tags


def classify_event(message_type, text=None):
    """依 LINE message type 分類。"""
    if message_type == "text":
        return classify_text(text)
    if message_type in ("image", "video"):
        return REPLY, "客人傳照片／影片（可能是膚況或術後狀況）", []
    if message_type == "sticker":
        return DONE, "貼圖", []
    if message_type == "audio":
        return REPLY, "客人傳語音", []
    if message_type == "location":
        return REPLY, "客人傳位置", []
    if message_type == "file":
        return REPLY, "客人傳檔案", []
    return REPLY, f"其他訊息（{message_type}）", []


def merge_status(current, new):
    """客人還沒被處理時，新訊息只能讓狀態往上升，不會把『緊急』蓋成『無需處理』。"""
    if current is None:
        return new
    return new if PRIORITY[new] >= PRIORITY[current] else current
