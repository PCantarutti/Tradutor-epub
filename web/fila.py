"""Fila de tradução: vários EPUBs, um depois do outro, sem ninguém olhando.

Para cada livro: cria o job, traduz, retraduz as falhas até ``rodadas_extras``
vezes (o cache cobre o resto) e copia o EPUB final para ``destino``. A chave
de API só vive na memória do processo, como nos jobs comuns.

Um erro de job (ex.: chave recusada) **para a fila**: insistir com o próximo
livro só repetiria o erro.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .jobs import JobManager

logger = logging.getLogger("epub_translator.fila")


@dataclass
class ItemFila:
    origem: str
    nome_saida: str
    estado: str = "aguardando"  # aguardando | traduzindo | concluido | erro | pulado
    job_id: str | None = None
    rodadas: int = 0
    falhas: int = 0
    destino: str | None = None
    erro: str | None = None
    inicio: float | None = None
    fim: float | None = None


@dataclass
class Fila:
    itens: list[ItemFila]
    destino: str
    rodadas_extras: int
    estado: str = "rodando"  # rodando | concluida | parada
    erro: str | None = None
    _thread: threading.Thread | None = field(default=None, repr=False)

    def resumo(self) -> dict[str, Any]:
        return {
            "estado": self.estado,
            "erro": self.erro,
            "destino": self.destino,
            "itens": [asdict(item) for item in self.itens],
        }


def nome_traduzido(origem: str) -> str:
    return f"{Path(origem).stem} (traduzido).epub"


def rodar_fila(fila: Fila, manager: JobManager, config: dict[str, Any]) -> None:
    """Processa a fila inteira (roda numa thread própria)."""
    Path(fila.destino).mkdir(parents=True, exist_ok=True)
    for item in fila.itens:
        # Um job por vez: o provedor (Ollama Cloud) não atende dois modelos em
        # paralelo direito — espera qualquer outro job em andamento terminar.
        # Só conta thread viva: um job.json "running" de um servidor reiniciado
        # não pode travar a fila.
        while any(not manager.aguardar(j, 0) for j in manager.listar()):
            time.sleep(15)
        item.estado = "traduzindo"
        item.inicio = time.time()
        try:
            job = manager.criar(item.origem, {**config, "output_name": item.nome_saida})
            item.job_id = job.id
            manager.iniciar(job)
            manager.aguardar(job)

            # Retradução das falhas: só os blocos sem tradução voltam ao modelo.
            while job.state == "done" and job.failed + job.discarded > 0 and item.rodadas < fila.rodadas_extras:
                item.rodadas += 1
                manager.retomar(job)
                manager.aguardar(job)

            if job.state != "done":
                item.estado = "erro"
                item.erro = job.error or f"job terminou em '{job.state}'"
                fila.estado = "parada"
                fila.erro = f"{Path(item.origem).name}: {item.erro}"
                logger.error("Fila parada: %s", fila.erro)
                return

            item.falhas = job.failed + job.discarded
            destino = Path(fila.destino) / item.nome_saida
            shutil.copyfile(job.out, destino)
            item.destino = str(destino)
            item.estado = "concluido"
        except Exception as exc:  # noqa: BLE001 — a fila registra e para
            item.estado = "erro"
            item.erro = str(exc)
            fila.estado = "parada"
            fila.erro = f"{Path(item.origem).name}: {exc}"
            logger.exception("Fila parada")
            return
        finally:
            item.fim = time.time()
    fila.estado = "concluida"
