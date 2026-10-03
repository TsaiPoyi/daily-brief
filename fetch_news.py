"""每日簡報產生器（早報／午報／晚報）
流程：抓 RSS 標題 -> Gemini 整理核心新聞與目錄類 -> Gemini 寫「AI 今日總摘要」-> 存成 data/<edition>.json
由 GitHub Actions 每天跑三次；環境變數：GEMINI_API_KEY（免費金鑰）、EDITION（auto/morning/noon/evening）。
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
# 模型清單：依序嘗試，前一個不可用（下架、免費版不開放）就自動換下一個。
# 想指定模型可在 workflow 設環境變數 GEMINI_MODEL。
MODELS = ([os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else []) + [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
    "gemini-3-flash-preview", "gemini-2.5-flash"]
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GOOD_MODEL = None  # 第一次成功的模型，之後直接沿用
VERSION = "v6-三報+完整總摘要"  # 版本標記：網頁頂端會顯示，用來確認 GitHub 上跑的是不是最新版

EDITIONS = {"morning": "早報", "noon": "午報", "evening": "晚報"}

# ---------- 1. 固定行程（你可以自己新增／修改）----------
# date 與 end 格式：YYYY-MM-DD；end 可省略
CALENDAR = [
    {"date": "2026-10-05", "tag": "諾貝爾", "t": "諾貝爾生理學或醫學獎公布"},
    {"date": "2026-10-06", "tag": "諾貝爾", "t": "諾貝爾物理學獎公布"},
    {"date": "2026-10-07", "tag": "諾貝爾", "t": "諾貝爾化學獎公布"},
    {"date": "2026-10-08", "tag": "諾貝爾", "t": "諾貝爾文學獎公布"},
    {"date": "2026-10-09", "tag": "諾貝爾", "t": "諾貝爾和平獎公布"},
    {"date": "2026-10-10", "tag": "國內", "t": "中華民國國慶日"},
    {"date": "2026-10-12", "tag": "諾貝爾", "t": "諾貝爾經濟學獎公布"},
    {"date": "2026-10-12", "end": "2026-10-18", "tag": "IMF/世銀",
     "t": "IMF–世界銀行年會（曼谷）", "s": "10/16 全體會議"},
]

# ---------- 2. 追蹤主題 ----------
# 核心（頁面上方）：領袖、會議、台灣
# 目錄式（預設收合，但每個主題都會列出，沒新聞就標「近期無相關新聞」）：縣市、委員會、部會、學術
CORE_GROUPS = ("領袖", "會議", "台灣")
DIR_GROUPS = ("縣市", "委員會", "部會", "學術")
DIR_TITLES = {"縣市": "各縣市大事", "委員會": "立法院各委員會",
              "部會": "行政院各部會", "學術": "學術與知識界"}


def T(label, query, group, days=1, lang="zh", limit=6):
    return {"label": label, "query": query, "group": group,
            "days": days, "lang": lang, "limit": limit}


TOPICS = [
    T("川普", "川普 行程 OR 會晤 OR 訪問", "領袖", limit=8),
    T("習近平", "習近平 會見 OR 訪問 OR 會議", "領袖", limit=8),
    T("日本", "日本首相 會談 OR 訪問", "領袖", limit=8),
    T("韓國", "韓國總統 會談 OR 訪問", "領袖", limit=8),
    T("國際會議", "聯合國 OR G20 OR APEC OR 金磚 OR 東協 峰會", "會議", limit=8),
    T("IMF/世銀", "IMF OR 世界銀行 報告 OR 預測", "會議", limit=8),
    T("立法院", "立法院 院會 OR 三讀 OR 委員會", "台灣", limit=8),
    T("行政院", "行政院會 政策 OR 通過", "台灣", limit=8),
]

COUNTIES = ["臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市", "基隆市",
            "新竹市", "嘉義市", "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣",
            "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣"]
for c in COUNTIES:
    short = c.replace("臺", "台")  # 媒體多用「台」，搜尋用這個比較準
    kw = "市長 OR 市府 OR 市議會" if c.endswith("市") else "縣長 OR 縣府 OR 縣議會"
    TOPICS.append(T(c, f"{short} {kw}", "縣市", days=2, limit=4))

COMMITTEES = ["內政", "外交及國防", "經濟", "財政", "教育及文化", "交通",
              "司法及法制", "社會福利及衛生環境"]
for c in COMMITTEES:  # 委員會新聞稀疏（10/8 才選召委），搜尋 7 天
    TOPICS.append(T(f"{c}委員會", f"立法院 {c}委員會", "委員會", days=7, limit=5))

MINISTRIES = [
    ("內政部", "內政部"), ("外交部", "外交部"), ("國防部", "國防部"), ("財政部", "財政部"),
    ("教育部", "教育部"), ("法務部", "法務部"), ("經濟部", "經濟部"), ("交通部", "交通部"),
    ("勞動部", "勞動部"), ("農業部", "農業部"), ("衛福部", "衛福部 OR 衛生福利部"),
    ("環境部", "環境部"), ("文化部", "文化部"), ("數位發展部", "數位發展部"),
    ("國科會", "國科會 OR 國家科學及技術委員會"), ("金管會", "金管會 OR 金融監督管理委員會"),
    ("國發會", "國發會 OR 國家發展委員會"), ("陸委會", "陸委會 OR 大陸委員會"),
    ("運動部", "運動部"),
]
for label, q in MINISTRIES:
    TOPICS.append(T(label, f"{q} 宣布 OR 公告 OR 政策 OR 修正", "部會", days=3, limit=5))

TOPICS += [
    T("哲學", "哲學 學者 OR 新書 OR 研究 OR 論壇", "學術", days=3, limit=4),
    T("文學", "文學獎 OR 作家 OR 新書 OR 詩人", "學術", days=3, limit=4),
    T("藝術", "藝術 展覽 OR 藝術家 OR 雙年展 OR 美術館", "學術", days=3, limit=4),
    T("社會學", "社會學 研究 OR 調查 OR 學者", "學術", days=3, limit=4),
    T("經濟學", "經濟學家 OR 經濟學 研究 OR 諾貝爾經濟學獎", "學術", days=3, limit=4),
    T("心理學", "心理學 研究 OR 心理學家", "學術", days=3, limit=4),
    T("法律", "大法官 OR 憲法法庭 OR 法律 修法 OR 判決", "學術", days=2, limit=4),
    T("數學", "mathematics breakthrough OR mathematician OR theorem", "學術", days=7, lang="en", limit=4),
    T("物理學", "physics discovery OR physicists", "學術", days=3, lang="en", limit=4),
    T("生物學", "biology discovery OR biologists OR genome", "學術", days=3, lang="en", limit=4),
    T("工程", "engineering breakthrough OR semiconductor OR robotics research", "學術", days=3, lang="en", limit=4),
    T("醫學", "medical study OR clinical trial OR FDA approval", "學術", days=3, lang="en", limit=4),
]
TOPIC_BY_LABEL = {t["label"]: t for t in TOPICS}
TOPIC_ORDER = {t["label"]: n for n, t in enumerate(TOPICS)}

# ---------- 3. 新聞來源 ----------
BASE_FEEDS = [
    ("中央社-政治", "https://feeds.feedburner.com/rsscna/politics", 10),
    ("中央社-國際", "https://feeds.feedburner.com/rsscna/intworld", 10),
    ("中央社-兩岸", "https://feeds.feedburner.com/rsscna/mainland", 8),
    ("中央社-財經", "https://feeds.feedburner.com/rsscna/finance", 8),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", 10),
]


def google_news_url(t):
    q = urllib.parse.quote(f"{t['query']} when:{t['days']}d")
    if t["lang"] == "en":
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"


FEEDS = BASE_FEEDS + [("主題:" + t["label"], google_news_url(t), t["limit"]) for t in TOPICS]


def fetch_items(name, url, limit):
    label = name[3:] if name.startswith("主題:") else None
    group = TOPIC_BY_LABEL[label]["group"] if label else None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 daily-brief"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        outlet = html.unescape((it.findtext("source") or "").strip())
        if outlet and title.endswith(" - " + outlet):  # 拆掉標題尾巴的「 - 媒體名」
            title = title[: -(len(outlet) + 3)].strip()
        if title and link:
            out.append({"src": outlet or (name if not label else "Google 新聞"),
                        "topic": label, "group": group, "title": title, "link": link})
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
            got = []
        if name.startswith("主題:"):
            time.sleep(0.4)  # 對 Google 客氣一點，避免被擋
        for it in got:
            key = (it["group"], it["title"][:18])  # 只在同一分組內去重，不同分組可各自保留
            if key in seen:
                continue
            seen.add(key)
            it["id"] = len(items)
            items.append(it)
    return items


# ---------- 4. 請 Gemini 整理 ----------
RULES = """規則（非常重要）：
1. 只能根據清單內的標題整理，不可加入清單以外的事實、日期或數字；標題沒說的就不要寫。
2. 每則用繁體中文（英文標題請譯成中文），text 不超過 28 字，detail 不超過 45 字（可省略）。
3. 每則必須附 refs：支持該則的標題編號（1~2 個整數）。
4. 沒有相關內容就給空陣列或省略，不要硬湊。
5. 同一事件只寫一則；依重要性排序。
"""

PROMPT_CORE = """你是每日新聞簡報編輯。讀者是台灣的公務員，每天早上要快速掌握大事。
以下是抓到的新聞標題清單（格式：[編號] 來源 | 標題）。

""" + RULES + """
請只輸出一個 JSON 物件，不要任何其他文字，格式：
{
 "top": [ {"text":"", "detail":"", "refs":[0]} ],
 "sections": [
  {"title":"國際領袖動態", "items":[{"text":"","detail":"","refs":[0]}]},
  {"title":"國際會議與機構", "items":[]},
  {"title":"台灣政府與立法", "items":[]},
  {"title":"其他重要新聞", "items":[]}
 ]
}
說明：top 為最重要 3~5 則；「國際領袖動態」最多 6 則，每位人物最多 2 則，讓美、中、日、韓盡量都有機會出現；
「國際會議與機構」（聯合國、G20、APEC、金磚、IMF、世銀等）最多 5 則；「台灣政府與立法」（立法院、行政院、重要政策）最多 6 則；
「其他重要新聞」最多 5 則。

標題清單：
"""

PROMPT_DIR = """你是每日新聞簡報編輯。以下標題已依「主題」分類（格式：[編號] 來源（主題:xxx） | 標題）。
請為每個有新聞的主題各寫 1 則最值得一看的重點（重要的主題可寫到 2 則）。

""" + RULES + """6. 每則另加 tag 欄位，內容就是該標題的「主題」名稱（例如 臺北市、內政委員會、衛福部、物理學）。
7. 只要某主題有標題，就為它寫 1 則（即使是例行消息也要寫）；只有該主題完全沒有標題，或標題與主題無關時才略過。
8. 學術類（哲學、文學、藝術、社會學、經濟學、心理學、法律、數學、物理學、生物學、工程、醫學）優先挑「新發現、重要獎項、重大研究或政策」。

請只輸出一個 JSON 物件，不要任何其他文字，格式：
{
 "sections": [
  {"title":"各縣市大事", "items":[{"tag":"","text":"","detail":"","refs":[0]}]},
  {"title":"立法院各委員會", "items":[]},
  {"title":"行政院各部會", "items":[]},
  {"title":"學術與知識界", "items":[]}
 ]
}

標題清單：
"""

PROMPT_OVERVIEW = """你是每日新聞簡報的總編輯，現在要為「{LABEL}」寫「AI 今日總摘要」。
下面是本期已整理好的全部重點（涵蓋國際、台灣中央的行政院與立法院、各縣市、學術）{PREV_NOTE}

寫作要求：
1. 只能根據下面提供的內容整理，不可加入任何未提供的事實、日期、數字或推測。
2. 輸出 JSON：{"overview": ["段落1", "段落2", ...]}，每個元素是一段，段落開頭用【】標明主題。
3. 段落順序與內容：
{PARAS}
4. 每段不超過 130 字，全部加起來不超過 520 字；用流暢的繁體中文句子串起重點，不要只是列標題。
5. 某個領域本期沒有內容，就用一句話寫「本期無明顯重大消息」，不要編造。

本期重點：
"""


def line(i):
    topic = f"（主題:{i['topic']}）" if i.get("topic") else ""
    return f"[{i['id']}] {i['src']}{topic} | {i['title']}"


def list_models():
    """全部失敗時，列出這把金鑰實際能用的模型，方便你回報給我。"""
    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
            headers={"x-goog-api-key": API_KEY})
        with urllib.request.urlopen(req, timeout=30) as r:
            names = [m["name"].split("/")[-1] for m in json.loads(r.read()).get("models", [])]
        print("[可用模型]", [n for n in names if "flash" in n or "pro" in n])
    except Exception as e:
        print("[無法列出模型]", e)


def call_model(model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.2, "maxOutputTokens": 16384},
    }).encode("utf-8")
    for attempt in range(3):  # 忙碌(429/500/503)時重試
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "x-goog-api-key": API_KEY})
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read())
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < 0:
                raise ValueError("回傳內容中找不到 JSON：" + text[:200])
            return json.loads(text[start:end + 1])
        except urllib.error.HTTPError as e:
            msg = e.read()[:300]
            print(f"[{model}] HTTP {e.code}: {msg!r}")
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            raise
        except (ValueError, KeyError, IndexError) as e:  # 內容格式不對，重試一次
            print(f"[{model}] 內容異常：{e}")
            if attempt < 1:
                continue
            raise


def call_gemini(prompt):
    global GOOD_MODEL
    order = [GOOD_MODEL] if GOOD_MODEL else MODELS
    last = None
    for m in order:
        try:
            out = call_model(m, prompt)
            if GOOD_MODEL != m:
                print("[使用模型]", m)
            GOOD_MODEL = m
            return out
        except Exception as e:
            last = e
            print(f"[{m} 不可用，嘗試下一個] {e}")
    list_models()
    raise RuntimeError(f"所有模型都失敗：{last}")


def attach_links(block, by_id):
    """把 refs 編號換成真正的來源連結（由程式處理，避免 AI 亂編網址）。"""
    for item in block:
        links = []
        for r in item.pop("refs", [])[:2]:
            src = by_id.get(r) if isinstance(r, int) else None
            if src:
                links.append({"src": src["src"], "url": src["link"]})
        item["links"] = links
        if not isinstance(item.get("tag", ""), str):
            item["tag"] = ""


# ---------- 5. 備用模式、補齊、版面 ----------
def mk(it, tag=""):
    return {"tag": tag, "text": it["title"], "detail": "",
            "links": [{"src": it["src"], "url": it["link"]}]}


def placeholder(label):
    return {"tag": label, "text": "近期無相關新聞", "detail": "", "links": [], "empty": True}


def core_headlines(items):
    """每個關鍵字各取固定幾則，避免被單一主題（如川普）佔滿。"""
    def by_topic(label):
        return [i for i in items if i.get("topic") == label]

    def by_src(name):
        return [i for i in items if i["src"] == name and not i.get("topic")]

    groups = [("國際領袖動態", "領袖", 2), ("國際會議與機構", "會議", 3),
              ("台灣政府與立法", "台灣", 3)]
    sections = []
    for title, g, n in groups:
        its = []
        for t in TOPICS:
            if t["group"] == g:
                its += [mk(i, t["label"]) for i in by_topic(t["label"])[:n]]
        if g == "台灣":
            its += [mk(i, "政治") for i in by_src("中央社-政治")[:3]]
        sections.append({"title": title, "items": its})
    other = ([mk(i, "國際") for i in by_src("中央社-國際")[:3]]
             + [mk(i, "兩岸") for i in by_src("中央社-兩岸")[:2]]
             + [mk(i, "BBC") for i in by_src("BBC World")[:3]]
             + [mk(i, "財經") for i in by_src("中央社-財經")[:2]])
    sections.append({"title": "其他重要新聞", "items": other})
    return {"top": [], "sections": sections}


def dir_headlines(items):
    """目錄式備用模式：每個主題 1 則標題，沒有新聞的標「近期無相關新聞」。"""
    sections = []
    for g, title in DIR_TITLES.items():
        its = []
        for t in TOPICS:
            if t["group"] == g:
                got = [i for i in items if i.get("topic") == t["label"]]
                its.append(mk(got[0], t["label"]) if got else placeholder(t["label"]))
        sections.append({"title": title, "items": its})
    return {"sections": sections}


def backfill(dirs, dir_items):
    """AI 若漏掉某些主題：有標題就用第一則補上；真的沒新聞才標「近期無相關新聞」。
    結果：每個縣市／委員會／部會／學科都一定會出現。"""
    secs = {x.get("title"): x for x in dirs.get("sections", [])}
    for g, title in DIR_TITLES.items():
        sec = secs.get(title)
        if sec is None:
            sec = {"title": title, "items": []}
            dirs.setdefault("sections", []).append(sec)
            secs[title] = sec
        for t in TOPICS:
            if t["group"] != g:
                continue
            lab, matched = t["label"], False
            for it in sec["items"]:
                h = str(it.get("tag", ""))
                if h and (h == lab or h in lab or lab in h):
                    it["tag"], matched = lab, True
            if not matched:
                got = [i for i in dir_items if i.get("topic") == lab]
                sec["items"].append(mk(got[0], lab) if got else placeholder(lab))
        sec["items"].sort(key=lambda i: TOPIC_ORDER.get(i.get("tag"), 999))


# ---------- 6. 三報：前期內容、總摘要、「新」標記 ----------
def pick_edition():
    e = os.environ.get("EDITION", "auto").strip()
    if e in EDITIONS:
        return e
    h = datetime.now(TW).hour
    return "morning" if h < 10 else "noon" if h < 16 else "evening"


def load_prev(edition, today):
    """讀取今天較早的各期（早報→午報→晚報），供總摘要回顧與標記「新」。"""
    order = list(EDITIONS)
    out = []
    for e in order[:order.index(edition)]:
        try:
            with open(f"data/{e}.json", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == today:
                out.append((e, d))
        except Exception:
            pass
    return out


def all_items(data):
    for it in data.get("top", []):
        yield "今日重點", it
    for sec in data.get("sections", []):
        for it in sec.get("items", []):
            yield sec.get("title", ""), it


def mark_new(data, prev):
    if not prev:
        return
    seen = set()
    for _, d in prev:
        for _, it in all_items(d):
            for l in it.get("links", []):
                seen.add(l.get("url"))
    for _, it in all_items(data):
        links = it.get("links", [])
        if links and not it.get("empty") and links[0].get("url") not in seen:
            it["new"] = True


def make_overview(data, edition, prev):
    parts = []
    for title, it in all_items(data):
        if it.get("empty"):
            continue
        tag = f"{it['tag']}：" if it.get("tag") else ""
        det = f"（{it['detail']}）" if it.get("detail") else ""
        parts.append(f"- [{title}] {tag}{it.get('text','')}{det}")
    if not parts:
        return []
    paras = ["   - 【國際】：領袖動態、國際會議與機構、其他國際要聞",
             "   - 【台灣中央】：行政院與各部會、立法院與各委員會的動向",
             "   - 【各縣市】：各縣市值得注意的地方大事",
             "   - 【學術】：哲學、文學、藝術、社會學、經濟學、心理學、法律、數學、物理學、生物學、工程、醫學的動態"]
    prev_note = "。"
    if prev:
        prev_txt = []
        for e, d in prev:
            ov = d.get("overview") or []
            if ov:
                prev_txt.append(f"〔{EDITIONS[e]}總摘要〕" + " ".join(ov))
            else:
                prev_txt.append(f"〔{EDITIONS[e]}重點〕" + "；".join(
                    it.get("text", "") for it in d.get("top", [])[:5]))
        prev_note = "。另外附上今天較早各期的內容。" + "\n".join(prev_txt)
        paras.insert(0, "   - 【今日回顧】：先用 2~3 句回顧今天較早各期的重點，並點出本期相較之前有什麼新進展")
    prompt = (PROMPT_OVERVIEW.replace("{LABEL}", EDITIONS[edition])
              .replace("{PREV_NOTE}", prev_note).replace("{PARAS}", "\n".join(paras)))
    out = call_gemini(prompt + "\n".join(parts))
    ov = out.get("overview", [])
    if isinstance(ov, str):
        ov = [ov]
    return [x.strip() for x in ov if isinstance(x, str) and x.strip()]


# ---------- 7. 主程式 ----------
def main():
    print("程式版本：", VERSION)
    edition = pick_edition()
    today = datetime.now(TW).strftime("%Y-%m-%d")
    print("本次產生：", EDITIONS[edition], today)
    items = collect()
    if not items:
        raise SystemExit("沒有抓到任何新聞，保留舊的資料。")
    by_id = {i["id"]: i for i in items}
    core_items = [i for i in items if i.get("group") in (None,) + CORE_GROUPS]
    dir_items = [i for i in items if i.get("group") in DIR_GROUPS]
    prev = load_prev(edition, today)

    core, dirs, ai_core, ai_dir = None, None, False, False
    print("GEMINI_API_KEY：", f"已設定（長度 {len(API_KEY)}）" if API_KEY else "【未設定】")
    if API_KEY:
        try:
            core = call_gemini(PROMPT_CORE + "\n".join(line(i) for i in core_items))
            attach_links(core.get("top", []), by_id)
            for s in core.get("sections", []):
                attach_links(s.get("items", []), by_id)
            ai_core = True
        except Exception as e:
            print("[核心 AI 失敗，改用標題模式]", e)
        if dir_items:
            try:
                dirs = call_gemini(PROMPT_DIR + "\n".join(line(i) for i in dir_items))
                for s in dirs.get("sections", []):
                    attach_links(s.get("items", []), by_id)
                backfill(dirs, dir_items)
                ai_dir = True
            except Exception as e:
                print("[目錄 AI 失敗，改用標題模式]", e)
    else:
        print("[沒有 GEMINI_API_KEY，使用只列標題模式]")
    if core is None:
        core = core_headlines(core_items)
    if dirs is None:
        dirs = dir_headlines(dir_items)
    for s in dirs.get("sections", []):
        s["collapsed"] = True  # 目錄式區塊預設收合；總摘要已涵蓋其內容

    data = {
        "edition": edition,
        "edition_label": EDITIONS[edition],
        "date": today,
        "updated": datetime.now(TW).strftime("%Y-%m-%d %H:%M"),
        "top": core.get("top", []),
        "sections": core.get("sections", []) + dirs.get("sections", []),
        "calendar": CALENDAR,
        "weekly": [{"dow": 4, "tag": "行政院", "t": "行政院院會（每週四上午）"}],
        "source_count": len(items),
        "ai": ai_core,
        "ai_dir": ai_dir,
        "version": VERSION,
        "overview": [],
    }
    mark_new(data, prev)
    if API_KEY and (ai_core or ai_dir):
        try:
            data["overview"] = make_overview(data, edition, prev)
        except Exception as e:
            print("[總摘要失敗，本期不顯示總摘要]", e)
    data["model"] = GOOD_MODEL or ""

    os.makedirs("data", exist_ok=True)
    with open(f"data/{edition}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"data/{edition}.json 已更新：", data["updated"], "| 核心AI:", ai_core,
          "| 目錄AI:", ai_dir, "| 總摘要段數:", len(data["overview"]))


if __name__ == "__main__":
    main()
