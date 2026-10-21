"""Teste de integração do pipeline completo sobre o EPUB de validação.

Roda o pipeline (extract -> mock -> parse -> replay -> serialize -> write)
sobre ``validacao.epub`` e prova que o EPUB de saída preserva a estrutura do
original: mesmas 51 entradas, ``mimetype`` como primeira entrada ZIP_STORED,
binários byte-idênticos, contagem de tags igual, doctype e declaração XML na
ordem correta, ``dc:language`` atualizado e NCX intacto.

O MockClient é usado SEM transform (identity), então o texto traduzido é
idêntico ao original e a comparação de contagens de tags é estrita.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from lxml import etree

from epub_translator.epub_io import EpubData, read_epub
from epub_translator.lmstudio_client import MockClient
from epub_translator.translator import Report, TranslationConfig, translate_epub


# ── Constantes do EPUB de validação ─────────────────────────────────────────

RAIZ = Path(__file__).resolve().parent.parent
EPUB_VALIDACAO = RAIZ / "validacao.epub"

# XHTML usado nas comparações de conteúdo (capítulo maior do livro).
XHTML_ALVO = "OEBPS/Text/section-0005.html"

# Total de entradas do EPUB de validação (mimetype + container + opf + ...).
TOTAL_ENTRADAS = 51

# Extensões de arquivos que NÃO são traduzidos (precisam sair byte-idênticos).
EXTENSOES_BINARIAS = (".jpg", ".jpeg", ".png", ".gif", ".css")

# Marcadores de tag inline que não podem sobrar no texto final.
RE_MARCADOR = re.compile(r"\[\[/?\d+\]\]")

# Quantos arquivos de texto o pipeline processa (limit do TranslationConfig).
LIMIT_ARQUIVOS = 12


# ── Resultado do pipeline ────────────────────────────────────────────────────

class ResultadoPipeline(NamedTuple):
    """Resultado da execução do pipeline sobre o EPUB de validação."""

    epub: Path
    """Caminho do EPUB gerado."""

    original: EpubData
    """Conteúdo do EPUB original (antes da tradução)."""

    report: Report
    """Relatório do pipeline (estatísticas de blocos)."""


# ── Fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ResultadoPipeline]:
    """Roda o pipeline uma única vez sobre ``validacao.epub``.

    Usa a API (``translate_epub``) — não subprocess — para ser rápido e não
    depender do PATH. O cache e o log ficam no diretório temporário e são
    removidos no final (nada de arquivo residual no repositório).
    """
    if not EPUB_VALIDACAO.exists():
        pytest.skip(f"EPUB de validação ausente: {EPUB_VALIDACAO}")

    tmp_dir = tmp_path_factory.mktemp("validacao")
    out_epub = tmp_dir / "validacao_traduzido.epub"
    cache_path = tmp_dir / "validacao_traduzido.epub.cache.json"
    log_path = tmp_dir / "validacao_traduzido.epub.log"

    original = read_epub(str(EPUB_VALIDACAO))

    cfg = TranslationConfig(
        src=str(EPUB_VALIDACAO),
        output=str(out_epub),
        target_lang="pt-BR",
        limit=LIMIT_ARQUIVOS,
        use_cache=True,
        cache_path=str(cache_path),
    )

    report = translate_epub(
        str(EPUB_VALIDACAO), str(out_epub), cfg, client=MockClient()
    )

    yield ResultadoPipeline(epub=out_epub, original=original, report=report)

    # ── Limpeza: cache, log e handler de logging ficam fora do repo ──
    for handler in list(logging.getLogger("epub_translator").handlers):
        base = getattr(handler, "baseFilename", "")
        if base == os.path.abspath(str(log_path)):
            handler.close()
            logging.getLogger("epub_translator").removeHandler(handler)
    for leftover in (cache_path, log_path):
        leftover.unlink(missing_ok=True)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _countar_tags(xhtml_bytes: bytes) -> tuple[int, int]:
    """Conta <p> e <span> de um XHTML (parse estrito, sem recover)."""
    root = etree.fromstring(xhtml_bytes)
    paragrafos = root.xpath("//*[local-name()='p']")
    spans = root.xpath("//*[local-name()='span']")
    return len(paragrafos), len(spans)


def _textos_opf(opf_bytes: bytes) -> tuple[str, str]:
    """Devolve (dc:title, dc:language) do OPF."""
    root = etree.fromstring(opf_bytes)
    titulo = root.xpath(
        "string(//*[local-name()='metadata']/*[local-name()='title'])"
    )
    idioma = root.xpath(
        "string(//*[local-name()='metadata']/*[local-name()='language'])"
    )
    return titulo, idioma


# ── Testes: o pipeline roda sem falhas ───────────────────────────────────────

def test_pipeline_roda_sem_falhas(pipeline: ResultadoPipeline) -> None:
    """O pipeline traduz os 12 arquivos do limit sem nenhuma falha.

    ``translated_blocks + cached_blocks`` cobre todos os blocos: o cache
    pode reservar alguns blocos já traduzidos (a chave do cache inclui o
    índice do bloco dentro do lote), mas nada pode falhar.
    """
    assert pipeline.report.total_blocks > 0
    assert pipeline.report.failed_blocks == 0
    assert (
        pipeline.report.translated_blocks + pipeline.report.cached_blocks
        == pipeline.report.total_blocks
    )
    assert len(pipeline.report.files_processed) == LIMIT_ARQUIVOS


# ── Testes: integridade do zip de saída ──────────────────────────────────────

def test_epub_de_saida_existe_e_e_zip_valido(
    pipeline: ResultadoPipeline,
) -> None:
    """O arquivo de saída existe e é um zip legível."""
    assert pipeline.epub.exists()
    assert pipeline.epub.stat().st_size > 0
    assert zipfile.is_zipfile(pipeline.epub)
    with zipfile.ZipFile(pipeline.epub) as zf:
        assert zf.testzip() is None
        assert zf.namelist()


def test_numero_e_nomes_das_entradas_preservados(
    pipeline: ResultadoPipeline,
) -> None:
    """A saída tem as mesmas 51 entradas, com os mesmos nomes do original."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        nomes = zf.namelist()
    with zipfile.ZipFile(EPUB_VALIDACAO) as zf_orig:
        nomes_orig = zf_orig.namelist()

    assert len(nomes) == TOTAL_ENTRADAS
    assert len(nomes_orig) == TOTAL_ENTRADAS
    assert set(nomes) == set(nomes_orig)


def test_mimetype_e_primeira_entrada_e_esta_armazenado(
    pipeline: ResultadoPipeline,
) -> None:
    """``mimetype`` é a primeira entrada do zip e usa ZIP_STORED."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        nomes = zf.namelist()
        info = zf.getinfo("mimetype")
        conteudo = zf.read("mimetype")

    assert nomes[0] == "mimetype"
    assert info.compress_type == zipfile.ZIP_STORED
    assert conteudo == b"application/epub+zip"


def test_nao_ha_nomes_duplicados(pipeline: ResultadoPipeline) -> None:
    """Nenhuma entrada aparece duas vezes no zip de saída."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        nomes = zf.namelist()
    assert len(nomes) == len(set(nomes))


def test_binarios_sao_byte_identicos(pipeline: ResultadoPipeline) -> None:
    """Imagens e CSS do original saem intactos (byte a byte) na saída."""
    with zipfile.ZipFile(pipeline.epub) as zf_out:
        with zipfile.ZipFile(EPUB_VALIDACAO) as zf_orig:
            binarios = [
                nome for nome in zf_orig.namelist()
                if nome.lower().endswith(EXTENSOES_BINARIAS)
            ]
            assert binarios, "O EPUB de validação não tem binários para comparar"
            for nome in binarios:
                assert zf_out.read(nome) == zf_orig.read(nome), nome


# ── Testes: conteúdo XHTML traduzido ─────────────────────────────────────────

def test_xhtml_traduzido_parseia_sem_erro(pipeline: ResultadoPipeline) -> None:
    """O XHTML traduzido é XML válido (parse estrito, sem recover)."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        xhtml = zf.read(XHTML_ALVO)

    root = etree.fromstring(xhtml)  # levanta XMLSyntaxError se corrompido
    assert root is not None


def test_contagem_de_paragrafos_e_spans_preservada(
    pipeline: ResultadoPipeline,
) -> None:
    """A quantidade de <p> e <span> é a mesma do original."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        xhtml_saida = zf.read(XHTML_ALVO)
    with zipfile.ZipFile(EPUB_VALIDACAO) as zf:
        xhtml_original = zf.read(XHTML_ALVO)

    assert _countar_tags(xhtml_saida) == _countar_tags(xhtml_original)
    assert _countar_tags(xhtml_saida) == (229, 198)


def test_nao_sobram_marcadores_inline_no_texto_traduzido(
    pipeline: ResultadoPipeline,
) -> None:
    """Nenhum marcador [[n]]/[[/n]] sobra no XHTML traduzido."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        for nome in zf.namelist():
            if not nome.endswith((".html", ".xhtml")):
                continue
            texto = zf.read(nome).decode("utf-8")
            assert not RE_MARCADOR.search(texto), nome


def test_doctype_xhtml_preservado_e_declaracao_xml_primeiro(
    pipeline: ResultadoPipeline,
) -> None:
    """A declaração XML vem primeiro e o doctype XHTML 1.1 é preservado."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        xhtml = zf.read(XHTML_ALVO)

    assert xhtml.startswith(b"<?xml")
    # O doctype vem logo depois da declaração XML, ainda no início do arquivo.
    cabecalho = xhtml[:400].decode("utf-8")
    assert "<!DOCTYPE html PUBLIC" in cabecalho
    assert "XHTML 1.1" in cabecalho
    assert "xhtml11.dtd" in cabecalho
    assert cabecalho.index("<?xml") < cabecalho.index("<!DOCTYPE")


# ── Testes: metadados (OPF) e índice (NCX) ───────────────────────────────────

def test_opf_de_saida_tem_dc_language_pt_br(
    pipeline: ResultadoPipeline,
) -> None:
    """O dc:language do OPF gravado na saída é o idioma alvo."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        opf = zf.read(pipeline.original.opf_path)

    _, idioma = _textos_opf(opf)
    assert idioma == "pt-BR"


def test_dc_title_preservado_no_opf_de_saida(
    pipeline: ResultadoPipeline,
) -> None:
    """O dc:title do OPF de saída é igual ao do original (não é traduzido)."""
    with zipfile.ZipFile(pipeline.epub) as zf:
        opf_saida = zf.read(pipeline.original.opf_path)
    with zipfile.ZipFile(EPUB_VALIDACAO) as zf:
        opf_original = zf.read(pipeline.original.opf_path)

    titulo_saida, _ = _textos_opf(opf_saida)
    titulo_original, _ = _textos_opf(opf_original)

    assert titulo_saida == titulo_original
    assert titulo_saida == pipeline.original.title
    assert titulo_saida


def test_ncx_de_saida_preserva_navmap(pipeline: ResultadoPipeline) -> None:
    """O NCX de saída continua com o elemento navMap (índice intacto)."""
    assert pipeline.original.ncx_paths, "O EPUB de validação não tem NCX"

    with zipfile.ZipFile(pipeline.epub) as zf:
        for ncx_path in pipeline.original.ncx_paths:
            ncx = zf.read(ncx_path)
            root = etree.fromstring(ncx)
            assert root.xpath("//*[local-name()='navMap']"), ncx_path
