#!/usr/bin/env python3
"""
36Kr AI News Scraper
- Scrapes latest AI articles from 36kr.com/information/AI
- Sends styled email via Maileroo
- Publishes to GitHub Pages (docs/)
"""

import html as html_mod
import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

# ── timezone ────────────────────────────────────────────────────────────
BJT = timezone(timedelta(hours=8))

# ── inline stealth script ──────────────────────────────────────────────
STEALTH_JS = r"""
(() => {
    // ── webdriver ──
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true
    });
    try { delete navigator.__proto__.webdriver; } catch(_){}

    // ── chrome runtime ──
    window.chrome = {
        runtime: {
            onConnect:  { addListener: function(){} },
            onMessage:  { addListener: function(){} },
            connect:    function(){ return { onMessage: { addListener: function(){} } }; }
        },
        loadTimes: function(){ return {}; },
        csi:       function(){ return {}; },
        app:       { isInstalled: false, getDetails: function(){}, getIsInstalled: function(){}, installState: function(){ return 'disabled'; } }
    };

    // ── plugins ──
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [
                { name:'Chrome PDF Plugin',  filename:'internal-pdf-viewer',           description:'Portable Document Format', length:1 },
                { name:'Chrome PDF Viewer',  filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'',                        length:1 },
                { name:'Native Client',      filename:'internal-nacl-plugin',          description:'',                        length:1 }
            ];
            arr.item    = i => arr[i];
            arr.namedItem = n => arr.find(p => p.name === n);
            arr.refresh = () => {};
            return arr;
        }
    });

    // ── mimeTypes ──
    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => {
            const arr = [
                { type:'application/pdf',              suffixes:'pdf', description:'Portable Document Format' },
                { type:'application/x-google-chrome-pdf', suffixes:'pdf', description:'Portable Document Format' }
            ];
            arr.item    = i => arr[i];
            arr.namedItem = n => arr.find(m => m.type === n);
            return arr;
        }
    });

    // ── languages ──
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN','zh','en-US','en']
    });

    // ── platform ──
    Object.defineProperty(navigator, 'platform', {
        get: () => 'Win32'
    });

    // ── hardwareConcurrency ──
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => 8
    });

    // ── deviceMemory ──
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => 8
    });

    // ── maxTouchPoints ──
    Object.defineProperty(navigator, 'maxTouchPoints', {
        get: () => 0
    });

    // ── permissions ──
    try {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = params => {
            if (params.name === 'notifications')
                return Promise.resolve({ state: Notification.permission });
            return origQuery(params);
        };
    } catch(_){}

    // ── WebGL ──
    try {
        const getP = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Intel Inc.';
            if (p === 37446) return 'Intel Iris OpenGL Engine';
            return getP.call(this, p);
        };
    } catch(_){}
    try {
        const getP2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Intel Inc.';
            if (p === 37446) return 'Intel Iris OpenGL Engine';
            return getP2.call(this, p);
        };
    } catch(_){}

    // ── connection ──
    if (!navigator.connection) {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({ effectiveType:'4g', rtt:50, downlink:10, saveData:false })
        });
    }

    // ── toString mask ──
    const origToString = Function.prototype.toString;
    Function.prototype.toString = function() {
        if (this === Function.prototype.toString) return 'function toString() { [native code] }';
        return origToString.call(this);
    };
})();
"""


# ═══════════════════════════════════════════════════════════════════════
#  SCRAPING
# ═══════════════════════════════════════════════════════════════════════

def _articles_from_state(state: dict) -> list[dict]:
    """Extract article list from the initialState object."""
    out = []
    try:
        items = state["information"]["informationList"]["itemList"]
        for item in items[:15]:
            tm = item.get("templateMaterial", {})
            title = tm.get("widgetTitle", "").strip()
            source = tm.get("authorName", "").strip()
            item_id = item.get("itemId", "")
            if title and item_id:
                out.append({
                    "title": title,
                    "url": f"https://36kr.com/p/{item_id}",
                    "source": source,
                })
    except (KeyError, TypeError, IndexError) as exc:
        print(f"  [state-parse] error: {exc}")
    return out


def _state_from_html(raw_html: str):
    """Parse window.initialState JSON out of raw HTML."""
    marker = "window.initialState="
    idx = raw_html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    end = raw_html.find("</script>", start)
    if end == -1:
        return None
    json_str = raw_html[start:end].strip().rstrip(";")
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as exc:
        print(f"  [json-parse] error: {exc}")
        return None


def scrape_articles() -> list[dict]:
    """Main scraping routine with 3 fallback levels."""
    articles: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1920,1080",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="8"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()

        try:
            print("[1/4] Navigating to 36kr.com/information/AI …")
            page.goto(
                "https://36kr.com/information/AI",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(5000)

            # ── Method A: evaluate window.initialState ──
            print("[2/4] Trying window.initialState evaluation …")
            state = page.evaluate(
                "() => { try { return window.initialState; } catch(_){ return null; } }"
            )
            if state:
                articles = _articles_from_state(state)
                if articles:
                    print(f"  ✓ Got {len(articles)} articles via JS evaluation")

            # ── Method B: regex on HTML source ──
            if not articles:
                print("[2/4] Trying HTML source regex …")
                content = page.content()
                state = _state_from_html(content)
                if state:
                    articles = _articles_from_state(state)
                    if articles:
                        print(f"  ✓ Got {len(articles)} articles via HTML regex")

            # ── Method C: DOM queries ──
            if not articles:
                print("[2/4] Trying DOM extraction …")
                page.wait_for_selector(".information-flow-item", timeout=20_000)
                nodes = page.query_selector_all(".information-flow-item")
                for node in nodes[:15]:
                    t_el = node.query_selector("a.article-item-title")
                    s_el = node.query_selector("a.kr-flow-bar-author")
                    if t_el:
                        title = (t_el.inner_text() or "").strip()
                        href = t_el.get_attribute("href") or ""
                        source = (s_el.inner_text() or "").strip() if s_el else ""
                        url = f"https://36kr.com{href}" if href.startswith("/") else href
                        if title:
                            articles.append({"title": title, "url": url, "source": source})
                if articles:
                    print(f"  ✓ Got {len(articles)} articles via DOM")

        except Exception as exc:
            print(f"  ✗ Scrape exception: {exc}")
            # last-resort: try page source
            try:
                state = _state_from_html(page.content())
                if state:
                    articles = _articles_from_state(state)
            except Exception:
                pass
        finally:
            browser.close()

    return articles


# ═══════════════════════════════════════════════════════════════════════
#  EMAIL
# ═══════════════════════════════════════════════════════════════════════

def _build_email_html(articles: list[dict], date_str: str, time_str: str) -> str:
    rows = ""
    for i, a in enumerate(articles):
        t = html_mod.escape(a["title"])
        s = html_mod.escape(a["source"])
        u = a["url"]
        rows += f"""
<tr><td style="padding:0 24px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="padding:18px 0;border-bottom:1px solid #E2E8F0;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        <td width="40" valign="top" style="padding-right:14px;">
          <div style="width:30px;height:30px;border-radius:50%;
            background-color:#6366F1;background:linear-gradient(135deg,#6366F1,#8B5CF6);
            color:#fff;font-size:13px;font-weight:700;text-align:center;line-height:30px;
            font-family:Arial,sans-serif;">{i+1}</div>
        </td>
        <td style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
          <a href="{u}" target="_blank"
             style="color:#1E293B;text-decoration:none;font-size:15px;font-weight:600;
                    line-height:1.55;display:block;">
            {t}</a>
          <div style="margin-top:8px;">
            <span style="display:inline-block;background-color:#EEF2FF;
              background:linear-gradient(135deg,#EEF2FF,#E0E7FF);
              color:#4338CA;font-size:11px;padding:3px 10px;border-radius:20px;
              font-weight:500;font-family:Arial,sans-serif;">{s}</span>
          </div>
        </td>
      </tr></table>
    </td>
  </tr></table>
</td></tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>36Kr AI Update</title></head>
<body style="margin:0;padding:0;background-color:#F1F5F9;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" border="0"
  style="background-color:#F1F5F9;">
<tr><td align="center" style="padding:32px 12px;">
<table width="100%" cellpadding="0" cellspacing="0" border="0"
  style="max-width:640px;background-color:#ffffff;border-radius:16px;
    overflow:hidden;box-shadow:0 4px 30px rgba(0,0,0,0.08);">

<!-- ═══ HEADER ═══ -->
<tr><td align="center"
  style="background-color:#0F172A;
    background:linear-gradient(135deg,#0F172A 0%,#1E293B 45%,#334155 100%);
    padding:44px 32px 36px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td align="center"
      style="color:#94A3B8;font-size:30px;padding-top:8px;
        letter-spacing:1.5px;font-family:Arial,sans-serif;">
      🧬 Daily 36Kr AI</td></tr>
    <tr><td align="center" style="padding-top:18px;">
      <div style="width:52px;height:3px;border-radius:2px;margin:0 auto;
        background-color:#6366F1;
        background:linear-gradient(90deg,#6366F1,#A78BFA);"></div>
    </td></tr>
  </table>
</td></tr>

<!-- ═══ BODY ═══ -->
<tr><td style="padding:6px 0 2px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    {rows}
  </table>
</td></tr>

<!-- ═══ FOOTER ═══ -->
<tr><td align="center"
  style="background-color:#0F172A;
    background:linear-gradient(135deg,#0F172A 0%,#1E293B 55%,#0F172A 100%);
    padding:30px 32px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td align="center"
      style="color:#64748B;font-size:12px;line-height:1.6;
        font-family:Arial,sans-serif;">
      Data updated at {time_str} UTC+8</td></tr>
  </table>
</td></tr>

</table>
</td></tr></table>
</body></html>"""


def send_email(articles: list[dict], now: datetime):
    api_key = os.environ.get("MAILEROO_API_KEY", "")
    mail_to = os.environ.get("MAIL_TO", "")
    mail_from = os.environ.get("MAIL_FROM", "")

    if not (api_key and mail_to and mail_from):
        print("[email] Missing credentials — skipping.")
        return

    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%Y-%m-%d %H:%M")
    subject = f"🤖 Daily 36Kr AI - {date_str}"
    body = _build_email_html(articles, date_str, time_str)

    recipients = [r.strip() for r in mail_to.split(",") if r.strip()]
    for to_addr in recipients:
        try:
            resp = requests.post(
                "https://smtp.maileroo.com/send",
                headers={"X-API-Key": api_key},
                data={
                    "from": f"Newsletter <{mail_from}>",
                    "to": to_addr,
                    "subject": subject,
                    "html": body,
                },
                timeout=30,
            )
            print(f"  ✉ → {to_addr}  status={resp.status_code}")
        except Exception as exc:
            print(f"  ✗ email to {to_addr} failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════
#  GITHUB PAGES
# ═══════════════════════════════════════════════════════════════════════

def _generate_page_html(articles: list[dict], time_str: str) -> str:
    cards = ""
    for i, a in enumerate(articles):
        t = html_mod.escape(a["title"])
        s = html_mod.escape(a.get("source", ""))
        d = html_mod.escape(a.get("date", ""))
        u = a["url"]
        cards += f"""
      <a href="{u}" target="_blank" rel="noopener noreferrer" class="card" style="animation-delay:{i*40}ms">
        <span class="idx">{i+1}</span>
        <span class="body">
          <span class="ttl">{t}</span>
          <span class="meta">
            <span class="src">{s}</span>
            <span class="dt">{d}</span>
          </span>
        </span>
        <span class="arrow">&#8599;</span>
      </a>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>36Kr AI Daily</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC',sans-serif;
  background:#06080F;color:#E2E8F0;min-height:100vh;
  background-image:
    radial-gradient(ellipse 80% 50% at 50% -20%,rgba(99,102,241,.15),transparent),
    radial-gradient(ellipse 60% 40% at 80% 110%,rgba(139,92,246,.1),transparent);
}}
header{{
  text-align:center;padding:56px 20px 40px;
  background:linear-gradient(180deg,#0F172A 0%,#06080F 100%);
  border-bottom:1px solid rgba(99,102,241,.12);
}}
header .emoji{{font-size:52px;margin-bottom:16px}}
header h1{{
  font-size:clamp(26px,5vw,36px);font-weight:800;
  color:#F8FAFC;letter-spacing:-.5px;
  background:linear-gradient(135deg,#F8FAFC 30%,#A78BFA);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;
}}
header .sub{{color:#64748B;font-size:14px;margin-top:8px;letter-spacing:2px;text-transform:uppercase}}
header .line{{width:56px;height:3px;border-radius:2px;margin:22px auto 0;
  background:linear-gradient(90deg,#6366F1,#A78BFA)}}
main{{max-width:820px;margin:0 auto;padding:28px 16px 48px}}
.card{{
  display:flex;align-items:flex-start;gap:14px;
  padding:18px 20px;margin-bottom:10px;border-radius:14px;
  background:rgba(30,41,59,.45);
  border:1px solid rgba(99,102,241,.08);
  text-decoration:none;color:inherit;
  transition:all .25s ease;
  animation:fadeUp .45s ease both;
}}
.card:hover{{
  background:rgba(30,41,59,.75);
  border-color:rgba(99,102,241,.25);
  transform:translateY(-2px);
  box-shadow:0 8px 30px rgba(99,102,241,.08);
}}
.idx{{
  flex-shrink:0;width:32px;height:32px;border-radius:50%;
  background:linear-gradient(135deg,#6366F1,#8B5CF6);
  color:#fff;font-size:13px;font-weight:700;
  display:flex;align-items:center;justify-content:center;
  margin-top:2px;
}}
.body{{flex:1;min-width:0}}
.ttl{{
  display:block;font-size:15px;font-weight:600;line-height:1.6;
  color:#F1F5F9;
}}
.card:hover .ttl{{color:#C4B5FD}}
.meta{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px}}
.src{{
  display:inline-block;background:rgba(99,102,241,.13);
  color:#A5B4FC;font-size:11px;padding:3px 10px;border-radius:20px;font-weight:500;
}}
.dt{{color:#475569;font-size:11px}}
.arrow{{
  flex-shrink:0;color:#475569;font-size:18px;margin-top:4px;
  transition:transform .2s;
}}
.card:hover .arrow{{color:#A78BFA;transform:translate(2px,-2px)}}
footer{{
  text-align:center;padding:36px 20px;
  background:linear-gradient(180deg,#06080F 0%,#0F172A 100%);
  border-top:1px solid rgba(99,102,241,.08);
}}
footer p{{color:#475569;font-size:13px}}
footer .sm{{font-size:11px;margin-top:8px;color:#334155}}
@keyframes fadeUp{{
  from{{opacity:0;transform:translateY(16px)}}
  to{{opacity:1;transform:translateY(0)}}
}}
@media(max-width:600px){{
  header{{padding:40px 16px 32px}}
  .card{{padding:14px 16px;gap:12px}}
  .ttl{{font-size:14px}}
  .idx{{width:28px;height:28px;font-size:12px}}
}}
</style>
</head>
<body>

<header>
  <div class="emoji">&#129302;</div>
  <h1>Daily 36Kr AI</h1>
  <p class="sub">Artificial Intelligence News</p>
  <div class="line"></div>
</header>

<main>
{cards}
</main>

<footer>
  <p>Data updated at {time_str} UTC+8</p>
  <p class="sm">Powered by GitHub Actions</p>
</footer>

</body>
</html>"""


def update_pages(articles: list[dict], now: datetime):
    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / ".nojekyll").touch()

    data_file = docs / "data.json"
    existing: list[dict] = []
    if data_file.exists():
        try:
            existing = json.loads(data_file.read_text("utf-8"))
        except Exception:
            existing = []

    date_stamp = now.strftime("%Y-%m-%d %H:%M")

    # merge: new first, then old — dedup by url — cap 30
    seen: set[str] = set()
    merged: list[dict] = []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            merged.append({
                "title": a["title"],
                "url": a["url"],
                "source": a.get("source", ""),
                "date": date_stamp,
            })
    for a in existing:
        if a["url"] not in seen:
            seen.add(a["url"])
            merged.append(a)
    merged = merged[:30]

    data_file.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")

    page_html = _generate_page_html(merged, now.strftime("%Y-%m-%d %H:%M"))
    (docs / "index.html").write_text(page_html, "utf-8")
    print(f"[pages] wrote {len(merged)} articles → docs/")


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(BJT)
    print(f"{'='*60}")
    print(f"  36Kr AI Scraper  —  {now.strftime('%Y-%m-%d %H:%M:%S')} BJT")
    print(f"{'='*60}\n")

    # ── scrape ──
    articles = scrape_articles()
    if not articles:
        print("\n✗ No articles scraped. Exiting.")
        sys.exit(1)

    print(f"\n[3/4] Scraped {len(articles)} articles:")
    for i, a in enumerate(articles, 1):
        print(f"  {i:>2}. {a['title']}")

    # ── email ──
    print("\n[3/4] Sending email …")
    send_email(articles, now)

    # ── pages ──
    print("\n[4/4] Updating GitHub Pages …")
    update_pages(articles, now)

    print("\n✓ All done.\n")


if __name__ == "__main__":
    main()
