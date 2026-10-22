"""Preview de capítulos de EPUB para a interface web.

Biblioteca **pura**: não importa FastAPI e não conhece jobs. Ela:

1. lista os capítulos na ordem do **spine** (``data.text_files``), com o
   título de cada um resolvido pelo **NCX**;
2. gera o HTML sanitizado de **um** capítulo, com ``data-block-index``
   1-based em cada elemento de bloco, para a UI alinhar o parágrafo original
   com o traduzido.

Estratégia de alinhamento (validada empiricamente em ``validacao.epub``)::

    parse_xhtml → extract_blocks → data-block-index → replay → sanitizar →
    serialize_xhtml → conteúdo do <body>

O ``replay`` reconstrói as tags inline a partir dos marcadores ``[[n]]`` que
o ``extract_blocks`` deixou no texto — por isso o lado "original" passa pelo
mesmo caminho do lado "translated" (o texto original também é um texto com
marcadores).

O conteúdo vem de um EPUB do usuário (entrada NÃO confiável): todo o HTML
gerado é sanitizado (elementos perigosos removidos, handlers ``on*``
removidos, URLs ``javascript:``/``data:`` removidas).
"""

from __future__ import annotations

import posixpath
from typing import Any
from urllib.parse import quote

from lxml import etree

from epub_translator.blocks import extract_blocks, replay
from epub_translator.epub_io import (
    EpubData,
    parse_xhtml,
    resolve_href,
    serialize_xhtml,
)


# ── Constantes ───────────────────────────────────────────────────────────────

LADO_ORIGINAL = "original"
"""Lado do preview com o texto original do EPUB."""

LADO_TRADUZIDO = "translated"
"""Lado do preview com o texto traduzido."""

_LADOS_VALIDOS: frozenset[str] = frozenset((LADO_ORIGINAL, LADO_TRADUZIDO))

# Elementos removidos do HTML do preview (entrada não confiável).
_ELEMENTOS_PERIGOSOS: frozenset[str] = frozenset((
    "script", "style", "noscript", "iframe", "object", "embed",
    "applet", "base", "meta", "link", "form", "input",
))

# Atributos cujo valor é uma URL (verificados contra ``javascript:``/``data:``).
_ATRIBUTOS_DE_URL: frozenset[str] = frozenset((
    "href", "src", "formaction", "action",
))

# Prefixos de URL perigosos (removem o atributo inteiro).
_PREFIXOS_DE_URL_PERIGOSA: tuple[str, ...] = ("javascript:", "data:")

# Atributos removidos sem análise do valor.
_ATRIBUTOS_REMOVIDOS: frozenset[str] = frozenset((
    "srcdoc", "http-equiv",
))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _localname(tag: Any) -> str:
    """Nome local de uma tag, sem o namespace (ex.: ``{ns}p`` → ``p``).

    Tags que não são elementos (comentários, PIs) devolvem string vazia.
    """
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _localname_atributo(nome: str) -> str:
    """Nome local de um atributo, sem namespace nem prefixo.

    Remove o namespace XML (ex.: ``{http://www.w3.org/1999/xlink}href`` →
    ``href``) e também o prefixo literal usado quando o namespace não está
    declarado (ex.: ``xlink:href`` → ``href``).
    """
    if nome.startswith("{"):
        return nome.split("}", 1)[1]
    if ":" in nome:
        return nome.rsplit(":", 1)[1]
    return nome


def _titulo_padrao(caminho: str) -> str:
    """Título de reserva: nome do arquivo sem extensão (ex.: ``section-0005``)."""
    return posixpath.splitext(posixpath.basename(caminho))[0]


def _url_perigosa(valor: str) -> bool:
    """Diz se a URL é perigosa (``javascript:`` ou ``data:``).

    Espaços e caracteres de controle são removidos antes da comparação, para
    pegar o truque clássico de ``java&#9;script:``.
    """
    normalizado = "".join(
        ch for ch in valor if ch not in " \t\r\n\f\v\x00"
    ).lower()
    return normalizado.startswith(_PREFIXOS_DE_URL_PERIGOSA)


# ── Títulos via NCX ──────────────────────────────────────────────────────────

def _titulos_ncx(data: EpubData) -> tuple[dict[str, str], dict[str, str]]:
    """Mapeia os títulos do NCX.

    Returns:
        Tupla ``(por_caminho, por_nome)``:

        - ``por_caminho``: ``{caminho_no_zip: titulo}`` — o ``content/@src``
          do ``navPoint`` resolvido com ``resolve_href(opf_dir, src)`` (sem o
          fragmento ``#...``);
        - ``por_nome``: ``{nome_do_arquivo: titulo}`` — só para os nomes que
          aparecem **uma única vez** no NCX (evita ambiguidade); usado como
          reserva, para EPUBs cujo NCX aponte para um caminho inconsistente
          com o manifesto.
    """
    por_caminho: dict[str, str] = {}
    por_nome: dict[str, str] = {}
    ocorrencias: dict[str, int] = {}

    for ncx_path in data.ncx_paths:
        bruto = data.files.get(ncx_path)
        if bruto is None:
            continue
        try:
            raiz = etree.fromstring(bruto)
        except etree.XMLSyntaxError:
            continue

        diretorio_ncx = posixpath.dirname(ncx_path)

        for no in raiz.iter():
            if not isinstance(no.tag, str) or _localname(no.tag) != "navPoint":
                continue
            titulo = no.xpath(
                "string(*[local-name()='navLabel']/*[local-name()='text'])"
            ).strip()
            src = no.xpath("string(*[local-name()='content']/@src)").strip()
            if not titulo or not src:
                continue

            alvo = src.split("#", 1)[0]
            if not alvo:
                continue

            # O src do NCX é relativo ao diretório do OPF; alguns EPUBs o
            # usam relativo ao diretório do próprio NCX — as duas tentativas
            # são feitas (a que existir no zip vence).
            candidatos = [
                resolve_href(data.opf_dir, alvo),
                resolve_href(diretorio_ncx, alvo),
            ]
            caminho = next(
                (c for c in candidatos if c and c in data.files),
                candidatos[0] if candidatos else "",
            )
            if caminho and caminho not in por_caminho:
                por_caminho[caminho] = titulo

            nome = posixpath.basename(alvo)
            if nome:
                ocorrencias[nome] = ocorrencias.get(nome, 0) + 1
                por_nome.setdefault(nome, titulo)

    # Só os nomes únicos são seguros como reserva.
    por_nome = {
        nome: titulo
        for nome, titulo in por_nome.items()
        if ocorrencias.get(nome, 0) == 1
    }
    return por_caminho, por_nome


def _titulo_do_capitulo(
    data: EpubData,
    caminho: str,
    por_caminho: dict[str, str],
    por_nome: dict[str, str],
) -> str:
    """Título do capítulo: NCX → correspondência única por nome → nome do arquivo."""
    titulo = por_caminho.get(caminho)
    if titulo:
        return titulo

    nome = posixpath.basename(caminho)
    candidatos = [
        c for c in data.text_files if posixpath.basename(c) == nome
    ]
    if len(candidatos) == 1:
        return (
            por_nome.get(nome)
            or por_caminho.get(candidatos[0])
            or _titulo_padrao(caminho)
        )

    return _titulo_padrao(caminho)


# ── Listagem de capítulos ────────────────────────────────────────────────────

def _contar_blocos(data: EpubData, caminho: str) -> int:
    """Número de blocos de um capítulo (via ``extract_blocks``)."""
    bruto = data.files.get(caminho)
    if bruto is None:
        return 0
    tree, _doctype = parse_xhtml(bruto)
    return len(extract_blocks(tree))


def listar_capitulos(data: EpubData) -> list[dict[str, Any]]:
    """Lista os capítulos do EPUB na ordem do spine.

    Args:
        data: Conteúdo do EPUB já lido (``EpubReader(...).read()``).

    Returns:
        Um dict por capítulo, na ordem de ``data.text_files``::

            {"index": 0, "path": "OEBPS/Text/section-0005.html",
             "title": "Chapter 1: The Diary (Part 1)", "blocks": 229}

        O ``title`` vem do NCX; sem título no NCX, usa o nome do arquivo sem
        extensão. O ``blocks`` é o número de blocos do capítulo.
    """
    por_caminho, por_nome = _titulos_ncx(data)

    capitulos: list[dict[str, Any]] = []
    for indice, caminho in enumerate(data.text_files):
        capitulos.append({
            "index": indice,
            "path": caminho,
            "title": _titulo_do_capitulo(data, caminho, por_caminho, por_nome),
            "blocks": _contar_blocos(data, caminho),
        })
    return capitulos


def capitulo_por_path(data: EpubData, path: str) -> dict[str, Any] | None:
    """Devolve o capítulo cujo ``path`` casar, ou ``None``.

    Só o capítulo procurado é lido (contagem de blocos) — o resto do livro
    não é carregado.

    Args:
        data: Conteúdo do EPUB já lido.
        path: Caminho real da entrada no zip (ex.:
            ``"OEBPS/Text/section-0005.html"``).
    """
    try:
        indice = data.text_files.index(path)
    except ValueError:
        return None

    por_caminho, por_nome = _titulos_ncx(data)
    return {
        "index": indice,
        "path": path,
        "title": _titulo_do_capitulo(data, path, por_caminho, por_nome),
        "blocks": _contar_blocos(data, path),
    }


# ── Sanitização ──────────────────────────────────────────────────────────────

def _preservar_tail(pai: etree._Element, filho: etree._Element) -> None:
    """Move o texto que ficava depois de *filho* para o elemento anterior.

    Ao remover um elemento o lxml descarta o ``tail`` dele; sem isso, o texto
    (às vezes relevante) sumiria do preview.
    """
    tail = filho.tail
    if not tail:
        return
    anterior = filho.getprevious()
    if anterior is not None:
        anterior.tail = (anterior.tail or "") + tail
    else:
        pai.text = (pai.text or "") + tail


def _sanitizar_atributos(elemento: etree._Element) -> None:
    """Remove os atributos perigosos de um elemento."""
    for nome, valor in list(elemento.attrib.items()):
        local = _localname_atributo(nome).lower()

        if local.startswith("on"):
            # Handler de evento (onclick, onload, ...).
            del elemento.attrib[nome]
            continue

        if local in _ATRIBUTOS_REMOVIDOS:
            del elemento.attrib[nome]
            continue

        if local in _ATRIBUTOS_DE_URL and _url_perigosa(valor):
            del elemento.attrib[nome]


def _sanitizar_elemento(elemento: etree._Element) -> None:
    """Sanitiza *elemento* e toda a sua descendência (in-place)."""
    _sanitizar_atributos(elemento)

    for filho in list(elemento):
        if not isinstance(filho.tag, str):
            # Comentário ou PI: preservado (não é código executável).
            continue

        if _localname(filho.tag) in _ELEMENTOS_PERIGOSOS:
            _preservar_tail(elemento, filho)
            elemento.remove(filho)
            continue

        _sanitizar_elemento(filho)


# ── URLs de imagem ───────────────────────────────────────────────────────────

def _reescrever_atributo_de_imagem(
    elemento: etree._Element,
    nome: str,
    diretorio: str,
    job_id: str,
) -> None:
    """Troca a URL relativa de uma imagem pela rota de asset do job."""
    valor = elemento.get(nome)
    if not valor:
        return
    if valor.startswith(("http://", "https://", "//", "data:", "mailto:", "#")):
        # URL absoluta: o browser já sabe buscar.
        return

    resolvido = resolve_href(diretorio, valor)
    if not resolvido:
        return

    elemento.set(
        nome,
        f"/api/jobs/{quote(job_id, safe='')}/asset"
        f"?path={quote(resolvido, safe='')}",
    )


def _reescrever_imagens(
    raiz: etree._Element,
    caminho: str,
    job_id: str | None,
) -> None:
    """Reescreve as URLs de imagem do capítulo para a rota de asset do job.

    ``img/@src`` e ``image/@xlink:href`` (SVG) que sejam relativas são
    resolvidas dentro do zip (ex.: ``OEBPS/Text/cap.html`` +
    ``../Images/capa.jpg`` → ``OEBPS/Images/capa.jpg``) e apontadas para
    ``/api/jobs/{job_id}/asset?path=...``.

    Com ``job_id`` ``None`` as URLs ficam como estão.
    """
    if not job_id:
        return

    diretorio = posixpath.dirname(caminho)

    for elemento in raiz.iter():
        if not isinstance(elemento.tag, str):
            continue
        local = _localname(elemento.tag)

        if local == "img":
            _reescrever_atributo_de_imagem(elemento, "src", diretorio, job_id)
        elif local == "image":
            # SVG: o href pode ser "xlink:href" (com namespace) ou "href".
            for nome in list(elemento.attrib):
                if _localname_atributo(nome) == "href":
                    _reescrever_atributo_de_imagem(
                        elemento, nome, diretorio, job_id
                    )


# ── Geração do HTML ──────────────────────────────────────────────────────────

def _conteudo_do_corpo(documento: str) -> str:
    """Extrai o conteúdo do ``<body>`` de um XHTML já serializado.

    Devolve só o conteúdo do capítulo (sem ``<html>``, ``<head>`` e
    ``<body>``), pronto para ser injetado no container do preview.

    O corte é feito no texto serializado: como o XML escapa ``<`` dentro de
    texto e de valores de atributo, o único ``<body``/``</body>`` do
    documento é o elemento de verdade.
    """
    inicio = documento.find("<body")
    if inicio == -1:
        # Capítulo sem <body>: devolve o documento sem a declaração XML e
        # sem o doctype (o preview é um fragmento, não um documento).
        return _sem_declaracao(documento)

    fim_tag = documento.find(">", inicio)
    if fim_tag == -1:
        return documento

    fim = documento.rfind("</body>")
    if fim == -1 or fim < fim_tag:
        return documento[fim_tag + 1:]

    return documento[fim_tag + 1:fim]


def _sem_declaracao(documento: str) -> str:
    """Remove a declaração XML e o doctype de um documento serializado."""
    sem_declaracao = documento
    if sem_declaracao.startswith("<?xml"):
        fim = sem_declaracao.find("?>")
        if fim != -1:
            sem_declaracao = sem_declaracao[fim + 2:]
    marca = sem_declaracao.find("<!DOCTYPE")
    if marca != -1:
        fim = sem_declaracao.find(">", marca)
        if fim != -1:
            sem_declaracao = sem_declaracao[:marca] + sem_declaracao[fim + 1:]
    return sem_declaracao.lstrip("\n")


def html_preview(
    data: EpubData,
    path: str,
    lado: str,
    traducoes: dict[int, str] | None = None,
    job_id: str | None = None,
) -> str:
    """Gera o HTML sanitizado de UM capítulo do EPUB.

    Args:
        data: Conteúdo do EPUB já lido.
        path: Caminho real da entrada no zip (ex.:
            ``"OEBPS/Text/section-0005.html"``).
        lado: ``"original"`` ou ``"translated"``.
        traducoes: Mapa ``{índice_do_bloco (1-based): texto_traduzido}``. O
            texto traduzido continua com os marcadores ``[[n]]`` — o
            ``replay`` reconstrói as tags inline a partir deles. Índices
            ausentes mantêm o texto original.
        job_id: Id do job; quando informado, as URLs de imagem relativas
            viram ``/api/jobs/{job_id}/asset?path=...``.

    Returns:
        O HTML do capítulo (conteúdo do ``<body>``, sem ``<html>``/``<head>``/
        ``<body>``), sanitizado e com ``data-block-index`` 1-based em cada
        elemento de bloco.

    Raises:
        ValueError: Se ``lado`` for inválido ou o capítulo não existir.
    """
    if lado not in _LADOS_VALIDOS:
        raise ValueError(
            f"Lado inválido para o preview: {lado!r} "
            f"(use {LADO_ORIGINAL!r} ou {LADO_TRADUZIDO!r})"
        )

    bruto = data.files.get(path)
    if bruto is None:
        raise ValueError(f"Capítulo não encontrado no EPUB: {path}")

    # (1) parse e (2) extração dos blocos (a árvore é modificada in-place)
    tree, doctype = parse_xhtml(bruto)
    blocos = extract_blocks(tree)

    # (3) índice 1-based de cada bloco, na ordem do documento
    for indice, bloco in enumerate(blocos, start=1):
        if bloco.element is not None:
            bloco.element.set("data-block-index", str(indice))

    # (4) lado traduzido: troca o texto dos blocos traduzidos
    # Blocos sem tradução ganham ``data-pending`` (a UI os destaca).
    if lado == LADO_TRADUZIDO:
        for indice, bloco in enumerate(blocos, start=1):
            texto = (traducoes or {}).get(indice)
            if texto is not None:
                bloco.text = texto
            elif bloco.element is not None:
                bloco.element.set("data-pending", "1")

    # (5) reconstrói as tags inline a partir dos marcadores
    raiz = replay(blocos, tree)

    # (6) sanitização (depois do replay: as tags inline recriadas também
    #     podem trazer atributos perigosos) e URLs de imagem
    _sanitizar_elemento(raiz)
    _reescrever_imagens(raiz, path, job_id)

    # (7) serialização (o replay devolve o ELEMENTO RAIZ, não a ElementTree)
    documento = serialize_xhtml(etree.ElementTree(raiz), doctype).decode("utf-8")
    return _conteudo_do_corpo(documento)
