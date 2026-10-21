"""Testes dos hooks de progresso e pausa do tradutor (TASK-020).

Cobre:
- ``ProgressEvent``: campos, ordem e default;
- ``progress_cb``: um evento por bloco concluído, nos 4 desfechos
  (traduzido/cache/falhou/descartado), com ``done``/``total`` coerentes;
- ``progress_cb`` que levanta exceção: a tradução NÃO pode ser derrubada;
- ``pause_cb``: pausa ENTRE LOTES, Report parcial e EPUB parcial em disco;
- a pausa NÃO perde trabalho: a 2ª execução reusa o cache.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import fields
from pathlib import Path

import pytest

from epub_translator.lmstudio_client import MockClient
from epub_translator.prompts import separator_index
from epub_translator.translator import (
    ProgressEvent,
    TranslationConfig,
    translate_epub,
)


# ── Fixtures e helpers ───────────────────────────────────────────────────────


def _escrever_epub(caminho: Path, capitulos: list[int]) -> None:
    """Cria um EPUB com um capítulo por item da lista (nº de <p> em cada).

    O primeiro parágrafo de cada capítulo tem uma tag inline (``<b>``) —
    é o que gera os marcadores ``[[n]]``/``[[/n]]``.
    """
    with zipfile.ZipFile(caminho, "w") as zf:
        zf.writestr(
            "mimetype",
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles>'
            "</container>",
        )
        itens = "".join(
            f'<item id="ch{i}" href="ch{i}.xhtml" '
            'media-type="application/xhtml+xml"/>'
            for i in range(1, len(capitulos) + 1)
        )
        itemrefs = "".join(
            f'<itemref idref="ch{i}"/>' for i in range(1, len(capitulos) + 1)
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="uid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="uid">urn:uuid:00000000-0000-0000-0000-000000000001'
            "</dc:identifier>"
            "<dc:language>en</dc:language>"
            "<dc:title>Test Book</dc:title>"
            '<meta property="dcterms:modified">2025-01-01T00:00:00Z</meta>'
            "</metadata>"
            f"<manifest>{itens}</manifest>"
            f"<spine>{itemrefs}</spine>"
            "</package>",
        )
        for i, n_paragrafos in enumerate(capitulos, start=1):
            paragrafos = []
            for j in range(1, n_paragrafos + 1):
                if j == 1:
                    paragrafos.append(
                        f"<p>Paragrafo <b>forte</b> {j} do capitulo {i}</p>"
                    )
                else:
                    paragrafos.append(f"<p>Paragrafo {j} do capitulo {i}</p>")
            zf.writestr(
                f"OEBPS/ch{i}.xhtml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<!DOCTYPE html>"
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                f"<head><title>Chapter {i}</title></head>"
                f"<body>{''.join(paragrafos)}</body>"
                "</html>",
            )


@pytest.fixture()
def epub_1_capitulo(tmp_path: Path) -> Path:
    """EPUB com 1 capítulo de 6 blocos."""
    caminho = tmp_path / "in.epub"
    _escrever_epub(caminho, [6])
    return caminho


@pytest.fixture()
def epub_2_capitulos(tmp_path: Path) -> Path:
    """EPUB com 2 capítulos de 4 blocos cada."""
    caminho = tmp_path / "in.epub"
    _escrever_epub(caminho, [4, 4])
    return caminho


@pytest.fixture(autouse=True)
def _fechar_handlers(tmp_path: Path) -> None:
    """Fecha os handlers de log abertos pelo tradutor (um por output)."""
    yield
    log = logging.getLogger("epub_translator")
    for handler in list(log.handlers):
        base = getattr(handler, "baseFilename", "")
        if base.startswith(str(tmp_path)):
            handler.close()
            log.removeHandler(handler)


class ClienteRespostaCrua:
    """Cliente que devolve a resposta CRUA (sem mascarar os marcadores).

    O ``MockClient`` mascara os marcadores ``[[n]]``/``[[/n]]`` por design
    (nunca corrompe marcador). Para exercitar o desfecho "descartado" é
    preciso um cliente que devolva a resposta com os marcadores quebrados.
    """

    def __init__(self, transform) -> None:
        self._transform = transform

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
        ultima = ""
        for msg in messages:
            if msg.get("role") == "user":
                ultima = msg.get("content", "")

        partes: list[str] = []
        indice: int | None = None
        linhas: list[str] = []
        for linha in ultima.split("\n"):
            sep = separator_index(linha)
            if sep is not None:
                if indice is not None:
                    partes.append(f"[{indice}]")
                    partes.append(self._transform("\n".join(linhas)))
                indice = sep
                linhas = []
            elif indice is not None:
                linhas.append(linha)
        if indice is not None:
            partes.append(f"[{indice}]")
            partes.append(self._transform("\n".join(linhas)))

        return "\n".join(partes)

    @property
    def is_mock(self) -> bool:
        return True


class ClienteQueFalha:
    """Cliente que sempre falha — exercita o desfecho "falhou"."""

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


# ── ProgressEvent ────────────────────────────────────────────────────────────


class TestProgressEvent:
    """Estrutura do evento de progresso."""

    def test_campos_e_ordem(self) -> None:
        """Os campos são exatamente os do contrato, nesta ordem."""
        nomes = [f.name for f in fields(ProgressEvent)]
        assert nomes == [
            "file",
            "block_index",
            "total_blocks",
            "done",
            "total",
            "elapsed_s",
            "status",
            "detail",
        ]

    def test_detail_tem_default_vazio(self) -> None:
        """``detail`` é opcional e vem vazio."""
        evento = ProgressEvent(
            file="OEBPS/ch1.xhtml",
            block_index=1,
            total_blocks=3,
            done=1,
            total=3,
            elapsed_s=0.0,
            status="traduzido",
        )
        assert evento.detail == ""

    def test_config_aceita_os_hooks_com_default_none(self) -> None:
        """TranslationConfig(src=..., output=...) continua funcionando."""
        cfg = TranslationConfig(src="a.epub", output="b.epub")
        assert cfg.progress_cb is None
        assert cfg.pause_cb is None


# ── Hook de progresso ────────────────────────────────────────────────────────


class TestProgressCallback:
    """``progress_cb`` é chamado a cada bloco concluído."""

    def test_um_evento_por_bloco(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Cada bloco gera exatamente um evento, com os campos coerentes."""
        eventos: list[ProgressEvent] = []
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=False,
            progress_cb=eventos.append,
        )
        report = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )

        assert report.translated_blocks == 6
        assert len(eventos) == report.total_blocks == 6

        for i, ev in enumerate(eventos, start=1):
            assert ev.file == "OEBPS/ch1.xhtml"
            assert ev.block_index == i
            assert ev.total_blocks == 6
            assert ev.total == 6
            assert ev.done == i
            assert ev.status == "traduzido"
            assert ev.detail == ""
            assert ev.elapsed_s >= 0.0

    def test_total_e_o_do_livro_e_nao_do_arquivo(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """``total`` soma os blocos do livro; ``total_blocks`` é do arquivo."""
        eventos: list[ProgressEvent] = []
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=False,
            progress_cb=eventos.append,
        )
        translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient()
        )

        assert len(eventos) == 8
        assert {ev.total for ev in eventos} == {8}
        assert {ev.total_blocks for ev in eventos} == {4}
        assert {ev.file for ev in eventos} == {"OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml"}

        # O índice do bloco reinicia a cada arquivo, mas ``done`` acumula.
        por_arquivo: dict[str, list[ProgressEvent]] = {}
        for ev in eventos:
            por_arquivo.setdefault(ev.file, []).append(ev)
        assert [ev.block_index for ev in por_arquivo["OEBPS/ch1.xhtml"]] == [1, 2, 3, 4]
        assert [ev.block_index for ev in por_arquivo["OEBPS/ch2.xhtml"]] == [1, 2, 3, 4]
        assert [ev.done for ev in por_arquivo["OEBPS/ch1.xhtml"]] == [1, 2, 3, 4]
        assert [ev.done for ev in por_arquivo["OEBPS/ch2.xhtml"]] == [5, 6, 7, 8]

    def test_status_cache_na_segunda_execucao(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Na 2ª execução os blocos vêm do cache (status "cache")."""
        out = tmp_path / "out.epub"
        cache = tmp_path / "cache.json"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
        )
        translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )

        eventos: list[ProgressEvent] = []
        cfg2 = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
            progress_cb=eventos.append,
        )
        report = translate_epub(
            str(epub_1_capitulo), str(out), cfg2, client=MockClient()
        )

        assert report.cached_blocks == 6
        assert report.translated_blocks == 0
        assert len(eventos) == 6
        assert {ev.status for ev in eventos} == {"cache"}
        assert [ev.done for ev in eventos] == [1, 2, 3, 4, 5, 6]

    def test_status_falhou_com_detalhe(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Resposta malformada vira status "falhou", com o motivo no detail."""
        eventos: list[ProgressEvent] = []
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=False,
            max_attempts=1,
            progress_cb=eventos.append,
        )
        report = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=ClienteQueFalha()
        )

        assert report.failed_blocks == 6
        assert len(eventos) == 6
        assert {ev.status for ev in eventos} == {"falhou"}
        assert all(ev.detail for ev in eventos)
        assert [ev.done for ev in eventos] == [1, 2, 3, 4, 5, 6]

    def test_status_descartado_com_detalhe(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Marcador quebrado vira status "descartado", com o motivo."""
        def corrompe(texto: str) -> str:
            return re.sub(r"\[\[/\d+\]\]", "", texto)

        eventos: list[ProgressEvent] = []
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=False,
            progress_cb=eventos.append,
        )
        report = translate_epub(
            str(epub_1_capitulo),
            str(out),
            cfg,
            client=ClienteRespostaCrua(corrompe),
        )

        # Só o bloco com tag inline tem marcadores: 1 descartado, 5 traduzidos.
        assert report.discarded_blocks == 1
        assert report.translated_blocks == 5
        assert len(eventos) == 6
        assert [ev.status for ev in eventos].count("descartado") == 1
        descartado = next(ev for ev in eventos if ev.status == "descartado")
        assert descartado.detail
        assert descartado.block_index == 1

    def test_progress_cb_que_levanta_excecao_nao_derruba(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Uma exceção no callback é logada e a tradução continua."""
        chamadas: list[int] = []

        def cb_quebrado(evento: ProgressEvent) -> None:
            chamadas.append(evento.block_index)
            raise RuntimeError("falha no consumidor")

        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=False,
            progress_cb=cb_quebrado,
        )
        report = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )

        # O callback foi chamado para todos os blocos...
        assert chamadas == [1, 2, 3, 4, 5, 6]
        # ... e a tradução terminou normalmente.
        assert report.translated_blocks == 6
        assert report.failed_blocks == 0
        assert report.accounted_blocks == report.total_blocks
        assert out.exists()

        # A falha do hook fica no log.
        log = (tmp_path / "out.epub.log").read_text(encoding="utf-8")
        assert "Falha no progress_cb" in log

    def test_capitulo_com_229_blocos_gera_229_eventos(
        self, tmp_path: Path
    ) -> None:
        """Um capítulo grande emite um evento por bloco (229 no total)."""
        epub = tmp_path / "grande.epub"
        _escrever_epub(epub, [229])

        eventos: list[ProgressEvent] = []
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub),
            output=str(out),
            use_cache=False,
            progress_cb=eventos.append,
        )
        report = translate_epub(str(epub), str(out), cfg, client=MockClient())

        assert report.total_blocks == 229
        assert len(eventos) == 229
        assert [ev.block_index for ev in eventos] == list(range(1, 230))
        assert [ev.done for ev in eventos] == list(range(1, 230))
        assert {ev.total for ev in eventos} == {229}

    def test_sem_arquivos_no_progresso_total_zero(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Filtro que não casa nada: nenhum evento, total = 0, sem quebrar."""
        eventos: list[ProgressEvent] = []
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=False,
            only="OEBPS/nao_existe.xhtml",
            progress_cb=eventos.append,
        )
        report = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )

        assert eventos == []
        assert report.total_blocks == 0
        assert out.exists()


# ── Hook de pausa ────────────────────────────────────────────────────────────


class TestPauseCallback:
    """``pause_cb`` para a tradução entre lotes."""

    def test_pausa_apos_o_primeiro_lote(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """A pausa interrompe no fim do 1º lote e devolve um Report parcial."""
        chamadas: list[int] = []

        def pause() -> bool:
            chamadas.append(1)
            return True

        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=False,
            batch_size=2,
            pause_cb=pause,
        )
        report = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )

        # 6 blocos, batch_size=2 => 3 lotes; a pausa vale no 1º.
        assert chamadas == [1]
        assert report.translated_blocks == 2
        assert report.accounted_blocks == 2
        assert report.accounted_blocks < report.total_blocks
        assert report.total_blocks == 6

        # O EPUB parcial existe em disco.
        assert out.exists()
        with zipfile.ZipFile(out) as zf:
            assert "OEBPS/ch1.xhtml" in zf.namelist()

        # A pausa fica registrada no log.
        log = (tmp_path / "out.epub.log").read_text(encoding="utf-8")
        assert "Tradução pausada" in log

    def test_pausa_entre_lotes_e_nao_entre_blocos(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """O pause_cb é consultado uma vez por lote processado."""
        chamadas: list[int] = []

        def pause() -> bool:
            chamadas.append(len(chamadas) + 1)
            return len(chamadas) >= 2

        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=False,
            batch_size=2,
            pause_cb=pause,
        )
        report = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )

        # 2º lote processado e então a pausa: 4 blocos traduzidos.
        assert chamadas == [1, 2]
        assert report.translated_blocks == 4
        assert report.accounted_blocks < report.total_blocks

    def test_pausa_preserva_o_trabalho_feito_no_cache(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Rodar de novo com o mesmo cache_path reusa os blocos pausados."""
        out = tmp_path / "out.epub"
        cache = tmp_path / "cache.json"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
            batch_size=2,
            pause_cb=lambda: True,
        )
        report1 = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )
        assert report1.translated_blocks == 2

        eventos: list[ProgressEvent] = []
        cfg2 = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
            progress_cb=eventos.append,
        )
        report2 = translate_epub(
            str(epub_1_capitulo), str(out), cfg2, client=MockClient()
        )

        # Os blocos da pausa vieram do cache — nada foi re-traduzido.
        assert report2.cached_blocks == 2
        assert report2.cached_blocks > 0
        assert report2.translated_blocks == report2.total_blocks - 2
        assert report2.accounted_blocks == report2.total_blocks

        # E o status "cache" aparece no hook de progresso.
        assert [ev.status for ev in eventos].count("cache") == 2

    def test_blocos_pausados_nao_sao_retraduzidos(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Os blocos pausados vêm do cache: nenhum é traduzido de novo.

        Só os blocos que sobraram (os não alcançados antes da pausa) são
        traduzidos — o critério é que os blocos JÁ traduzidos não voltem
        para o modelo.
        """
        out = tmp_path / "out.epub"
        cache = tmp_path / "cache.json"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
            batch_size=2,
            pause_cb=lambda: True,
        )
        report1 = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )
        assert report1.translated_blocks == 2

        eventos: list[ProgressEvent] = []
        cfg2 = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
            progress_cb=eventos.append,
        )
        report2 = translate_epub(
            str(epub_1_capitulo), str(out), cfg2, client=MockClient()
        )

        assert report2.cached_blocks == 2
        assert report2.cached_blocks > 0
        # Os 2 blocos pausados NÃO foram traduzidos de novo.
        assert report2.translated_blocks == 4
        cacheados = [ev for ev in eventos if ev.status == "cache"]
        assert [ev.block_index for ev in cacheados] == [1, 2]
        traduzidos = [ev for ev in eventos if ev.status == "traduzido"]
        assert [ev.block_index for ev in traduzidos] == [3, 4, 5, 6]

    def test_pausa_que_levanta_excecao_nao_derruba(
        self, epub_1_capitulo: Path, tmp_path: Path
    ) -> None:
        """Uma exceção no pause_cb é logada e a tradução continua."""

        def pause_quebrado() -> bool:
            raise RuntimeError("falha no controle de pausa")

        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_1_capitulo),
            output=str(out),
            use_cache=False,
            pause_cb=pause_quebrado,
        )
        report = translate_epub(
            str(epub_1_capitulo), str(out), cfg, client=MockClient()
        )

        assert report.translated_blocks == report.total_blocks == 6
        log = (tmp_path / "out.epub.log").read_text(encoding="utf-8")
        assert "Falha no pause_cb" in log
