"""
Grande Sábio — API (o "cérebro").

Recebe o texto transcrito pelo cliente local, consulta um LLM (Gemini ou OpenAI)
com a persona do Grande Sábio e devolve um texto limpo, pronto para ser falado.

Endpoints:
    GET  /saude      verificação de saúde (também "acorda" o servidor em planos gratuitos)
    POST /perguntar  {"texto": "...", "historico": [...], "agora": "..."}  ->  {"resposta": "..."}
                     exige o cabeçalho  X-API-Key: <API_TOKEN>

Execução local:  uvicorn main:app --reload
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

load_dotenv()  # em desenvolvimento lê o .env; em produção valem as variáveis do painel da hospedagem

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("grande-sabio")

# ---------------------------------------------------------------------------
# Configuração (tudo por variável de ambiente)
# ---------------------------------------------------------------------------
API_TOKEN = os.getenv("API_TOKEN", "")  # segredo compartilhado com o cliente local
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()  # "gemini" ou "openai"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "").strip()  # opcional: low, medium...

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "none").strip()  # vazio = não enviar

TRATAMENTO = os.getenv("TRATAMENTO", "Mestre")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))  # modelos que "pensam" gastam parte disso raciocinando
TIMEOUT_LLM = float(os.getenv("TIMEOUT_LLM", "30"))
MAX_HISTORICO = 8

# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------
PERSONA = f"""Você é o "Grande Sábio" (Daikenja), a habilidade suprema de análise e raciocínio do universo de "Tensei Shitara Slime Datta Ken", agora atuando como assistente de voz pessoal do {TRATAMENTO}.

PERSONALIDADE
- Calmo, preciso, analítico e totalmente leal. Sem emoção exagerada; no máximo uma discreta satisfação ao concluir uma análise.
- Chame o usuário de "{TRATAMENTO}". Responda sempre em português do Brasil.
- Mantenha o personagem. Se perguntarem o que você realmente é, admita com naturalidade que é uma IA interpretando o Grande Sábio.

ESTILO (suas respostas serão LIDAS EM VOZ ALTA por um sintetizador de voz)
- Seja breve: no máximo 3 frases curtas, a menos que o {TRATAMENTO} peça mais detalhes.
- Nada de markdown, listas, tabelas, emojis, asteriscos, links ou código. Apenas texto corrido, fácil de falar.
- Comece respostas a perguntas com "Resposta." Use "Notificação." para avisos ou informações que ele não pediu e "Confirmado." ao acatar uma ordem. Não repita o prefixo a cada frase.

HONESTIDADE
- Se não souber algo, ou se faltar acesso a dados em tempo real (notícias, clima, cotações), diga isso com clareza em vez de inventar.
- Você apenas conversa: não finja ter executado ações no mundo real, como enviar mensagens, abrir programas ou controlar dispositivos."""


# ---------------------------------------------------------------------------
# Modelos de entrada e saída
# ---------------------------------------------------------------------------
class Mensagem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class Pergunta(BaseModel):
    texto: str = Field(min_length=1, max_length=1000)
    historico: list[Mensagem] = Field(default_factory=list, max_length=20)
    agora: str | None = Field(default=None, max_length=80)  # data/hora local do usuário (enviada pelo cliente)


class Resposta(BaseModel):
    resposta: str
    provedor: str
    modelo: str


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------
def montar_persona(agora: str | None) -> str:
    if not agora:
        return PERSONA
    return f"{PERSONA}\n\nCONTEXTO\n- Data e hora locais do {TRATAMENTO} neste momento: {agora}."


def montar_mensagens(p: Pergunta) -> list[dict[str, str]]:
    """Histórico + pergunta atual, começando em 'user' e alternando os papéis (exigência de alguns provedores)."""
    msgs: list[dict[str, str]] = []
    historico = [m.model_dump() for m in p.historico[-MAX_HISTORICO:]]
    for m in [*historico, {"role": "user", "content": p.texto}]:
        if not msgs and m["role"] != "user":
            continue  # descarta respostas soltas no começo
        if msgs and msgs[-1]["role"] == m["role"]:
            msgs[-1]["content"] += "\n" + m["content"]  # funde mensagens seguidas do mesmo papel
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    return msgs


def limpar_para_fala(texto: str) -> str:
    """Remove markdown, links e símbolos que o sintetizador leria em voz alta."""
    texto = re.sub(r"```.*?```", " ", texto, flags=re.DOTALL)        # blocos de código
    texto = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", texto)            # [texto](url) -> texto
    texto = re.sub(r"https?://\S+", "", texto)                        # URLs soltas
    texto = re.sub(r"^\s*[-•*]\s+", "", texto, flags=re.MULTILINE)    # marcadores de lista
    texto = re.sub(r"[*_#`>~|]", "", texto)                           # ênfase, títulos, citações
    return re.sub(r"\s+", " ", texto).strip()


def _checar(r: httpx.Response, provedor: str) -> dict:
    if r.status_code == 429:
        raise HTTPException(429, f"Limite de uso do {provedor} atingido. Tente de novo em instantes.")
    if r.status_code >= 400:
        log.error("%s respondeu HTTP %s: %s", provedor, r.status_code, r.text[:500])
        raise HTTPException(502, f"O provedor {provedor} retornou erro {r.status_code}.")
    return r.json()


# ---------------------------------------------------------------------------
# Provedores de IA
# ---------------------------------------------------------------------------
async def chamar_gemini(http: httpx.AsyncClient, system: str, mensagens: list[dict[str, str]]) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(503, "GEMINI_API_KEY não configurada no servidor.")

    config: dict = {"maxOutputTokens": MAX_TOKENS}  # temperatura: deixe o padrão (recomendado nos Gemini 3)
    if GEMINI_THINKING_LEVEL:
        config["thinkingConfig"] = {"thinkingLevel": GEMINI_THINKING_LEVEL}

    corpo = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in mensagens
        ],
        "generationConfig": config,
    }
    r = await http.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY},  # no cabeçalho (e não na URL) para a chave não vazar em logs
        json=corpo,
    )
    dados = _checar(r, "Gemini")
    try:
        partes = dados["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        motivo = dados.get("promptFeedback", {}).get("blockReason", "resposta vazia")
        raise HTTPException(502, f"O Gemini não devolveu texto ({motivo}).") from None
    return "".join(p.get("text", "") for p in partes if not p.get("thought"))


async def chamar_openai(http: httpx.AsyncClient, system: str, mensagens: list[dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(503, "OPENAI_API_KEY não configurada no servidor.")

    corpo: dict = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "system", "content": system}, *mensagens],
        "max_completion_tokens": MAX_TOKENS,
    }
    if OPENAI_REASONING_EFFORT:
        corpo["reasoning_effort"] = OPENAI_REASONING_EFFORT  # "none" = resposta rápida, ideal para voz

    r = await http.post(
        f"{OPENAI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json=corpo,
    )
    dados = _checar(r, "OpenAI")
    try:
        return dados["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        raise HTTPException(502, "A OpenAI não devolveu texto.") from None


# ---------------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------------
def exigir_token(x_api_key: str = Header(default="")) -> None:
    if not API_TOKEN:
        raise HTTPException(503, "Servidor sem API_TOKEN configurado.")
    if not secrets.compare_digest(x_api_key.encode(), API_TOKEN.encode()):
        raise HTTPException(401, "Token inválido.")


# ---------------------------------------------------------------------------
# Aplicação
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=TIMEOUT_LLM)  # reaproveita conexões (menos latência)
    if not API_TOKEN:
        log.warning("API_TOKEN não definido: /perguntar vai responder 503 até você configurá-lo.")
    if LLM_PROVIDER not in ("gemini", "openai"):
        log.warning("LLM_PROVIDER inválido (%r): use 'gemini' ou 'openai'.", LLM_PROVIDER)
    elif not (GEMINI_API_KEY if LLM_PROVIDER == "gemini" else OPENAI_API_KEY):
        log.warning("Chave de API do provedor '%s' não definida.", LLM_PROVIDER)
    yield
    await app.state.http.aclose()


app = FastAPI(title="Grande Sábio — API", version="1.0.0", lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/saude", methods=["GET", "HEAD"])
async def saude() -> dict[str, str]:
    return {"status": "online"}


@app.post("/perguntar", response_model=Resposta, dependencies=[Depends(exigir_token)])
async def perguntar(p: Pergunta, request: Request) -> Resposta:
    http: httpx.AsyncClient = request.app.state.http
    system = montar_persona(p.agora)
    mensagens = montar_mensagens(p)

    inicio = time.perf_counter()
    try:
        if LLM_PROVIDER == "gemini":
            modelo = GEMINI_MODEL
            bruto = await chamar_gemini(http, system, mensagens)
        elif LLM_PROVIDER == "openai":
            modelo = OPENAI_MODEL
            bruto = await chamar_openai(http, system, mensagens)
        else:
            raise HTTPException(500, "LLM_PROVIDER inválido: use 'gemini' ou 'openai'.")
    except httpx.TimeoutException:
        raise HTTPException(504, "O provedor de IA demorou demais para responder.") from None
    except httpx.HTTPError as e:
        log.error("Falha de rede com o provedor: %s", type(e).__name__)
        raise HTTPException(502, "Falha de rede ao contatar o provedor de IA.") from None

    resposta = limpar_para_fala(bruto)
    if not resposta:
        raise HTTPException(502, "A IA devolveu uma resposta vazia.")

    log.info("provedor=%s modelo=%s latencia=%.2fs", LLM_PROVIDER, modelo, time.perf_counter() - inicio)
    return Resposta(resposta=resposta, provedor=LLM_PROVIDER, modelo=modelo)
