"""每日簡報產生器：抓 RSS 標題 -> 請 Gemini 整理 -> 寫出 data.json
由 GitHub Actions 每天自動執行；環境變數 GEMINI_API_KEY（免費金鑰）。
沒有金鑰或 AI 失敗時，自動改為「只列標題」模式，網頁照常更新。
"""
import os
import json
import html
import time
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

TW = timezone(timedelta(hours=8))
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")  # 想換模型改這裡或 workflow
API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ---------- 1. 固定行程（你可以自己新增／修改）----------
# date 與 end 格式：YYYY-MM-DD；end 可省略
CALENDAR = [
    {"date": "2026-10-10", "tag": "國內", "t": "中華民國國慶日"},
    {"date": "2026-10-12", "end": "2026-10-18", "tag": "IMF/世銀",
     "t": "IMF–世界銀行年會（曼谷）", "s": "10/16 全體會議"},
]

# ---------- 2. 新聞來源 ----------
def google_news(query, limit=8):
    q = urllib.parse.quote(query + " when:1d")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    return (f"搜尋:{query}", url, limit)

FEEDS = [
    ("中央社-政治", "https://feeds.feedburner.com/rsscna/politics", 10),
    ("中央社-國際", "https://feeds.feedburner.com/rsscna/intworld", 10),
    ("中央社-兩岸", "https://feeds.feedburner.com/rsscna/mainland", 8),
    ("中央社-財經", "https://feeds.feedburner.com/rsscna/finance", 8),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", 10),
    google_news("川普 行程 OR 會晤 OR 訪問"),
    google_news("習近平 會見 OR 訪問 OR 會議"),
    google_news("日本首相 會談 OR 訪問"),
    google_news("韓國總統 會談 OR 訪問"),
    google_news("聯合國 OR G20 OR APEC OR 金磚 OR 東協 峰會"),
    google_news("IMF OR 世界銀行 報告 OR 預測"),
    google_news("立法院 院會 OR 三讀 OR 委員會"),
    google_news("行政院會 政策 OR 通過"),
]


def fetch_items(name, url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 daily-brief"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        if title and link:
            out.append({"src": name, "title": title, "link": link})
        if len(out) >= limit:
            break
    return out


def collect():
    items, seen = [], set()
    for name, url, limit in FEEDS:
        try:
            got = fetch_items(name, url, limit)
            print(f"[ok] {name}: {len(got)}")
        except Exception as e:  # 某個來源壞掉不影響其他來源
            print(f"[skip] {name}: {e}")
            continue
        for it in got:
            key = it["title"][:18]
            if key in seen:
                continue
            seen.add(key)
            it["id"] = len(items)
            items.append(it)
    return items[:160]


# ---------- 3. 請 Gemini 整理 ----------
PROMPT = """你是每日新聞簡報編輯。讀者是台灣的公務員，每天早上要在 1 分鐘內讀完。
以下是今天抓到的新聞標題清單（格式：[編號] 來源 | 標題）。

規則（非常重要）：
1. 只能根據清單內的標題整理，不可加入清單以外的事實、日期或數字；標題沒說的就不要寫。
2. 每則重點用繁體中文，text 不超過 28 字，detail 不超過 45 字（可省略）。
3. 每則必須附 refs：支持該則的標題編號（1~2 個整數）。
4. 清單中沒有相關內容的分類，items 給空陣列，不要硬湊。
5. 同一事件只寫一則；依重要性排序。

請只輸出一個 JSON 物件，不要任何其他文字，格式：
{
 "top": [ {"text":"", "detail":"", "refs":[0]} ],            // 今日最重要 3~5 則
 "sections": [
  {"title":"國際領袖動態", "items":[{"text":"","detail":"","refs":[0]}]},   // 美國、中國、日本、韓國等領袖行程與會談，最多 5 則
  {"title":"國際會議與機構", "items":[...]},                                 // 聯合國、G20、APEC、金磚、IMF、世銀等，最多 4 則
  {"title":"台灣政府與立法", "items":[...]},                                 // 立法院、行政院、重要政策，最多 5 則
  {"title":"其他重要新聞", "items":[...]}                                    // 最多 4 則
 ]
}

標題清單：
"""


def summarize(items):
    lines = "\n".join(f"[{i['id']}] {i['src']} | {i['title']}" for i in items)
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent")
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT + lines}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode("utf-8")
    last_err = None
    for attempt in range(4):  # 遇到忙碌(429/503)最多重試 4 次
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "x-goog-api-key": API_KEY})
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < 0:
                raise RuntimeError("回傳內容中找不到 JSON：" + text[:200])
            return json.loads(text[start:end + 1])
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read()[:300]!r}"
            print("[retry]", last_err)
            if e.code not in (429, 500, 503):
                break
            time.sleep(20 * (attempt + 1))
        except Exception as e:
            last_err = str(e)
            print("[retry]", last_err)
            time.sleep(5)
    raise RuntimeError("Gemini 呼叫失敗：" + str(last_err))


def headlines_only(items):
    """備用模式：不用 AI，直接依來源列出標題。"""
    def mk(it):
        return {"text": it["title"], "detail": "",
                "links": [{"src": it["src"], "url": it["link"]}]}
    groups = [("中央社 · 國際與兩岸", ("中央社-國際", "中央社-兩岸")),
              ("台灣政治與財經", ("中央社-政治", "中央社-財經")),
              ("BBC 國際", ("BBC World",)),
              ("主題追蹤（領袖、會議、立法院）", None)]
    sections = []
    for title, srcs in groups:
        if srcs is None:
            sel = [i for i in items if i["src"].startswith("搜尋:")]
        else:
            sel = [i for i in items if i["src"] in srcs]
        sections.append({"title": title, "items": [mk(i) for i in sel[:6]]})
    return {"top": [mk(i) for i in items[:5]], "sections": sections}


def attach_links(block, by_id):
    """把 refs 編號換成真正的來源連結（由程式處理，避免 AI 亂編網址）。"""
    for item in block:
        links = []
        for r in item.pop("refs", [])[:2]:
            src = by_id.get(r) if isinstance(r, int) else None
            if src:
                links.append({"src": src["src"], "url": src["link"]})
        item["links"] = links


def main():
    items = collect()
    if not items:
        raise SystemExit("沒有抓到任何新聞，保留舊的 data.json。")
    by_id = {i["id"]: i for i in items}
    ai_used = False
    data = None
    if API_KEY:
        try:
            data = summarize(items)
            ai_used = True
        except Exception as e:
            print("[AI 失敗，改用只列標題模式]", e)
    else:
        print("[沒有 GEMINI_API_KEY，使用只列標題模式]")
    if data is None:
        data = headlines_only(items)
    else:
        attach_links(data.get("top", []), by_id)
        for sec in data.get("sections", []):
            attach_links(sec.get("items", []), by_id)
    data["calendar"] = CALENDAR
    data["weekly"] = [{"dow": 4, "tag": "行政院", "t": "行政院院會（每週四上午）"}]
    data["updated"] = datetime.now(TW).strftime("%Y-%m-%d %H:%M")
    data["source_count"] = len(items)
    data["ai"] = ai_used
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("data.json 已更新：", data["updated"], "| AI 摘要：", ai_used)


if __name__ == "__main__":
    main()
