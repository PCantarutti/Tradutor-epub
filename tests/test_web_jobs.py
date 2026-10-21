"""Testes da camada de jobs (web/jobs.py).

Cobre:

- ``EventBus``: ``push``/``drain`` são thread-safe (nenhum evento se perde
  com ``push`` e ``drain`` concorrentes);
- ``JobManager.criar``: pasta do job, cópia do EPUB original e ``job.json``;
- job iniciado e concluído: estado "done" + eventos "progress" e "done";
- ETA: ``None`` no começo e número depois de 3+ blocos;
- pausa: estado "paused" com ``Report`` parcial salvo;
- retomada: reusa o cache (o contador ``cached`` sobe);
- erro do provedor: estado "error" com mensagem em pt-BR;
- a api_key NUNCA aparece no ``job.json``, nos eventos nem no snapshot;
- os handlers de log de outros jobs são removidos do logger
  ``epub_translator`` antes de iniciar um novo job.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from epub_translator.lmstudio_client import MockClient
from web import jobs
from web.jobs import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_ERROR,
    STATE_PAUSED,
    STATE_RUNNING,
    EventBus,
    Job,
    JobManager,
    fechar_logs,
)


# ── Constantes ──────────────────────────────────────────────────────────────

RAIZ = Path(__file__).resolve().parent.parent
EPUB_TESTE = RAIZ / "fixtures" / "test_input.epub"

# O EPUB de teste tem 2 capítulos: 13 + 3 blocos = 16 blocos no total.
TOTAL_BLOCOS = 16
BLOCOS_CAPITULO_1 = 13

TIMEOUT = 30.0


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def epub_teste() -> Path:
    """EPUB pequeno de teste (2 capítulos, 13 + 3 blocos)."""
    if not EPUB_TESTE.exists():
        pytest.skip(f"EPUB de teste ausente: {EPUB_TESTE}")
    return EPUB_TESTE


@pytest.fixture()
def manager(tmp_path: Path) -> JobManager:
    """Gerenciador de jobs com a pasta em ``tmp_path``."""
    return JobManager(pasta=tmp_path / "jobs")


@pytest.fixture(autouse=True)
def _fechar_handlers() -> Any:
    """Fecha os handlers de log do tradutor ao fim de cada teste."""
    yield
    fechar_logs()


def _config(**extra: Any) -> dict[str, Any]:
    """Config mínima de um job (com api_key de mentira)."""
    base: dict[str, Any] = {
        "provider": "lmstudio",
        "base_url": "http://localhost:1234/v1",
        "api_key": "sk-chave-secreta-1234",
        "model": None,
        "target_lang": "pt-BR",
        "batch_size": 5,
        "temperature": 0.25,
        "glossary": {},
    }
    base.update(extra)
    return base


def _aguardar(manager: JobManager, job: Job, timeout: float = TIMEOUT) -> None:
    """Espera a thread do job terminar (falha o teste se não terminar)."""
    assert manager.aguardar(job, timeout), "a thread do job não terminou"


def _eventos(job: Job) -> list[dict[str, Any]]:
    """Todos os eventos emitidos pelo job (drena o barramento)."""
    return job.bus.drain()


class ClienteLento:
    """MockClient que demora um pouco em cada requisição.

    Sem a pausa, o job termina antes de o teste conseguir pedir a pausa.
    O atraso dá ao teste a janela para agir entre lotes.
    """

    def __init__(self, atraso: float = 0.05) -> None:
        self._interno = MockClient()
        self._atraso = atraso

    def list_models(self) -> list[str]:
        return self._interno.list_models()

    def resolve_model(self) -> str:
        return self._interno.resolve_model()

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.25,
        max_tokens: int | None = None,
    ) -> str:
        time.sleep(self._atraso)
        return self._interno.chat(
            messages, temperature=temperature, max_tokens=max_tokens
        )

    @property
    def is_mock(self) -> bool:
        return True


class ClienteContador:
    """Provedor LENTO que conta as chamadas **simultâneas**.

    É o detector do bug da retomada: se duas threads de tradução
    traduzirem ao mesmo tempo, ``max_ativos`` passa de 1.

    Attributes:
        max_ativos: Maior número de chamadas de ``chat`` simultâneas.
        chamadas: Quantas chamadas de ``chat`` foram atendidas.
    """

    def __init__(self, atraso: float = 3.0) -> None:
        self._interno = MockClient()
        self._atraso = atraso
        self._lock = threading.Lock()
        self._ativos = 0
        self.max_ativos = 0
        self.chamadas = 0

    def list_models(self) -> list[str]:
        return self._interno.list_models()

    def resolve_model(self) -> str:
        return self._interno.resolve_model()

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.25,
        max_tokens: int | None = None,
    ) -> str:
        with self._lock:
            self._ativos += 1
            self.chamadas += 1
            self.max_ativos = max(self.max_ativos, self._ativos)
        try:
            time.sleep(self._atraso)
            return self._interno.chat(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        finally:
            with self._lock:
                self._ativos -= 1

    @property
    def is_mock(self) -> bool:
        return True


def _pausar_apos(manager: JobManager, job: Job, blocos: int) -> None:
    """Espera o job chegar a ``blocos`` blocos e então pede a pausa."""
    limite = time.monotonic() + TIMEOUT
    while job.done < blocos and time.monotonic() < limite:
        time.sleep(0.005)
    manager.pausar(job)
    _aguardar(manager, job)


def _esperar_estado(job: Job, estado: str, timeout: float = TIMEOUT) -> None:
    """Espera o job chegar a um estado (falha o teste se não chegar)."""
    limite = time.monotonic() + timeout
    while job.state != estado and time.monotonic() < limite:
        time.sleep(0.005)
    assert job.state == estado, (
        f"o job não chegou ao estado {estado!r} em {timeout}s "
        f"(estado atual: {job.state!r})"
    )


def _esperar_chamadas(
    cliente: ClienteContador, quantidade: int, timeout: float = TIMEOUT
) -> None:
    """Espera o provedor receber ``quantidade`` chamadas de chat."""
    limite = time.monotonic() + timeout
    while cliente.chamadas < quantidade and time.monotonic() < limite:
        time.sleep(0.005)


# ── EventBus ────────────────────────────────────────────────────────────────

class TestEventBus:
    """Barramento de eventos thread-safe."""

    def test_push_e_drain(self) -> None:
        """Os eventos empilhados saem no drain, na ordem."""
        bus = EventBus()

        bus.push({"type": "progress", "n": 1})
        bus.push({"type": "progress", "n": 2})

        eventos = bus.drain()

        assert [e["n"] for e in eventos] == [1, 2]
        assert len(bus) == 0

    def test_drain_limpa_a_fila(self) -> None:
        """O drain devolve e limpa: a segunda chamada não devolve nada."""
        bus = EventBus()
        bus.push({"type": "progress"})

        assert len(bus.drain()) == 1
        assert bus.drain() == []

    def test_push_e_drain_concorrentes_nao_perdem_evento(self) -> None:
        """Push e drain simultâneos não perdem nenhum evento."""
        bus = EventBus()
        total = 500
        recebidos: list[dict[str, Any]] = []
        parar = threading.Event()

        def produtor() -> None:
            for i in range(total):
                bus.push({"type": "progress", "n": i})
            parar.set()

        def consumidor() -> None:
            while not parar.is_set() or len(bus) > 0:
                recebidos.extend(bus.drain())
                time.sleep(0.001)

        t1 = threading.Thread(target=produtor)
        t2 = threading.Thread(target=consumidor)
        t1.start()
        t2.start()
        t1.join(TIMEOUT)
        t2.join(TIMEOUT)

        recebidos.extend(bus.drain())

        assert len(recebidos) == total
        assert sorted(e["n"] for e in recebidos) == list(range(total))

    def test_nao_usa_asyncio_queue(self) -> None:
        """O barramento é síncrono (a thread do tradutor é sync)."""
        import ast

        import web.jobs as jobs

        arvore = ast.parse(Path(jobs.__file__).read_text(encoding="utf-8"))
        importados: set[str] = set()
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                importados.update(alias.name.split(".")[0] for alias in no.names)
            elif isinstance(no, ast.ImportFrom) and no.module:
                importados.add(no.module.split(".")[0])

        assert "asyncio" not in importados
        assert "queue" not in importados


# ── Criação do job ──────────────────────────────────────────────────────────

class TestCriacao:
    """``JobManager.criar``."""

    def test_cria_a_pasta_e_copia_o_epub(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """A pasta do job existe e tem o EPUB original copiado."""
        job = manager.criar(str(epub_teste), _config())

        assert Path(job.dir).is_dir()
        assert Path(job.dir).parent == manager.pasta
        assert Path(job.src).is_file()
        assert Path(job.src).read_bytes() == epub_teste.read_bytes()
        assert Path(job.src).name == "original.epub"

    def test_campos_do_job(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O job tem todos os campos do contrato, com os valores certos."""
        job = manager.criar(str(epub_teste), _config())

        assert job.id
        assert job.state == STATE_RUNNING
        assert job.out.endswith(".epub")
        assert job.cache_path.endswith(".json")
        assert job.log_path == job.out + ".log"
        assert job.data is not None
        assert job.data.text_files  # o EPUB foi lido em memória
        assert job.bus is not None
        assert not job.paused_event.is_set()
        assert not job.cancel_event.is_set()

    def test_grava_o_job_json(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O ``job.json`` é gravado com o estado e os contadores."""
        job = manager.criar(str(epub_teste), _config())

        arquivo = Path(job.dir) / "job.json"
        assert arquivo.is_file()

        dados = json.loads(arquivo.read_text(encoding="utf-8"))

        assert dados["id"] == job.id
        assert dados["state"] == STATE_RUNNING
        assert dados["done"] == 0
        assert dados["total"] == 0
        assert dados["config"]["target_lang"] == "pt-BR"

    def test_obter_e_listar(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O job é encontrado pelo id e aparece na listagem."""
        job = manager.criar(str(epub_teste), _config())

        assert manager.obter(job.id) is job
        assert job.id in [j.id for j in manager.listar()]
        assert manager.obter("inexistente") is None

    def test_epub_inexistente(
        self, manager: JobManager, tmp_path: Path
    ) -> None:
        """Um EPUB que não existe é erro em pt-BR."""
        with pytest.raises(FileNotFoundError) as exc:
            manager.criar(str(tmp_path / "nao-existe.epub"), _config())

        assert "não encontrado" in str(exc.value)


# ── Job concluído ───────────────────────────────────────────────────────────

class TestJobConcluido:
    """Job iniciado e concluído."""

    def test_estado_done_e_eventos(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O job chega a "done" e emite "progress" e "done"."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        assert job.state == STATE_DONE

        eventos = _eventos(job)
        tipos = [e["type"] for e in eventos]

        assert "progress" in tipos
        assert "done" in tipos
        assert tipos[-1] == "state"
        assert eventos[-1]["state"] == STATE_DONE

    def test_um_evento_progresso_por_bloco(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """ sai um evento de progresso por bloco, com os contadores certos."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        progresso = [e for e in _eventos(job) if e["type"] == "progress"]

        assert len(progresso) == TOTAL_BLOCOS
        assert [e["done"] for e in progresso] == list(range(1, TOTAL_BLOCOS + 1))
        assert {e["total"] for e in progresso} == {TOTAL_BLOCOS}
        assert job.translated == TOTAL_BLOCOS
        assert job.done == TOTAL_BLOCOS
        assert job.total == TOTAL_BLOCOS

    def test_evento_done_tem_report_e_download_url(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O evento "done" traz o report e a URL de download."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        done = next(e for e in _eventos(job) if e["type"] == "done")

        assert done["report"]["total_blocks"] == TOTAL_BLOCOS
        assert done["report"]["translated_blocks"] == TOTAL_BLOCOS
        assert isinstance(done["report"]["files_processed"], list)
        assert done["download_url"] == f"/api/jobs/{job.id}/download"

    def test_gera_o_epub_de_saida(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O EPUB traduzido é gravado em disco."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        assert Path(job.out).is_file()
        assert Path(job.out).stat().st_size > 0

    def test_integridade_rodada(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """A verificação de integridade roda e devolve os itens."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        assert job.integrity
        assert all({"item", "ok", "detail"} <= set(i) for i in job.integrity)
        assert all(isinstance(i["ok"], bool) for i in job.integrity)

    def test_job_json_relido_com_o_estado_correto(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O ``job.json`` é relido com o estado "done"."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        dados = json.loads(
            (Path(job.dir) / "job.json").read_text(encoding="utf-8")
        )

        assert dados["state"] == STATE_DONE
        assert dados["done"] == TOTAL_BLOCOS
        assert dados["translated"] == TOTAL_BLOCOS
        assert dados["report"]["total_blocks"] == TOTAL_BLOCOS


# ── ETA ─────────────────────────────────────────────────────────────────────

class TestEta:
    """Cálculo do ETA."""

    def test_eta_none_no_comeco(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Com menos de 3 amostras o ETA é None."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        progresso = [e for e in _eventos(job) if e["type"] == "progress"]

        # Os 2 primeiros blocos ainda não têm amostra suficiente.
        assert progresso[0]["eta_s"] is None
        assert progresso[1]["eta_s"] is None

    def test_eta_numerico_depois_de_3_blocos(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Depois de 3+ blocos o ETA vira um número."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        progresso = [e for e in _eventos(job) if e["type"] == "progress"]

        assert progresso[2]["eta_s"] is not None
        assert isinstance(progresso[2]["eta_s"], (int, float))
        assert progresso[2]["eta_s"] >= 0

        # E o job termina com um ETA coerente.
        assert job.eta_s is None or job.eta_s >= 0

    def test_eta_no_snapshot(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O snapshot expõe o eta_s."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        snapshot = manager.snapshot(job)

        assert "eta_s" in snapshot
        assert snapshot["eta_s"] is None or isinstance(snapshot["eta_s"], (int, float))


# ── Pausa e retomada ────────────────────────────────────────────────────────

class TestPausaERetomada:
    """Pausa, retomada e reaproveitamento do cache."""

    def test_pausar_deixa_o_estado_paused(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Pausar no meio deixa o estado "paused" e o Report parcial salvo."""
        job = manager.criar(str(epub_teste), _config(batch_size=2))

        # Pausa depois de N blocos: o pause_cb roda ENTRE LOTES.
        def parar_apos_2_lotes() -> bool:
            return job.done >= 4

        job = manager.criar(str(epub_teste), _config(batch_size=2))
        manager.iniciar(job, client=ClienteLento())
        _pausar_apos(manager, job, 4)

        assert job.state == STATE_PAUSED
        assert job.report is not None
        assert job.report["translated_blocks"] >= 4
        assert job.report["total_blocks"] > job.report["translated_blocks"]

    def test_pausa_gera_o_epub_parcial(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """A pausa grava o EPUB parcial em disco."""
        job = manager.criar(str(epub_teste), _config(batch_size=2))
        manager.iniciar(job, client=ClienteLento())
        _pausar_apos(manager, job, 4)

        assert job.state == STATE_PAUSED
        assert Path(job.out).is_file()

    def test_evento_state_paused(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """A pausa emite o evento de estado "paused"."""
        job = manager.criar(str(epub_teste), _config(batch_size=2))
        manager.iniciar(job, client=ClienteLento())
        _pausar_apos(manager, job, 4)

        estados = [e for e in _eventos(job) if e["type"] == "state"]

        assert estados
        assert estados[-1]["state"] == STATE_PAUSED
        assert "Pausado" in estados[-1]["message"]

    def test_retomar_reusa_o_cache(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """A retomada relança com o mesmo cache: o contador "cached" sobe."""
        job = manager.criar(str(epub_teste), _config(batch_size=2))
        manager.iniciar(job, client=ClienteLento())
        _pausar_apos(manager, job, 4)

        assert job.state == STATE_PAUSED
        traduzidos_1a = job.translated
        cache_path = job.cache_path

        # Retoma: os blocos já feitos vêm do cache.
        manager.retomar(job, client=MockClient())
        _aguardar(manager, job)

        assert job.state == STATE_DONE
        assert job.cache_path == cache_path
        assert job.cached >= traduzidos_1a
        assert job.cached > 0
        # O total traduzido + cacheado cobre o livro inteiro.
        assert job.cached + job.translated == TOTAL_BLOCOS

    def test_retomar_nao_retraduz_os_blocos_do_cache(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Os blocos pausados vêm do cache (status "cache" no evento)."""
        job = manager.criar(str(epub_teste), _config(batch_size=2))
        manager.iniciar(job, client=ClienteLento())
        _pausar_apos(manager, job, 4)

        traduzidos_1a = job.translated
        assert traduzidos_1a > 0

        manager.retomar(job, client=MockClient())
        _aguardar(manager, job)

        progresso = [e for e in _eventos(job) if e["type"] == "progress"]
        cacheados = [e for e in progresso if e["status"] == "cache"]

        assert cacheados
        # Os blocos do capítulo 1 já tinham sido traduzidos: eles vêm do
        # cache e NÃO são traduzidos de novo.
        assert [e["block_index"] for e in cacheados] == list(
            range(1, traduzidos_1a + 1)
        )
        assert all(e["file"] == "OEBPS/Text/section-0001.html" for e in cacheados)


# ── Regressão: a retomada NUNCA lança uma segunda thread ────────────────────

class TestRetomadaSemThreadDupla:
    """Regressão do bug de orquestração (medido com o LM Studio real).

    O ``resume`` antigo limpava o evento de pausa e lançava uma thread
    NOVA **sem esperar** a thread anterior terminar: as duas traduções
    escreviam no MESMO cache e no MESMO EPUB de saída, e a pausa era
    perdida (o evento de pausa era limpo de baixo da thread antiga).

    Estes testes usam um provedor LENTO que conta as chamadas
    simultâneas: com a correção, ``max_ativos`` é sempre 1.
    """

    def test_retomar_com_a_thread_ainda_viva_nao_duplica(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O ``resume`` com a thread anterior viva NÃO lança uma 2ª thread.

        Reproduz o bug real: pausa pedida, ``resume`` chamado **antes** da
        pausa efetivar (a thread ainda está no meio do lote).
        """
        cliente = ClienteContador(atraso=1.0)
        job = manager.criar(
            str(epub_teste),
            _config(batch_size=1, only="OEBPS/Text/section-0001.html"),
        )
        manager.iniciar(job, client=cliente)

        # Espera a thread entrar no primeiro lote (ela fica ~1 s no chat).
        _esperar_chamadas(cliente, 1)
        assert cliente.chamadas >= 1, "a thread não chegou a chamar o modelo"

        # (a) pede a pausa — a thread A ainda está no meio do lote.
        manager.pausar(job)

        # (b) retoma ENQUANTO a thread anterior ainda está viva.
        manager.retomar(job, client=cliente)

        # (c) o job termina e NUNCA houve duas traduções simultâneas.
        assert manager.aguardar(job, TIMEOUT), "a thread do job não terminou"

        assert cliente.max_ativos == 1, (
            "houve duas threads de tradução ao mesmo tempo "
            f"(max_ativos={cliente.max_ativos})"
        )
        assert job.state == STATE_DONE, job.error
        # Só o capítulo 1 foi traduzido (13 blocos).
        assert job.total == BLOCOS_CAPITULO_1
        assert job.translated + job.cached == job.total
        assert job.total > 0

    def test_retomar_nao_perde_a_pausa(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """A pausa não se perde: o job chega a "paused" e só aí retoma.

        Com o ``resume`` chegando no meio do lote, a thread antiga tem de
        parar (estado "paused") **antes** da nova thread começar.
        """
        cliente = ClienteContador(atraso=1.0)
        job = manager.criar(
            str(epub_teste),
            _config(batch_size=1, only="OEBPS/Text/section-0001.html"),
        )
        manager.iniciar(job, client=cliente)

        _esperar_chamadas(cliente, 1)
        assert cliente.chamadas >= 1

        manager.pausar(job)

        # O "resume" chega no meio do lote: a thread antiga é pausada e
        # esperada — a nova thread só começa depois.
        manager.retomar(job, client=cliente)

        assert manager.aguardar(job, TIMEOUT), "a thread do job não terminou"

        assert cliente.max_ativos == 1, (
            "houve duas threads de tradução ao mesmo tempo "
            f"(max_ativos={cliente.max_ativos})"
        )
        # A thread antiga parou de verdade (o estado "paused" foi emitido).
        estados = [e for e in _eventos(job) if e["type"] == "state"]
        assert any(e["state"] == STATE_PAUSED for e in estados), estados
        assert job.state == STATE_DONE, job.error

    def test_retomar_depois_da_pausa_reusa_o_cache(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Pausa → espera "paused" → retoma: o cache é reusado.

        Nenhum bloco é traduzido duas vezes: com ``batch_size=1`` cada
        bloco vira um lote, então o provedor é chamado **uma vez por
        bloco**. O total de chamadas é, portanto, a prova de que nenhum
        bloco foi traduzido duas vezes.
        """
        cliente = ClienteContador(atraso=0.02)
        job = manager.criar(str(epub_teste), _config(batch_size=1))
        manager.iniciar(job, client=cliente)

        # Pausa depois de alguns blocos e ESPERA a pausa efetivar.
        _pausar_apos(manager, job, 3)
        assert job.state == STATE_PAUSED

        traduzidos_1a = job.translated
        cache_path = job.cache_path
        assert traduzidos_1a > 0

        # Os eventos da 1ª execução saem do barramento (o 2º passe é que
        # interessa aqui).
        _eventos(job)

        # Só agora retoma.
        manager.retomar(job, client=cliente)
        _aguardar(manager, job)

        assert job.state == STATE_DONE, job.error
        assert job.cache_path == cache_path
        # Os blocos anteriores voltaram do cache.
        assert job.cached > 0
        assert job.cached == traduzidos_1a
        assert job.translated + job.cached == TOTAL_BLOCOS

        # NENHUM bloco foi traduzido duas vezes: o modelo foi chamado
        # exatamente uma vez por bloco do livro.
        assert cliente.chamadas == TOTAL_BLOCOS, (
            "algum bloco foi traduzido mais de uma vez "
            f"({cliente.chamadas} chamadas para {TOTAL_BLOCOS} blocos)"
        )

        # Os blocos do capítulo 1 já traduzidos vieram do cache.
        progresso = [e for e in _eventos(job) if e["type"] == "progress"]
        cacheados = [
            e for e in progresso
            if e["status"] == "cache"
            and e["file"] == "OEBPS/Text/section-0001.html"
        ]
        assert [e["block_index"] for e in cacheados] == list(
            range(1, traduzidos_1a + 1)
        )

    def test_snapshot_expoe_pausando(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O snapshot diz "pausando" enquanto a thread ainda não parou.

        O estado do job continua "running" (o "paused" só vem no fim do
        lote), mas a UI precisa saber que a pausa foi pedida.
        """
        cliente = ClienteContador(atraso=2.0)
        job = manager.criar(
            str(epub_teste),
            _config(batch_size=1, only="OEBPS/Text/section-0001.html"),
        )
        manager.iniciar(job, client=cliente)

        _esperar_chamadas(cliente, 1)
        assert cliente.chamadas >= 1

        manager.pausar(job)

        # A pausa foi pedida, mas a thread ainda está no meio do lote.
        snapshot = manager.snapshot(job)
        assert snapshot["state"] == STATE_RUNNING
        assert snapshot["pausando"] is True

        # O ``job.json`` também carrega a flag.
        manager.salvar(job)
        dados = json.loads(
            (Path(job.dir) / "job.json").read_text(encoding="utf-8")
        )
        assert dados["pausando"] is True

        # Quando a thread para, o estado vira "paused" e a flag cai.
        _aguardar(manager, job)
        snapshot = manager.snapshot(job)
        assert snapshot["state"] == STATE_PAUSED
        assert snapshot["pausando"] is False

    def test_retomar_sem_thread_viva_comeca_normalmente(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Sem thread anterior, o "resume" começa a tradução de imediato."""
        job = manager.criar(str(epub_teste), _config(batch_size=1))
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)
        assert job.state == STATE_DONE

        manager.retomar(job, client=MockClient())
        _aguardar(manager, job)

        assert job.state == STATE_DONE
        # O 2º passe é todo do cache.
        assert job.cached == TOTAL_BLOCOS
        assert job.translated == 0

    def test_cancelar_nao_lanca_thread_nova(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O cancelamento NÃO relança a thread: a existente termina sozinha."""
        cliente = ClienteContador(atraso=0.2)
        job = manager.criar(
            str(epub_teste),
            _config(batch_size=1, only="OEBPS/Text/section-0001.html"),
        )
        manager.iniciar(job, client=cliente)

        _esperar_chamadas(cliente, 1)
        assert cliente.chamadas >= 1

        thread_antes = manager._execucao(job).thread
        manager.cancelar(job)
        _aguardar(manager, job)

        assert job.state == STATE_CANCELLED
        # A MESMA thread (nenhuma nova foi lançada).
        assert manager._execucao(job).thread is thread_antes
        assert cliente.max_ativos == 1

    def test_iniciar_com_thread_viva_nao_duplica(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O ``iniciar`` também se protege: nunca lança uma 2ª thread."""
        cliente = ClienteContador(atraso=1.0)
        job = manager.criar(
            str(epub_teste),
            _config(batch_size=1, only="OEBPS/Text/section-0001.html"),
        )
        manager.iniciar(job, client=cliente)

        _esperar_chamadas(cliente, 1)
        assert cliente.chamadas >= 1

        # O "iniciar" com a thread ainda viva: ele pausa a thread anterior,
        # espera ela terminar e só então relança.
        manager.iniciar(job, client=cliente)
        assert manager.aguardar(job, TIMEOUT), "a thread do job não terminou"

        assert cliente.max_ativos == 1, (
            "houve duas threads de tradução ao mesmo tempo "
            f"(max_ativos={cliente.max_ativos})"
        )
        assert job.state == STATE_DONE, job.error

    def test_retomar_com_thread_viva_sem_prazo_nao_lanca_thread(
        self, manager: JobManager, epub_teste: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem prazo, o "resume" NÃO lança thread nova e avisa no log.

        Este é o caminho de segurança: se a thread antiga não terminar
        dentro do prazo, é melhor não traduzir do que traduzir duas
        vezes no mesmo cache e no mesmo EPUB de saída.
        """
        cliente = ClienteContador(atraso=5.0)
        job = manager.criar(
            str(epub_teste),
            _config(batch_size=1, only="OEBPS/Text/section-0001.html"),
        )
        manager.iniciar(job, client=cliente)

        _esperar_chamadas(cliente, 1)
        assert cliente.chamadas >= 1

        # Prazo curtíssimo: a thread do modelo (5 s) não termina a tempo.
        monkeypatch.setattr(jobs, "TIMEOUT_ESPERA_THREAD", 0.01)

        thread_antes = manager._execucao(job).thread
        manager.retomar(job, client=cliente)

        # Nenhuma thread nova foi lançada.
        assert manager._execucao(job).thread is thread_antes
        # O aviso foi registrado no log do job.
        log = Path(job.log_path)
        assert log.is_file()
        assert "Não foi possível retomar" in log.read_text(encoding="utf-8")

        # Deixa a thread original terminar (o job fica pausado).
        _aguardar(manager, job)
        assert job.state == STATE_PAUSED


# ── Cancelamento ────────────────────────────────────────────────────────────

class TestCancelamento:
    """Cancelamento do job."""

    def test_cancelar_deixa_o_estado_cancelled(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Cancelar no meio deixa o estado "cancelled"."""
        job = manager.criar(str(epub_teste), _config(batch_size=2))
        manager.iniciar(job, client=ClienteLento())

        limite = time.monotonic() + TIMEOUT
        while job.done < 4 and time.monotonic() < limite:
            time.sleep(0.005)
        manager.cancelar(job)
        _aguardar(manager, job)

        assert job.state == STATE_CANCELLED

    def test_cancelamento_preserva_o_cache(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O cache gravado antes do cancelamento é preservado."""
        job = manager.criar(str(epub_teste), _config(batch_size=2))
        manager.iniciar(job, client=ClienteLento())

        limite = time.monotonic() + TIMEOUT
        while job.done < 4 and time.monotonic() < limite:
            time.sleep(0.005)
        manager.cancelar(job)
        _aguardar(manager, job)

        assert Path(job.cache_path).is_file()
        assert job.translated + job.cached > 0

    def test_cancelar_para_a_traducao(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O cancelamento interrompe a tradução (não termina o livro)."""
        job = manager.criar(str(epub_teste), _config(batch_size=2))
        manager.iniciar(job, client=ClienteLento())

        limite = time.monotonic() + TIMEOUT
        while job.done < 4 and time.monotonic() < limite:
            time.sleep(0.005)
        manager.cancelar(job)
        _aguardar(manager, job)

        assert job.state == STATE_CANCELLED
        assert job.done < TOTAL_BLOCOS


# ── Erro do provedor ────────────────────────────────────────────────────────

class ClienteQueFalha:
    """Provedor que sempre falha — exercita o estado "error"."""

    def list_models(self) -> list[str]:
        return ["mock-model"]

    def resolve_model(self) -> str:
        return "mock-model"

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.25,
        max_tokens: int | None = None,
    ) -> str:
        raise RuntimeError("modelo fora do ar")

    @property
    def is_mock(self) -> bool:
        return True


class TestErro:
    """Erro do provedor deixa o job no estado "error"."""

    def test_estado_error_com_mensagem_em_pt_br(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O job falha com mensagem em pt-BR e o evento "error" é emitido."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=ClienteQueFalha())
        _aguardar(manager, job)

        assert job.state == STATE_ERROR
        assert job.error
        assert job.error.startswith("Erro durante a tradução")

        erros = [e for e in _eventos(job) if e["type"] == "error"]
        assert erros
        assert erros[-1]["message"] == job.error

    def test_erro_e_gravado_no_job_json(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O ``job.json`` guarda o estado "error" e a mensagem."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=ClienteQueFalha())
        _aguardar(manager, job)

        dados = json.loads(
            (Path(job.dir) / "job.json").read_text(encoding="utf-8")
        )

        assert dados["state"] == STATE_ERROR
        assert dados["error"] == job.error

    def test_erro_do_lm_studio_desligado(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O LM Studio fora do ar dá a mensagem de erro específica."""
        from epub_translator.lmstudio_client import ServerNotRunningError

        class ClienteServidorFora(ClienteQueFalha):
            def chat(
                self,
                messages: list[dict[str, str]],
                temperature: float = 0.25,
                max_tokens: int | None = None,
            ) -> str:
                raise ServerNotRunningError("servidor fora do ar")

        job = manager.criar(
            str(epub_teste),
            _config(base_url="http://localhost:9999/v1"),
        )
        manager.iniciar(job, client=ClienteServidorFora())
        _aguardar(manager, job)

        assert job.state == STATE_ERROR
        assert "LM Studio" in job.error
        assert "http://localhost:9999/v1" in job.error


# ── Segurança: a api_key nunca aparece ──────────────────────────────────────

class TestApiKey:
    """A api_key não pode vazar para o disco, os eventos ou o snapshot."""

    def test_api_key_nao_esta_no_job_json(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O ``job.json`` guarda a chave mascarada (nunca em claro)."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        texto = (Path(job.dir) / "job.json").read_text(encoding="utf-8")

        assert "sk-chave-secreta-1234" not in texto
        assert job.config["api_key"] == "sk-chave-secreta-1234"
        assert job.config["api_key"] not in texto

    def test_api_key_nao_esta_nos_eventos(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Nenhum evento carrega a api_key."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        for evento in _eventos(job):
            assert "sk-chave-secreta-1234" not in json.dumps(evento, ensure_ascii=False)

    def test_api_key_nao_esta_no_snapshot(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O snapshot do job não carrega a api_key."""
        job = manager.criar(str(epub_teste), _config())
        manager.iniciar(job, client=MockClient())
        _aguardar(manager, job)

        snapshot = manager.snapshot(job)

        assert "sk-chave-secreta-1234" not in json.dumps(snapshot, ensure_ascii=False)

    def test_api_key_curta_vira_asteriscos(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """Uma chave com menos de 8 caracteres vira "***"."""
        job = manager.criar(str(epub_teste), _config(api_key="abc"))
        manager.salvar(job)

        dados = json.loads((Path(job.dir) / "job.json").read_text(encoding="utf-8"))

        assert dados["config"]["api_key"] == "***"


# ── Logs: um job por arquivo ────────────────────────────────────────────────

class TestLogs:
    """Cada job tem o seu próprio arquivo de log."""

    def test_handlers_de_outros_jobs_sao_removidos(
        self, manager: JobManager, epub_teste: Path, tmp_path: Path
    ) -> None:
        """Iniciar um job tira do logger os handlers dos outros jobs."""
        log = logging.getLogger("epub_translator")

        job1 = manager.criar(str(epub_teste), _config())
        manager.iniciar(job1, client=MockClient())
        _aguardar(manager, job1)

        # O tradutor criou o handler do job 1.
        assert log.handlers
        base1 = os.path.abspath(job1.log_path)
        assert any(
            os.path.abspath(getattr(h, "baseFilename", "")) == base1
            for h in log.handlers
        )

        # Inicia o job 2: o handler do job 1 tem que sair.
        job2 = manager.criar(str(epub_teste), _config())
        manager.iniciar(job2, client=MockClient())
        _aguardar(manager, job2)

        base2 = os.path.abspath(job2.log_path)
        assert any(
            os.path.abspath(getattr(h, "baseFilename", "")) == base2
            for h in log.handlers
        )
        assert not any(
            os.path.abspath(getattr(h, "baseFilename", "")) == base1
            for h in log.handlers
        )

    def test_log_do_job_nao_tem_linhas_de_outro_job(
        self, manager: JobManager, epub_teste: Path
    ) -> None:
        """O log de um job não recebe as linhas de outro job."""
        job1 = manager.criar(str(epub_teste), _config())
        manager.iniciar(job1, client=MockClient())
        _aguardar(manager, job1)

        job2 = manager.criar(str(epub_teste), _config())
        manager.iniciar(job2, client=MockClient())
        _aguardar(manager, job2)

        log1 = Path(job1.log_path).read_text(encoding="utf-8")
        log2 = Path(job2.log_path).read_text(encoding="utf-8")

        assert "Traduzindo arquivo" in log1
        assert "Traduzindo arquivo" in log2

        # O identificador do job 2 não aparece no log do job 1.
        assert job2.id not in log1
        assert job1.id not in log2


# ── Módulo puro (sem FastAPI) ──────────────────────────────────────────────

def test_jobs_nao_importa_fastapi() -> None:
    """O módulo é uma biblioteca pura: não importa FastAPI."""
    import ast

    import web.jobs as jobs

    arvore = ast.parse(Path(jobs.__file__).read_text(encoding="utf-8"))
    importados: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            importados.update(alias.name.split(".")[0] for alias in no.names)
        elif isinstance(no, ast.ImportFrom) and no.module:
            importados.add(no.module.split(".")[0])

    assert "fastapi" not in importados
    assert "starlette" not in importados
