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

GEMINI_MODEL = "gemini-3.1-flash-lite-preview"  # 注意：不要修改模型名
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
    # 支持: a@x.com,b@y.com  或 a@x.com; b@y.com
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
        # fallback: some feeds use summary as content
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
    # 删除明显不适合邮件的节点
    for name in ["script", "style", "button", "svg", "form", "input", "noscript"]:
        for t in soup.find_all(name):
            t.decompose()

    # 删除 class/id 命中广告/订阅/推荐等
    bad_class_re = re.compile(r"(subscribe|subscription|recommend|promo|advert|sponsor|cta|paywall|banner|share)", re.I)
    for t in soup.find_all(True):
        cls = " ".join(t.get("class", []))
        tid = t.get("id", "") or ""
        if bad_class_re.search(cls) or bad_class_re.search(tid):
            t.decompose()


def drop_header_before_first_real_paragraph(soup: BeautifulSoup) -> None:
    """
    需求：示例里“Yesterday...”之前的内容都不要（包括顶部图片、分类链接、分隔线图片等）。
    做法：从文档中找到第一个“有足够正文文本”的 <p>，移除其之前的所有兄弟节点。
    """
    # 取 body（若没有 body 就用 soup 本身）
    root = soup.body if soup.body else soup

    # 找到第一个“正文段落”
    first_p = None
    for p in root.find_all("p"):
        txt = p.get_text(" ", strip=True)
        # 排除纯分类链接/太短的
        if len(txt) >= 40 and not is_probably_ad_text(txt):
            first_p = p
            break

    if not first_p:
        return

    # 删除 first_p 之前的内容（同层级）
    # 找到 first_p 的顶层兄弟：通常 content:encoded 是一坨平铺的 div/p/figure 等
    # 我们从 root 的 contents 开始删除，直到遇到包含 first_p 的节点
    new_children = []
    found = False
    for child in list(root.contents):
        if isinstance(child, NavigableString):
            # 丢弃纯空白或游离字符串
            if not str(child).strip():
                continue
            # 游离文本通常是重复摘要/噪声：丢弃
            continue

        if not isinstance(child, Tag):
            continue

        if child is first_p or child.find(lambda x: x is first_p):
            found = True

        if found:
            new_children.append(child)

    # 清空并重建
    root.clear()
    for c in new_children:
        root.append(c)


def remove_ads_in_body(soup: BeautifulSoup) -> None:
    """
    删除正文中、正文后出现的广告图片及 URL：
    - 含广告关键词的段落/容器
    - href 命中常见追踪/订阅/重定向
    - 很薄的分隔线图片（高度很小）也删
    """
    # 先删“广告型链接”所在的 <a> 以及只包含该链接的父段落
    for a in list(soup.find_all("a")):
        href = (a.get("href") or "").strip()
        a_text = a.get_text(" ", strip=True)
        if is_probably_ad_url(href) or is_probably_ad_text(a_text):
            # 如果父亲是 p/div 且内容基本就是它，删父亲更干净
            parent = a.parent if isinstance(a.parent, Tag) else None
            if parent and parent.name in {"p", "div"}:
                parent_text = parent.get_text(" ", strip=True)
                if len(parent_text) <= max(60, len(a_text) + 10):
                    parent.decompose()
                    continue
            a.decompose()

    # 删除“广告型文本容器”
    for t in list(soup.find_all(["p", "div", "section"])):
        txt = t.get_text(" ", strip=True)
        if is_probably_ad_text(txt):
            # 避免误杀正文：只对短文本/典型 CTA 更激进
            if len(txt) <= 200 or re.search(r"(subscribe|upgrade|sponsored|advertis)", txt, re.I):
                t.decompose()

    # 删除“广告/分隔线图片”与其容器（比如 2920x10 这种）
    for img in list(soup.find_all("img")):
        w = img.get("width")
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
    """
    - 保留图片与链接
    - 去掉图片尺寸信息（width/height/srcset/sizes 等），避免手机端撑破
    - 去掉 picture/source 等复杂结构，只保留 img
    - 给 img 加 max-width:100%; height:auto;
    """
    # picture -> img
    for pic in list(soup.find_all("picture")):
        img = pic.find("img")
        if img:
            pic.replace_with(img)
        else:
            pic.decompose()

    # 移除 source（如果还有残留）
    for s in list(soup.find_all("source")):
        s.decompose()

    # 清理 img 属性
    for img in soup.find_all("img"):
        # 保留 src / alt
        src = img.get("src") or ""
        alt = img.get("alt") or ""
        img.attrs = {"src": src, "alt": alt}

        # 防止撑破
        img["style"] = "max-width:100% !important;height:auto !important;display:block;border:0;"

    # 统一 a 标签（邮件里 target/_blank 不一定有效，但不影响）
    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            a["href"] = href.strip()


def normalize_content_html(content_html: str) -> str:
    """
    清洗 content:encoded：
    - 删除头部非正文
    - 删除广告图片与链接
    - 去掉图片尺寸信息
    - 尽量去掉游离重复文本
    """
    if not content_html:
        return ""

    soup = BeautifulSoup(content_html, "lxml")

    remove_unwanted_nodes(soup)
    drop_header_before_first_real_paragraph(soup)
    remove_ads_in_body(soup)
    simplify_images_and_links(soup)

    # 删除空节点
    for t in list(soup.find_all(True)):
        # 空 div/figure 等
        if t.name in {"div", "figure"} and not t.get_text(strip=True) and not t.find("img") and not t.find("a"):
            t.decompose()

    # 输出：取 body 内部（若有）
    root = soup.body if soup.body else soup
    # 仅拼接 Tag（忽略游离字符串，避免“重复摘要”）
    parts = []
    for child in root.contents:
        if isinstance(child, Tag):
            parts.append(str(child))
    return "\n".join(parts).strip()


def split_html_by_block_boundaries(html_str: str, max_chars: int) -> List[str]:
    """
    按块级 HTML 标签边界拆分，尽量保证每段 <= max_chars，不从标签中间切断。
    """
    html_str = (html_str or "").strip()
    if len(html_str) <= max_chars:
        return [html_str] if html_str else []

    soup = BeautifulSoup(html_str, "lxml")
    root = soup.body if soup.body else soup

    # 只取顶层 tag，作为天然边界
    blocks: List[str] = []
    for child in root.contents:
        if isinstance(child, Tag):
            blocks.append(str(child))

    if not blocks:
        # 兜底：纯文本硬切（尽量不发生）
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
                # 单个块超长：递归拆
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
        """
        return: (translated_html, http_status, err_type, retry_after_seconds_str)
        """
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
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "temperature": 0.2,
            },
        }

        resp = self._post(payload)

        retry_after = resp.headers.get("Retry-After")
        status = resp.status_code

        if status >= 200 and status < 300:
            try:
                data = resp.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    return None, status, "empty_candidates", retry_after
                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    return None, status, "empty_parts", retry_after
                text = parts[0].get("text", "")
                text = (text or "").strip()
                if not text:
                    return None, status, "empty_text", retry_after
                return text, status, None, retry_after
            except Exception as ex:
                return None, status, f"json_parse_error:{ex}", retry_after

        # 非 2xx
        err_type = None
        if status in (400, 401, 403):
            err_type = "fatal"
        elif status == 429:
            err_type = "rate_limited"
        else:
            err_type = "retryable"

        return None, status, err_type, retry_after


def backoff_sleep_seconds(attempt_index: int) -> float:
    # attempt_index: 0..n-1
    base = 2.0
    cap = 120.0
    delay = min(cap, base * (2 ** attempt_index))
    jitter = random.uniform(0.0, 1.0)
    return delay + jitter


def translate_long_html(translator: GeminiTranslator, html_str: str) -> Tuple[Optional[str], bool]:
    """
    返回 (translated_html_or_none, ok)
    - ok=False 表示翻译失败（按需求：继续发英文邮件）
    """
    html_str = (html_str or "").strip()
    if not html_str:
        return "", True

    chunks = split_html_by_block_boundaries(html_str, MAX_TRANSLATE_CHARS)
    if not chunks:
        return "", True

    translated_chunks: List[str] = []

    for idx, chunk in enumerate(chunks):
        success = False
        fatal_abort = False

        for attempt in range(TRANSLATE_RETRIES):
            translated, status, err_type, retry_after = translator.translate_html(chunk)

            if translated is not None:
                translated_chunks.append(translated)
                success = True
                break

            # 不可重试错误：立即退出翻译（但不终止脚本）
            if err_type == "fatal":
                fatal_abort = True
                break

            # 429：按 Retry-After 自适应等待
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

            # 其他可重试
            time.sleep(backoff_sleep_seconds(attempt))

        if not success:
            return None, False  # 任意一段失败：整体按失败处理（按需求发英文）

        # 每段翻译成功后暂停 15s（最后一段不需要）
        if idx != len(chunks) - 1:
            time.sleep(15)

        if fatal_abort:
            return None, False

    return "\n".join(translated_chunks).strip(), True


def translate_short_text(translator: GeminiTranslator, text: str) -> Tuple[Optional[str], bool]:
    text = (text or "").strip()
    if not text:
        return "", True
    if len(text) >= 6000:
        # 当作“长文本”，走分段（用 <p> 包起来，避免破坏结构）
        wrapped = f"<p>{html.escape(text)}</p>"
        translated_html, ok = translate_long_html(translator, wrapped)
        if not ok or translated_html is None:
            return None, False
        # 提取文本
        soup = BeautifulSoup(translated_html, "lxml")
        p = soup.find("p")
        return (p.get_text(strip=True) if p else soup.get_text(" ", strip=True)), True

    # 小文本直接翻译（走 html 翻译，避免额外输出）
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
    no_articles_today: bool,
) -> str:
    updated_str = send_dt_bj.strftime("%Y-%m-%d %H:%M UTC+8")

    header_style = (
        "background-color:#0F172A;"
        "background-image:linear-gradient(90deg,#0F172A,#111827);"
    )
    footer_style = header_style

    # 通用文本样式
    en_style = "color:#111827;font-size:14px !important;line-height:1.6 !important;"
    zh_style = "color:#374151;font-size:14px !important;line-height:1.6 !important;"

    # 中文每行约 20 字：用较窄最大宽度（移动端更接近要求）
    # 同时保留整体容器 max-width 600，避免太窄。
    zh_narrow_wrap = "max-width:360px;margin:0 auto;"

    def esc(s: str) -> str:
        return html.escape(s or "")

    if no_articles_today:
        body_inner = f"""
          <div style="{en_style}">
            <p style="margin:0 0 12px 0;"><strong>💤 No articles today.</strong></p>
          </div>
        """
    elif not article:
        body_inner = f"""
          <div style="{en_style}">
            <p style="margin:0 0 12px 0;"><strong>💤 No articles today.</strong></p>
          </div>
        """
    else:
        pub_bj = article.pub_dt_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        pub_bj_str = pub_bj.strftime("%Y-%m-%d %H:%M UTC+8")

        # 英文块
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
            # 按需求：翻译失败时，邮件只含英文原文
            body_inner = en_block + f"""
              <div style="{en_style}">
                <p style="margin:18px 0 0 0;">
                  Note: Chinese translation is unavailable due to a translation error.
                </p>
              </div>
            """

    # 邮件 HTML（table 布局，提高移动端兼容性）
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
              <div style="color:#FFFFFF;font-size:16px;line-height:1.4;font-weight:700;">
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


def build_plain_text(send_dt_bj: datetime, article: Optional[Article], no_articles_today: bool, translation_ok: bool) -> str:
    updated_str = send_dt_bj.strftime("%Y-%m-%d %H:%M UTC+8")
    if no_articles_today or not article:
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
        # description 可能含 HTML，这里简单去标签
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
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
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

        # 去重（按 link）
        history = [h for h in history if h.get("link") != record["link"]]
        history.insert(0, record)
        history = history[:10]

        write_history(history)

    # index 始终重建（即使没文章也刷新 updated）
    index_html = render_docs_index(history, send_dt_bj)
    ensure_docs_dir()
    with open(DOCS_INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(index_html)


def main():
    # env（不要写进代码）
    maileroo_key = env_required("MAILEROO_API_KEY")
    mail_from = env_required("MAIL_FROM")
    mail_to_raw = env_required("MAIL_TO")
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    to_list = parse_mail_to(mail_to_raw)
    send_dt_bj = now_beijing()
    subject_date_bj = send_dt_bj.strftime("%Y-%m-%d")
    subject = f"🥤a16z news - {subject_date_bj}"

    article = None
    no_articles_today = False
    translation_ok = False

    zh_title = zh_desc = zh_creator = zh_pub = None
    zh_content_html = None

    try:
        feed_xml = fetch_feed_xml(FEED_URL)
        article = parse_latest_article(feed_xml)
    except Exception:
        article = None

    if article:
        # 判断“当天是否有文章”（按北京时间日期严格匹配）
        pub_bj_date = article.pub_dt_utc.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if pub_bj_date != send_dt_bj.date():
            no_articles_today = True

    normalized_content_html = ""
    if article and not no_articles_today:
        normalized_content_html = normalize_content_html(article.content_html)

    # 翻译（如果没有 KEY 或当天无文章则跳过）
    if article and (not no_articles_today) and gemini_key:
        translator = GeminiTranslator(gemini_key)

        # title/creator/pubDate/description/content 分别翻译（creator 可以不翻，但这里也给翻译）
        try:
            zh_title, ok1 = translate_short_text(translator, article.title)
            zh_creator, ok2 = translate_short_text(translator, article.creator or "")
            pub_bj = article.pub_dt_utc.astimezone(ZoneInfo("Asia/Shanghai"))
            pub_bj_str = pub_bj.strftime("%Y-%m-%d %H:%M UTC+8")
            zh_pub, ok3 = translate_short_text(translator, pub_bj_str)

            # description 可能是 html：用 translate_html，长度通常不大
            desc_html = (article.description or "").strip()
            if desc_html:
                zh_desc_html, ok4 = translate_long_html(translator, desc_html)
                zh_desc = zh_desc_html if ok4 else None
            else:
                zh_desc, ok4 = "", True

            # content：长文本按块拆分
            zh_content_html, ok5 = translate_long_html(translator, normalized_content_html)

            translation_ok = all([ok1, ok2, ok3, ok4, ok5]) and (zh_content_html is not None)
        except Exception:
            translation_ok = False
            zh_content_html = None

    # 邮件 html/plain
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
        no_articles_today=no_articles_today,
    )
    plain = build_plain_text(send_dt_bj, article, no_articles_today, translation_ok)

    # 发送邮件（即使翻译失败也必须继续发）
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
        # 按需求：不要终止整个脚本（Pages/history 仍需更新）
        pass

    # 更新 GitHub Pages（保留最近 10 条历史）
    try:
        # 即使 no_articles_today，也不新增历史，但会刷新 index 的 updated 时间
        update_github_pages_history(article if (article and not no_articles_today) else None, send_dt_bj)
    except Exception:
        pass


if __name__ == "__main__":
    main()
