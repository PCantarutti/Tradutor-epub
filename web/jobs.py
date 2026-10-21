"""Job de tradução em disco, barramento de eventos e orquestração.

Este módulo encapsula **um** job de tradução:

1. o **modelo em disco** — cada job tem uma pasta própria
   (``web/jobs/<id>/``) com o EPUB original copiado, o EPUB de saída, o cache
   JSON, o log e o ``job.json`` (estado + contadores, **sem** a api_key);
2. o **barramento de eventos** (:class:`EventBus`) — a thread do tradutor
   empurra eventos (``push``) e o gerador SSE, que roda no event loop, os
   lê (``drain``). É tudo **sync**: a thread do tradutor é síncrona, então
   nunca usamos ``asyncio.Queue`` (o event loop travaria);
3. a **orquestração** — iniciar, pausar, retomar e cancelar, com ETA,
   verificação de integridade e persistência do estado.

Invariante da orquestração (pausa/retomada): **nunca há duas threads de
tradução no mesmo job**. As duas escreveriam no MESMO cache e no MESMO EPUB
de saída, e o ``resume`` perderia a pausa (o evento de pausa seria limpo
com a thread antiga ainda viva). Por isso, antes de lançar uma thread, a
thread anterior é **pausada e esperada** — ela para no fim do lote em
andamento, grava o EPUB parcial e só depois a nova thread começa.

A camada web é uma **capa fina**: o módulo apenas *consome* o pipeline
existente (:func:`epub_translator.translator.translate_epub`) e a verificação
final (:func:`traduzir.verificar_epub`). Nada de EPUB, cache ou blocos é
reimplementado aqui.

Importante: este módulo **não** importa FastAPI — é uma biblioteca pura, como
``web/preview.py``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# O script `traduzir.py` mora na RAIZ do projeto (é um script, não um pacote):
# para o `import` funcionar com qualquer diretório de trabalho, a raiz entra
# no `sys.path` ANTES do import (o servidor pode ser iniciado de qualquer
# pasta).
RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from epub_translator.epub_io import EpubData, EpubReader  # noqa: E402
from epub_translator.lmstudio_client import (  # noqa: E402
    ChatProvider,
    LMStudioClient,
    MockClient,
    NoModelError,
    ServerNotRunningError,
)
from epub_translator.translator import (  # noqa: E402
    ProgressEvent,
    Report,
    TranslationCancelled,
    TranslationConfig,
    translate_epub,
)
from traduzir import verificar_epub  # noqa: E402


# ── Constantes ───────────────────────────────────────────────────────────────

STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"

ESTADOS_TERMINAIS: frozenset[str] = frozenset(
    {STATE_DONE, STATE_ERROR, STATE_CANCELLED}
)

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_API_KEY = "lm-studio"
DEFAULT_TARGET_LANG = "pt-BR"
DEFAULT_BATCH_SIZE = 5
DEFAULT_TEMPERATURE = 0.25

# Número de amostras de "segundos por bloco" usadas no cálculo do ETA.
TAMANHO_AMOSTRAS_ETA = 20
# Mínimo de amostras para o ETA deixar de ser None.
MINIMO_AMOSTRAS_ETA = 3

# A cada quantos blocos o `job.json` é reescrito (o cache é o registro
# confiável do progresso; o json é só a fotografia para a UI).
BLOCOS_POR_GRAVACAO = 25

# Prazo máximo para esperar a thread de tradução anterior terminar antes de
# relançar o job. O lote em andamento pode levar ~150 s com um modelo lento
# (medido com o qwen3.6-35b no LM Studio), então o prazo é generoso.
TIMEOUT_ESPERA_THREAD = 300.0

logger = logging.getLogger("web.jobs")


# ── Barramento de eventos ────────────────────────────────────────────────────

class EventBus:
    """Barramento de eventos thread-safe (síncrono).

    A thread do tradutor empurra os eventos com :meth:`push`; o gerador SSE
    (que roda no event loop) lê com :meth:`drain`. O lock garante que nenhum
    evento se perca quando ``push`` e ``drain`` acontecem ao mesmo tempo.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._eventos: list[dict[str, Any]] = []

    def push(self, event: dict[str, Any]) -> None:
        """Empilha um evento (chamado pela thread do tradutor)."""
        with self._lock:
            self._eventos.append(event)

    def drain(self) -> list[dict[str, Any]]:
        """Devolve os eventos acumulados e limpa a fila.

        Returns:
            A lista de eventos pendentes (vazia se não houver nada).
        """
        with self._lock:
            eventos = self._eventos
            self._eventos = []
            return eventos

    def __len__(self) -> int:
        with self._lock:
            return len(self._eventos)


# ── Job ──────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    """Estado de um job de tradução.

    Attributes:
        id: Identificador único do job (também é o nome da pasta).
        dir: Pasta do job (``web/jobs/<id>/``).
        src: EPUB original (cópia dentro da pasta do job).
        out: EPUB de saída (traduzido).
        cache_path: Cache JSON dos blocos traduzidos.
        log_path: Arquivo de log da tradução.
        config: Configuração normalizada (contém a api_key **em memória**).
        state: Estado atual (uma das constantes ``STATE_*``).
        report: Último ``Report`` serializado (``None`` enquanto não houve).
        error: Mensagem de erro em pt-BR (vazia se não houve).
        translated: Blocos traduzidos na execução atual.
        cached: Blocos reusados do cache na execução atual.
        failed: Blocos que falharam na execução atual.
        discarded: Blocos descartados (marcador inválido) na execução atual.
        total: Total de blocos do livro.
        done: Blocos já concluídos (traduzidos + cache + falhas + descartes).
        elapsed_s: Segundos decorridos desde o início da execução.
        eta_s: Estimativa de término (em segundos) ou ``None``.
        current_file: Capítulo em processamento.
        current_block: Índice (1-based) do bloco em processamento.
        model: Modelo em uso (resolvido uma vez, no início).
        glossary: Glossário de nomes próprios (``termo`` → ``tradução``).
        integrity: Resultado da verificação final do EPUB gerado.
        paused_event: Evento setado para pedir a pausa.
        cancel_event: Evento setado para pedir o cancelamento.
        bus: Barramento de eventos do job.
        data: Conteúdo do EPUB original em memória (capítulos/preview).
        lock: Lock dos contadores (a thread do tradutor e a UI leem juntos).
    """

    id: str
    dir: str
    src: str
    out: str
    cache_path: str
    log_path: str
    config: dict[str, Any] = field(default_factory=dict)
    state: str = STATE_RUNNING
    report: dict[str, Any] | None = None
    error: str = ""
    translated: int = 0
    cached: int = 0
    failed: int = 0
    discarded: int = 0
    total: int = 0
    done: int = 0
    elapsed_s: float = 0.0
    eta_s: float | None = None
    current_file: str = ""
    current_block: int = 0
    model: str = ""
    glossary: dict[str, str] = field(default_factory=dict)
    integrity: list[dict[str, Any]] = field(default_factory=list)
    paused_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    bus: EventBus = field(default_factory=EventBus)
    data: EpubData | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


# ── Estado interno da execução (fora do dataclass público) ──────────────────

@dataclass
class _Execucao:
    """Dados de execução que NÃO fazem parte do contrato público do job.

    Ficam fora do :class:`Job` para que o conjunto de campos do job continue
    exatamente o do contrato (o estado volátil da execução é do gerenciador).
    """

    amostras: deque[float] = field(
        default_factory=lambda: deque(maxlen=TAMANHO_AMOSTRAS_ETA)
    )
    ultimo_elapsed: float = 0.0
    ultimo_done: int = 0
    gravados: int = 0
    thread: threading.Thread | None = None
    ultimo_detalhe: str = ""
    # Serializa as operações de controle (iniciar/pausar/retomar/cancelar)
    # do MESMO job: dois "resume" simultâneos não podem lançar duas threads.
    # É reentrante porque `retomar` chama `iniciar`.
    lock_controle: threading.RLock = field(default_factory=threading.RLock)


class _ClienteEspelhado:
    """Encapsula um provedor de chat e guarda a ÚLTIMA exceçãoRaisida.

    O tradutor engole a exceção do modelo (marca os blocos como "falhou" e
    segue). Com este espelho, a camada web consegue distinguir uma falha real
    do provedor de uma tradução concluída com 0 blocos — e mapear a exceção
    para a mensagem de erro certa em pt-BR.
    """

    def __init__(self, interno: ChatProvider) -> None:
        self._interno = interno
        self.ultimo_erro: Exception | None = None

    def list_models(self) -> list[str]:
        try:
            return self._interno.list_models()
        except Exception as exc:
            self.ultimo_erro = exc
            raise

    def resolve_model(self) -> str:
        try:
            return self._interno.resolve_model()
        except Exception as exc:
            self.ultimo_erro = exc
            raise

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.25,
        max_tokens: int | None = None,
    ) -> str:
        try:
            return self._interno.chat(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:
            self.ultimo_erro = exc
            raise


# ── Helpers ──────────────────────────────────────────────────────────────────

def _novo_id() -> str:
    """Gera um id de job (ordenável pelo horário + aleatório)."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _mascarar_chave(chave: str) -> str:
    """Mascara a api_key: primeiros 3 e últimos 4 caracteres.

    Args:
        chave: Segredo a mascarar.

    Returns:
        A chave mascarada (ex.: ``"sk-…abcd"``) ou ``"***"`` quando a chave
        tem menos de 8 caracteres.
    """
    if not chave:
        return ""
    if len(chave) < 8:
        return "***"
    return f"{chave[:3]}…{chave[-4:]}"


def _config_publica(config: dict[str, Any]) -> dict[str, Any]:
    """Cópia da config sem a api_key em claro (apenas mascarada)."""
    publica = dict(config)
    if "api_key" in publica:
        publica["api_key"] = _mascarar_chave(str(publica["api_key"]))
    return publica


def _normalizar_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Normaliza a config recebida da UI (valores padrão garantidos)."""
    bruto = dict(config or {})
    glossario = bruto.get("glossary") or {}
    if not isinstance(glossario, dict):
        glossario = {}

    limite_lotes = bruto.get("limit_batches")
    try:
        limite_lotes = int(limite_lotes) if limite_lotes is not None else None
    except (TypeError, ValueError):
        limite_lotes = None

    try:
        batch_size = int(bruto.get("batch_size") or DEFAULT_BATCH_SIZE)
    except (TypeError, ValueError):
        batch_size = DEFAULT_BATCH_SIZE

    try:
        temperatura = float(bruto.get("temperature", DEFAULT_TEMPERATURE))
    except (TypeError, ValueError):
        temperatura = DEFAULT_TEMPERATURE

    return {
        "provider": bruto.get("provider") or "lmstudio",
        "base_url": bruto.get("base_url") or DEFAULT_BASE_URL,
        "api_key": bruto.get("api_key") or DEFAULT_API_KEY,
        "model": bruto.get("model") or None,
        "target_lang": bruto.get("target_lang") or DEFAULT_TARGET_LANG,
        "batch_size": batch_size,
        "temperature": temperatura,
        "glossary": {str(k): str(v) for k, v in glossario.items()},
        "output_name": bruto.get("output_name") or "traducao.epub",
        "only": bruto.get("only") or None,
        "limit_batches": limite_lotes,
        "mock": bool(bruto.get("mock")),
    }


def _mensagem_de_erro(exc: Exception, base_url: str) -> str:
    """Traduz a exceção numa mensagem em pt-BR para a UI."""
    if isinstance(exc, ServerNotRunningError):
        return (
            f"O LM Studio não está rodando em {base_url} — "
            "abra o LM Studio e inicie o servidor na aba Server."
        )
    if isinstance(exc, NoModelError):
        return "Nenhum modelo carregado — carregue um modelo no LM Studio"
    return f"Erro durante a tradução: {exc}"


# ── Gerenciador de jobs ──────────────────────────────────────────────────────

class JobManager:
    """Cria, executa e controla os jobs de tradução.

    Args:
        pasta: Pasta onde ficam os jobs. O padrão é ``web/jobs``, na raiz do
            projeto.
    """

    PASTA_JOBS = "web/jobs"

    def __init__(self, pasta: str | Path | None = None) -> None:
        if pasta is None:
            self.pasta = RAIZ_PROJETO / self.PASTA_JOBS
        else:
            self.pasta = Path(pasta)
        self.pasta.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._execucoes: dict[str, _Execucao] = {}
        self._clientes: dict[str, ChatProvider] = {}

    # ── Criação e leitura ──────────────────────────────────────────────

    def criar(self, src: str, config: dict[str, Any] | None = None) -> Job:
        """Cria um job novo: pasta, cópia do EPUB original e ``job.json``.

        Args:
            src: Caminho do EPUB original (é **copiado** para a pasta do job).
            config: Configuração vinda da UI (é normalizada aqui).

        Returns:
            O :class:`Job` criado, no estado ``running`` (ainda sem thread).
        """
        origem = Path(src)
        if not origem.is_file():
            raise FileNotFoundError(f"EPUB de origem não encontrado: {src}")

        cfg = _normalizar_config(config)
        job_id = _novo_id()
        pasta = self.pasta / job_id
        pasta.mkdir(parents=True, exist_ok=True)

        destino = pasta / "original.epub"
        shutil.copyfile(origem, destino)

        saida = str(pasta / "traducao.epub")
        job = Job(
            id=job_id,
            dir=str(pasta),
            src=str(destino),
            out=saida,
            cache_path=str(pasta / "cache.json"),
            # O tradutor cria o log em "<output>.log" — o mesmo arquivo.
            log_path=saida + ".log",
            config=cfg,
            state=STATE_RUNNING,
            glossary=dict(cfg["glossary"]),
        )

        try:
            job.data = EpubReader(job.src).read()
        except Exception as exc:
            raise ValueError(
                f"Não foi possível ler o EPUB enviado: {exc}"
            ) from exc

        with self._lock:
            self._jobs[job_id] = job
            self._execucoes.setdefault(job_id, _Execucao())
        self.salvar(job)
        return job

    def obter(self, job_id: str) -> Job | None:
        """Devolve o job pelo id (``None`` se não existir)."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job
        return self._carregar_do_disco(job_id)

    def listar(self) -> list[Job]:
        """Lista os jobs (da memória e do disco), do mais recente ao mais antigo.

        O id começa com o horário (``YYYYmmdd-HHMMSS``), então a ordem
        alfabética dos ids é a ordem cronológica.
        """
        with self._lock:
            jobs: dict[str, Job] = dict(self._jobs)

        if self.pasta.exists():
            for pasta in sorted(self.pasta.iterdir(), reverse=True):
                if not pasta.is_dir() or pasta.name in jobs:
                    continue
                job = self._carregar_do_disco(pasta.name)
                if job is not None:
                    jobs[job.id] = job

        return sorted(jobs.values(), key=lambda j: j.id, reverse=True)

    def _carregar_do_disco(self, job_id: str) -> Job | None:
        """Reconstrói um job a partir do ``job.json`` (servidor reiniciado)."""
        pasta = self.pasta / job_id
        arquivo = pasta / "job.json"
        if not arquivo.is_file():
            return None
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(dados, dict):
            return None

        job = Job(
            id=str(dados.get("id") or job_id),
            dir=str(dados.get("dir") or str(pasta)),
            src=str(dados.get("src") or str(pasta / "original.epub")),
            out=str(dados.get("out") or str(pasta / "traducao.epub")),
            cache_path=str(dados.get("cache_path") or str(pasta / "cache.json")),
            log_path=str(dados.get("log_path") or str(pasta / "traducao.epub.log")),
            config=_normalizar_config(dados.get("config") or {}),
            state=str(dados.get("state") or STATE_RUNNING),
            report=dados.get("report"),
            error=str(dados.get("error") or ""),
            translated=int(dados.get("translated") or 0),
            cached=int(dados.get("cached") or 0),
            failed=int(dados.get("failed") or 0),
            discarded=int(dados.get("discarded") or 0),
            total=int(dados.get("total") or 0),
            done=int(dados.get("done") or 0),
            elapsed_s=float(dados.get("elapsed_s") or 0.0),
            eta_s=dados.get("eta_s"),
            current_file=str(dados.get("current_file") or ""),
            current_block=int(dados.get("current_block") or 0),
            model=str(dados.get("model") or ""),
            glossary=dict(dados.get("glossary") or {}),
            integrity=list(dados.get("integrity") or []),
        )
        with self._lock:
            self._jobs.setdefault(job.id, job)
            self._execucoes.setdefault(job.id, _Execucao())
        return job

    # ── Persistência ────────────────────────────────────────────────────

    def salvar(self, job: Job) -> Path:
        """Grava o ``job.json`` do job (escrita atômica: tmp + ``os.replace``).

        A api_key **nunca** é gravada em claro (fica mascarada).
        """
        pasta = Path(job.dir)
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / "job.json"
        tmp = pasta / "job.json.tmp"

        with job.lock:
            dados: dict[str, Any] = {
                "id": job.id,
                "dir": job.dir,
                "src": job.src,
                "out": job.out,
                "cache_path": job.cache_path,
                "log_path": job.log_path,
                "config": _config_publica(job.config),
                "state": job.state,
                "pausando": self._pausando(job),
                "report": job.report,
                "error": job.error,
                "translated": job.translated,
                "cached": job.cached,
                "failed": job.failed,
                "discarded": job.discarded,
                "total": job.total,
                "done": job.done,
                "elapsed_s": round(job.elapsed_s, 1),
                "eta_s": None if job.eta_s is None else round(job.eta_s, 1),
                "current_file": job.current_file,
                "current_block": job.current_block,
                "model": job.model,
                "glossary": job.glossary,
                "integrity": job.integrity,
            }

        tmp.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, caminho)
        return caminho

    def snapshot(self, job: Job) -> dict[str, Any]:
        """Fotografia do job para a UI (sem a api_key)."""
        with job.lock:
            return {
                "job_id": job.id,
                "state": job.state,
                "pausando": self._pausando(job),
                "total": job.total,
                "done": job.done,
                "translated": job.translated,
                "cached": job.cached,
                "failed": job.failed,
                "discarded": job.discarded,
                "elapsed_s": round(job.elapsed_s, 1),
                "eta_s": None if job.eta_s is None else round(job.eta_s, 1),
                "current_file": job.current_file or None,
                "current_block": job.current_block or None,
                "error": job.error or None,
                "report": job.report,
                "model": job.model or None,
                "output_name": job.config.get("output_name") or "traducao.epub",
                "has_output": os.path.exists(job.out),
                "integrity": list(job.integrity),
            }

    # ── Controle ────────────────────────────────────────────────────────

    def iniciar(self, job: Job, client: ChatProvider | None = None) -> Job:
        """Lança a tradução do job numa thread daemon.

        Antes de lançar a thread, a thread anterior do MESMO job (se ainda
        estiver viva) é pausada e **esperada**: nunca podem existir duas
        threads de tradução no mesmo job (elas escreveriam no mesmo cache e
        no mesmo EPUB de saída).

        Args:
            job: Job a executar.
            client: Provedor de chat. ``None`` cria um ``LMStudioClient``
                (ou reaproveita o cliente do próprio job, quando existe).

        Returns:
            O mesmo job (com a thread já iniciada). Se a thread anterior não
            terminou no prazo, o job é devolvido **sem** thread nova (e com
            um aviso no log).
        """
        with self._execucao(job).lock_controle:
            if not self._garantir_thread_anterior_terminada(job):
                return job

            with job.lock:
                # Os eventos são limpos DENTRO do lock: o snapshot lê o
                # estado e o "pausando" sob o mesmo lock, então a virada
                # para "running" é atômica para quem observa.
                job.paused_event.clear()
                job.cancel_event.clear()

                job.state = STATE_RUNNING
                job.error = ""
                job.report = None
                job.integrity = []
                job.translated = 0
                job.cached = 0
                job.failed = 0
                job.discarded = 0
                job.total = 0
                job.done = 0
                job.elapsed_s = 0.0
                job.eta_s = None
                job.current_file = ""
                job.current_block = 0

            execucao = self._execucao(job)
            execucao.amostras.clear()
            execucao.ultimo_elapsed = 0.0
            execucao.ultimo_done = 0
            execucao.gravados = 0

            # O log de cada job é separado: os handlers de OUTROS jobs saem do
            # logger antes de a tradução começar (senão cada arquivo de log
            # receberia as linhas de todos os jobs).
            self._preparar_log(job)

            thread = threading.Thread(
                target=self._rodar,
                args=(job, client),
                name=f"traducao-{job.id}",
                daemon=True,
            )
            execucao.thread = thread
            thread.start()
            return job

    def pausar(self, job: Job) -> Job:
        """Pede a pausa: a tradução para no fim do lote atual.

        A pausa é um **pedido**: a thread em andamento termina o lote e
        só então o job vira "paused". Enquanto isso, o snapshot expõe
        ``pausando=True`` (a UI mostra "pausando…").
        """
        job.paused_event.set()
        return job

    def retomar(self, job: Job, client: ChatProvider | None = None) -> Job:
        """Retoma a tradução com o **mesmo** cache_path.

        A ordem é a que garante que a pausa não se perca e que nunca haja
        duas threads no mesmo job:

        1. **pausa** a thread que ainda possa estar viva (o evento de pausa
           é setado — a thread para no fim do lote em andamento);
        2. **espera** essa thread terminar (ela grava o EPUB parcial e o
           estado "paused");
        3. só então **limpa** os eventos de pausa/cancelamento e **relança**
           a tradução com a mesma thread de partida.

        Os blocos já traduzidos voltam do cache (o contador ``cached`` sobe
        e nada é traduzido duas vezes).

        Se a thread anterior não terminar no prazo, a nova thread **não**
        é lançada: o job fica como está e um aviso é registrado no log.
        """
        with self._execucao(job).lock_controle:
            # (1) A pausa é pedida ANTES de esperar: é ela que faz a thread
            # em andamento parar no fim do lote.
            job.paused_event.set()

            # (2) A thread anterior tem de terminar antes de qualquer
            # limpeza — senão o `clear()` abaixo tiraria a pausa de baixo
            # dela e as duas threads escreveriam no mesmo cache/EPUB.
            if not self._garantir_thread_anterior_terminada(job):
                return job

            # (3) Agora é seguro: a thread antiga morreu, o cache está
            # gravado e a nova thread pode começar.
            job.paused_event.clear()
            job.cancel_event.clear()
            return self.iniciar(job, client=client)

    def cancelar(self, job: Job) -> Job:
        """Cancela a tradução (o cache já gravado é preservado).

        O evento de cancelamento é checado a cada bloco (o ``progress_cb``
        aborta) e o evento de pausa faz o tradutor parar de forma limpa no
        fim do lote atual — assim nada fica pela metade.

        O cancelamento **não** lança thread nenhuma: a thread existente
        termina sozinha no fim do lote.
        """
        job.cancel_event.set()
        job.paused_event.set()
        return job

    def aguardar(self, job: Job, timeout: float | None = None) -> bool:
        """Espera a thread do job terminar.

        Returns:
            ``True`` se a thread terminou dentro do prazo.
        """
        thread = self._execucao(job).thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _garantir_thread_anterior_terminada(
        self, job: Job, timeout: float | None = None
    ) -> bool:
        """Garante que não há thread de tradução viva para este job.

        A thread anterior é **pausada** (para que ela pare no fim do lote em
        andamento) e **esperada** — o prazo é generoso porque o lote pode
        levar mais de 2 minutos com um modelo lento.

        Args:
            job: Job a verificar.
            timeout: Prazo máximo de espera (segundos). ``None`` usa
                :data:`TIMEOUT_ESPERA_THREAD`.

        Returns:
            ``True`` se não há thread viva (ou se ela terminou dentro do
            prazo), ``False`` se a thread antiga continua viva — nesse caso
            o chamador **não** pode lançar uma thread nova.
        """
        if timeout is None:
            timeout = TIMEOUT_ESPERA_THREAD

        execucao = self._execucao(job)
        thread = execucao.thread
        if thread is None or not thread.is_alive():
            return True

        # O pedido de parada para a thread em andamento: ela termina o lote
        # atual, grava o EPUB parcial e encerra.
        job.paused_event.set()

        if self.aguardar(job, timeout):
            return True

        logger.warning(
            "Não foi possível retomar: a tradução anterior ainda está em "
            "andamento (job %s).",
            job.id,
        )
        self._avisar_no_log(
            job,
            "Não foi possível retomar: a tradução anterior ainda está em andamento",
        )
        return False

    @staticmethod
    def _pausando(job: Job) -> bool:
        """Diz se a pausa foi pedida mas a thread ainda não parou.

        O estado do job continua ``running`` até a thread realmente parar
        (o estado ``paused`` só é setado em :meth:`_finalizar`). Esta
        flag é a que a UI usa para mostrar "pausando…" no lugar de
        "rodando" enquanto o lote em andamento não termina.

        NÃO adquire ``job.lock`` (o snapshot e o ``job.json`` já o têm):
        o método é chamado com o lock travado.
        """
        return job.paused_event.is_set() and job.state == STATE_RUNNING

    def _avisar_no_log(self, job: Job, mensagem: str) -> None:
        """Escreve um aviso no log do job (e no log do servidor)."""
        logger.warning("%s (job %s)", mensagem, job.id)
        try:
            with open(job.log_path, "a", encoding="utf-8") as arquivo:
                arquivo.write(f"{mensagem}\n")
        except OSError as exc:
            logger.warning("Não foi possível escrever no log do job: %s", exc)

    def dados_traduzidos(self, job: Job) -> EpubData | None:
        """Lê o EPUB de saída em memória (lado traduzido do preview)."""
        if not os.path.exists(job.out):
            return None
        try:
            return EpubReader(job.out).read()
        except Exception as exc:
            logger.warning("Não foi possível ler o EPUB de saída: %s", exc)
            return None

    # ── Internos ────────────────────────────────────────────────────────

    def _execucao(self, job: Job) -> _Execucao:
        """Devolve (criando se preciso) o estado volátil da execução."""
        with self._lock:
            return self._execucoes.setdefault(job.id, _Execucao())

    def _preparar_log(self, job: Job) -> None:
        """Deixa o logger ``epub_translator`` só com o handler deste job.

        O ``_setup_logging`` do tradutor adiciona um ``FileHandler`` por
        saída e **nunca** remove os antigos: sem esta limpeza, o log de um job
        receberia as linhas de todos os outros jobs.
        """
        log = logging.getLogger("epub_translator")
        alvo = os.path.abspath(job.log_path)
        for handler in list(log.handlers):
            if not isinstance(handler, logging.FileHandler):
                continue
            base = os.path.abspath(getattr(handler, "baseFilename", "") or "")
            if base != alvo:
                log.removeHandler(handler)
                handler.close()

    def _cliente_do_job(self, job: Job) -> ChatProvider:
        """Devolve o cliente do job (reaproveita o anterior, se houver)."""
        with self._lock:
            cliente = self._clientes.get(job.id)
        if cliente is not None:
            return cliente

        if job.config.get("mock"):
            return MockClient()

        return LMStudioClient(
            base_url=job.config.get("base_url") or DEFAULT_BASE_URL,
            model=job.config.get("model") or None,
            api_key=job.config.get("api_key") or DEFAULT_API_KEY,
        )

    def _progresso(self, job: Job) -> Callable[[ProgressEvent], None]:
        """Cria o ``progress_cb`` do job (contadores + ETA + evento SSE)."""
        execucao = self._execucao(job)

        def cb(ev: ProgressEvent) -> None:
            with job.lock:
                if ev.status == "traduzido":
                    job.translated += 1
                elif ev.status == "cache":
                    job.cached += 1
                elif ev.status == "falhou":
                    job.failed += 1
                elif ev.status == "descartado":
                    job.discarded += 1

                job.done = ev.done
                job.total = ev.total
                job.elapsed_s = ev.elapsed_s
                job.current_file = ev.file
                job.current_block = ev.block_index

                if ev.status == "falhou" and ev.detail:
                    execucao.ultimo_detalhe = ev.detail

                eta = self._calcular_eta(execucao, ev)
                job.eta_s = eta

                evento: dict[str, Any] = {
                    "type": "progress",
                    "file": ev.file,
                    "block_index": ev.block_index,
                    "total": ev.total,
                    "done": ev.done,
                    "elapsed_s": round(ev.elapsed_s, 1),
                    "status": ev.status,
                    "detail": ev.detail,
                    "translated": job.translated,
                    "cached": job.cached,
                    "failed": job.failed,
                    "discarded": job.discarded,
                    "eta_s": None if eta is None else round(eta, 1),
                }

                execucao.gravados += 1
                deve_gravar = execucao.gravados % BLOCOS_POR_GRAVACAO == 0

            job.bus.push(evento)

            if deve_gravar:
                try:
                    self.salvar(job)
                except OSError as exc:
                    logger.warning("Não foi possível gravar o job.json: %s", exc)

            if job.cancel_event.is_set():
                raise TranslationCancelled("Tradução cancelada pelo usuário")

        return cb

    def _calcular_eta(
        self, execucao: _Execucao, ev: ProgressEvent
    ) -> float | None:
        """Calcula o ETA a partir das últimas amostras de tempo por bloco.

        Cada amostra é ``delta_t / delta_n`` (tempo gasto / blocos feitos
        desde o evento anterior). Com menos de 3 amostras não há ETA
        confiável — o retorno é ``None``.
        """
        delta_t = ev.elapsed_s - execucao.ultimo_elapsed
        delta_n = ev.done - execucao.ultimo_done
        execucao.ultimo_elapsed = ev.elapsed_s
        execucao.ultimo_done = ev.done

        if delta_n > 0 and delta_t >= 0:
            execucao.amostras.append(delta_t / delta_n)

        if len(execucao.amostras) < MINIMO_AMOSTRAS_ETA:
            return None

        media = sum(execucao.amostras) / len(execucao.amostras)
        return max(0.0, (ev.total - ev.done) * media)

    def _pausa(self, job: Job) -> bool:
        """``pause_cb``: devolve ``True`` quando a pausa foi pedida.

        O tradutor consulta o callback ENTRE LOTES: parando aí, o EPUB
        parcial é gravado e o ``Report`` parcial é devolvido.

        O cancelamento também é atendido aqui — é o único ponto em que o
        tradutor aceita parar de forma limpa (o ``progress_cb`` levanta uma
        exceção, mas o tradutor a engole e segue até o fim do lote).
        """
        return job.paused_event.is_set() or job.cancel_event.is_set()

    def _rodar(self, job: Job, client: ChatProvider | None) -> None:
        """Corpo da thread do tradutor."""
        espelho: _ClienteEspelhado | None = None
        try:
            self._preparar_log(job)

            if client is None:
                client = self._cliente_do_job(job)
            with self._lock:
                self._clientes[job.id] = client

            espelho = _ClienteEspelhado(client)

            # O modelo é resolvido UMA vez por job: o preview precisa
            # recalcular a chave do cache com o MESMO modelo.
            model = job.model or espelho.resolve_model()
            job.model = model

            cfg = TranslationConfig(
                src=job.src,
                output=job.out,
                target_lang=job.config.get("target_lang") or DEFAULT_TARGET_LANG,
                model=model,
                base_url=job.config.get("base_url") or DEFAULT_BASE_URL,
                batch_size=int(job.config.get("batch_size") or DEFAULT_BATCH_SIZE),
                temperature=float(
                    job.config.get("temperature", DEFAULT_TEMPERATURE)
                ),
                glossary=job.glossary or None,
                use_cache=True,
                cache_path=job.cache_path,
                only=job.config.get("only") or None,
                limit_batches=job.config.get("limit_batches"),
                progress_cb=self._progresso(job),
                pause_cb=lambda: self._pausa(job),
            )

            report = translate_epub(job.src, job.out, cfg, client=espelho)
        except Exception as exc:
            self._tratar_erro(job, exc)
            return

        self._finalizar(job, report, espelho)

    def _finalizar(
        self,
        job: Job,
        report: Report,
        espelho: _ClienteEspelhado | None = None,
    ) -> None:
        """Trata o fim normal da thread (pausado, cancelado ou concluído)."""
        with job.lock:
            job.report = dataclasses.asdict(report)

        # O cancelamento tem precedência sobre a pausa: um job cancelado
        # nunca pode parar no estado "paused".
        if job.cancel_event.is_set():
            with job.lock:
                job.state = STATE_CANCELLED
            job.bus.push({
                "type": "state",
                "state": STATE_CANCELLED,
                "message": (
                    f"Cancelado — {job.done} bloco(s) preservado(s) no cache"
                ),
            })
        elif job.paused_event.is_set():
            with job.lock:
                job.state = STATE_PAUSED
            job.bus.push({
                "type": "state",
                "state": STATE_PAUSED,
                "message": f"Pausado — {job.done} bloco(s) preservado(s)",
            })
        else:
            # Todos os blocos falharam (provedor fora do ar, modelo ausente):
            # o job é um ERRO, não uma tradução concluída com 0 blocos.
            if (
                report.total_blocks > 0
                and report.translated_blocks == 0
                and report.cached_blocks == 0
                and report.failed_blocks == report.total_blocks
            ):
                execucao = self._execucao(job)
                erro = (
                    espelho.ultimo_erro
                    if espelho is not None and espelho.ultimo_erro is not None
                    else RuntimeError(
                        execucao.ultimo_detalhe
                        or "nenhum bloco pôde ser traduzido"
                    )
                )
                self._tratar_erro(job, erro)
                return

            integridade = self._verificar_integridade(job, report)
            with job.lock:
                job.state = STATE_DONE
                job.integrity = integridade
            job.bus.push({
                "type": "done",
                "report": job.report,
                "download_url": f"/api/jobs/{job.id}/download",
                "integrity": integridade,
            })
            job.bus.push({"type": "state", "state": STATE_DONE, "message": ""})

        self.salvar(job)

    def _tratar_erro(self, job: Job, exc: Exception) -> None:
        """Trata a exceção da thread (estado ``error`` ou ``cancelled``)."""
        if job.cancel_event.is_set() or isinstance(exc, TranslationCancelled):
            with job.lock:
                job.state = STATE_CANCELLED
            job.bus.push({
                "type": "state",
                "state": STATE_CANCELLED,
                "message": (
                    f"Cancelado — {job.done} bloco(s) preservado(s) no cache"
                ),
            })
        else:
            mensagem = _mensagem_de_erro(
                exc, job.config.get("base_url") or DEFAULT_BASE_URL
            )
            with job.lock:
                job.state = STATE_ERROR
                job.error = mensagem
            job.bus.push({"type": "error", "message": mensagem})

        self.salvar(job)

    def _verificar_integridade(
        self, job: Job, report: Report
    ) -> list[dict[str, Any]]:
        """Roda a verificação final do EPUB gerado.

        O capítulo verificado é o **primeiro** arquivo processado (é o único
        com tradução garantida). Se o EPUB de saída não existe (tradução
        interrompida antes de escrever), a verificação é pulada.
        """
        if not os.path.exists(job.out):
            return []

        arquivos = list(report.files_processed)
        if not arquivos:
            return []

        alvo = arquivos[0]
        verificacao = verificar_epub(
            job.src,
            job.out,
            alvo,
            job.config.get("target_lang") or DEFAULT_TARGET_LANG,
        )
        return [
            {"item": r.item, "ok": r.ok, "detail": r.detalhe}
            for r in verificacao.resultados
        ]


# ── Instância padrão ─────────────────────────────────────────────────────────

_PADRAO: JobManager | None = None
_PADRAO_LOCK = threading.Lock()


def gerenciador_padrao() -> JobManager:
    """Devolve (criando na primeira chamada) o gerenciador de jobs padrão."""
    global _PADRAO
    with _PADRAO_LOCK:
        if _PADRAO is None:
            _PADRAO = JobManager()
        return _PADRAO


def fechar_logs() -> None:
    """Fecha os handlers de arquivo do logger ``epub_translator``.

    Útil no encerramento do servidor e nos testes (no Windows, um arquivo
    aberto impede a remoção da pasta temporária).
    """
    log = logging.getLogger("epub_translator")
    for handler in list(log.handlers):
        if isinstance(handler, logging.FileHandler):
            log.removeHandler(handler)
            handler.close()
