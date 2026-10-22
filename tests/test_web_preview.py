"""Testes da camada de preview (web/preview.py).

Cobre:

- listagem dos capítulos na ordem do spine, com o título vindo do NCX;
- busca de um capítulo pelo caminho;
- geração do HTML de um capítulo: ``data-block-index`` 1-based sem buracos,
  tags inline preservadas, nenhum marcador ``[[`` remanescente;
- sanitização: sem ``<script>``, ``<style>``, handlers ``on*`` nem
  ``javascript:``;
- reescrita das URLs de imagem para a rota de asset do job;
- integridade: o HTML devolvido é XML bem-formado.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from lxml import etree

from epub_translator.epub_io import EpubReader
from web.preview import capitulo_por_path, html_preview, listar_capitulos


# ── Constantes ──────────────────────────────────────────────────────────────

RAIZ = Path(__file__).resolve().parent.parent
EPUB_TESTE = RAIZ / "fixtures" / "test_input.epub"
EPUB_VALIDACAO = RAIZ / "validacao.epub"

# Capítulo de texto do EPUB de validação (Chapter 1).
CAPITULO_VALIDACAO = "OEBPS/Text/section-0005.html"

# Título desse capítulo no NCX do EPUB de validação.
TITULO_VALIDACAO = "Chapter 1: The Diary (Part 1)"

# Contagens medidas no capítulo de validação (ver TASK-022).
BLOCOS_VALIDACAO = 229
TAGS_P_VALIDACAO = 229
TAGS_SPAN_VALIDACAO = 198

RE_INDEX = re.compile(r'data-block-index="(\d+)"')


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def data_teste() -> EpubReader:
    """Conteúdo do EPUB pequeno de teste."""
    if not EPUB_TESTE.exists():
        pytest.skip(f"EPUB de teste ausente: {EPUB_TESTE}")
    return EpubReader(str(EPUB_TESTE)).read()


@pytest.fixture(scope="module")
def data_validacao() -> EpubReader:
    """Conteúdo do EPUB de validação (o livro real do usuário)."""
    if not EPUB_VALIDACAO.exists():
        pytest.skip(f"EPUB de validação ausente: {EPUB_VALIDACAO}")
    return EpubReader(str(EPUB_VALIDACAO)).read()


# ── Listagem de capítulos ────────────────────────────────────────────────────

def test_listar_capitulos_esta_na_ordem_do_spine(data_teste: EpubReader) -> None:
    """A lista tem um capítulo por arquivo de texto, na ordem do spine."""
    capitulos = listar_capitulos(data_teste)

    assert len(capitulos) == len(data_teste.text_files)
    assert [c["path"] for c in capitulos] == list(data_teste.text_files)
    assert [c["index"] for c in capitulos] == list(range(len(capitulos)))


def test_listar_capitulos_tem_os_campos_do_contrato(data_teste: EpubReader) -> None:
    """Cada capítulo tem index, path, title e blocks."""
    for capitulo in listar_capitulos(data_teste):
        assert set(capitulo) == {"index", "path", "title", "blocks"}
        assert isinstance(capitulo["index"], int)
        assert isinstance(capitulo["path"], str)
        assert isinstance(capitulo["title"], str)
        assert isinstance(capitulo["blocks"], int)
        assert capitulo["title"]


def test_titulos_vem_do_ncx(data_teste: EpubReader) -> None:
    """Os títulos do EPUB de teste vêm do NCX ('Chapter 1', 'Chapter 2')."""
    capitulos = listar_capitulos(data_teste)

    assert capitulos[0]["title"] == "Chapter 1"
    assert capitulos[1]["title"] == "Chapter 2"


def test_titulos_vem_do_ncx_no_epub_de_validacao(data_validacao: EpubReader) -> None:
    """No livro real o título do Chapter 1 vem do NCX (sem fragmento #...)."""
    capitulos = listar_capitulos(data_validacao)

    assert len(capitulos) == len(data_validacao.text_files) == 24

    # O NCX aponta para "Text/section-0005.html#auto_bookmark_toc_top".
    alvo = next(c for c in capitulos if c["path"] == CAPITULO_VALIDACAO)
    assert alvo["title"] == TITULO_VALIDACAO

    # Os demais títulos do NCX também aparecem (ex.: "Table of Contents").
    titulos = {c["title"] for c in capitulos}
    assert "Table of Contents" in titulos


def test_numero_de_blocos_por_capitulo(data_validacao: EpubReader) -> None:
    """O campo ``blocks`` conta os blocos do capítulo."""
    capitulos = listar_capitulos(data_validacao)

    alvo = next(c for c in capitulos if c["path"] == CAPITULO_VALIDACAO)
    assert alvo["blocks"] == BLOCOS_VALIDACAO


def test_listar_capitulos_aceita_epub_sem_ncx() -> None:
    """Sem NCX, o título cai para o nome do arquivo sem extensão."""
    from epub_translator.epub_io import EpubData

    data = EpubData(
        files={"OEBPS/Text/cap1.html": b"<html><body><p>Oi</p></body></html>"},
        text_files=["OEBPS/Text/cap1.html"],
    )

    capitulos = listar_capitulos(data)

    assert capitulos[0]["title"] == "cap1"
    assert capitulos[0]["blocks"] == 1


# ── Busca por caminho ────────────────────────────────────────────────────────

def test_capitulo_por_path_encontra(data_validacao: EpubReader) -> None:
    """A busca pelo caminho real do zip devolve o capítulo."""
    capitulo = capitulo_por_path(data_validacao, CAPITULO_VALIDACAO)

    assert capitulo is not None
    assert capitulo["path"] == CAPITULO_VALIDACAO
    assert capitulo["title"] == TITULO_VALIDACAO
    assert capitulo["blocks"] == BLOCOS_VALIDACAO


def test_capitulo_por_path_devolve_none_quando_nao_existe(
    data_validacao: EpubReader,
) -> None:
    """Caminho desconhecido devolve None."""
    assert capitulo_por_path(data_validacao, "OEBPS/Text/nao-existe.html") is None


# ── HTML do preview ──────────────────────────────────────────────────────────

def test_html_preview_tem_um_indice_por_bloco(data_validacao: EpubReader) -> None:
    """Cada bloco tem um data-block-index 1..N, sem buracos."""
    html = html_preview(data_validacao, CAPITULO_VALIDACAO, "original")

    indices = [int(m) for m in RE_INDEX.findall(html)]

    assert len(indices) == BLOCOS_VALIDACAO
    assert indices == list(range(1, BLOCOS_VALIDACAO + 1))


def test_html_preview_preserva_as_tags_inline(data_validacao: EpubReader) -> None:
    """As tags <p> e <span> do original sobrevivem ao preview."""
    html = html_preview(data_validacao, CAPITULO_VALIDACAO, "original")

    assert len(re.findall(r"<p[ >]", html)) == TAGS_P_VALIDACAO
    assert len(re.findall(r"<span[ >]", html)) == TAGS_SPAN_VALIDACAO


def test_html_preview_nao_deixa_marcadores(data_validacao: EpubReader) -> None:
    """Nenhum marcador [[n]] sobrevive no HTML (o replay os consumiu)."""
    html = html_preview(data_validacao, CAPITULO_VALIDACAO, "original")

    assert "[[" not in html
    assert "]]" not in html


def test_html_preview_nao_tem_html_head_ou_body(data_validacao: EpubReader) -> None:
    """O preview devolve só o conteúdo do capítulo."""
    html = html_preview(data_validacao, CAPITULO_VALIDACAO, "original")

    assert "<html" not in html
    assert "<head" not in html
    assert "<body" not in html


def test_html_preview_do_lado_traduzido(data_validacao: EpubReader) -> None:
    """No lado traduzido, o texto traduzido substitui o original."""
    html_original = html_preview(data_validacao, CAPITULO_VALIDACAO, "original")
    html_traduzido = html_preview(
        data_validacao,
        CAPITULO_VALIDACAO,
        "translated",
        traducoes={1: "Primeiro parágrafo traduzido", 2: "Segundo parágrafo"},
    )

    assert "Primeiro parágrafo traduzido" in html_traduzido
    assert "Segundo parágrafo" in html_traduzido
    # O resto do capítulo continua igual ao original.
    assert len(RE_INDEX.findall(html_traduzido)) == BLOCOS_VALIDACAO
    assert len(html_original) > 0


def test_html_preview_traducoes_incompletas_mantem_o_original(
    data_validacao: EpubReader,
) -> None:
    """Índices sem tradução mantêm o texto original."""
    html = html_preview(
        data_validacao,
        CAPITULO_VALIDACAO,
        "translated",
        traducoes={5: "Tradução do quinto"},
    )

    assert "Tradução do quinto" in html
    assert "I</span><span" in html  # o 3º parágrafo segue original


def test_html_preview_aceita_ausencia_de_traducoes(
    data_validacao: EpubReader,
) -> None:
    """Sem traduções, o lado traduzido é o original com os blocos marcados
    como pendentes (``data-pending``)."""
    html_original = html_preview(data_validacao, CAPITULO_VALIDACAO, "original")
    html_traduzido = html_preview(
        data_validacao, CAPITULO_VALIDACAO, "translated"
    )

    assert 'data-pending="1"' in html_traduzido
    assert html_traduzido.replace(' data-pending="1"', "") == html_original


def test_html_preview_rejeita_lado_invalido(data_validacao: EpubReader) -> None:
    """Um lado desconhecido é erro com mensagem em pt-BR."""
    with pytest.raises(ValueError) as exc:
        html_preview(data_validacao, CAPITULO_VALIDACAO, "lado-errado")

    assert "lado" in str(exc.value).lower()


def test_html_preview_rejeita_capitulo_inexistente(
    data_validacao: EpubReader,
) -> None:
    """Um capítulo que não existe é erro com mensagem em pt-BR."""
    with pytest.raises(ValueError) as exc:
        html_preview(data_validacao, "OEBPS/Text/nao-existe.html", "original")

    assert "não encontrado" in str(exc.value)


# ── Sanitização ──────────────────────────────────────────────────────────────

def test_html_preview_nao_tem_script_nem_style(
    data_validacao: EpubReader,
) -> None:
    """O preview não contém <script> nem <style>."""
    html = html_preview(data_validacao, CAPITULO_VALIDACAO, "original")

    assert "<script" not in html
    assert "<style" not in html


def test_html_preview_sanitiza_conteudo_malicioso() -> None:
    """Script, handlers e URLs perigosas são removidos do preview."""
    from epub_translator.epub_io import EpubData

    malicioso = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>x</title></head>
<body>
<script>alert('xss')</script>
<style>body { color: red; }</style>
<noscript>Ative o JS</noscript>
<iframe src="https://exemplo.test"></iframe>
<object data="x.swf"></object>
<embed src="x.swf"></embed>
<applet code="x.class"></applet>
<base href="https://exemplo.test/"/>
<meta http-equiv="refresh" content="0;url=https://exemplo.test"/>
<link rel="stylesheet" href="../Styles/styles.css"/>
<form action="javascript:alert(1)"><input type="text"/></form>
<p onclick="alert(1)" onmouseover="alert(2)">Texto perigoso</p>
<p><a href="javascript:alert(3)">Link malicioso</a></p>
<p><a href="https://exemplo.test">Link bom</a></p>
<p><img src="data:image/svg+xml;base64,AAAA" alt="x"/></p>
<p><a href="pagina.html" target="_blank">Link interno</a></p>
</body>
</html>"""

    data = EpubData(
        files={"OEBPS/Text/cap.html": malicioso},
        text_files=["OEBPS/Text/cap.html"],
    )

    html = html_preview(data, "OEBPS/Text/cap.html", "original")

    for proibido in (
        "<script", "<style", "<noscript", "<iframe", "<object", "<embed",
        "<applet", "<base", "<meta", "<link", "<form", "<input",
        "onclick", "onmouseover", "javascript:", "data:image",
        "alert(",
    ):
        assert proibido not in html, f"o preview ainda contém {proibido!r}"

    # O conteúdo legítimo continua lá.
    assert "Texto perigoso" in html
    assert "Link bom" in html
    assert "https://exemplo.test" in html
    assert "pagina.html" in html


def test_html_preview_remove_somente_os_handlers_perigosos(
    data_teste: EpubReader,
) -> None:
    """Atributos normais (class, href, id) sobrevivem à sanitização."""
    html = html_preview(data_teste, "OEBPS/Text/section-0001.html", "original")

    assert "class=" in html
    assert "highlight" in html
    assert "footnote1" in html


# ── URLs de imagem ───────────────────────────────────────────────────────────

def test_urls_de_imagem_sao_reescritas_para_a_rota_de_asset(
    data_validacao: EpubReader,
) -> None:
    """A imagem relativa do capítulo vira /api/jobs/{id}/asset?path=..."""
    html = html_preview(
        data_validacao,
        "OEBPS/Text/section-0001.html",
        "original",
        job_id="job-abc",
    )

    assert "../Images/COLORGALLERY_.jpg" not in html
    assert "/api/jobs/job-abc/asset?path=" in html
    assert "OEBPS%2FImages%2FCOLORGALLERY_.jpg" in html


def test_urls_de_imagem_ficam_iguais_sem_job_id(
    data_validacao: EpubReader,
) -> None:
    """Sem job_id, a URL da imagem não é reescrita."""
    html = html_preview(
        data_validacao, "OEBPS/Text/section-0001.html", "original"
    )

    assert "../Images/COLORGALLERY_.jpg" in html
    assert "/api/jobs/" not in html


def test_imagem_em_svg_e_reescrita(data_validacao: EpubReader) -> None:
    """O <image xlink:href> do SVG da capa também é reescrito."""
    html = html_preview(
        data_validacao,
        "OEBPS/Text/CoverPage.html",
        "original",
        job_id="job-abc",
    )

    assert "../Images/CoverDesign.jpg" not in html
    assert "/api/jobs/job-abc/asset?path=OEBPS%2FImages%2FCoverDesign.jpg" in html


# ── Integridade ──────────────────────────────────────────────────────────────

def test_html_preview_e_xml_bem_formado(data_validacao: EpubReader) -> None:
    """O fragmento devolvido é XML bem-formado (parse estrito do lxml)."""
    html = html_preview(data_validacao, CAPITULO_VALIDACAO, "original")

    raiz = etree.fromstring(
        f"<preview>{html}</preview>".encode("utf-8"),
        etree.XMLParser(resolve_entities=False, no_network=True),
    )

    assert raiz is not None


def test_html_preview_e_xml_bem_formado_em_todos_os_capitulos(
    data_validacao: EpubReader,
) -> None:
    """Todos os capítulos do livro produzem XML bem-formado."""
    for caminho in data_validacao.text_files:
        html = html_preview(data_validacao, caminho, "original")

        etree.fromstring(
            f"<preview>{html}</preview>".encode("utf-8"),
            etree.XMLParser(resolve_entities=False, no_network=True),
        )


def test_html_preview_nao_depende_de_fastapi() -> None:
    """O módulo é uma biblioteca pura: não importa FastAPI nem jobs."""
    import ast
    import web.preview as preview

    arvore = ast.parse(Path(preview.__file__).read_text(encoding="utf-8"))
    importados: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            importados.update(alias.name.split(".")[0] for alias in no.names)
        elif isinstance(no, ast.ImportFrom) and no.module:
            importados.add(no.module.split(".")[0])

    assert "fastapi" not in importados
    assert "starlette" not in importados
    assert "jobs" not in importados
