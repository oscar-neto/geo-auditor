# -*- coding: utf-8 -*-
"""
AI Agent Content Visibility Auditor (GEO)
=========================================
Compares raw HTML (what AI bots like GPTBot and ClaudeBot read)
with the rendered DOM (what users see after JavaScript executes),
checks robots.txt permissions and computes an AI Visibility Score per URL.

Deploy: Streamlit Community Cloud (see README.md)
"""

import asyncio
import io
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ---------------------------------------------------------------
# Settings
# ---------------------------------------------------------------

MAX_URLS = 50
REQUEST_TIMEOUT = 25
RENDER_TIMEOUT_MS = 45000
MAX_MISSING_SAMPLES = 25

AI_BOTS = {
    "GPTBot": "OpenAI (training)",
    "ChatGPT-User": "OpenAI (ChatGPT browsing)",
    "OAI-SearchBot": "OpenAI (search)",
    "ClaudeBot": "Anthropic (training)",
    "Claude-User": "Anthropic (Claude browsing)",
    "PerplexityBot": "Perplexity",
    "Google-Extended": "Google (Gemini/AI)",
    "CCBot": "Common Crawl",
    "Bytespider": "ByteDance",
}

UA_AI_BOT = (
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); "
    "compatible; GPTBot/1.2; +https://openai.com/gptbot"
)
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

st.set_page_config(page_title="AI Content Visibility Auditor (GEO)",
                   page_icon="🔎", layout="wide")


# ---------------------------------------------------------------
# Playwright setup (installs Chromium on the server's 1st run)
# ---------------------------------------------------------------

@st.cache_resource(show_spinner="Preparing headless browser (first run only)...")
def ensure_chromium() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False, capture_output=True, timeout=600,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------
# Text extraction and normalization
# ---------------------------------------------------------------

def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe", "canvas"]):
        tag.decompose()
    text = soup.get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> Counter:
    return Counter(re.findall(r"\w+", normalize(text), flags=re.UNICODE))


def content_coverage(raw_text: str, rendered_text: str) -> float:
    t_raw, t_rend = tokens(raw_text), tokens(rendered_text)
    total = sum(t_rend.values())
    if total == 0:
        return 100.0
    covered = sum(min(t_raw.get(w, 0), n) for w, n in t_rend.items())
    return round(100.0 * covered / total, 1)


def missing_blocks(raw_text: str, rendered_text: str) -> list:
    raw_norm = normalize(raw_text)
    sentences = re.split(r"(?<=[.!?…])\s+|\n+|(?<=[:;])\s{2,}", rendered_text)
    missing, seen = [], set()
    for s in sentences:
        s = s.strip()
        if len(s) < 40:
            continue
        s_norm = normalize(s)
        if s_norm and s_norm not in raw_norm and s_norm not in seen:
            seen.add(s_norm)
            missing.append(s[:300])
        if len(missing) >= MAX_MISSING_SAMPLES:
            break
    return missing


# ---------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------

def fetch_raw(url: str) -> dict:
    result = {"html": "", "status": None, "ua_blocked": False, "error": ""}
    try:
        r = requests.get(url, headers={"User-Agent": UA_AI_BOT}, timeout=REQUEST_TIMEOUT)
        result["status"] = r.status_code
        if r.status_code in (401, 403, 406, 429, 503):
            r2 = requests.get(url, headers={"User-Agent": UA_BROWSER}, timeout=REQUEST_TIMEOUT)
            if r2.ok:
                result.update(html=r2.text, ua_blocked=True, status=r2.status_code)
                return result
        r.raise_for_status()
        result["html"] = r.text
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


@st.cache_data(ttl=3600, show_spinner=False)
def get_robots_txt(origin: str) -> str:
    try:
        r = requests.get(urljoin(origin, "/robots.txt"),
                         headers={"User-Agent": UA_BROWSER}, timeout=15)
        return r.text if r.ok else ""
    except Exception:
        return ""


def check_robots(url: str) -> dict:
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    robots_txt = get_robots_txt(origin)
    result = {}
    for bot in AI_BOTS:
        rp = robotparser.RobotFileParser()
        rp.parse(robots_txt.splitlines())
        result[bot] = rp.can_fetch(bot, url) if robots_txt else True
    return result


async def render_urls(urls: list, progress_cb=None) -> dict:
    from playwright.async_api import async_playwright
    out = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent=UA_BROWSER, viewport={"width": 1366, "height": 900}
        )
        for i, url in enumerate(urls, 1):
            page = await context.new_page()
            try:
                try:
                    await page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
                except Exception:
                    await page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
                await page.wait_for_timeout(2500)
                out[url] = {"html": await page.content(), "error": ""}
            except Exception as e:
                out[url] = {"html": "", "error": f"{type(e).__name__}: {e}"}
            finally:
                await page.close()
            if progress_cb:
                progress_cb(i, len(urls), url)
        await browser.close()
    return out


# ---------------------------------------------------------------
# Diagnostics and scoring
# ---------------------------------------------------------------

def diagnostics(raw_html: str) -> dict:
    soup = BeautifulSoup(raw_html or "", "lxml")
    meta = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    meta_content = (meta.get("content") or "").lower() if meta else ""
    jsonld = soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)})
    return {
        "noindex": "noindex" in meta_content or "none" in meta_content,
        "jsonld_count": len(jsonld),
    }


def extract_headings(html: str) -> list:
    """Returns the page headings in document order as (level, text) tuples."""
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    headings = []
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        text = re.sub(r"\s+", " ", h.get_text(" ")).strip()
        if text:
            headings.append((int(h.name[1]), text[:150]))
    return headings


def hierarchy_breaks(headings: list) -> list:
    """Detects heading level skips, e.g. H1 → H3 without an H2 in between."""
    issues = []
    if headings and headings[0][0] != 1:
        issues.append(f"First heading is H{headings[0][0]} (should be H1)")
    for (prev_lvl, _), (curr_lvl, curr_txt) in zip(headings, headings[1:]):
        if curr_lvl > prev_lvl + 1:
            issues.append(f"H{prev_lvl} → H{curr_lvl} (\"{curr_txt[:50]}\")")
    return issues


def heading_analysis(raw_html: str, rendered_html: str) -> dict:
    """Full heading diagnosis: H1 status, AI visibility and hierarchy breaks."""
    raw_h = extract_headings(raw_html)
    rend_h = extract_headings(rendered_html)

    raw_h1 = [t for lvl, t in raw_h if lvl == 1]
    rend_h1 = [t for lvl, t in rend_h if lvl == 1]

    # H1 status considering the AI bot view (raw HTML)
    if len(raw_h1) == 1:
        h1_status = "✅ OK (1 H1 in raw HTML)"
    elif len(raw_h1) > 1:
        h1_status = f"⚠️ Multiple H1s in raw HTML ({len(raw_h1)})"
    elif rend_h1:
        h1_status = "🔴 H1 injected via JavaScript (invisible to AI)"
    else:
        h1_status = "🔴 No H1 found"

    # Headings that only exist after JavaScript (invisible to AI bots)
    raw_norm = {normalize(t) for _, t in raw_h}
    js_only = [f"H{lvl}: {t}" for lvl, t in rend_h if normalize(t) not in raw_norm]

    breaks_raw = hierarchy_breaks(raw_h)
    breaks_rend = hierarchy_breaks(rend_h)

    return {
        "h1_status": h1_status,
        "h1_text": rend_h1[0] if rend_h1 else (raw_h1[0] if raw_h1 else ""),
        "h1_count_raw": len(raw_h1),
        "headings_raw": len(raw_h),
        "headings_rendered": len(rend_h),
        "headings_js_only": js_only,
        "breaks_raw": breaks_raw,
        "breaks_rendered": breaks_rend,
        "hierarchy_ok": not breaks_rend and not breaks_raw,
        "outline_rendered": " > ".join(f"H{lvl}" for lvl, _ in rend_h[:25]),
    }


def heading_points(heads: dict) -> float:
    """Heading component of the score (0–10 pts): 6 for H1 status, 4 for hierarchy."""
    # H1 evaluated in the raw HTML (AI bot view)
    if heads["h1_count_raw"] == 1:
        h1_pts = 6
    elif heads["h1_count_raw"] > 1:
        h1_pts = 3  # multiple H1s
    else:
        h1_pts = 0  # missing or JS-injected H1 (invisible to AI)

    # Hierarchy breaks (e.g. H1 → H3 without H2)
    has_breaks_raw = bool(heads["breaks_raw"])
    has_breaks_rend = bool(heads["breaks_rendered"])
    if not has_breaks_raw and not has_breaks_rend:
        hier_pts = 4
    elif has_breaks_raw and has_breaks_rend:
        hier_pts = 0
    else:
        hier_pts = 2  # breaks in only one of the two versions

    return h1_pts + hier_pts


def visibility_score(coverage: float, robots: dict, diag: dict,
                     raw_words: int, ua_blocked: bool, heads: dict) -> int:
    """
    Composite AI Visibility Score (0–100).
    Legacy criteria keep the higher weight (90 pts); headings add up to 10 pts:
      45% content coverage + 25% robots.txt permissions + 8 JSON-LD
      + 8 no noindex + 4 content volume + 10 headings (H1 + hierarchy).
    UA blocking halves the final score.
    """
    allowed_pct = 100.0 * sum(robots.values()) / max(len(robots), 1)
    score = (
        0.45 * coverage
        + 0.25 * allowed_pct
        + (8 if diag["jsonld_count"] > 0 else 0)
        + (8 if not diag["noindex"] else 0)
        + (4 if raw_words >= 200 else 0)
        + heading_points(heads)
    )
    if ua_blocked:
        score *= 0.5
    return int(round(min(score, 100)))


def classify(score: int) -> str:
    if score >= 80:
        return "🟢 High"
    if score >= 55:
        return "🟡 Medium"
    return "🔴 Low"


# ---------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------

def run_audit(urls: list, status_box, progress_bar) -> tuple:
    raw_results, robots_results = {}, {}

    status_box.info("Step 1/2 — Fetching raw HTML (AI bot view) and robots.txt...")
    for i, url in enumerate(urls, 1):
        raw_results[url] = fetch_raw(url)
        robots_results[url] = check_robots(url)
        progress_bar.progress(i / (2 * len(urls)),
                              text=f"Raw fetch {i}/{len(urls)}: {url}")

    status_box.info("Step 2/2 — Rendering pages with headless browser (user view)...")

    def cb(i, total, url):
        progress_bar.progress(0.5 + i / (2 * total),
                              text=f"Rendering {i}/{total}: {url}")

    rendered_results = asyncio.run(render_urls(urls, cb))

    rows, robots_rows, missing_rows, heading_rows = [], [], [], []
    for url in urls:
        raw, rend, robots = raw_results[url], rendered_results[url], robots_results[url]
        if raw["error"] and rend["error"]:
            rows.append({"URL": url, "AI Visibility": "⚠️ Error", "Score": None,
                         "Content Coverage (%)": None, "Error": raw["error"]})
            continue

        raw_text = extract_visible_text(raw["html"])
        rend_text = extract_visible_text(rend["html"]) if not rend["error"] else raw_text
        coverage = content_coverage(raw_text, rend_text)
        diag = diagnostics(raw["html"])
        raw_words = sum(tokens(raw_text).values())
        rend_words = sum(tokens(rend_text).values())
        score = None  # computed after heading analysis below
        heads = heading_analysis(
            raw["html"], rend["html"] if not rend["error"] else raw["html"]
        )
        score = visibility_score(coverage, robots, diag, raw_words,
                                 raw["ua_blocked"], heads)
        blocked = [b for b, ok in robots.items() if not ok]

        rows.append({
            "URL": url,
            "AI Visibility": classify(score),
            "Score": score,
            "Content Coverage (%)": coverage,
            "Words (raw HTML)": raw_words,
            "Words (rendered)": rend_words,
            "Words invisible to AI": max(rend_words - raw_words, 0),
            "Blocked bots (robots.txt)": ", ".join(blocked) or "None",
            "User-Agent blocking": "⚠️ Yes" if raw["ua_blocked"] else "No",
            "Noindex": "Yes" if diag["noindex"] else "No",
            "JSON-LD in raw HTML": diag["jsonld_count"],
            "H1 status": heads["h1_status"],
            "Heading hierarchy": "✅ OK" if heads["hierarchy_ok"]
                                 else f"⚠️ {len(heads['breaks_rendered']) or len(heads['breaks_raw'])} break(s)",
            "Error": rend["error"] or raw["error"] or "",
        })

        robots_rows.append({"URL": url, **{b: ("✅" if ok else "🚫")
                                           for b, ok in robots.items()}})

        # One row per URL: all invisible blocks aggregated in a single cell
        blocks = missing_blocks(raw_text, rend_text)
        missing_rows.append({
            "URL": url,
            "Invisible blocks": len(blocks),
            "Content invisible to AI agents": (
                "\n".join(f"{i}. {b}" for i, b in enumerate(blocks, 1))
                if blocks else "None — all visible content is present in the raw HTML ✅"
            ),
        })

        # One row per URL: full heading diagnosis
        heading_rows.append({
            "URL": url,
            "H1 status": heads["h1_status"],
            "H1 text": heads["h1_text"],
            "Headings (raw HTML)": heads["headings_raw"],
            "Headings (rendered)": heads["headings_rendered"],
            "Headings invisible to AI": (
                "\n".join(f"{i}. {h}" for i, h in enumerate(heads["headings_js_only"], 1))
                if heads["headings_js_only"] else "None ✅"
            ),
            "Hierarchy breaks (raw HTML)": (
                "\n".join(f"{i}. {b}" for i, b in enumerate(heads["breaks_raw"], 1))
                if heads["breaks_raw"] else "None ✅"
            ),
            "Hierarchy breaks (rendered)": (
                "\n".join(f"{i}. {b}" for i, b in enumerate(heads["breaks_rendered"], 1))
                if heads["breaks_rendered"] else "None ✅"
            ),
            "Heading outline (rendered)": heads["outline_rendered"],
        })

    progress_bar.progress(1.0, text="Audit completed ✅")
    return (pd.DataFrame(rows), pd.DataFrame(robots_rows),
            pd.DataFrame(missing_rows), pd.DataFrame(heading_rows))


def to_excel(df_summary, df_robots, df_missing, df_headings) -> bytes:
    buf = io.BytesIO()
    from openpyxl.styles import Alignment
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_summary.to_excel(writer, sheet_name="Summary", index=False)
        df_robots.to_excel(writer, sheet_name="Robots by Bot", index=False)
        df_missing.to_excel(writer, sheet_name="Invisible Content", index=False)
        df_headings.to_excel(writer, sheet_name="Headings", index=False)

        # wrap text in the invisible-content column for readability
        ws = writer.sheets["Invisible Content"]
        ws.column_dimensions["A"].width = 60
        ws.column_dimensions["B"].width = 14
        ws.column_dimensions["C"].width = 120
        for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

        # wrap text in the headings sheet (multi-line cells)
        ws2 = writer.sheets["Headings"]
        widths = {"A": 55, "B": 28, "C": 45, "D": 12, "E": 12,
                  "F": 60, "G": 45, "H": 45, "I": 45}
        for col, w in widths.items():
            ws2.column_dimensions[col].width = w
        for row in ws2.iter_rows(min_row=2, min_col=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    return buf.getvalue()


# ---------------------------------------------------------------
# Interface
# ---------------------------------------------------------------

st.title("🔎 AI Agent Content Visibility Auditor")
st.caption(
    "Compares the **raw HTML** (what GPTBot, ClaudeBot, PerplexityBot and other AI "
    "crawlers read) with the **rendered DOM** (what users see after JavaScript runs) "
    "and identifies content invisible to AI agents. Includes robots.txt checks, "
    "meta robots, JSON-LD detection and an AI Visibility Score (0–100)."
)

with st.expander("ℹ️ How it works and how to read the results"):
    st.markdown(
        """
Most AI crawlers **do not execute JavaScript** — they only read the HTML returned
by the server. Content injected via JS (SPA product descriptions, reviews loaded
from APIs, dynamic tabs/accordions) is **invisible** to these agents.

**Metrics per URL:**
- **Content Coverage (%)** — how much of the user-visible text exists in the raw HTML. Below ~80% indicates significant JavaScript dependency.
- **Content invisible to AI agents** — the exact passages AI bots cannot read. Typical fixes: SSR, pre-rendering, or moving critical content into the initial HTML.
- **Robots.txt by bot** — declared permissions for 9 AI crawlers.
- **User-Agent blocking** — if the server returns 403/429 to a bot UA but 200 to a browser, there is a WAF/CDN-level block that robots.txt does not reveal.
- **Headings** — H1 presence in the raw HTML (missing, duplicated or JS-injected H1s are flagged), headings invisible to AI, and hierarchy breaks such as H1 → H3 without an H2.
- **Score (0–100)** — composite: 45% content coverage, 25% robots.txt permissions, 8 pts JSON-LD, 8 pts no noindex, 4 pts minimum content volume, and 10 pts headings (6 for a single H1 in the raw HTML + 4 for a clean hierarchy with no level skips). UA blocking halves the score. 🟢 ≥80 | 🟡 55–79 | 🔴 <55
        """
    )

urls_input = st.text_area(
    f"Paste the URLs to audit (one per line, max {MAX_URLS}):",
    height=160,
    placeholder="https://www.example.com/product-1\nhttps://www.example.com/category/appliances",
)

col1, col2 = st.columns([1, 3])
start = col1.button("🚀 Run audit", type="primary", use_container_width=True)

if start:
    urls = [u.strip() for u in urls_input.splitlines() if u.strip()]
    urls = [u if u.startswith("http") else "https://" + u for u in urls]
    urls = list(dict.fromkeys(urls))[:MAX_URLS]

    if not urls:
        st.warning("Please enter at least one valid URL.")
        st.stop()

    if not ensure_chromium():
        st.error("Could not prepare the headless browser. Check the deploy (packages.txt).")
        st.stop()

    st.write(f"**{len(urls)} URL(s)** queued. Estimated time: ~{len(urls) * 12 // 60 + 1} min.")
    status_box = st.empty()
    progress_bar = st.progress(0.0)

    df_summary, df_robots, df_missing, df_headings = run_audit(urls, status_box, progress_bar)
    status_box.success(f"✅ Audit completed: {len(df_summary)} URL(s) processed.")

    st.session_state["results"] = (df_summary, df_robots, df_missing, df_headings)

if "results" in st.session_state and len(st.session_state["results"]) == 4:
    df_summary, df_robots, df_missing, df_headings = st.session_state["results"]

    if df_summary["Score"].notna().any():
        m1, m2, m3 = st.columns(3)
        m1.metric("Average score", f"{df_summary['Score'].mean():.0f}/100")
        m2.metric("Average content coverage",
                  f"{df_summary['Content Coverage (%)'].mean():.0f}%")
        m3.metric("Critical URLs (🔴)", int((df_summary["Score"] < 55).sum()))

    tab1, tab2, tab3, tab4 = st.tabs(
        ["📊 Summary by URL", "🤖 Robots.txt by bot",
         "🙈 Content invisible to AI", "🏷️ Headings"]
    )
    with tab1:
        st.dataframe(df_summary.sort_values("Score", na_position="last"),
                     use_container_width=True, hide_index=True)
    with tab2:
        st.dataframe(df_robots, use_container_width=True, hide_index=True)
        st.caption("✅ = bot allowed by robots.txt | 🚫 = bot blocked")
    with tab3:
        if df_missing.empty:
            st.success("🎉 No invisible content blocks were identified.")
        else:
            url_filter = st.selectbox("Filter by URL:",
                                      ["All"] + sorted(df_missing["URL"].unique().tolist()))
            shown = df_missing if url_filter == "All" else df_missing[df_missing["URL"] == url_filter]
            st.dataframe(
                shown,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Content invisible to AI agents": st.column_config.TextColumn(
                        width="large",
                    ),
                },
                row_height=140,
            )
            st.caption("One row per URL — all invisible text blocks are aggregated "
                       "and numbered in a single cell.")
    with tab4:
        h1_issues = int(df_headings["H1 status"].str.contains("🔴|⚠️").sum())
        break_issues = int(
            (df_headings["Hierarchy breaks (rendered)"] != "None ✅").sum()
            + ((df_headings["Hierarchy breaks (raw HTML)"] != "None ✅")
               & (df_headings["Hierarchy breaks (rendered)"] == "None ✅")).sum()
        )
        c1, c2 = st.columns(2)
        c1.metric("URLs with H1 issues", h1_issues)
        c2.metric("URLs with hierarchy breaks", break_issues)
        st.dataframe(
            df_headings,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Headings invisible to AI": st.column_config.TextColumn(width="large"),
                "Hierarchy breaks (raw HTML)": st.column_config.TextColumn(width="medium"),
                "Hierarchy breaks (rendered)": st.column_config.TextColumn(width="medium"),
            },
            row_height=120,
        )
        st.caption(
            "**H1 status** evaluates the raw HTML (AI bot view): missing H1, multiple H1s "
            "or H1 injected via JavaScript. **Hierarchy breaks** flag level skips such as "
            "H1 → H3 without an H2 — checked in both the raw HTML and the rendered DOM."
        )

    st.download_button(
        "📥 Download Excel report (4 sheets)",
        data=to_excel(df_summary, df_robots, df_missing, df_headings),
        file_name=f"ai_visibility_audit_{datetime.now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.divider()
st.caption("Built for GEO (Generative Engine Optimization) analysis in e-commerce.")
