import os
import re
import json
import time
import random
import html
from dataclasses import dataclass
from typing import List, Optional, Tuple
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import feedparser
from bs4 import BeautifulSoup, Tag, NavigableString
from dateutil import parser as dateparser


FEED_URL = "https://www.a16z.news/feed"
DOCS_HISTORY_PATH = "docs/history.json"
DOCS_INDEX_PATH = "docs/index.html"

GEMINI_MODEL = "gemini-3.1-flash-lite-preview"
MAX_TRANSLATE_CHARS = 5500  # 保守小于 6000，给提示词和响应留余量
TRANSLATE_RETRIES = 10


BLOCK_TAGS = {
    "p", "div", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "blockquote",
    "pre", "code",
    "table", "thead", "tbody", "tr", "td", "th",
    "figure", "figcaption",
    "hr",
}


AD_TEXT_PATTERNS = [
    r"\bsubscribe\b",
    r"\bnewsletter\b",
    r"\bupgrade\b",
    r"\bpaid subscribers?\b",
    r"\bsponsored\b",
    r"\badvertis(e|ement)\b",
    r"\bpromotion\b",
    r"\bpartner\b",
]

AD_URL_PATTERNS = [
    r"substack\.com/redirect",
    r"substack\.com/app-link",
    r"substack\.com/subscribe",
    r"/subscribe",
    r"utm_",
]


def env_required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env: {name}")
    return val


def now_beijing() -> datetime:
    return datetime.now(tz=ZoneInfo("Asia/Shanghai"))


def parse_mail_to(raw: str) -> List[dict]:
    parts = re.split(r"[;,]\s*", raw.strip())
    parts = [p.strip() for p in parts if p.strip()]
    return [{"address": p, "display_name": ""} for p in parts]


@dataclass
class Article:
    title: str
    description: str
    creator: str
    pub_date_raw: str
    pub_dt_utc: datetime
    link: str
    content_html: str


def fetch_feed_xml(url: str) -> str:
    r = requests.get(url, timeout=30, headers={"User-Agent": "a16z-news-mailer/1.0"})
    r.raise_for_status()
    return r.text


def parse_latest_article(feed_xml: str) -> Optional[Article]:
    feed = feedparser.parse(feed_xml)
    if not feed.entries:
        return None

    e = feed.entries[0]

    title = (e.get("title") or "").strip()
    description = (e.get("description") or e.get("summary") or "").strip()
    creator = (e.get("dc_creator") or e.get("author") or "").strip()
    pub_raw = (e.get("published") or e.get("pubDate") or "").strip()
    link = (e.get("link") or "").strip()

    content_html = ""
    if "content" in e and e.content:
        content_html = (e.content[0].value or "").strip()
    else:
        content_html = (e.get("summary") or "").strip()

    if not title or not pub_raw:
        return None

    pub_dt = dateparser.parse(pub_raw)
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    pub_dt_utc = pub_dt.astimezone(timezone.utc)

    return Article(
        title=title,
        description=description,
        creator=creator,
        pub_date_raw=pub_raw,
        pub_dt_utc=pub_dt_utc,
        link=link,
        content_html=content_html,
    )


def is_probably_ad_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(re.search(p, u) for p in AD_URL_PATTERNS)


def is_probably_ad_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(re.search(p, t) for p in AD_TEXT_PATTERNS)


def remove_unwanted_nodes(soup: BeautifulSoup) -> None:
    for name in ["script", "style", "button", "svg", "form", "input", "noscript"]:
        for t in soup.find_all(name):
            t.decompose()

    bad_class_re = re.compile(r"(subscribe|subscription|recommend|promo|advert|sponsor|cta|paywall|banner|share)", re.I)
    for t in soup.find_all(True):
        cls = " ".join(t.get("class", []))
        tid = t.get("id", "") or ""
        if bad_class_re.search(cls) or bad_class_re.search(tid):
            t.decompose()


def drop_header_before_first_real_paragraph(soup: BeautifulSoup) -> None:
    root = soup.body if soup.body else soup

    first_p = None
    for p in root.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if len(txt) >= 40 and not is_probably_ad_text(txt):
            first_p = p
            break

    if not first_p:
        return

    new_children = []
    found = False
    for child in list(root.contents):
        if isinstance(child, NavigableString):
            if not str(child).strip():
                continue
            continue

        if not isinstance(child, Tag):
            continue

        if child is first_p or child.find(lambda x: x is first_p):
            found = True

        if found:
            new_children.append(child)

    root.clear()
    for c in new_children:
        root.append(c)


def remove_ads_in_body(soup: BeautifulSoup) -> None:
    for a in list(soup.find_all("a")):
        href = (a.get("href") or "").strip()
        a_text = a.get_text(" ", strip=True)
        if is_probably_ad_url(href) or is_probably_ad_text(a_text):
            parent = a.parent if isinstance(a.parent, Tag) else None
            if parent and parent.name in {"p", "div"}:
                parent_text = parent.get_text(" ", strip=True)
                if len(parent_text) <= max(60, len(a_text) + 10):
                    parent.decompose()
                    continue
            a.decompose()

    for t in list(soup.find_all(["p", "div", "section"])):
        txt = t.get_text(" ", strip=True)
        if is_probably_ad_text(txt):
            if len(txt) <= 200 or re.search(r"(subscribe|upgrade|sponsored|advertis)", txt, re.I):
                t.decompose()

    for img in list(soup.find_all("img")):
        h = img.get("height")
        try:
            hi = int(h) if h is not None else None
        except Exception:
            hi = None

        src = (img.get("src") or "").strip()
        if is_probably_ad_url(src):
            container = img.find_parent(["p", "div", "figure"])
            if container:
                container.decompose()
            else:
                img.decompose()
            continue

        if hi is not None and hi <= 10:
            container = img.find_parent(["p", "div", "figure"])
            if container:
                container.decompose()
            else:
                img.decompose()
            continue


def simplify_images_and_links(soup: BeautifulSoup) -> None:
    for pic in list(soup.find_all("picture")):
        img = pic.find("img")
        if img:
            pic.replace_with(img)
        else:
            pic.decompose()

    for s in list(soup.find_all("source")):
        s.decompose()

    for img in soup.find_all("img"):
        src = img.get("src") or ""
        alt = img.get("alt") or ""
        img.attrs = {"src": src, "alt": alt}
        img["style"] = "max-width:100% !important;height:auto !important;display:block;border:0;"

    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            a["href"] = href.strip()


def normalize_content_html(content_html: str) -> str:
    if not content_html:
        return ""

    soup = BeautifulSoup(content_html, "lxml")
    remove_unwanted_nodes(soup)
    drop_header_before_first_real_paragraph(soup)
    remove_ads_in_body(soup)
    simplify_images_and_links(soup)

    for t in list(soup.find_all(True)):
        if t.name in {"div", "figure"} and not t.get_text(strip=True) and not t.find("img") and not t.find("a"):
            t.decompose()

    root = soup.body if soup.body else soup
    parts = []
    for child in root.contents:
        if isinstance(child, Tag):
            parts.append(str(child))
    return "\n".join(parts).strip()


def split_html_by_block_boundaries(html_str: str, max_chars: int) -> List[str]:
    html_str = (html_str or "").strip()
    if len(html_str) <= max_chars:
        return [html_str] if html_str else []

    soup = BeautifulSoup(html_str, "lxml")
    root = soup.body if soup.body else soup

    blocks: List[str] = []
    for child in root.contents:
        if isinstance(child, Tag):
            blocks.append(str(child))

    if not blocks:
        chunks = []
        s = html_str
        while s:
            chunks.append(s[:max_chars])
            s = s[max_chars:]
        return chunks

    chunks: List[str] = []
    buf = ""
    for b in blocks:
        if not buf:
            if len(b) <= max_chars:
                buf = b
            else:
                chunks.extend(split_html_by_block_boundaries(b, max_chars))
                buf = ""
            continue

        if len(buf) + 1 + len(b) <= max_chars:
            buf = buf + "\n" + b
        else:
            chunks.append(buf)
            if len(b) <= max_chars:
                buf = b
            else:
                chunks.extend(split_html_by_block_boundaries(b, max_chars))
                buf = ""

    if buf:
        chunks.append(buf)

    return chunks


class GeminiTranslator:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

    def _post(self, payload: dict) -> requests.Response:
        return requests.post(
            self.endpoint,
            params={"key": self.api_key},
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=60,
        )

    def translate_html(self, html_chunk: str) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
        prompt = (
            "You are a professional translator.\n"
            "Translate the following HTML from English to Simplified Chinese.\n"
            "Rules:\n"
            "1) Keep ALL HTML tags and structure unchanged.\n"
            "2) Only translate human-readable text nodes.\n"
            "3) Do NOT translate URLs.\n"
            "4) Do NOT add any extra commentary.\n"
            "5) Output ONLY the translated HTML.\n\n"
            "HTML:\n"
            f"{html_chunk}"
        )

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }

        resp = self._post(payload)

        retry_after = resp.headers.get("Retry-After")
        status = resp.status_code

        if 200 <= status < 300:
            try:
                data = resp.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    return None, status, "empty_candidates", retry_after
                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    return None, status, "empty_parts", retry_after
                text = (parts[0].get("text", "") or "").strip()
                if not text:
                    return None, status, "empty_text", retry_after
                return text, status, None, retry_after
            except Exception as ex:
                return None, status, f"json_parse_error:{ex}", retry_after

        if status in (400, 401, 403):
            err_type = "fatal"
        elif status == 429:
            err_type = "rate_limited"
        else:
            err_type = "retryable"

        return None, status, err_type, retry_after


def backoff_sleep_seconds(attempt_index: int) -> float:
    base = 2.0
    cap = 120.0
    delay = min(cap, base * (2 ** attempt_index))
    jitter = random.uniform(0.0, 1.0)
    return delay + jitter


def translate_long_html(translator: GeminiTranslator, html_str: str) -> Tuple[Optional[str], bool]:
    html_str = (html_str or "").strip()
    if not html_str:
        return "", True

    chunks = split_html_by_block_boundaries(html_str, MAX_TRANSLATE_CHARS)
    if not chunks:
        return "", True

    translated_chunks: List[str] = []

    for idx, chunk in enumerate(chunks):
        success = False

        for attempt in range(TRANSLATE_RETRIES):
            translated, status, err_type, retry_after = translator.translate_html(chunk)

            if translated is not None:
                translated_chunks.append(translated)
                success = True
                break

            if err_type == "fatal":
                return None, False

            if err_type == "rate_limited":
                if retry_after:
                    try:
                        wait_s = float(retry_after)
                    except Exception:
                        wait_s = backoff_sleep_seconds(attempt)
                else:
                    wait_s = backoff_sleep_seconds(attempt)
                time.sleep(wait_s)
                continue

            time.sleep(backoff_sleep_seconds(attempt))

        if not success:
            return None, False

        if idx != len(chunks) - 1:
            time.sleep(15)

    return "\n".join(translated_chunks).strip(), True


def translate_short_text(translator: GeminiTranslator, text: str) -> Tuple[Optional[str], bool]:
    text = (text or "").strip()
    if not text:
        return "", True

    if len(text) >= 6000:
        wrapped = f"<p>{html.escape(text)}</p>"
        translated_html, ok = translate_long_html(translator, wrapped)
        if not ok or translated_html is None:
            return None, False
        soup = BeautifulSoup(translated_html, "lxml")
        p = soup.find("p")
        return (p.get_text(strip=True) if p else soup.get_text(" ", strip=True)), True

    wrapped = f"<p>{html.escape(text)}</p>"
    translated_html, ok = translate_long_html(translator, wrapped)
    if not ok or translated_html is None:
        return None, False
    soup = BeautifulSoup(translated_html, "lxml")
    p = soup.find("p")
    return (p.get_text(strip=True) if p else soup.get_text(" ", strip=True)), True


def build_email_html(
    send_dt_bj: datetime,
    article: Optional[Article],
    normalized_content_html: str,
    zh_title: Optional[str],
    zh_desc: Optional[str],
    zh_creator: Optional[str],
    zh_pub: Optional[str],
    zh_content_html: Optional[str],
    translation_ok: bool,
) -> str:
    updated_str = send_dt_bj.strftime("%Y-%m-%d %H:%M UTC+8")

    header_style = (
        "background-color:#0F172A;"
        "background-image:linear-gradient(90deg,#0F172A,#111827);"
    )
    footer_style = header_style

    en_style = "color:#111827;font-size:14px !important;line-height:1.6 !important;"
    zh_style = "color:#374151;font-size:14px !important;line-height:1.6 !important;"
    zh_narrow_wrap = "max-width:360px;margin:0 auto;"

    def esc(s: str) -> str:
        return html.escape(s or "")

    if not article:
        body_inner = f"""
          <div style="{en_style}">
            <p style="margin:0 0 12px 0;"><strong>💤 No articles today.</strong></p>
          </div>
        """
    else:
        pub_bj = article.pub_dt_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        pub_bj_str = pub_bj.strftime("%Y-%m-%d %H:%M UTC+8")

        en_block = f"""
          <div style="{en_style}">
            <p style="margin:0 0 10px 0;"><strong>📖 ENGLISH</strong></p>
            <h2 style="margin:0 0 10px 0;color:#111827;font-size:18px;line-height:1.3;">{esc(article.title)}</h2>
            <p style="margin:0 0 6px 0;">✍️ {esc(article.creator) if article.creator else ""}</p>
            <p style="margin:0 0 12px 0;">📅 {esc(pub_bj_str)}</p>
            <p style="margin:0 0 12px 0;"><a href="{esc(article.link)}" style="color:#2563EB;text-decoration:underline;">Open original</a></p>
            {f'<div style="margin:0 0 14px 0;">{article.description}</div>' if article.description else ''}
            <div style="margin:0 0 4px 0;">{normalized_content_html}</div>
          </div>
        """

        if translation_ok and zh_content_html is not None:
            zh_block = f"""
              <div style="{zh_style}">
                <p style="margin:18px 0 10px 0;"><strong>🤖 中文翻译</strong></p>
                <div style="{zh_narrow_wrap}">
                  <h2 style="margin:0 0 10px 0;color:#374151;font-size:18px;line-height:1.3;">{esc(zh_title or "")}</h2>
                  <p style="margin:0 0 6px 0;">✍️ {esc(zh_creator or "")}</p>
                  <p style="margin:0 0 12px 0;">📅 {esc(zh_pub or "")}</p>
                  <p style="margin:0 0 12px 0;"><a href="{esc(article.link)}" style="color:#2563EB;text-decoration:underline;">打开原文</a></p>
                  {f'<div style="margin:0 0 14px 0;">{zh_desc}</div>' if zh_desc else ''}
                  <div style="margin:0 0 4px 0;">{zh_content_html}</div>
                </div>
              </div>
            """
            body_inner = en_block + zh_block
        else:
            body_inner = en_block + f"""
              <div style="{en_style}">
                <p style="margin:18px 0 0 0;">
                  Note: Chinese translation is unavailable due to a translation error.
                </p>
              </div>
            """

    html_out = f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>a16z news</title>
</head>
<body style="margin:0;padding:0;background-color:#F3F4F6;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#F3F4F6;">
    <tr>
      <td align="center" style="padding:0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:600px;margin:0 auto;">
          <tr>
            <td style="padding:16px 18px;{header_style}">
              <div style="color:#FFFFFF;font-size:28px;line-height:1.4;font-weight:700;">
                🥤 a16z news
              </div>
            </td>
          </tr>

          <tr>
            <td style="padding:16px 18px;background-color:#FFFFFF;">
              {body_inner}
            </td>
          </tr>

          <tr>
            <td style="padding:14px 18px;{footer_style}">
              <div style="color:#E5E7EB;font-size:12px;line-height:1.4;text-align:center;">
                Updated at {esc(updated_str)}
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return html_out


def build_plain_text(send_dt_bj: datetime, article: Optional[Article], translation_ok: bool) -> str:
    updated_str = send_dt_bj.strftime("%Y-%m-%d %H:%M UTC+8")
    if not article:
        return f"🥤 a16z news\n\n💤 No articles today.\n\nUpdated at {updated_str}\n"

    pub_bj = article.pub_dt_utc.astimezone(ZoneInfo("Asia/Shanghai"))
    pub_bj_str = pub_bj.strftime("%Y-%m-%d %H:%M UTC+8")

    lines = [
        "🥤 a16z news",
        "",
        "📖 ENGLISH",
        article.title,
        f"✍️ {article.creator}",
        f"📅 {pub_bj_str}",
        f"Link: {article.link}",
        "",
    ]
    if article.description:
        soup = BeautifulSoup(article.description, "lxml")
        lines.append(soup.get_text(" ", strip=True))
        lines.append("")

    if not translation_ok:
        lines.append("Note: Chinese translation is unavailable due to a translation error.")
        lines.append("")

    lines.append(f"Updated at {updated_str}")
    return "\n".join([l for l in lines if l is not None])


def send_mail_via_maileroo(
    api_key: str,
    mail_from: str,
    to_list: List[dict],
    subject: str,
    html_body: str,
    plain_body: str,
) -> None:
    url = "https://smtp.maileroo.com/api/v2/emails"
    payload = {
        "from": {"address": mail_from, "display_name": "Newsletter"},
        "to": to_list,
        "subject": subject,
        "html": html_body,
        "plain": plain_body,
    }

    r = requests.post(
        url,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        data=json.dumps(payload),
        timeout=30,
    )
    r.raise_for_status()


def ensure_docs_dir():
    os.makedirs("docs", exist_ok=True)
    if not os.path.exists(DOCS_HISTORY_PATH):
        with open(DOCS_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)


def read_history() -> List[dict]:
    ensure_docs_dir()
    try:
        with open(DOCS_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def write_history(items: List[dict]) -> None:
    ensure_docs_dir()
    with open(DOCS_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def render_docs_index(history: List[dict], updated_bj: datetime) -> str:
    updated_str = updated_bj.strftime("%Y-%m-%d %H:%M UTC+8")

    rows = []
    for it in history[:10]:
        title = html.escape(it.get("title", ""))
        creator = html.escape(it.get("creator", ""))
        pub = html.escape(it.get("pubDate", ""))
        link = html.escape(it.get("link", ""))

        rows.append(f"""
          <tr>
            <td style="padding:10px 8px;border-bottom:1px solid #E5E7EB;">
              <a href="{link}" target="_blank" rel="noreferrer" style="color:#2563EB;text-decoration:none;">{title}</a>
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #E5E7EB;color:#111827;">{creator}</td>
            <td style="padding:10px 8px;border-bottom:1px solid #E5E7EB;color:#374151;white-space:nowrap;">{pub}</td>
          </tr>
        """)

    table_html = "\n".join(rows) if rows else """
      <tr><td colspan="3" style="padding:12px 8px;color:#374151;">No records.</td></tr>
    """

    return f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>a16z news - history</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, "Noto Sans", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      margin: 0;
      background: #F3F4F6;
      color: #111827;
    }}
    .wrap {{
      max-width: 900px;
      margin: 0 auto;
      padding: 18px 14px;
    }}
    .card {{
      background: #fff;
      border: 1px solid #E5E7EB;
      border-radius: 12px;
      overflow: hidden;
    }}
    .header {{
      padding: 14px 16px;
      color: #fff;
      background: linear-gradient(90deg, #0F172A, #111827);
      font-weight: 700;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th {{
      text-align: left;
      padding: 10px 8px;
      background: #F9FAFB;
      border-bottom: 1px solid #E5E7EB;
      font-size: 14px;
    }}
    td {{
      font-size: 14px;
      vertical-align: top;
    }}
    .footer {{
      padding: 12px 16px;
      color: #6B7280;
      font-size: 12px;
      text-align: right;
      background: #fff;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="header">🥤 a16z news - latest 10</div>
      <div style="padding: 8px 16px 2px 16px; color:#374151; font-size: 13px;">
        Showing &lt;title&gt;, &lt;dc:creator&gt;, &lt;pubDate&gt; (latest 10 including history)
      </div>
      <div style="padding: 10px 16px 16px 16px;">
        <table>
          <thead>
            <tr>
              <th style="width:55%;">Title</th>
              <th style="width:25%;">Creator</th>
              <th style="width:20%;">PubDate</th>
            </tr>
          </thead>
          <tbody>
            {table_html}
          </tbody>
        </table>
      </div>
      <div class="footer">Updated at {html.escape(updated_str)}</div>
    </div>
  </div>
</body>
</html>
"""


def update_github_pages_history(article: Optional[Article], send_dt_bj: datetime) -> None:
    history = read_history()

    if article:
        pub_bj = article.pub_dt_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        pub_bj_str = pub_bj.strftime("%Y-%m-%d %H:%M UTC+8")

        record = {
            "title": article.title,
            "creator": article.creator,
            "pubDate": pub_bj_str,
            "link": article.link,
        }

        history = [h for h in history if h.get("link") != record["link"]]
        history.insert(0, record)
        history = history[:10]
        write_history(history)

    index_html = render_docs_index(history, send_dt_bj)
    ensure_docs_dir()
    with open(DOCS_INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(index_html)


def main():
    maileroo_key = env_required("MAILEROO_API_KEY")
    mail_from = env_required("MAIL_FROM")
    mail_to_raw = env_required("MAIL_TO")
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    to_list = parse_mail_to(mail_to_raw)

    send_dt_bj = now_beijing()
    subject_date_bj = send_dt_bj.strftime("%Y-%m-%d")
    subject = f"🥤a16z news - {subject_date_bj}"

    article = None
    translation_ok = False

    zh_title = zh_desc = zh_creator = zh_pub = None
    zh_content_html = None
    normalized_content_html = ""

    try:
        feed_xml = fetch_feed_xml(FEED_URL)
        article = parse_latest_article(feed_xml)
    except Exception:
        article = None

    if article:
        normalized_content_html = normalize_content_html(article.content_html)

    # 翻译（没有 key 或无文章则跳过；翻译失败不影响发英文）
    if article and gemini_key:
        translator = GeminiTranslator(gemini_key)

        try:
            zh_title, ok1 = translate_short_text(translator, article.title)
            zh_creator, ok2 = translate_short_text(translator, article.creator or "")

            pub_bj = article.pub_dt_utc.astimezone(ZoneInfo("Asia/Shanghai"))
            pub_bj_str = pub_bj.strftime("%Y-%m-%d %H:%M UTC+8")
            zh_pub, ok3 = translate_short_text(translator, pub_bj_str)

            desc_html = (article.description or "").strip()
            if desc_html:
                zh_desc_html, ok4 = translate_long_html(translator, desc_html)
                zh_desc = zh_desc_html if ok4 else None
            else:
                zh_desc, ok4 = "", True

            zh_content_html, ok5 = translate_long_html(translator, normalized_content_html)

            translation_ok = all([ok1, ok2, ok3, ok4, ok5]) and (zh_content_html is not None)
        except Exception:
            translation_ok = False
            zh_content_html = None

    email_html = build_email_html(
        send_dt_bj=send_dt_bj,
        article=article,
        normalized_content_html=normalized_content_html,
        zh_title=zh_title,
        zh_desc=zh_desc,
        zh_creator=zh_creator,
        zh_pub=zh_pub,
        zh_content_html=zh_content_html,
        translation_ok=translation_ok,
    )
    plain = build_plain_text(send_dt_bj, article, translation_ok)

    try:
        send_mail_via_maileroo(
            api_key=maileroo_key,
            mail_from=mail_from,
            to_list=to_list,
            subject=subject,
            html_body=email_html,
            plain_body=plain,
        )
    except Exception:
        # 不终止脚本
        pass

    try:
        update_github_pages_history(article, send_dt_bj)
    except Exception:
        pass


if __name__ == "__main__":
    main()
