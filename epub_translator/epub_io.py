"""Leitura e escrita de EPUB com fidelidade estrutural.

Preserva mimetype (ZIP_STORED, primeira entrada), imagens/CSS byte-idênticos,
tags inline, índice (NCX/nav), título e dc:language do OPF.
"""

from __future__ import annotations

import os
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from typing import Callable, Mapping, Optional
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from lxml import etree


# ── Dados ────────────────────────────────────────────────────────────────────

@dataclass
class ItemInfo:
    """Metadado de uma entrada do manifest OPF.

    ``href`` é o valor original do manifest (relativo ao diretório do OPF);
    ``zip_path`` é o caminho real da entrada dentro do zip.
    """
    href: str
    media_type: str
    title: str | None = None
    properties: str | None = None
    zip_path: str = ""


@dataclass
class EpubData:
    """Conteúdo de um EPUB descompactado."""
    files: dict[str, bytes] = field(default_factory=dict)
    text_files: list[str] = field(default_factory=list)
    ncx_paths: list[str] = field(default_factory=list)
    opf_path: str = ""
    opf_bytes: bytes = b""
    title: str = ""
    language: str = ""
    spine: list[str] = field(default_factory=list)
    all_items: dict[str, ItemInfo] = field(default_factory=dict)
    opf_dir: str = ""


# ── Helpers XML comuns ───────────────────────────────────────────────────────

# Declaração XML no estilo dos EPUBs originais (aspas duplas, encoding UTF-8).
_XML_DECLARATION = '<?xml version="{version}" encoding="UTF-8"?>'


def _xml_declaration(tree: ET.ElementTree) -> str:
    """Declaração XML no estilo do original: aspas duplas e encoding UTF-8.

    A versão vem do ``docinfo`` da árvore (1.0 por padrão) e o encoding é
    sempre UTF-8, que é o que a serialização realmente usa.
    """
    version = "1.0"
    docinfo = getattr(tree, "docinfo", None)
    if docinfo is not None and docinfo.xml_version:
        version = docinfo.xml_version
    return _XML_DECLARATION.format(version=version)


def _serialize_xml_document(root: etree._Element) -> bytes:
    """Serializa um documento XML preservando doctype, PIs e comentários.

    A serialização da árvore já inclui o doctype, as instruções de
    processamento e os comentários do nível superior; a declaração XML é
    montada à mão (aspas duplas, UTF-8) e vai na frente, como exige o XML.
    """
    tree = root.getroottree()
    content = etree.tostring(tree, xml_declaration=False, encoding="UTF-8")
    return _xml_declaration(tree).encode("utf-8") + b"\n" + content


# ── Leitura ──────────────────────────────────────────────────────────────────

def resolve_href(opf_dir: str, href: str) -> str:
    """Resolve um href do manifest OPF para o caminho real dentro do zip.

    O href do manifest é relativo ao diretório do OPF (ex.: 'Text/ch1.xhtml'
    com OPF em 'OEBPS/content.opf'). O resultado é o caminho da entrada no zip
    (ex.: 'OEBPS/Text/ch1.xhtml'), com segmentos '.' e '..' removidos.
    """
    path = href.split("#", 1)[0].replace("\\", "/")
    if not path:
        return ""
    if path.startswith("/"):
        base, path = "", path.lstrip("/")
    else:
        base = opf_dir or ""

    parts: list[str] = []
    for part in (f"{base}/{path}" if base else path).split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _zip_path_for(opf_dir: str, href: str, files: Mapping[str, bytes]) -> str:
    """Descobre a chave real da entrada no zip correspondente a um href do manifest.

    Usa a resolução canônica (relativa ao diretório do OPF) e, se ela não
    existir no zip, tenta o href cru (EPUBs que repetem o prefixo do OPF) e
    variantes com percent-decoding.
    """
    resolved = resolve_href(opf_dir, href)
    raw = href.split("#", 1)[0].replace("\\", "/").lstrip("/")

    candidates = [resolved, raw]
    decoded = unquote(raw)
    if decoded != raw:
        candidates.extend([decoded, resolve_href(opf_dir, decoded)])

    for candidate in candidates:
        if candidate and candidate in files:
            return candidate
    return resolved or raw


def _find_opf_path(z: zipfile.ZipFile) -> str:
    """Retorna o caminho da entrada .opf a partir do container.xml."""
    container = z.read("META-INF/container.xml")
    root = etree.fromstring(container)
    full_path = root.xpath(
        "//*[local-name()='rootfile']/@full-path"
    )
    if not full_path:
        raise ValueError("Não foi possível encontrar o caminho do OPF em META-INF/container.xml")
    return full_path[0]


class EpubReader:
    """Abre um arquivo .epub e expõe seus metadados e conteúdo."""

    def __init__(self, path: str) -> None:
        self.path = path

    def read(self) -> EpubData:
        """Lê o EPUB e retorna EpubData."""
        data = EpubData()
        with zipfile.ZipFile(self.path, "r") as z:
            # todas as entradas byte-a-byte
            data.files = {name: z.read(name) for name in z.namelist()}

            # OPF
            data.opf_path = _find_opf_path(z)
            data.opf_bytes = z.read(data.opf_path)
            opf_root = etree.fromstring(data.opf_bytes)

            # diretório base do OPF — hrefs do manifest são relativos a ele
            data.opf_dir = posixpath.dirname(data.opf_path)

            # metadados
            ns = {"dc": "http://purl.org/dc/elements/1.1/"}
            data.title = opf_root.xpath(
                "string(//*[local-name()='metadata']/*[local-name()='title'])"
            ) or ""
            data.language = opf_root.xpath(
                "string(//*[local-name()='metadata']/*[local-name()='language'])"
            ) or ""

            # manifest → all_items
            manifest = opf_root.xpath("//*[local-name()='manifest']")[0]
            for item in manifest.xpath("*[local-name()='item']"):
                item_id = item.get("id", "")
                href = item.get("href", "")
                media_type = item.get("media-type", "")
                title = item.get("title")
                properties = item.get("properties")
                data.all_items[item_id] = ItemInfo(
                    href=href,
                    media_type=media_type,
                    title=title,
                    properties=properties,
                    zip_path=_zip_path_for(data.opf_dir, href, data.files),
                )

            # spine → text_files + ncx_paths
            spine_el = opf_root.xpath("//*[local-name()='spine']")[0]
            data.spine = [
                s.get("idref", "")
                for s in spine_el.xpath("*[local-name()='itemref']")
            ]

            text_exts = {".xhtml", ".html"}
            text_media = {"application/xhtml+xml", "text/html"}
            ncx_media = {"application/x-dtbncx+xml"}

            for item_id in data.spine:
                info = data.all_items.get(item_id)
                if info is None:
                    continue
                ext = "." + info.href.rsplit(".", 1)[-1] if "." in info.href else ""
                if info.media_type in text_media or ext in text_exts:
                    data.text_files.append(info.zip_path or info.href)
                if info.media_type in ncx_media or ext == ".ncx":
                    data.ncx_paths.append(info.zip_path or info.href)

            # NCX referenciado por spine@toc (e não por itemref) também conta
            toc_id = spine_el.get("toc")
            if toc_id:
                toc_info = data.all_items.get(toc_id)
                if toc_info is not None:
                    toc_path = toc_info.zip_path or toc_info.href
                    if toc_path not in data.ncx_paths:
                        data.ncx_paths.append(toc_path)

        return data


def read_epub(path: str) -> EpubData:
    """Função prática: lê um EPUB e retorna EpubData."""
    return EpubReader(path).read()


# ── Escrita ──────────────────────────────────────────────────────────────────

def write_epub(
    out_path: str,
    data: EpubData,
    overrides: Mapping[str, bytes] | None = None,
) -> None:
    """Reescreve um EPUB. mimetype é a 1ª entrada (ZIP_STORED), resto ZIP_DEFLATED.

    Cada entrada de ``data.files`` é escrita uma única vez: se o nome estiver
    em ``overrides``, o conteúdo do override substitui o original no lugar
    (sem criar entrada duplicada).
    """
    overrides = overrides or {}

    # Garante que a pasta de destino exista (ex.: "saida/").
    pasta = os.path.dirname(out_path)
    if pasta:
        os.makedirs(pasta, exist_ok=True)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype primeiro, sem compressão
        if "mimetype" in data.files:
            z.writestr(zipfile.ZipInfo("mimetype"),
                       overrides.get("mimetype", data.files["mimetype"]),
                       compress_type=zipfile.ZIP_STORED)
        # demais entradas — override substitui o conteúdo no lugar
        for name, content in data.files.items():
            if name == "mimetype":
                continue
            z.writestr(name, overrides.get(name, content),
                       compress_type=zipfile.ZIP_DEFLATED)
        # sobrescritas de arquivos que não existiam no EPUB original
        for name, content in overrides.items():
            if name == "mimetype" or name in data.files:
                continue
            z.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED)


# ── Helpers OPF ──────────────────────────────────────────────────────────────

# Namespace do Dublin Core (dc:).
DC_NS = "http://purl.org/dc/elements/1.1/"


def _opf_metadata(root: etree._Element) -> etree._Element:
    """Devolve o elemento <metadata> do OPF."""
    meta = root.xpath("//*[local-name()='metadata']")
    if not meta:
        raise ValueError("OPF sem elemento <metadata>")
    return meta[0]


def _set_opf_dc_text(opf_bytes: bytes, local_name: str, value: str) -> bytes:
    """Define o texto de um elemento dc:* do <metadata>, criando se faltar.

    O resto do OPF (doctype, manifesto, spine, comentários, namespaces) é
    preservado: a serialização usa a árvore inteira, não só o elemento raiz.
    """
    root = etree.fromstring(opf_bytes)
    meta = _opf_metadata(root)

    elements = meta.xpath(f"*[local-name()='{local_name}']")
    if elements:
        elements[0].text = value
    else:
        element = etree.SubElement(meta, f"{{{DC_NS}}}{local_name}")
        element.text = value

    return _serialize_xml_document(root)


def set_opf_language(opf_bytes: bytes, lang: str) -> bytes:
    """Altera o valor de dc:language no OPF, preservando o resto."""
    return _set_opf_dc_text(opf_bytes, "language", lang)


def set_opf_title(opf_bytes: bytes, title: str) -> bytes:
    """Altera o valor de dc:title no OPF, preservando o resto."""
    return _set_opf_dc_text(opf_bytes, "title", title)


# ── Helpers NCX ──────────────────────────────────────────────────────────────

def translate_ncx(ncx_bytes: bytes, fn: Callable[[str], str]) -> bytes:
    """Aplica *fn* ao texto de todos os nós <text> (docTitle e navLabel)."""
    root = etree.fromstring(ncx_bytes)
    ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
    for text_el in root.xpath("//ncx:text", namespaces=ns):
        if text_el.text:
            text_el.text = fn(text_el.text)
    return _serialize_xml_document(root)


# ── Helpers XHTML ────────────────────────────────────────────────────────────

_DOCTYPE_XHTML11 = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"'
    ' "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
)

_DOCTYPE_HTML5 = "<!DOCTYPE html>"

_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)


def parse_xhtml(data: bytes) -> tuple[etree.ElementTree, str | None]:
    """Parseia XHTML e devolve (árvore, doctype original ou None)."""
    doctype: str | None = None
    text = data.decode("utf-8")
    m = _DOCTYPE_RE.search(text)
    if m:
        doctype = m.group()

    parser = etree.XMLParser(
        recover=True,
        encoding="utf-8",
        huge_tree=True,
        no_network=True,
    )
    tree = etree.fromstring(data, parser)
    return etree.ElementTree(tree), doctype


def _infer_doctype(root: etree._Element) -> str:
    """Doctype usado quando o original não tem um.

    ``<html>`` sem namespace XHTML → HTML5; qualquer outro caso → XHTML 1.1.
    """
    tag = root.tag
    if isinstance(tag, str) and tag == "html":
        return _DOCTYPE_HTML5
    return _DOCTYPE_XHTML11


def serialize_xhtml(tree: ET.ElementTree, doctype: str | None) -> bytes:
    """Serializa árvore XHTML para bytes, com declaração XML e doctype.

    A ordem das partes é a exigida pelo XML: (1) declaração XML, (2) doctype,
    (3) conteúdo do elemento raiz. O doctype vai antes do elemento, mas
    DEPOIS da declaração XML — invertê-la faz o lxml recusar o documento
    ("XML declaration allowed only to be at the start of the document").
    """
    root = tree.getroot()

    if doctype is None:
        doctype = _infer_doctype(root)

    content = etree.tostring(
        root,
        xml_declaration=False,
        encoding="UTF-8",
        pretty_print=False,
    )

    parts = [_xml_declaration(tree).encode("utf-8")]
    if doctype:
        parts.append(doctype.encode("utf-8"))
    parts.append(content)
    return b"\n".join(parts)
