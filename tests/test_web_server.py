"""Testes do back-end HTTP (web/server.py).

Cobre o contrato completo das rotas:

- ``POST /api/providers/models`` — lista de modelos ou ``error`` em pt-BR;
- ``POST /api/jobs`` — upload multipart (file + config) e criação do job;
- ``GET /api/jobs/{id}`` — snapshot (sem a api_key);
- ``GET /api/jobs/{id}/events`` — SSE (o conteúdo é consumido pelo GERADOR
  DIRETO: o ``TestClient`` do FastAPI espera a resposta inteira e trava
  enquanto o stream SSE está aberto);
- ``POST /api/jobs/{id}/pause|resume|cancel|discard-cache``;
- ``GET /api/jobs/{id}/download|chapters|preview|asset|style.css|log|integrity``;
- erros sempre em pt-BR e em JSON ``{"error": "..."}``;
- a api_key NUNCA aparece em nenhuma resposta.

Estratégia: os jobs criados **pela rota** também disparam a thread de
tradução (com o provedor MOCK). Para os testes que precisam de um job
parado e determinístico (download, preview traduzido, log), o job é criado
direto pelo :class:`JobManager` — sem thread — e a rota o consome.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from epub_translator.lmstudio_client import (
    NoModelError,
    ServerNotRunningError,
)
from web import server
from web.jobs import JobManager, fechar_logs


# ── Constantes ──────────────────────────────────────────────────────────────

RAIZ = Path(__file__).resolve().parent.parent
EPUB_TESTE = RAIZ / "fixtures" / "test_input.epub"

CAPITULO_1 = "OEBPS/Text/section-0001.html"
CAPITULO_2 = "OEBPS/Text/section-0002.html"

API_KEY = "sk-chave-secreta-1234"

RE_INDEX = re.compile(r'data-block-index="(\d+)"')


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def epub_teste() -> Path:
    """EPUB pequeno de teste (2 capítulos)."""
    if not EPUB_TESTE.exists():
        pytest.skip(f"EPUB de teste ausente: {EPUB_TESTE}")
    return EPUB_TESTE


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


def _config(**extra: Any) -> dict[str, Any]:
    """Config de um job (com api_key de mentira)."""
    base: dict[str, Any] = {
        "provider": "lmstudio",
        "base_url": "http://localhost:1234/v1",
        "api_key": API_KEY,
        "model": None,
        "target_lang": "pt-BR",
        "batch_size": 5,
        "temperature": 0.25,
        "glossary": {},
        "output_name": "traducao_resultado.epub",
        # O provedor é o MOCK: o teste não depende do LM Studio (e não
        # espera o timeout de 300 s do cliente real).
        "mock": True,
    }
    base.update(extra)
    return base


def _criar_job_pela_rota(
    cliente: TestClient, epub: Path, **extra: Any
) -> str:
    """Cria um job pela rota ``POST /api/jobs`` e devolve o ``job_id``."""
    with epub.open("rb") as arquivo:
        resposta = cliente.post(
            "/api/jobs",
            files={"file": (epub.name, arquivo, "application/epub+zip")},
            data={"config": json.dumps(_config(**extra))},
        )

    assert resposta.status_code == 200, resposta.text
    return str(resposta.json()["job_id"])


def _job_parado(manager: JobManager, epub: Path) -> Any:
    """Cria um job SEM iniciar a thread (estado determinístico)."""
    return manager.criar(str(epub), _config())


# ── Provedores ──────────────────────────────────────────────────────────────

class TestProvedores:
    """``POST /api/providers/models``."""

    def test_devolve_a_lista_de_modelos(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Com o provedor de pé, a lista de modelos vem preenchida."""

        class ClienteFalso:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            def list_models(self) -> list[str]:
                return ["modelo-a", "modelo-b"]

        monkeypatch.setattr(server, "LMStudioClient", ClienteFalso)

        resposta = cliente.post(
            "/api/providers/models",
            json={"base_url": "http://localhost:1234/v1", "api_key": API_KEY},
        )

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["models"] == ["modelo-a", "modelo-b"]
        assert "error" not in corpo

    def test_servidor_fora_devolve_error_em_pt_br(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Servidor fora do ar: HTTP 200 com ``error`` em pt-BR."""

        class ClienteFalso:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def list_models(self) -> list[str]:
                raise ServerNotRunningError("fora do ar")

        monkeypatch.setattr(server, "LMStudioClient", ClienteFalso)

        resposta = cliente.post(
            "/api/providers/models",
            json={"base_url": "http://localhost:1234/v1", "api_key": API_KEY},
        )

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["models"] == []
        assert "LM Studio" in corpo["error"]
        assert "http://localhost:1234/v1" in corpo["error"]
        assert API_KEY not in json.dumps(corpo, ensure_ascii=False)

    def test_sem_modelo_carregado(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Servidor sem modelo: a mensagem fala em carregar um modelo."""

        class ClienteFalso:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def list_models(self) -> list[str]:
                raise NoModelError("nenhum modelo")

        monkeypatch.setattr(server, "LMStudioClient", ClienteFalso)

        resposta = cliente.post(
            "/api/providers/models",
            json={"base_url": "http://localhost:1234/v1", "api_key": API_KEY},
        )

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["models"] == []
        assert "modelo" in corpo["error"].lower()

    def test_erro_de_rede_generico(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Erro genérico de rede vira mensagem em pt-BR (sem traceback)."""

        class ClienteFalso:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def list_models(self) -> list[str]:
                raise OSError("conexão recusada")

        monkeypatch.setattr(server, "LMStudioClient", ClienteFalso)

        resposta = cliente.post(
            "/api/providers/models",
            json={"base_url": "http://exemplo.test/v1", "api_key": API_KEY},
        )

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["models"] == []
        assert "Não foi possível conectar" in corpo["error"]
        assert "http://exemplo.test/v1" in corpo["error"]
        assert "Traceback" not in corpo["error"]

    def test_a_api_key_nunca_volta(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mesmo num erro, a api_key não aparece na resposta."""

        class ClienteFalso:
            def __init__(self, **kwargs: Any) -> None:
                self.api_key = kwargs.get("api_key")

            def list_models(self) -> list[str]:
                raise OSError(f"falhou com {self.api_key}")

        monkeypatch.setattr(server, "LMStudioClient", ClienteFalso)

        resposta = cliente.post(
            "/api/providers/models",
            json={"base_url": "http://exemplo.test/v1", "api_key": API_KEY},
        )

        assert resposta.status_code == 200
        assert API_KEY not in resposta.text


# ── Criação de job ──────────────────────────────────────────────────────────

class TestCriacaoDeJob:
    """``POST /api/jobs``."""

    def test_aceita_multipart_e_devolve_o_job_id(
        self, cliente: TestClient, epub_teste: Path
    ) -> None:
        """O upload multipart (file + config) cria o job."""
        with epub_teste.open("rb") as arquivo:
            resposta = cliente.post(
                "/api/jobs",
                files={"file": (epub_teste.name, arquivo, "application/epub+zip")},
                data={"config": json.dumps(_config())},
            )

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["job_id"]
        assert corpo["state"] in ("running", "paused", "done", "error", "cancelled")

    def test_o_epub_e_copiado_para_a_pasta_do_job(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O EPUB enviado é copiado para a pasta do job."""
        job_id = _criar_job_pela_rota(cliente, epub_teste)

        job = manager.obter(job_id)
        assert job is not None
        assert Path(job.src).is_file()
        assert Path(job.src).read_bytes() == epub_teste.read_bytes()

    def test_recusa_arquivo_nao_epub(
        self, cliente: TestClient
    ) -> None:
        """Um arquivo que não é .epub é recusado com 400."""
        resposta = cliente.post(
            "/api/jobs",
            files={"file": ("livro.txt", b"nao sou um epub", "text/plain")},
            data={"config": json.dumps(_config())},
        )

        assert resposta.status_code == 400
        assert resposta.json()["error"] == "O arquivo enviado não é um .epub."

    def test_recusa_config_invalida(
        self, cliente: TestClient, epub_teste: Path
    ) -> None:
        """Um config que não é JSON válido é recusado com 400."""
        with epub_teste.open("rb") as arquivo:
            resposta = cliente.post(
                "/api/jobs",
                files={"file": (epub_teste.name, arquivo, "application/epub+zip")},
                data={"config": "{isto nao e json"},
            )

        assert resposta.status_code == 400
        assert resposta.json()["error"].startswith("Configuração inválida")

    def test_config_e_normalizado(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """A config normalizada tem os defaults do contrato."""
        job_id = _criar_job_pela_rota(cliente, epub_teste, batch_size=999)

        job = manager.obter(job_id)
        assert job is not None
        assert job.config["provider"] == "lmstudio"
        assert job.config["base_url"] == "http://localhost:1234/v1"
        assert job.config["target_lang"] == "pt-BR"
        assert job.config["temperature"] == 0.25
        assert job.config["glossary"] == {}
        assert job.config["output_name"] == "traducao_resultado.epub"
        # O batch_size é limitado a 1..20.
        assert job.config["batch_size"] == 20

    def test_batch_size_abaixo_de_um_vira_um(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """``batch_size=0`` é corrigido para 1."""
        job_id = _criar_job_pela_rota(cliente, epub_teste, batch_size=0)

        job = manager.obter(job_id)
        assert job is not None
        assert job.config["batch_size"] == 1

    def test_only_e_limit_batches_sao_aceitos(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """As opções opcionais ``only`` e ``limit_batches`` passam pelo job."""
        job_id = _criar_job_pela_rota(
            cliente,
            epub_teste,
            only=CAPITULO_1,
            limit_batches=2,
        )

        job = manager.obter(job_id)
        assert job is not None
        assert job.config["only"] == CAPITULO_1
        assert job.config["limit_batches"] == 2

    def test_epub_invalido_devolve_erro_em_pt_br(
        self, cliente: TestClient
    ) -> None:
        """Um .epub corrompido é recusado com 400 e mensagem em pt-BR."""
        resposta = cliente.post(
            "/api/jobs",
            files={"file": ("ruim.epub", b"nao sou um zip", "application/epub+zip")},
            data={"config": json.dumps(_config())},
        )

        assert resposta.status_code == 400
        mensagem = resposta.json()["error"]
        assert "EPUB" in mensagem
        assert "Traceback" not in mensagem


# ── Snapshot ────────────────────────────────────────────────────────────────

class TestSnapshot:
    """``GET /api/jobs/{id}``."""

    def test_snapshot_tem_os_campos_do_contrato(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O snapshot tem todos os campos do contrato da API."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(f"/api/jobs/{job.id}")

        assert resposta.status_code == 200
        corpo = resposta.json()
        for campo in (
            "job_id", "state", "total", "done", "translated", "cached",
            "failed", "discarded", "elapsed_s", "eta_s", "current_file",
            "current_block", "error", "report", "model", "output_name",
            "has_output", "integrity",
        ):
            assert campo in corpo, f"faltou o campo {campo!r}"

    def test_job_desconhecido_devolve_404(
        self, cliente: TestClient
    ) -> None:
        """Job inexistente: 404 com mensagem em pt-BR."""
        resposta = cliente.get("/api/jobs/nao-existe")

        assert resposta.status_code == 404
        assert resposta.json()["error"] == "Job não encontrado."

    def test_api_key_nao_aparece_no_snapshot(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """A api_key nunca aparece na resposta."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(f"/api/jobs/{job.id}")

        assert API_KEY not in resposta.text


# ── SSE ─────────────────────────────────────────────────────────────────────

class TestEventosSSE:
    """``GET /api/jobs/{id}/events`` (consumido pelo gerador direto)."""

    def test_o_gerador_entrega_eventos_no_formato_sse(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Cada evento sai como ``data: {json}\\n\\n``."""
        job = _job_parado(manager, epub_teste)
        job.bus.push({"type": "progress", "done": 1, "total": 16})
        job.bus.push({"type": "state", "state": "running"})

        async def consumir() -> list[str]:
            gerador = server.gerar_eventos(job)
            partes: list[str] = []
            async for parte in gerador:
                partes.append(parte)
                if len(partes) == 2:
                    break
            await gerador.aclose()
            return partes

        partes = asyncio.run(consumir())

        assert partes[0].startswith("data: ")
        assert partes[0].endswith("\n\n")
        assert json.loads(partes[0][len("data: "):])["type"] == "progress"
        assert json.loads(partes[1][len("data: "):])["type"] == "state"

    def test_o_gerador_encerra_no_estado_final(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Com o job encerrado, o gerador termina (não fica pendurado)."""
        job = _job_parado(manager, epub_teste)
        job.bus.push({"type": "done", "report": {}})

        async def consumir() -> list[str]:
            gerador = server.gerar_eventos(job)
            partes: list[str] = []
            async for parte in gerador:
                partes.append(parte)
            return partes

        partes = asyncio.run(consumir())

        assert partes
        assert json.loads(partes[0][len("data: "):])["type"] == "done"

    def test_evento_de_erro_encerra_o_fluxo(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O evento ``error`` é terminal."""
        job = _job_parado(manager, epub_teste)
        job.bus.push({"type": "error", "message": "falhou"})

        async def consumir() -> list[str]:
            gerador = server.gerar_eventos(job)
            partes: list[str] = []
            async for parte in gerador:
                partes.append(parte)
            return partes

        partes = asyncio.run(consumir())

        assert len(partes) == 1
        assert json.loads(partes[0][len("data: "):])["type"] == "error"

    def test_o_gerador_nao_trava_o_event_loop(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O gerador cede o controle: outras corrotinas rodam em paralelo."""
        job = _job_parado(manager, epub_teste)
        job.bus.push({"type": "progress", "done": 1, "total": 16})

        async def consumir() -> list[str]:
            gerador = server.gerar_eventos(job)
            partes: list[str] = []
            async for parte in gerador:
                partes.append(parte)
                break
            await gerador.aclose()
            return partes

        async def principal() -> list[str]:
            # Uma tarefa "de fora" precisa rodar enquanto o stream está
            # aberto: com um `queue.get()` bloqueante no async, ela nunca
            # rodaria.
            forca: list[str] = []

            async def outra() -> None:
                await asyncio.sleep(0.05)
                forca.append("rodou")

            await asyncio.gather(consumir(), outra())
            return forca

        assert asyncio.run(principal()) == ["rodou"]

    def test_a_rota_devolve_os_cabecalhos_de_sse(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """A rota de eventos declara ``text/event-stream`` e o anti-cache.

        O job é criado **encerrado** (``done``): o ``TestClient`` do FastAPI
        espera a resposta INTEIRA antes de devolvê-la, e um stream SSE só
        fecha no estado final — com um job em andamento o teste travaria.
        O conteúdo do stream é testado consumindo o gerador direto.
        """
        job = _job_parado(manager, epub_teste)
        job.state = "done"

        with cliente.stream(
            "GET",
            f"/api/jobs/{job.id}/events",
            headers={"accept": "text/event-stream"},
        ) as resposta:
            assert resposta.status_code == 200
            assert resposta.headers["content-type"].startswith("text/event-stream")
            assert resposta.headers["cache-control"] == "no-cache"
            assert resposta.headers["x-accel-buffering"] == "no"

    def test_eventos_de_um_job_que_roda(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Um job real (mock) emite progresso, state e done no barramento."""
        job = _job_parado(manager, epub_teste)
        manager.iniciar(job)
        assert manager.aguardar(job, 30.0)

        tipos = [evento["type"] for evento in job.bus.drain()]

        assert "progress" in tipos
        assert "done" in tipos
        assert "state" in tipos


# ── Controle do job ─────────────────────────────────────────────────────────

class TestControle:
    """Pausar, retomar, cancelar e descartar o cache."""

    def test_pausar(self, cliente: TestClient, manager: JobManager, epub_teste: Path) -> None:
        """A pausa responde ``{"paused": true}``."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.post(f"/api/jobs/{job.id}/pause")

        assert resposta.status_code == 200
        assert resposta.json()["paused"] is True

    def test_retomar(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """A retomada responde ``{"paused": false, "state": ...}``."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.post(f"/api/jobs/{job.id}/resume")

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["paused"] is False
        assert "state" in corpo
        manager.cancelar(job)
        manager.aguardar(job, 30.0)

    def test_retomar_com_chave_mascarada_exige_chave(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Job recarregado do disco só tem a chave mascarada: 400 sem chave."""
        job = _job_parado(manager, epub_teste)
        job.config["api_key"] = "abc…wxyz"

        assert cliente.post(f"/api/jobs/{job.id}/resume").status_code == 400

        resposta = cliente.post(
            f"/api/jobs/{job.id}/resume", json={"api_key": "chave-real-123"}
        )
        assert resposta.status_code == 200
        assert job.config["api_key"] == "chave-real-123"
        manager.cancelar(job)
        manager.aguardar(job, 30.0)

    def test_cancelar(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O cancelamento responde ``{"cancelled": true}``."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.post(f"/api/jobs/{job.id}/cancel")

        assert resposta.status_code == 200
        assert resposta.json()["cancelled"] is True

    def test_descartar_cache_apaga_o_arquivo(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Descartar o cache apaga o arquivo do job."""
        job = _job_parado(manager, epub_teste)

        Path(job.cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(job.cache_path).write_text("{}", encoding="utf-8")

        resposta = cliente.post(f"/api/jobs/{job.id}/discard-cache")

        assert resposta.status_code == 200
        assert resposta.json()["discarded"] is True
        assert not Path(job.cache_path).exists()

    def test_descartar_cache_sem_arquivo(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Descartar o cache de um job sem cache não dá erro."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.post(f"/api/jobs/{job.id}/discard-cache")

        assert resposta.status_code == 200
        assert resposta.json()["discarded"] is True

    def test_controle_de_job_inexistente(
        self, cliente: TestClient
    ) -> None:
        """As rotas de controle também devolvem 404 para job inexistente."""
        for rota in ("pause", "resume", "cancel", "discard-cache"):
            resposta = cliente.post(f"/api/jobs/nao-existe/{rota}")
            assert resposta.status_code == 404, rota
            assert resposta.json()["error"] == "Job não encontrado."


# ── Capítulos e preview ─────────────────────────────────────────────────────

class TestCapitulos:
    """``GET /api/jobs/{id}/chapters``."""

    def test_lista_na_ordem_do_spine(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """A lista traz os capítulos na ordem do spine, com título e blocos."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(f"/api/jobs/{job.id}/chapters")

        assert resposta.status_code == 200
        capitulos = resposta.json()
        assert [c["path"] for c in capitulos] == [CAPITULO_1, CAPITULO_2]
        for capitulo in capitulos:
            assert set(capitulo) == {"index", "path", "title", "blocks"}
            assert capitulo["title"]
            assert capitulo["blocks"] > 0

    def test_titulos_vem_do_ncx(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O título do capítulo vem do NCX ('Chapter 1')."""
        job = _job_parado(manager, epub_teste)

        capitulos = cliente.get(f"/api/jobs/{job.id}/chapters").json()

        assert capitulos[0]["title"] == "Chapter 1"
        assert capitulos[1]["title"] == "Chapter 2"


class TestPreview:
    """``GET /api/jobs/{id}/preview``."""

    def test_preview_original_tem_indice_por_bloco(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O HTML do capítulo tem ``data-block-index`` 1-based."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(
            f"/api/jobs/{job.id}/preview",
            params={"chapter": CAPITULO_1, "side": "original"},
        )

        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("text/html")
        indices = [int(m) for m in RE_INDEX.findall(resposta.text)]
        assert indices == list(range(1, len(indices) + 1))
        assert indices

    def test_preview_e_sanitizado(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O preview não traz ``<script>`` nem handlers ``on*``."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(
            f"/api/jobs/{job.id}/preview",
            params={"chapter": CAPITULO_1, "side": "original"},
        )

        assert "<script" not in resposta.text
        assert "onclick" not in resposta.text

    def test_preview_traduzido_usa_o_cache_do_job(
        self,
        cliente: TestClient,
        manager: JobManager,
        epub_teste: Path,
    ) -> None:
        """O lado traduzido mostra o texto que está no cache do job."""
        from epub_translator.blocks import extract_blocks, format_block
        from epub_translator.cache import BlockCache, key_for
        from epub_translator.epub_io import EpubReader, parse_xhtml

        job = _job_parado(manager, epub_teste)
        job.model = "modelo-de-teste"

        data = EpubReader(job.src).read()
        arvore, _doctype = parse_xhtml(data.files[CAPITULO_1])
        blocos = extract_blocks(arvore)

        cache = BlockCache(job.cache_path)
        cache.put(
            key_for(
                text=blocos[0].text,
                marker_text=format_block(1, blocos[0].text),
                target="pt-BR",
                model=job.model,
                source=job.src,
                glossary_hash="",
                context="",
                file_path=CAPITULO_1,
            ),
            "Texto traduzido de teste",
        )

        resposta = cliente.get(
            f"/api/jobs/{job.id}/preview",
            params={"chapter": CAPITULO_1, "side": "translated"},
        )

        assert resposta.status_code == 200
        assert "Texto traduzido de teste" in resposta.text

    def test_preview_traduzido_sem_cache_mantem_o_original(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Sem tradução no cache, o lado traduzido é igual ao original."""
        job = _job_parado(manager, epub_teste)

        original = cliente.get(
            f"/api/jobs/{job.id}/preview",
            params={"chapter": CAPITULO_1, "side": "original"},
        )
        traduzido = cliente.get(
            f"/api/jobs/{job.id}/preview",
            params={"chapter": CAPITULO_1, "side": "translated"},
        )

        assert traduzido.status_code == 200
        assert traduzido.text == original.text

    def test_capitulo_inexistente_devolve_404(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Capítulo que não existe: 404 em pt-BR."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(
            f"/api/jobs/{job.id}/preview",
            params={"chapter": "OEBPS/Text/nao-existe.html", "side": "original"},
        )

        assert resposta.status_code == 404
        assert resposta.json()["error"] == "Capítulo não encontrado."

    def test_lado_invalido_devolve_400(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Um lado desconhecido é recusado com 400."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(
            f"/api/jobs/{job.id}/preview",
            params={"chapter": CAPITULO_1, "side": "outro"},
        )

        assert resposta.status_code == 400
        assert "error" in resposta.json()


# ── Recursos do EPUB ────────────────────────────────────────────────────────

class TestRecursos:
    """``GET /api/jobs/{id}/asset`` e ``/style.css``."""

    def test_asset_devolve_a_imagem(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """A imagem do EPUB é servida com o media type certo."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(
            f"/api/jobs/{job.id}/asset",
            params={"path": "OEBPS/Images/cover.jpg"},
        )

        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("image/jpeg")
        assert resposta.content.startswith(b"\xff\xd8")  # JPEG

    def test_asset_inexistente_devolve_404(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Um recurso que não existe no zip: 404."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(
            f"/api/jobs/{job.id}/asset",
            params={"path": "OEBPS/Images/nao-existe.jpg"},
        )

        assert resposta.status_code == 404
        assert "error" in resposta.json()

    def test_style_css_devolve_o_css_do_epub(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O CSS do próprio EPUB é servido."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(f"/api/jobs/{job.id}/style.css")

        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("text/css")
        assert len(resposta.text) > 0


# ── Log e integridade ───────────────────────────────────────────────────────

class TestLogEIntegridade:
    """``GET /api/jobs/{id}/log`` e ``/integrity``."""

    def test_log_devolve_o_texto(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O log do job é devolvido como texto."""
        job = _job_parado(manager, epub_teste)

        Path(job.log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(job.log_path).write_text(
            "Traduzindo arquivo: OEBPS/Text/section-0001.html\n", encoding="utf-8"
        )

        resposta = cliente.get(f"/api/jobs/{job.id}/log")

        assert resposta.status_code == 200
        assert "Traduzindo arquivo" in resposta.json()["text"]

    def test_log_inexistente_devolve_texto_vazio(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Sem arquivo de log, o texto é vazio (sem erro)."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(f"/api/jobs/{job.id}/log")

        assert resposta.status_code == 200
        assert resposta.json()["text"] == ""

    def test_integridade_devolve_a_lista(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """A rota de integridade devolve os itens da verificação."""
        job = _job_parado(manager, epub_teste)
        job.integrity = [
            {"item": "mimetype é a primeira entrada", "ok": True, "detail": ""}
        ]

        resposta = cliente.get(f"/api/jobs/{job.id}/integrity")

        assert resposta.status_code == 200
        assert resposta.json() == job.integrity

    def test_integridade_vazia_quando_nao_hou_verificacao(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Sem verificação, a lista é vazia."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(f"/api/jobs/{job.id}/integrity")

        assert resposta.status_code == 200
        assert resposta.json() == []


# ── Download ────────────────────────────────────────────────────────────────

class TestDownload:
    """``GET /api/jobs/{id}/download``."""

    def test_devolve_o_epub_com_content_disposition(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O EPUB sai com o media type e o Content-Disposition corretos."""
        job = _job_parado(manager, epub_teste)
        Path(job.out).write_bytes(b"conteudo-do-epub")

        resposta = cliente.get(f"/api/jobs/{job.id}/download")

        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("application/epub+zip")
        assert "attachment" in resposta.headers["content-disposition"]
        assert "traducao_resultado.epub" in resposta.headers["content-disposition"]
        assert resposta.content == b"conteudo-do-epub"

    def test_o_nome_do_arquivo_vem_do_output_name(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """O ``output_name`` da config é o nome sugerido no download."""
        job = manager.criar(str(epub_teste), _config(output_name="meu-livro.epub"))
        Path(job.out).write_bytes(b"epub")

        resposta = cliente.get(f"/api/jobs/{job.id}/download")

        assert "meu-livro.epub" in resposta.headers["content-disposition"]

    def test_sem_epub_pronto_devolve_404(
        self, cliente: TestClient, manager: JobManager, epub_teste: Path
    ) -> None:
        """Sem o arquivo de saída: 404 em pt-BR."""
        job = _job_parado(manager, epub_teste)

        resposta = cliente.get(f"/api/jobs/{job.id}/download")

        assert resposta.status_code == 404
        assert (
            resposta.json()["error"] == "O EPUB traduzido ainda não está pronto."
        )


# ── Estático e SPA ──────────────────────────────────────────────────────────

class TestEstatico:
    """Fallback de SPA e CORS."""

    def test_rota_desconhecida_devolve_o_index(
        self, cliente: TestClient
    ) -> None:
        """Uma rota fora de ``/api`` devolve o index.html do build."""
        if not (server.PASTA_DIST / "index.html").is_file():
            pytest.skip("o front-end não foi construído")

        resposta = cliente.get("/qualquer/coisa")

        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("text/html")
        assert "<div id=\"root\">" in resposta.text

    def test_rota_de_api_desconhecida_devolve_404_json(
        self, cliente: TestClient
    ) -> None:
        """Uma rota de API inexistente devolve 404 em JSON."""
        resposta = cliente.get("/api/nao-existe")

        assert resposta.status_code == 404
        assert "error" in resposta.json()

    def test_cors_permite_o_front_end_em_dev(
        self, cliente: TestClient
    ) -> None:
        """O CORS libera a origem do Vite em desenvolvimento."""
        resposta = cliente.get(
            "/api/health", headers={"Origin": "http://localhost:5173"}
        )

        assert resposta.status_code == 200
        assert (
            resposta.headers.get("access-control-allow-origin")
            == "http://localhost:5173"
        )


# ── Erros ───────────────────────────────────────────────────────────────────

class TestErros:
    """Erros sempre em JSON e em pt-BR."""

    def test_erro_generico_vira_json_em_pt_br(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uma exceção não tratada vira 500 com ``{"error": ...}`` (sem traceback)."""
        from web.jobs import Job

        def explodir(job: Job) -> dict[str, Any]:
            raise RuntimeError("falha interna")

        monkeypatch.setattr(server, "_obter_job", explodir)

        resposta = cliente.get("/api/jobs/qualquer")

        assert resposta.status_code == 500
        corpo = resposta.json()
        assert "error" in corpo
        assert "Traceback" not in json.dumps(corpo, ensure_ascii=False)
        assert "RuntimeError" not in json.dumps(corpo, ensure_ascii=False)

    def test_erro_de_validacao_vira_json_em_pt_br(
        self, cliente: TestClient
    ) -> None:
        """Uma requisição sem os campos obrigatórios devolve JSON."""
        resposta = cliente.post("/api/jobs")

        assert resposta.status_code == 422
        corpo = resposta.json()
        assert "error" in corpo
        assert "Traceback" not in json.dumps(corpo, ensure_ascii=False)
