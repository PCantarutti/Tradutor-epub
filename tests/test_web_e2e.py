"""Teste de ponta a ponta da camada web (back-end FastAPI).

Percorre a cadeia COMPLETA da API com um **servidor OpenAI-compatível falso**
(nada de LM Studio real — o teste precisa ser rápido e determinístico):

    upload → chapters → preview (original e translated) → snapshot
           → pausa → retomada (com cache) → download → EPUB válido

O servidor falso implementa o mesmo protocolo do ``LMStudioClient``:

- ``GET  {base}/models``            → ``{"data": [{"id": "modelo-falso"}]}``
- ``POST {base}/chat/completions``   → ``{"choices": [{"message": {"content": ...}}]}``

A resposta do modelo sai no MESMO formato do prompt (``[i]\\n<texto>``), que
é o que o :func:`epub_translator.prompts.parse_response` entende. O texto de
cada bloco volta com um prefixo (``"PT: "``), o que prova (no preview
traduzido e no EPUB final) que a tradução realmente passou pelo modelo.

ARMADILHAS respeitadas aqui:

1. **O ``TestClient`` do FastAPI TRAVA ao consumir um stream SSE** — por isso
   o SSE NUNCA é pedido via HTTP neste arquivo: o progresso é lido por
   *polling* do snapshot (``GET /api/jobs/{id}``), com ``time.sleep`` curto
   entre as tentativas. Quando o SSE precisa ser verificado, o gerador
   ``server.gerar_eventos`` é consumido **direto** (ver o teste 4).
2. O ``JobManager`` NÃO recebe cliente pela rota ``POST /api/jobs``: ele
   cria o ``LMStudioClient`` a partir da ``base_url`` da config. Por isso o
   servidor falso HTTP é a forma mais fiel de injetar o provedor falso.
3. O servidor falso é derrubado no ``finally`` e tudo que o teste cria fica
   dentro de ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from epub_translator.prompts import separator_index
from traduzir import verificar_epub
from web import server
from web.jobs import JobManager, fechar_logs


# ── Constantes ──────────────────────────────────────────────────────────────

RAIZ = Path(__file__).resolve().parent.parent
EPUB_TESTE = RAIZ / "fixtures" / "test_input.epub"

CAPITULO_1 = "OEBPS/Text/section-0001.html"
CAPITULO_2 = "OEBPS/Text/section-0002.html"

# O fixture tem 2 capítulos: 13 + 3 blocos.
BLOCOS_CAPITULO_1 = 13
BLOCOS_CAPITULO_2 = 3
TOTAL_BLOCOS = BLOCOS_CAPITULO_1 + BLOCOS_CAPITULO_2

MODELO_FALSO = "modelo-falso"
PREFIXO_TRADUCAO = "PT: "

RE_INDEX = re.compile(r'data-block-index="(\d+)"')

# Tempos-limite dos testes (o servidor falso responde na hora).
TIMEOUT_CONCLUSAO = 30.0
TIMEOUT_PAUSA = 60.0


# ── Servidor OpenAI-compatível falso ────────────────────────────────────────

def _dividir_blocos(texto: str) -> list[tuple[int, str]]:
    """Divide o prompt em ``(índice, texto)`` usando o separador oficial.

    Usa :func:`epub_translator.prompts.separator_index` — a fonte ÚNICA do
    formato de separador (``[i]``), a mesma usada pelo ``parse_response``.
    """
    blocos: list[tuple[int, str]] = []
    indice: int | None = None
    linhas: list[str] = []

    for linha in texto.split("\n"):
        sep = separator_index(linha)
        if sep is not None:
            if indice is not None:
                blocos.append((indice, "\n".join(linhas)))
            indice = sep
            linhas = []
        elif indice is not None:
            linhas.append(linha)

    if indice is not None:
        blocos.append((indice, "\n".join(linhas)))

    return blocos


class ServidorFalso:
    """Servidor OpenAI-compatível falso (rápido e determinístico).

    O ``chat`` devolve os mesmos separadores ``[i]`` do prompt, com o texto
    de cada bloco prefixado por ``prefixo`` (prova de que a tradução passou
    pelo modelo). As requisições recebidas ficam em :attr:`requisicoes` —
    é o que permite provar que nenhum bloco foi traduzido duas vezes.

    Args:
        prefixo: Prefixo aplicado ao texto traduzido de cada bloco.
    """

    def __init__(self, prefixo: str = PREFIXO_TRADUCAO) -> None:
        self.prefixo = prefixo
        self.requisicoes: list[list[tuple[int, str]]] = []
        self.bloquear_ate: int | None = None
        self.liberado = threading.Event()
        self._lock = threading.Lock()
        self._servidor: _ServidorHTTP | None = None
        self._thread: threading.Thread | None = None

    # ── Ciclo de vida ───────────────────────────────────────────────────

    def iniciar(self) -> str:
        """Sobe o servidor numa porta livre e devolve a ``base_url``."""
        self._servidor = _ServidorHTTP(("127.0.0.1", 0), self)
        self._thread = threading.Thread(
            target=self._servidor.serve_forever,
            name="servidor-falso",
            daemon=True,
        )
        self._thread.start()
        host, porta = self._servidor.server_address[:2]
        return f"http://{host}:{porta}/v1"

    def parar(self) -> None:
        """Derruba o servidor (sempre chamado no ``finally``)."""
        if self._servidor is not None:
            self._servidor.shutdown()
            self._servidor.server_close()
            self._servidor = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ── Respostas ───────────────────────────────────────────────────────

    def responder(self, conteudo: str) -> str:
        """Devolve a resposta do modelo para um prompt de usuário.

        Args:
            conteudo: Conteúdo da última mensagem ``user`` do prompt.

        Returns:
            A resposta no formato ``[i]\\n<texto traduzido>``.
        """
        blocos = _dividir_blocos(conteudo)

        with self._lock:
            numero = len(self.requisicoes) + 1
            self.requisicoes.append(blocos)

        # Gate de pausa: a N-ésima requisição fica esperando o teste
        # liberar — é o que torna a pausa determinística.
        if self.bloquear_ate is not None and numero == self.bloquear_ate:
            self.liberado.wait(timeout=60)

        partes: list[str] = []
        for indice, texto in blocos:
            partes.append(f"[{indice}]")
            partes.append(f"{self.prefixo}{texto}")
        return "\n".join(partes)

    # ── Auxiliares para o teste ──────────────────────────────────────────

    def esperar_requisicoes(self, quantidade: int, timeout: float = 10.0) -> None:
        """Espera o servidor receber ``quantidade`` requisições de chat."""
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            with self._lock:
                if len(self.requisicoes) >= quantidade:
                    return
            time.sleep(0.05)
        raise AssertionError(
            f"o servidor falso não recebeu {quantidade} requisição(ões) de chat"
        )

    @property
    def total_requisicoes(self) -> int:
        """Quantas requisições de chat o servidor já atendeu."""
        with self._lock:
            return len(self.requisicoes)


class _ServidorHTTP(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` que carrega o estado do servidor falso."""

    daemon_threads = True
    allow_reuse_address = True
    # Não espera as threads de keep-alive no ``server_close()``.
    block_on_close = False

    def __init__(self, endereco: Any, estado: ServidorFalso) -> None:
        super().__init__(endereco, _Handler)
        self.estado = estado


class _Handler(BaseHTTPRequestHandler):
    """Handler do protocolo OpenAI (modelos + chat)."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # noqa: D102 — silencia o log
        return

    # ── Helpers ─────────────────────────────────────────────────────────

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        corpo = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    # ── Rotas ───────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 — nome exigido pelo BaseHTTPRequestHandler
        """``GET /v1/models`` → a lista de modelos."""
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"data": [{"id": MODELO_FALSO}]})
        else:
            self._json(404, {"error": "rota desconhecida"})

    def do_POST(self) -> None:  # noqa: N802 — nome exigido pelo BaseHTTPRequestHandler
        """``POST /v1/chat/completions`` → a resposta do modelo."""
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": "rota desconhecida"})
            return

        tamanho = int(self.headers.get("Content-Length") or 0)
        bruto = self.rfile.read(tamanho) if tamanho else b"{}"
        try:
            corpo = json.loads(bruto.decode("utf-8") or "{}")
        except ValueError:
            self._json(400, {"error": "corpo inválido"})
            return

        mensagens = corpo.get("messages") or []
        conteudo = ""
        for mensagem in mensagens:
            if mensagem.get("role") == "user":
                conteudo = mensagem.get("content", "")

        estado: ServidorFalso = self.server.estado  # type: ignore[attr-defined]
        resposta = estado.responder(conteudo)

        self._json(200, {"choices": [{"message": {"content": resposta}}]})


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def epub_teste() -> Path:
    """EPUB pequeno de teste (2 capítulos: 13 + 3 blocos)."""
    if not EPUB_TESTE.exists():
        pytest.skip(f"EPUB de teste ausente: {EPUB_TESTE}")
    return EPUB_TESTE


@pytest.fixture()
def servidor_falso() -> ServidorFalso:
    """Servidor OpenAI-compatível falso (derrubado no fim do teste)."""
    servidor = ServidorFalso()
    try:
        yield servidor
    finally:
        servidor.parar()


@pytest.fixture()
def manager(tmp_path: Path) -> JobManager:
    """Gerenciador de jobs com a pasta em ``tmp_path``."""
    return JobManager(pasta=tmp_path / "jobs")


@pytest.fixture()
def cliente(manager: JobManager) -> TestClient:
    """TestClient com o gerenciador de jobs apontando para ``tmp_path``."""
    server.app.dependency_overrides[server.obter_gerenciador] = lambda: manager
    with TestClient(server.app) as cliente:
        yield cliente
    server.app.dependency_overrides.clear()
    fechar_logs()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _config(base_url: str, **extra: Any) -> dict[str, Any]:
    """Config de um job apontando para o servidor falso."""
    base: dict[str, Any] = {
        "provider": "lmstudio",
        "base_url": base_url,
        "api_key": "sk-chave-falsa-1234",
        "model": None,
        "target_lang": "pt-BR",
        "batch_size": 5,
        "temperature": 0.25,
        "glossary": {},
        "output_name": "traducao_resultado.epub",
        "mock": False,
    }
    base.update(extra)
    return base


def _criar_job(
    cliente: TestClient,
    epub: Path,
    base_url: str,
    **extra: Any,
) -> str:
    """Cria um job pela rota ``POST /api/jobs`` e devolve o ``job_id``."""
    with epub.open("rb") as arquivo:
        resposta = cliente.post(
            "/api/jobs",
            files={"file": (epub.name, arquivo, "application/epub+zip")},
            data={"config": json.dumps(_config(base_url, **extra))},
        )

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["job_id"]
    return str(corpo["job_id"])


def _esperar_estado(
    cliente: TestClient,
    job_id: str,
    estados: set[str],
    timeout: float = TIMEOUT_CONCLUSAO,
) -> dict[str, Any]:
    """Faz *polling* do snapshot até o job chegar a um dos ``estados``.

    (O SSE NÃO é consumido pelo ``TestClient`` — ele trava. O polling do
    snapshot é a alternativa segura e determinística.)
    """
    limite = time.monotonic() + timeout
    ultimo: dict[str, Any] = {}

    while time.monotonic() < limite:
        resposta = cliente.get(f"/api/jobs/{job_id}")
        assert resposta.status_code == 200, resposta.text
        ultimo = resposta.json()
        if ultimo["state"] in estados:
            return ultimo
        time.sleep(0.2)

    raise AssertionError(
        f"o job {job_id} não chegou ao estado {sorted(estados)} "
        f"em {timeout}s — último snapshot: {ultimo}"
    )


def _indices_do_preview(html: str) -> list[str]:
    """Lista os ``data-block-index`` presentes no HTML do preview."""
    return RE_INDEX.findall(html)


async def _coletar_eventos(
    gerador: AsyncIterator[str], timeout: float = TIMEOUT_CONCLUSAO
) -> list[str]:
    """Consome o gerador SSE **direto** (nunca pelo ``TestClient``).

    O ``TestClient`` do FastAPI espera a resposta inteira e trava enquanto
    o stream SSE está aberto — por isso o gerador é consumido aqui, direto.
    """
    partes: list[str] = []

    async def _consumir() -> None:
        async for parte in gerador:
            partes.append(parte)

    await asyncio.wait_for(_consumir(), timeout)
    return partes


# ── Teste 1: cadeia completa ────────────────────────────────────────────────

def test_cadeia_completa_upload_ate_download(
    cliente: TestClient,
    epub_teste: Path,
    servidor_falso: ServidorFalso,
    tmp_path: Path,
) -> None:
    """Upload → chapters → preview → espera → download → EPUB válido.

    Prova a cadeia inteira da API com o provedor falso: o job é criado pela
    rota, os capítulos são listados na ordem do spine, o preview tem o
    ``data-block-index`` de cada bloco, o job termina em ``done`` com os 16
    blocos traduzidos e o EPUB baixado passa na verificação final
    (``traduzir.verificar_epub``).
    """
    base_url = servidor_falso.iniciar()

    # (1) criação do job pela rota (upload multipart)
    job_id = _criar_job(cliente, epub_teste, base_url)

    # (2) capítulos: 2, na ordem do spine, com título e contagem de blocos
    resposta = cliente.get(f"/api/jobs/{job_id}/chapters")
    assert resposta.status_code == 200, resposta.text
    capitulos = resposta.json()

    assert len(capitulos) == 2
    assert capitulos[0]["path"] == CAPITULO_1
    assert capitulos[0]["blocks"] == BLOCOS_CAPITULO_1
    assert capitulos[0]["title"]
    assert capitulos[1]["path"] == CAPITULO_2
    assert capitulos[1]["blocks"] == BLOCOS_CAPITULO_2
    assert capitulos[1]["title"]
    assert capitulos[0]["title"] != capitulos[1]["title"]

    # (3) preview do lado ORIGINAL: 13 blocos com data-block-index 1-based
    resposta = cliente.get(
        f"/api/jobs/{job_id}/preview",
        params={"chapter": CAPITULO_1, "side": "original"},
    )
    assert resposta.status_code == 200, resposta.text
    indices = _indices_do_preview(resposta.text)
    assert len(indices) == BLOCOS_CAPITULO_1
    assert indices == [str(i) for i in range(1, BLOCOS_CAPITULO_1 + 1)]

    # (4) espera o job terminar (polling — o SSE não é consumido aqui)
    snapshot = _esperar_estado(cliente, job_id, {"done", "error"})

    assert snapshot["state"] == "done", snapshot
    assert snapshot["error"] is None
    assert snapshot["total"] == TOTAL_BLOCOS
    assert snapshot["translated"] == TOTAL_BLOCOS
    assert snapshot["cached"] == 0
    assert snapshot["failed"] == 0
    assert snapshot["discarded"] == 0
    assert snapshot["done"] == TOTAL_BLOCOS
    assert snapshot["has_output"] is True
    assert snapshot["model"] == MODELO_FALSO
    assert snapshot["output_name"] == "traducao_resultado.epub"

    # A verificação de integridade roda no fim do job (todas OK).
    assert snapshot["integrity"], "a verificação final não rodou"
    assert all(item["ok"] for item in snapshot["integrity"]), snapshot["integrity"]
    itens = {item["item"] for item in snapshot["integrity"]}
    assert "mimetype é a primeira entrada e está ZIP_STORED" in itens
    assert "Imagens e CSS byte a byte idênticos" in itens

    # (3b) preview do lado TRADUZIDO: vem do cache do job
    resposta = cliente.get(
        f"/api/jobs/{job_id}/preview",
        params={"chapter": CAPITULO_1, "side": "translated"},
    )
    assert resposta.status_code == 200, resposta.text
    html_traduzido = resposta.text
    indices = _indices_do_preview(html_traduzido)
    assert len(indices) == BLOCOS_CAPITULO_1
    assert PREFIXO_TRADUCAO in html_traduzido, (
        "o preview traduzido não usou a tradução do cache"
    )

    # (5) download: bytes de ZIP + Content-Disposition de anexo
    resposta = cliente.get(f"/api/jobs/{job_id}/download")
    assert resposta.status_code == 200, resposta.text
    assert resposta.content[:2] == b"PK"
    assert "attachment" in resposta.headers["content-disposition"]
    assert "traducao_resultado.epub" in resposta.headers["content-disposition"]

    # (6) o EPUB baixado é válido (mimetype ZIP_STORED, binários byte-idênticos,
    #     <p>/<span> preservados, dc:language, XHTML válido)
    destino = tmp_path / "baixado.epub"
    destino.write_bytes(resposta.content)

    verificacao = verificar_epub(str(EPUB_TESTE), str(destino), CAPITULO_1, "pt-BR")
    assert verificacao.ok, "\n".join(verificacao.linhas())

    # Nenhum bloco foi traduzido duas vezes: os 16 blocos foram traduzidos
    # em 4 lotes (13/5 + 3/5 → 3 + 1 requisições ao modelo).
    lotes = -(-BLOCOS_CAPITULO_1 // 5) + -(-BLOCOS_CAPITULO_2 // 5)
    assert servidor_falso.total_requisicoes == lotes


# ── Teste 2: pausa e retomada (com cache) ───────────────────────────────────

def test_pausa_e_retomada_reusam_o_cache(
    cliente: TestClient,
    epub_teste: Path,
    servidor_falso: ServidorFalso,
    tmp_path: Path,
) -> None:
    """Pausa no meio, retomada e download — sem traduzir duas vezes.

    A pausa é pedida com o 1º lote em voo (o servidor falso segura a
    requisição até o teste liberar), então o tradutor para **entre lotes**,
    como o ``pause_cb`` do pipeline prevê.

    Na retomada, os blocos já traduzidos voltam do cache: o snapshot final
    tem ``translated + cached == total`` e ``cached > 0`` — provando que a
    tradução NUNCA foi feita duas vezes.
    """
    base_url = servidor_falso.iniciar()

    # O 1º chat fica bloqueado no servidor: o job fica parado no 1º lote
    # enquanto o teste pede a pausa.
    servidor_falso.bloquear_ate = 1

    # ``batch_size=1`` dá um lote por bloco (a pausa é entre lotes).
    # ``limit_batches`` cobre TODOS os lotes do capítulo: se truncasse, os
    # blocos restantes nunca seriam traduzidos nem cacheados e a
    # contagem final (``translated + cached == total``) não fecharia.
    job_id = _criar_job(
        cliente,
        epub_teste,
        base_url,
        only=CAPITULO_1,
        batch_size=1,
        limit_batches=BLOCOS_CAPITULO_1,
    )

    # Espera o 1º lote entrar no servidor (e ficar esperando).
    servidor_falso.esperar_requisicoes(1, timeout=10)

    # (a) pede a pausa
    resposta = cliente.post(f"/api/jobs/{job_id}/pause")
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["paused"] is True

    # Libera o 1º lote: ele termina e o tradutor enxerga a pausa.
    servidor_falso.liberado.set()

    snapshot = _esperar_estado(cliente, job_id, {"paused", "done"}, TIMEOUT_PAUSA)
    assert snapshot["state"] == "paused", snapshot
    assert snapshot["translated"] == 1
    done_na_pausa = snapshot["done"]
    assert done_na_pausa == 1

    # (b) retoma — com o MESMO cache do job
    resposta = cliente.post(f"/api/jobs/{job_id}/resume")
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["state"] in ("running", "done")

    # (c) espera concluir
    snapshot = _esperar_estado(cliente, job_id, {"done", "error"}, TIMEOUT_PAUSA)
    assert snapshot["state"] == "done", snapshot
    assert snapshot["error"] is None

    # A TRADUÇÃO NÃO FOI FEITA DUAS VEZES: o que já estava pronto voltou
    # do cache e o resto foi traduzido agora.
    assert snapshot["total"] == BLOCOS_CAPITULO_1
    assert snapshot["translated"] + snapshot["cached"] == BLOCOS_CAPITULO_1
    assert snapshot["cached"] == done_na_pausa
    assert snapshot["cached"] > 0
    assert snapshot["failed"] == 0
    assert snapshot["discarded"] == 0
    assert snapshot["has_output"] is True

    # No total, cada bloco foi traduzido pelo modelo exatamente uma vez
    # (13 blocos = 13 requisições de chat, mesmo com a pausa no meio).
    assert servidor_falso.total_requisicoes == BLOCOS_CAPITULO_1

    # O EPUB retomado é válido.
    resposta = cliente.get(f"/api/jobs/{job_id}/download")
    assert resposta.status_code == 200, resposta.text
    assert resposta.content[:2] == b"PK"
    assert "attachment" in resposta.headers["content-disposition"]

    destino = tmp_path / "retomado.epub"
    destino.write_bytes(resposta.content)
    verificacao = verificar_epub(str(EPUB_TESTE), str(destino), CAPITULO_1, "pt-BR")
    assert verificacao.ok, "\n".join(verificacao.linhas())


# ── Teste 3: ``limit_batches`` limita os lotes ──────────────────────────────

def test_limit_batches_trunca_a_traducao(
    cliente: TestClient,
    epub_teste: Path,
    servidor_falso: ServidorFalso,
) -> None:
    """``limit_batches=1`` traduz só o primeiro lote do capítulo.

    Serve de contraponto ao teste de pausa/retomada: aqui o job termina
    (``done``) com apenas 1 bloco traduzido e 12 em branco, provando que
    o limite é respeitado.
    """
    base_url = servidor_falso.iniciar()

    job_id = _criar_job(
        cliente,
        epub_teste,
        base_url,
        only=CAPITULO_1,
        batch_size=1,
        limit_batches=1,
    )

    snapshot = _esperar_estado(cliente, job_id, {"done", "error"})

    assert snapshot["state"] == "done", snapshot
    assert snapshot["total"] == BLOCOS_CAPITULO_1
    assert snapshot["translated"] == 1
    assert snapshot["done"] == 1
    assert servidor_falso.total_requisicoes == 1


# ── Teste 4: o SSE é consumido pelo gerador direto ──────────────────────────

def test_eventos_sse_vem_do_gerador_direto(
    cliente: TestClient,
    epub_teste: Path,
    servidor_falso: ServidorFalso,
    manager: JobManager,
) -> None:
    """O fluxo SSE é consumido pelo GERADOR, nunca pelo ``TestClient``.

    O ``TestClient`` do FastAPI espera a resposta inteira e trava enquanto
    o stream SSE está aberto. Aqui o gerador ``server.gerar_eventos`` é
    consumido direto, e os eventos são lidos como JSON.
    """
    base_url = servidor_falso.iniciar()

    job_id = _criar_job(cliente, epub_teste, base_url)

    job = manager.obter(job_id)
    assert job is not None

    partes = asyncio.run(_coletar_eventos(server.gerar_eventos(job)))

    eventos: list[dict[str, Any]] = []
    for parte in partes:
        assert parte.startswith("data: ")
        assert parte.endswith("\n\n")
        eventos.append(json.loads(parte[len("data: "):-2]))

    tipos = [evento.get("type") for evento in eventos]
    assert "progress" in tipos
    assert "done" in tipos

    progresso = [e for e in eventos if e.get("type") == "progress"]
    assert len(progresso) == TOTAL_BLOCOS
    assert {e["status"] for e in progresso} == {"traduzido"}
    assert progresso[-1]["done"] == TOTAL_BLOCOS

    feito = next(e for e in eventos if e.get("type") == "done")
    assert feito["report"]["translated_blocks"] == TOTAL_BLOCOS
    assert feito["download_url"] == f"/api/jobs/{job_id}/download"
