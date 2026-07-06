# -*- coding: utf-8 -*-
"""
Auditor de Visibilidade para Agentes de IA (GEO)
=================================================
Compara o HTML bruto (o que bots de IA como GPTBot e ClaudeBot leem)
com o DOM renderizado (o que o usuário vê após o JavaScript), checa o
robots.txt e calcula um Score de Visibilidade IA por URL.

Deploy: Streamlit Community Cloud (ver README.md)
"""

import asyncio
import io
import os
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
# Configurações
# ---------------------------------------------------------------

MAX_URLS = 50
REQUEST_TIMEOUT = 25
RENDER_TIMEOUT_MS = 45000
MAX_MISSING_SAMPLES = 25

AI_BOTS = {
    "GPTBot": "OpenAI (treinamento)",
    "ChatGPT-User": "OpenAI (navegação do ChatGPT)",
    "OAI-SearchBot": "OpenAI (busca)",
    "ClaudeBot": "Anthropic (treinamento)",
    "Claude-User": "Anthropic (navegação do Claude)",
    "PerplexityBot": "Perplexity",
    "Google-Extended": "Google (Gemini/IA)",
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

st.set_page_config(page_title="Auditor de Visibilidade IA (GEO)",
                   page_icon="🔎", layout="wide")


# ---------------------------------------------------------------
# Setup do Playwright (instala o Chromium na 1ª execução do servidor)
# ---------------------------------------------------------------

@st.cache_resource(show_spinner="Preparando o navegador headless (1ª execução)...")
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
# Extração e normalização de texto
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
# Coletas
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
# Diagnóstico e score
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


def visibility_score(coverage: float, robots: dict, diag: dict,
                     raw_words: int, ua_blocked: bool) -> int:
    allowed_pct = 100.0 * sum(robots.values()) / max(len(robots), 1)
    score = (
        0.50 * coverage
        + 0.25 * allowed_pct
        + (10 if diag["jsonld_count"] > 0 else 0)
        + (10 if not diag["noindex"] else 0)
        + (5 if raw_words >= 200 else 0)
    )
    if ua_blocked:
        score *= 0.5
    return int(round(min(score, 100)))


def classify(score: int) -> str:
    if score >= 80:
        return "🟢 Alta"
    if score >= 55:
        return "🟡 Média"
    return "🔴 Baixa"


# ---------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------

def run_audit(urls: list, status_box, progress_bar) -> tuple:
    raw_results, robots_results = {}, {}

    status_box.info("Etapa 1/2 — Coletando HTML bruto (visão dos bots de IA) e robots.txt...")
    for i, url in enumerate(urls, 1):
        raw_results[url] = fetch_raw(url)
        robots_results[url] = check_robots(url)
        progress_bar.progress(i / (2 * len(urls)),
                              text=f"Fetch bruto {i}/{len(urls)}: {url}")

    status_box.info("Etapa 2/2 — Renderizando páginas com navegador headless (visão do usuário)...")

    def cb(i, total, url):
        progress_bar.progress(0.5 + i / (2 * total),
                              text=f"Renderização {i}/{total}: {url}")

    rendered_results = asyncio.run(render_urls(urls, cb))

    rows, robots_rows, missing_rows = [], [], []
    for url in urls:
        raw, rend, robots = raw_results[url], rendered_results[url], robots_results[url]
        if raw["error"] and rend["error"]:
            rows.append({"URL": url, "Visibilidade IA": "⚠️ Erro", "Score": None,
                         "Cobertura de conteúdo (%)": None, "Erro": raw["error"]})
            continue

        raw_text = extract_visible_text(raw["html"])
        rend_text = extract_visible_text(rend["html"]) if not rend["error"] else raw_text
        coverage = content_coverage(raw_text, rend_text)
        diag = diagnostics(raw["html"])
        raw_words = sum(tokens(raw_text).values())
        rend_words = sum(tokens(rend_text).values())
        score = visibility_score(coverage, robots, diag, raw_words, raw["ua_blocked"])
        blocked = [b for b, ok in robots.items() if not ok]

        rows.append({
            "URL": url,
            "Visibilidade IA": classify(score),
            "Score": score,
            "Cobertura de conteúdo (%)": coverage,
            "Palavras (HTML bruto)": raw_words,
            "Palavras (renderizado)": rend_words,
            "Palavras invisíveis p/ IA": max(rend_words - raw_words, 0),
            "Bots bloqueados (robots.txt)": ", ".join(blocked) or "Nenhum",
            "Bloqueio por User-Agent": "⚠️ Sim" if raw["ua_blocked"] else "Não",
            "Noindex": "Sim" if diag["noindex"] else "Não",
            "JSON-LD no HTML bruto": diag["jsonld_count"],
            "Erro": rend["error"] or raw["error"] or "",
        })

        robots_rows.append({"URL": url, **{b: ("✅" if ok else "🚫")
                                           for b, ok in robots.items()}})

        for i, block in enumerate(missing_blocks(raw_text, rend_text), 1):
            missing_rows.append({"URL": url, "#": i,
                                 "Conteúdo invisível para IA": block})

    progress_bar.progress(1.0, text="Auditoria concluída ✅")
    return pd.DataFrame(rows), pd.DataFrame(robots_rows), pd.DataFrame(missing_rows)


def to_excel(df_resumo, df_robots, df_missing) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_resumo.to_excel(writer, sheet_name="Resumo", index=False)
        df_robots.to_excel(writer, sheet_name="Robots por Bot", index=False)
        df_missing.to_excel(writer, sheet_name="Conteúdo Invisível", index=False)
    return buf.getvalue()


# ---------------------------------------------------------------
# Interface
# ---------------------------------------------------------------

st.title("🔎 Auditor de Visibilidade para Agentes de IA")
st.caption(
    "Compara o **HTML bruto** (o que GPTBot, ClaudeBot, PerplexityBot e outros leem) "
    "com o **DOM renderizado** (o que o usuário vê após o JavaScript) e identifica "
    "o conteúdo invisível para agentes de IA. Inclui checagem de robots.txt, "
    "meta robots, JSON-LD e Score de Visibilidade IA (0–100)."
)

with st.expander("ℹ️ Como funciona e como interpretar"):
    st.markdown(
        """
A maioria dos crawlers de IA **não executa JavaScript** — eles leem apenas o HTML
retornado pelo servidor. Conteúdo injetado via JS (descrições em SPAs, reviews via
API, abas dinâmicas) fica **invisível** para esses agentes.

**Métricas por URL:**
- **Cobertura de conteúdo (%)** — quanto do texto visível ao usuário existe no HTML bruto. Abaixo de ~80% indica dependência relevante de JavaScript.
- **Conteúdo invisível para IA** — os trechos exatos que os bots não conseguem ler. Soluções típicas: SSR, pré-renderização ou mover o conteúdo crítico para o HTML inicial.
- **Robots.txt por bot** — bloqueios declarados para 9 crawlers de IA.
- **Bloqueio por User-Agent** — se o servidor responde 403/429 ao UA de bot mas 200 ao navegador, há bloqueio em WAF/CDN que o robots.txt não revela.
- **Score (0–100)** — composto: 50% cobertura de conteúdo, 25% liberação no robots.txt, 10% JSON-LD, 10% ausência de noindex, 5% volume mínimo de conteúdo. Bloqueio por UA corta o score pela metade. 🟢 ≥80 | 🟡 55–79 | 🔴 <55
        """
    )

urls_input = st.text_area(
    f"Cole as URLs para auditoria (uma por linha, máx. {MAX_URLS}):",
    height=160,
    placeholder="https://www.exemplo.com.br/produto-1\nhttps://www.exemplo.com.br/categoria/eletro",
)

col1, col2 = st.columns([1, 3])
start = col1.button("🚀 Executar auditoria", type="primary", use_container_width=True)

if start:
    urls = [u.strip() for u in urls_input.splitlines() if u.strip()]
    urls = [u if u.startswith("http") else "https://" + u for u in urls]
    urls = list(dict.fromkeys(urls))[:MAX_URLS]

    if not urls:
        st.warning("Insira ao menos uma URL válida.")
        st.stop()

    if not ensure_chromium():
        st.error("Não foi possível preparar o navegador headless. Verifique o deploy (packages.txt).")
        st.stop()

    st.write(f"**{len(urls)} URL(s)** na fila. Tempo estimado: ~{len(urls) * 12 // 60 + 1} min.")
    status_box = st.empty()
    progress_bar = st.progress(0.0)

    df_resumo, df_robots, df_missing = run_audit(urls, status_box, progress_bar)
    status_box.success(f"✅ Auditoria concluída: {len(df_resumo)} URL(s) processada(s).")

    st.session_state["results"] = (df_resumo, df_robots, df_missing)

if "results" in st.session_state:
    df_resumo, df_robots, df_missing = st.session_state["results"]

    if df_resumo["Score"].notna().any():
        m1, m2, m3 = st.columns(3)
        m1.metric("Score médio", f"{df_resumo['Score'].mean():.0f}/100")
        m2.metric("Cobertura média de conteúdo",
                  f"{df_resumo['Cobertura de conteúdo (%)'].mean():.0f}%")
        m3.metric("URLs críticas (🔴)", int((df_resumo["Score"] < 55).sum()))

    tab1, tab2, tab3 = st.tabs(
        ["📊 Resumo por URL", "🤖 Robots.txt por bot", "🙈 Conteúdo invisível para IA"]
    )
    with tab1:
        st.dataframe(df_resumo.sort_values("Score", na_position="last"),
                     use_container_width=True, hide_index=True)
    with tab2:
        st.dataframe(df_robots, use_container_width=True, hide_index=True)
        st.caption("✅ = bot autorizado no robots.txt | 🚫 = bot bloqueado")
    with tab3:
        if df_missing.empty:
            st.success("🎉 Nenhum bloco de conteúdo invisível identificado.")
        else:
            url_filter = st.selectbox("Filtrar por URL:",
                                      ["Todas"] + sorted(df_missing["URL"].unique().tolist()))
            shown = df_missing if url_filter == "Todas" else df_missing[df_missing["URL"] == url_filter]
            st.dataframe(shown, use_container_width=True, hide_index=True)

    st.download_button(
        "📥 Baixar relatório Excel (3 abas)",
        data=to_excel(df_resumo, df_robots, df_missing),
        file_name=f"auditoria_visibilidade_ia_{datetime.now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.divider()
st.caption("Desenvolvido para análise de GEO (Generative Engine Optimization) em e-commerce.")
