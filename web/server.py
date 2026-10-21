"""Back-end HTTP da camada web (FastAPI).

Este módulo é uma **capa fina** sobre o pipeline já validado:

- **jobs**: orquestra :mod:`web.jobs` (cria, inicia, pausa, retoma, cancela);
- **preview**: orquestra :mod:`web.preview` (capítulos, títulos, HTML);
- **EPUB/cache/tradução**: nada é reimplementado aqui — quem faz isso é o
  pacote ``epub_translator`` e o script ``traduzir.py``.

Desenho do SSE (validado empiricamente):

- a **thread do tradutor** (síncrona) empurra os eventos no ``EventBus`` do
  job (``job.bus.push``);
- o **gerador SSE** (assíncrono, no event loop) lê com ``job.bus.drain()`` e
  espera com ``await asyncio.sleep(...)``.

NUNCA há ``asyncio.Queue`` atravessando thread nem ``queue.get()``
bloqueante dentro do async: um desses trava o event loop inteiro e impede as
rotas de pausa/retomada de responderem enquanto o stream está aberto.

Erros são sempre em pt-BR e sempre em JSON ``{"error": "..."}`` — nunca um
traceback. A ``api_key`` nunca aparece em nenhuma resposta.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# A raiz do projeto entra no `sys.path` ANTES de qualquer import de
# `epub_translator` (pacote) e de `traduzir` (script na raiz): assim o
# servidor funciona com qualquer diretório de trabalho — `uvicorn web.server:app`
# de qualquer pasta, `python web/server.py`, um serviço do Windows, etc.
RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from fastapi import (  # noqa: E402
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from epub_translator.blocks import extract_blocks, format_block  # noqa: E402
from epub_translator.cache import BlockCache, glossary_fingerprint, key_for  # noqa: E402
from epub_translator.epub_io import EpubData, EpubReader, parse_xhtml  # noqa: E402
from epub_translator.lmstudio_client import (  # noqa: E402
    LMStudioClient,
    NoModelError,
    ServerNotRunningError,
)
from web.jobs import (  # noqa: E402
    ESTADOS_TERMINAIS,
    Job,
    JobManager,
    fechar_logs,
    gerenciador_padrao,
)
from web.preview import html_preview, listar_capitulos  # noqa: E402


logger = logging.getLogger("web.server")


# ── Constantes ───────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_API_KEY = "lm-studio"
DEFAULT_TARGET_LANG = "pt-BR"
DEFAULT_BATCH_SIZE = 5
DEFAULT_TEMPERATURE = 0.25
DEFAULT_OUTPUT_NAME = "traducao_resultado.epub"

BATCH_SIZE_MINIMO = 1
BATCH_SIZE_MAXIMO = 20

# Timeout curto na sondagem de modelos: a UI usa isso no botão "Testar
# conexão" e não pode ficar minutos esperando.
TIMEOUT_MODELOS = 15.0

# Intervalo entre as leituras do barramento de eventos (segundos).
INTERVALO_SSE = 0.15

# Pasta do build do front-end (Vite) — servida como estática quando existe.
PASTA_DIST = Path(__file__).resolve().parent / "ui" / "dist"

ORIGENS_CORS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

# Media type deduzido da extensão do recurso (usado em /asset).
MEDIA_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".css": "text/css",
}


# ── Dependências ─────────────────────────────────────────────────────────────

def obter_gerenciador() -> JobManager:
    """Gerenciador de jobs usado pelas rotas.

    É uma dependência do FastAPI de propósito: os testes (e qualquer outro
    consumidor) podem trocar o gerenciador com
    ``app.dependency_overrides[obter_gerenciador] = lambda: manager``.
    """
    return gerenciador_padrao()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalizar_config(bruto: dict[str, Any]) -> dict[str, Any]:
    """Normaliza a config vinda da UI (valores padrão garantidos).

    Args:
        bruto: Dicionário já parseado do JSON enviado pela UI.

    Returns:
        A config normalizada, pronta para o :class:`JobManager`.
    """
    glossario = bruto.get("glossary") or {}
    if not isinstance(glossario, dict):
        glossario = {}

    # O batch_size é limitado a 1..20: um valor fora da faixa é corrigido
    # (0 e negativos viram 1; acima de 20 vira 20).
    bruto_batch = bruto.get("batch_size")
    if bruto_batch is None:
        batch_size = DEFAULT_BATCH_SIZE
    else:
        try:
            batch_size = int(bruto_batch)
        except (TypeError, ValueError):
            batch_size = DEFAULT_BATCH_SIZE
    batch_size = max(BATCH_SIZE_MINIMO, min(BATCH_SIZE_MAXIMO, batch_size))

    bruto_temperatura = bruto.get("temperature")
    if bruto_temperatura is None:
        temperature = DEFAULT_TEMPERATURE
    else:
        try:
            temperature = float(bruto_temperatura)
        except (TypeError, ValueError):
            temperature = DEFAULT_TEMPERATURE

    limite_lotes = bruto.get("limit_batches")
    if limite_lotes is not None:
        try:
            limite_lotes = int(limite_lotes)
        except (TypeError, ValueError):
            limite_lotes = None

    return {
        "provider": bruto.get("provider") or "lmstudio",
        "base_url": bruto.get("base_url") or DEFAULT_BASE_URL,
        "api_key": bruto.get("api_key") or DEFAULT_API_KEY,
        "model": bruto.get("model") or None,
        "target_lang": bruto.get("target_lang") or DEFAULT_TARGET_LANG,
        "batch_size": batch_size,
        "temperature": temperature,
        "glossary": {str(k): str(v) for k, v in glossario.items()},
        "output_name": bruto.get("output_name") or DEFAULT_OUTPUT_NAME,
        "only": bruto.get("only") or None,
        "limit_batches": limite_lotes,
        "mock": bool(bruto.get("mock")),
    }


def _mensagem_de_conexao(exc: Exception, base_url: str) -> str:
    """Traduz a exceção do provedor numa mensagem em pt-BR para a UI."""
    if isinstance(exc, ServerNotRunningError):
        return (
            f"O LM Studio não está rodando em {base_url} — "
            "abra o LM Studio e inicie o servidor na aba Server."
        )
    if isinstance(exc, NoModelError):
        return "Nenhum modelo carregado no servidor — carregue um modelo."
    return f"Não foi possível conectar em {base_url}: {exc}"


def _sem_segredo(texto: str, segredo: str) -> str:
    """Remove o segredo (api_key) de uma mensagem de erro."""
    if segredo and segredo in texto:
        return texto.replace(segredo, "***")
    return texto


def _obter_job(job_id: str, manager: JobManager) -> Job:
    """Devolve o job ou levanta um 404 em pt-BR."""
    # O id entra no caminho do disco: nada de travessia de diretório.
    if not job_id or any(parte in job_id for parte in ("/", "\\", "..")):
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    job = manager.obter(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return job


def _dados_do_job(job: Job) -> EpubData:
    """Conteúdo do EPUB original do job (lido em memória, se ainda não estiver)."""
    if job.data is not None:
        return job.data

    try:
        job.data = EpubReader(job.src).read()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Não foi possível ler o EPUB do job: {exc}",
        ) from exc
    return job.data


def _media_type(caminho: str) -> str:
    """Media type deduzido da extensão do recurso."""
    return MEDIA_TYPES.get(Path(caminho).suffix.lower(), "application/octet-stream")


def _evento_terminal(evento: dict[str, Any]) -> bool:
    """Diz se o evento encerra o fluxo (erro, done ou estado final)."""
    tipo = evento.get("type")
    if tipo in ("error", "done"):
        return True
    if tipo == "state" and evento.get("state") in ESTADOS_TERMINAIS:
        return True
    return False


async def gerar_eventos(job: Job) -> AsyncIterator[str]:
    """Gera o fluxo SSE de um job (``data: {json}\\n\\n``).

    O gerador roda **no event loop** e nunca bloqueia: entre uma leitura e
    outra ele só faz ``await asyncio.sleep(INTERVALO_SSE)``. Por isso as
    rotas de pausa/retomada respondem mesmo com o stream aberto.

    Args:
        job: Job cujo barramento de eventos será lido.

    Yields:
        Cada evento SSE, já no formato ``data: {json}\\n\\n``.
    """
    while True:
        eventos = job.bus.drain()
        terminal = False

        for evento in eventos:
            yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
            if _evento_terminal(evento):
                terminal = True

        if terminal:
            # Última leitura: pega o evento de estado que vem logo depois do
            # "done" (o "done" e o "state" são empurrados em sequência).
            await asyncio.sleep(INTERVALO_SSE)
            for evento in job.bus.drain():
                yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
            return

        # Job já encerrado e sem eventos novos: nada mais vai chegar.
        if not eventos and job.state in ESTADOS_TERMINAIS:
            return

        await asyncio.sleep(INTERVALO_SSE)


def _traducoes_do_capitulo(job: Job, data: EpubData, caminho: str) -> dict[int, str]:
    """Mapa ``{índice_do_bloco: texto_traduzido}`` de um capítulo.

    A chave do cache é recalculada EXATAMENTE como o tradutor faz
    (:func:`epub_translator.translator.translate_epub`): mesmo texto, mesmo
    marcador, mesmo modelo, mesma origem, mesmo glossário e mesmo caminho de
    arquivo. Por isso ``job.model`` e ``job.src`` são os valores resolvidos
    pelo :class:`JobManager` — nunca os da requisição.
    """
    bruto = data.files.get(caminho)
    if bruto is None:
        return {}

    arvore, _doctype = parse_xhtml(bruto)
    blocos = extract_blocks(arvore)

    cache = BlockCache(job.cache_path)
    target = job.config.get("target_lang") or DEFAULT_TARGET_LANG

    # O tradutor usa "" quando não há glossário e o fingerprint quando há:
    # as duas variantes são testadas para casar com o cache gravado.
    linhas = [f"{k}\t{v}" for k, v in (job.glossary or {}).items()]
    hashes: list[str] = []
    if linhas:
        hashes.append(glossary_fingerprint(linhas))
    hashes.append("")

    traducoes: dict[int, str] = {}
    for indice, bloco in enumerate(blocos, start=1):
        marcador = format_block(indice, bloco.text)
        for hash_glossario in hashes:
            chave = key_for(
                text=bloco.text,
                marker_text=marcador,
                target=target,
                model=job.model,
                source=job.src,
                glossary_hash=hash_glossario,
                context="",
                file_path=caminho,
            )
            valor = cache.get(chave)
            if valor is not None:
                traducoes[indice] = valor
                break

    return traducoes


# ── Middleware global de erro ────────────────────────────────────────────────

class ErroGlobalMiddleware:
    """Transforma qualquer exceção não tratada em ``{"error": "..."}``.

    É um middleware ASGI puro (e não um ``exception_handler``): o handler
    global do FastAPI relança a exceção depois de responder, o que faria o
    cliente de teste ver a exceção em vez do JSON.

    Args:
        app: A aplicação ASGI a proteger.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self, scope: Any, receive: Any, send: Any
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        iniciada = False

        async def enviar(mensagem: Any) -> None:
            nonlocal iniciada
            if mensagem.get("type") == "http.response.start":
                iniciada = True
            await send(mensagem)

        try:
            await self.app(scope, receive, enviar)
        except Exception as exc:  # noqa: BLE001 — o handler é justamente este
            logger.exception("Erro não tratado no servidor: %s", exc)
            if iniciada:
                # A resposta já começou: não dá para trocar o corpo.
                raise
            resposta = JSONResponse(
                status_code=500,
                content={"error": "Erro interno no servidor. Tente novamente."},
            )
            await resposta(scope, receive, send)


# ── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Fecha os handlers de log dos jobs ao encerrar o servidor."""
    yield
    fechar_logs()


app = FastAPI(
    title="Tradutor de EPUB",
    description=(
        "Camada web fina sobre o pipeline de tradução de EPUB "
        "(LM Studio ou outro servidor compatível com a API da OpenAI)."
    ),
    version="1.0.0",
    lifespan=_lifespan,
)

# A ordem importa: o último `add_middleware` é o mais externo. O CORS fica
# por fora para que a resposta de erro JSON também venha com os cabeçalhos
# de CORS (o front-end em dev roda em outra porta).
app.add_middleware(ErroGlobalMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENS_CORS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Handlers de erro (sempre JSON, sempre em pt-BR) ─────────────────────────

@app.exception_handler(StarletteHTTPException)
async def _erro_http(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Converte qualquer ``HTTPException`` em ``{"error": "..."}``."""
    detalhe = (
        exc.detail
        if isinstance(exc.detail, str)
        else "Erro inesperado no servidor."
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": detalhe},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def _erro_validacao(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Erros de validação do FastAPI viram ``{"error": "..."}`` em pt-BR."""
    partes: list[str] = []
    for erro in list(exc.errors())[:3]:
        msg = str(erro.get("msg", "campo inválido"))
        local = erro.get("loc") or ()
        campo = ".".join(str(parte) for parte in local if parte != "body")
        partes.append(f"{campo}: {msg}" if campo else msg)

    detalhe = "; ".join(partes) or "campos inválidos"
    return JSONResponse(
        status_code=422,
        content={"error": f"Requisição inválida — {detalhe}"},
    )


# ── Provedores ───────────────────────────────────────────────────────────────

@app.post("/api/providers/models")
async def listar_modelos(payload: dict[str, Any]) -> dict[str, Any]:
    """Lista os modelos do provedor OpenAI-compatível.

    Body: ``{"base_url": str, "api_key": str}``.

    Em falha devolve ``{"models": [], "error": "<mensagem em pt-BR>"}`` com
    HTTP 200 — a UI mostra o ponto vermelho sem quebrar. A ``api_key`` nunca
    aparece na resposta.
    """
    base_url = str(payload.get("base_url") or DEFAULT_BASE_URL)
    api_key = str(payload.get("api_key") or DEFAULT_API_KEY)

    try:
        cliente = LMStudioClient(
            base_url=base_url,
            api_key=api_key,
            timeout=TIMEOUT_MODELOS,
        )
        modelos = list(cliente.list_models())
    except Exception as exc:
        mensagem = _sem_segredo(_mensagem_de_conexao(exc, base_url), api_key)
        return {"models": [], "error": mensagem}

    return {"models": modelos}


# ── Jobs ─────────────────────────────────────────────────────────────────────

@app.post("/api/jobs")
async def criar_job(
    file: UploadFile = File(...),
    config: str = Form(...),
    manager: JobManager = Depends(obter_gerenciador),
) -> dict[str, Any]:
    """Cria um job de tradução a partir de um EPUB enviado.

    Multipart: ``file`` (o .epub) e ``config`` (a configuração em JSON).
    """
    nome = file.filename or ""
    if not nome.lower().endswith(".epub"):
        raise HTTPException(
            status_code=400, detail="O arquivo enviado não é um .epub."
        )

    try:
        bruto = json.loads(config)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Configuração inválida: {exc}"
        ) from exc

    if not isinstance(bruto, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                "Configuração inválida: o campo 'config' deve ser um objeto JSON."
            ),
        )

    cfg = _normalizar_config(bruto)
    conteudo = await file.read()

    # O upload é gravado numa pasta temporária (fora da raiz do projeto) e o
    # JobManager o copia para a pasta do job (`original.epub`).
    with tempfile.TemporaryDirectory(prefix="epub-upload-") as temporaria:
        origem = Path(temporaria) / "enviado.epub"
        origem.write_bytes(conteudo)

        try:
            job = manager.criar(str(origem), cfg)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=400, detail="O arquivo enviado não foi encontrado."
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    manager.iniciar(job)

    return {"job_id": job.id, "state": job.state}


@app.get("/api/jobs/{job_id}")
def obter_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> dict[str, Any]:
    """Fotografia do job (sem a api_key)."""
    job = _obter_job(job_id, manager)
    return manager.snapshot(job)


@app.get("/api/jobs/{job_id}/events")
async def eventos_do_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> StreamingResponse:
    """Fluxo SSE do job (``data: {json}\\n\\n``)."""
    job = _obter_job(job_id, manager)
    return StreamingResponse(
        gerar_eventos(job),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/pause")
def pausar_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> dict[str, Any]:
    """Pede a pausa do job (a tradução termina o lote atual)."""
    job = _obter_job(job_id, manager)
    manager.pausar(job)
    return {"paused": True}


@app.post("/api/jobs/{job_id}/resume")
def retomar_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> dict[str, Any]:
    """Retoma o job com o MESMO cache (os blocos já traduzidos voltam)."""
    job = _obter_job(job_id, manager)
    manager.retomar(job)
    return {"paused": False, "state": job.state}


@app.post("/api/jobs/{job_id}/cancel")
def cancelar_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> dict[str, Any]:
    """Cancela o job (o cache gravado é preservado)."""
    job = _obter_job(job_id, manager)
    manager.cancelar(job)
    return {"cancelled": True}


@app.post("/api/jobs/{job_id}/discard-cache")
def descartar_cache(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> dict[str, Any]:
    """Apaga o cache do job (ação separada do cancelamento)."""
    job = _obter_job(job_id, manager)
    caminho = Path(job.cache_path)
    if caminho.exists():
        try:
            caminho.unlink()
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Não foi possível apagar o cache do job: {exc}",
            ) from exc
    return {"discarded": True}


@app.get("/api/jobs/{job_id}/download")
def baixar_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> FileResponse:
    """Baixa o EPUB traduzido."""
    job = _obter_job(job_id, manager)
    if not os.path.exists(job.out):
        raise HTTPException(
            status_code=404, detail="O EPUB traduzido ainda não está pronto."
        )

    nome = Path(job.config.get("output_name") or DEFAULT_OUTPUT_NAME).name
    if not nome.lower().endswith(".epub"):
        nome = f"{nome}.epub"

    return FileResponse(
        job.out,
        media_type="application/epub+zip",
        filename=nome,
    )


@app.get("/api/jobs/{job_id}/chapters")
def capitulos_do_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> list[dict[str, Any]]:
    """Lista os capítulos do EPUB, na ordem do spine."""
    job = _obter_job(job_id, manager)
    data = _dados_do_job(job)
    return listar_capitulos(data)


@app.get("/api/jobs/{job_id}/preview")
def preview_do_capitulo(
    job_id: str,
    chapter: str,
    side: str = "original",
    manager: JobManager = Depends(obter_gerenciador),
) -> Response:
    """HTML sanitizado de UM capítulo (original ou traduzido)."""
    job = _obter_job(job_id, manager)
    data = _dados_do_job(job)

    if side not in ("original", "translated"):
        raise HTTPException(
            status_code=400,
            detail="Lado inválido: use 'original' ou 'translated'.",
        )

    if chapter not in data.files:
        raise HTTPException(status_code=404, detail="Capítulo não encontrado.")

    traducoes: dict[int, str] | None = None
    if side == "translated":
        traducoes = _traducoes_do_capitulo(job, data, chapter)

    html = html_preview(data, chapter, side, traducoes, job.id)
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/api/jobs/{job_id}/asset")
def recurso_do_epub(
    job_id: str,
    path: str,
    manager: JobManager = Depends(obter_gerenciador),
) -> Response:
    """Devolve uma entrada do EPUB original (imagens, CSS, ...)."""
    job = _obter_job(job_id, manager)
    data = _dados_do_job(job)

    bruto = data.files.get(path)
    if bruto is None:
        raise HTTPException(
            status_code=404, detail="Recurso não encontrado no EPUB."
        )

    return Response(content=bruto, media_type=_media_type(path))


@app.get("/api/jobs/{job_id}/style.css")
def css_do_epub(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> Response:
    """CSS do próprio EPUB (todas as entradas ``text/css`` do manifesto)."""
    job = _obter_job(job_id, manager)
    data = _dados_do_job(job)

    partes: list[str] = []
    for item in data.all_items.values():
        if item.media_type != "text/css":
            continue
        caminho = item.zip_path or item.href
        bruto = data.files.get(caminho)
        if bruto:
            partes.append(bruto.decode("utf-8", errors="replace"))

    return Response(
        content="\n".join(partes), media_type="text/css; charset=utf-8"
    )


@app.get("/api/jobs/{job_id}/log")
def log_do_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> dict[str, Any]:
    """Conteúdo do arquivo de log do job."""
    job = _obter_job(job_id, manager)

    texto = ""
    try:
        texto = Path(job.log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        texto = ""

    return {"text": texto}


@app.get("/api/jobs/{job_id}/integrity")
def integridade_do_job(
    job_id: str, manager: JobManager = Depends(obter_gerenciador)
) -> list[dict[str, Any]]:
    """Itens da verificação de integridade do EPUB gerado."""
    job = _obter_job(job_id, manager)
    return list(job.integrity)


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Sonda rápida (o front-end usa para saber se a API está de pé)."""
    return {"status": "ok"}


# ── Estático: build do front-end ────────────────────────────────────────────

if (PASTA_DIST / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(PASTA_DIST / "assets")),
        name="assets",
    )


@app.get("/{caminho:path}", include_in_schema=False)
def front_end(caminho: str) -> Response:
    """Fallback de SPA: qualquer rota fora de ``/api`` devolve o index.html."""
    if caminho == "api" or caminho.startswith("api/"):
        raise HTTPException(status_code=404, detail="Rota não encontrada.")

    index = PASTA_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                "O front-end ainda não foi construído "
                "(rode `npm run build` em web/ui)."
            ),
        )

    return FileResponse(index, media_type="text/html; charset=utf-8")
