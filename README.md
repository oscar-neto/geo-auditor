# 🔎 Auditor de Visibilidade para Agentes de IA (GEO)

Aplicação web que audita uma lista de URLs e identifica **quais conteúdos textuais estão visíveis para agentes de IA** (GPTBot, ClaudeBot, PerplexityBot, Google-Extended etc.), comparando o HTML bruto (visão dos bots, sem JavaScript) com o DOM renderizado (visão do usuário).

## O que a aplicação entrega por URL

- **Score de Visibilidade IA (0–100)** com classificação 🟢🟡🔴
- **Cobertura de conteúdo (%)**: quanto do texto visível ao usuário existe no HTML bruto
- **Conteúdo invisível para IA**: os trechos exatos dependentes de JavaScript
- **Robots.txt por bot**: permissões para 9 crawlers de IA
- **Bloqueio por User-Agent**: detecta bloqueios em WAF/CDN (403/429 para bots)
- **Noindex e JSON-LD** no HTML bruto
- **Export em Excel** com 3 abas

## 🚀 Deploy no Streamlit Community Cloud (gratuito, ~5 min)

1. Crie um repositório no GitHub (pode ser privado) e envie estes 4 arquivos:
   - `app.py`
   - `requirements.txt`
   - `packages.txt` ← obrigatório: dependências de sistema do Chromium
   - `README.md`
2. Acesse [share.streamlit.io](https://share.streamlit.io) e faça login com o GitHub.
3. Clique em **"Create app" → "Deploy a public app from GitHub"**.
4. Selecione o repositório, branch `main` e arquivo `app.py`.
5. Clique em **Deploy**. Em poucos minutos você terá um link público do tipo
   `https://seu-app.streamlit.app` para compartilhar com qualquer pessoa.

> **Nota:** na primeira auditoria após o deploy, a aplicação instala o Chromium
> automaticamente (~1 min). As execuções seguintes são imediatas.

## 💻 Rodar localmente (opcional)

```bash
pip install -r requirements.txt
playwright install chromium
streamlit run app.py
```

## 🔁 Alternativa de hospedagem: Hugging Face Spaces

Se preferir, o mesmo projeto roda no [Hugging Face Spaces](https://huggingface.co/spaces)
(também gratuito): crie um Space com SDK **Streamlit**, envie os mesmos arquivos e
renomeie `packages.txt` — no HF Spaces ele já é lido automaticamente com esse nome.

## ⚠️ Por que não Netlify/Vercel?

Netlify e Vercel hospedam sites estáticos/serverless JavaScript. Este auditor precisa
de um servidor Python com navegador headless (Playwright) e faz requisições a URLs de
terceiros — algo inviável direto no navegador por restrições de CORS. Por isso a
hospedagem recomendada é Streamlit Community Cloud ou Hugging Face Spaces.

## Limitações

- Recomenda-se até **50 URLs por auditoria** (~10–15s por URL).
- Sites com anti-bot agressivo podem bloquear até a renderização headless.
- O robots.txt indica *permissão declarada*; não garante que o bot rastreie a página.
- No plano gratuito do Streamlit Cloud, o app "hiberna" após períodos sem uso —
  o primeiro acesso seguinte demora ~30s para acordar.

---
*Desenvolvido para análise de GEO (Generative Engine Optimization) em e-commerce.*
