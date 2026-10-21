"""Testes unitários para epub_io — EPUB mínimo gerado em memória."""

import io
import os
import tempfile
import zipfile
from xml.etree import ElementTree as ET

import pytest
from lxml import etree

from epub_translator.epub_io import (
    EpubData,
    EpubReader,
    ItemInfo,
    parse_xhtml,
    read_epub,
    resolve_href,
    serialize_xhtml,
    set_opf_language,
    set_opf_title,
    translate_ncx,
    write_epub,
)


# ── Gerador de EPUB mínimo ──────────────────────────────────────────────────

def _make_minimal_epub(
    title: str = "Test Book",
    language: str = "en",
    spine_items: list[tuple[str, str, str]] | None = None,
    has_ncx: bool = True,
    xhtml_doctype: str | None = None,
    opf_dir: str = "OEBPS",
) -> bytes:
    """Gera bytes de um EPUB válido mínimo.

    *opf_dir* é o diretório (dentro do zip) onde ficam o OPF e o NCX; os
    hrefs do manifest são relativos a ele.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # 1. mimetype (ZIP_STORED)
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

        # 2. META-INF/container.xml
        opf_path = f"{opf_dir}/content.opf" if opf_dir else "content.opf"
        container = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            f'    <rootfile full-path="{opf_path}" '
            'media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>\n'
        )
        z.writestr("META-INF/container.xml", container)

        # 3. XHTML content
        doctype_line = ""
        if xhtml_doctype:
            doctype_line = xhtml_doctype + "\n"
        xhtml_content = (
            f'{doctype_line}'
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            '<head><title>Chapter</title></head>\n'
            '<body><p>Hello world.</p></body>\n'
            '</html>\n'
        )
        z.writestr(f"{opf_dir}/text/chap1.xhtml" if opf_dir else "text/chap1.xhtml",
                   xhtml_content)

        # 4. NCX (opcional) — antes do OPF para montar manifest/spine corretamente
        ncx_path = ""
        if has_ncx:
            ncx_path = "toc.ncx"
            ncx = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
                '  <head>\n'
                '    <meta name="dtb:uid" content="urn:uuid:00000000-0000-0000-0000-000000000001"/>\n'
                '    <meta name="dtb:depth" content="1"/>\n'
                '  </head>\n'
                '  <docTitle><text>Test Book</text></docTitle>\n'
                '  <navMap>\n'
                '    <navPoint id="np1" playOrder="1">\n'
                '      <navLabel><text>Chapter 1</text></navLabel>\n'
                '      <content src="text/chap1.xhtml"/>\n'
                '    </navPoint>\n'
                '  </navMap>\n'
                '</ncx>\n'
            )
            z.writestr(f"{opf_dir}/toc.ncx" if opf_dir else "toc.ncx", ncx)

        # 5. OPF
        spine_items = spine_items or [("chap1", "text/chap1.xhtml", "application/xhtml+xml")]
        manifest_lines = []
        spine_lines = []
        for item_id, href, media_type in spine_items:
            manifest_lines.append(
                f'  <item id="{item_id}" href="{href}" media-type="{media_type}"/>'
            )
            spine_lines.append(f'  <itemref idref="{item_id}"/>')
            # garante que a entrada exista no zip no caminho resolvido
            resolved = resolve_href(opf_dir, href)
            if resolved not in z.namelist():
                z.writestr(resolved, xhtml_content)

        # NCX ao manifest e spine
        if has_ncx:
            manifest_lines.append(
                '  <item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            )
            spine_lines.append('  <itemref idref="toc"/>')

        spine_xml = "\n".join(spine_lines)

        opf = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<package version="2.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:title>{title}</dc:title>\n'
            f'    <dc:language>{language}</dc:language>\n'
            '    <dc:identifier id="uid">urn:uuid:00000000-0000-0000-0000-000000000001</dc:identifier>\n'
            '  </metadata>\n'
            '  <manifest>\n'
            + "\n".join(manifest_lines) + "\n"
            '  </manifest>\n'
            '  <spine toc="toc">\n'
            + spine_xml + "\n"
            '  </spine>\n'
            '</package>\n'
        )
        z.writestr(opf_path, opf)

    return buf.getvalue()


def _replace_in_zip(epub_bytes: bytes, name: str, new_content: bytes) -> bytes:
    """Reescreve uma entrada dentro de um EPUB, preservando as demais."""
    src = zipfile.ZipFile(io.BytesIO(epub_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            content = new_content if info.filename == name else src.read(info.filename)
            if info.filename == "mimetype":
                dst.writestr("mimetype", content, compress_type=zipfile.ZIP_STORED)
            else:
                dst.writestr(info.filename, content, compress_type=zipfile.ZIP_DEFLATED)
    src.close()
    return buf.getvalue()


def _opf_with_toc_only() -> bytes:
    """OPF com NCX no manifest referenciado apenas por spine@toc."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package version="2.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:title>T</dc:title>\n'
        '    <dc:language>en</dc:language>\n'
        '    <dc:identifier id="uid">urn:uuid:00000000-0000-0000-0000-000000000001</dc:identifier>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="ch1" href="text/chap1.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '  </manifest>\n'
        '  <spine toc="ncx">\n'
        '    <itemref idref="ch1"/>\n'
        '  </spine>\n'
        '</package>\n'
    ).encode("utf-8")


def _resolve_path(opf_path: str, href: str) -> str:
    """Resolve href relativo ao OPF para caminho dentro do zip."""
    opf_dir = os.path.dirname(opf_path)
    if not opf_dir:
        return href
    # Junta caminho relativo ao diretório do OPF
    parts = opf_dir.split("/")
    parts.append(href)
    resolved = []
    for p in parts:
        if p == "..":
            resolved.pop()
        elif p == ".":
            continue
        else:
            resolved.append(p)
    return "/".join(resolved)


def _find_ncx_file(data: EpubData) -> bytes:
    """Encontra o conteúdo NCX em data.files, resolvendo caminhos relativos ao OPF."""
    for ncx_href in data.ncx_paths:
        # Tenta direto
        if ncx_href in data.files:
            return data.files[ncx_href]
        # Resolve relativo ao OPF
        resolved = _resolve_path(data.opf_path, ncx_href)
        if resolved in data.files:
            return data.files[resolved]
    raise KeyError(f"Nenhum arquivo NCX encontrado para {data.ncx_paths}")


# ── Testes: EpubReader ──────────────────────────────────────────────────────

class TestResolveHref:
    """Testes da resolução de href relativo ao diretório do OPF."""

    def test_resolve_relative_to_opf_dir(self):
        """href relativo ao OPF ganha o prefixo do diretório do OPF."""
        assert resolve_href("OEBPS", "Text/ch1.xhtml") == "OEBPS/Text/ch1.xhtml"

    def test_resolve_without_opf_dir(self):
        """OPF na raiz do zip: href fica como está."""
        assert resolve_href("", "Text/ch1.xhtml") == "Text/ch1.xhtml"

    def test_resolve_parent_segments(self):
        """Segmentos '..' são removidos."""
        assert resolve_href("OEBPS/Text", "../ch1.xhtml") == "OEBPS/ch1.xhtml"
        assert resolve_href("OEBPS", "../ch1.xhtml") == "ch1.xhtml"

    def test_resolve_normalizes_dots(self):
        """Segmentos '.' e barras duplicadas são normalizados."""
        assert resolve_href("OEBPS", "./Text/./ch1.xhtml") == "OEBPS/Text/ch1.xhtml"

    def test_resolve_strips_fragment(self):
        """Fragmento (#anchor) é removido do caminho."""
        assert resolve_href("OEBPS", "Text/ch1.xhtml#cap1") == "OEBPS/Text/ch1.xhtml"


class TestReadEpub:
    """Testes de leitura de EPUB."""

    def test_read_minimal_epub(self):
        """EPUB mínimo: verifica EpubData completo."""
        epub_bytes = _make_minimal_epub(
            title="Test Book",
            language="en",
            spine_items=[("chap1", "text/chap1.xhtml", "application/xhtml+xml")],
        )
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            assert data.title == "Test Book"
            assert data.language == "en"
            assert len(data.files) > 0
            assert "mimetype" in data.files
            assert "META-INF/container.xml" in data.files
            assert "OEBPS/content.opf" in data.files
            assert data.opf_path == "OEBPS/content.opf"
        finally:
            os.unlink(tmp_path)

    def test_text_files_order(self):
        """text_files respeita a ordem do spine."""
        epub_bytes = _make_minimal_epub(
            title="T",
            language="en",
            spine_items=[
                ("chap1", "text/chap1.xhtml", "application/xhtml+xml"),
                ("chap2", "text/chap2.html", "text/html"),
            ],
            has_ncx=False,
        )
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            # caminhos resolvidos: chaves reais do zip (OPF em 'OEBPS/')
            assert data.text_files == [
                "OEBPS/text/chap1.xhtml",
                "OEBPS/text/chap2.html",
            ]
        finally:
            os.unlink(tmp_path)

    def test_ncx_detection(self):
        """EPUB com NCX detecta ncx_paths e arquivo existe."""
        epub_bytes = _make_minimal_epub(
            title="T",
            language="en",
            spine_items=[("chap1", "text/chap1.xhtml", "application/xhtml+xml")],
            has_ncx=True,
        )
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            assert len(data.ncx_paths) > 0
            # o href do manifest é "toc.ncx" (relativo ao OPF)…
            assert data.all_items["toc"].href == "toc.ncx"
            # …mas ncx_paths guarda o caminho real no zip
            assert data.ncx_paths == ["OEBPS/toc.ncx"]
            # o arquivo no zip é "OEBPS/toc.ncx"
            ncx_content = _find_ncx_file(data)
            assert b"navMap" in ncx_content
        finally:
            os.unlink(tmp_path)

    def test_all_items(self):
        """all_items contém todos os itens do manifest."""
        epub_bytes = _make_minimal_epub(
            title="T",
            language="en",
            spine_items=[("chap1", "text/chap1.xhtml", "application/xhtml+xml")],
        )
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            assert "chap1" in data.all_items
            assert data.all_items["chap1"].href == "text/chap1.xhtml"
            assert data.all_items["chap1"].media_type == "application/xhtml+xml"
            # zip_path é o caminho real da entrada no zip
            assert data.all_items["chap1"].zip_path == "OEBPS/text/chap1.xhtml"
        finally:
            os.unlink(tmp_path)

    def test_opf_in_subdirectory_resolves_text_files(self):
        """OPF em subdiretório: text_files traz o caminho real do zip."""
        epub_bytes = _make_minimal_epub(
            title="T",
            language="en",
            spine_items=[
                ("ch1", "Text/ch1.xhtml", "application/xhtml+xml"),
                ("ch2", "Text/ch2.xhtml", "application/xhtml+xml"),
            ],
            has_ncx=False,
        )
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            # o OPF está em OEBPS/content.opf → prefixo 'OEBPS/'
            assert data.opf_path == "OEBPS/content.opf"
            assert data.opf_dir == "OEBPS"
            # hrefs do manifest são relativos ao OPF…
            assert data.all_items["ch1"].href == "Text/ch1.xhtml"
            # …mas text_files usa as chaves reais do zip
            assert data.text_files == ["OEBPS/Text/ch1.xhtml", "OEBPS/Text/ch2.xhtml"]
            # e cada caminho existe de fato em data.files
            for path in data.text_files:
                assert path in data.files
        finally:
            os.unlink(tmp_path)

    def test_opf_in_subdirectory_resolves_ncx(self):
        """OPF em subdiretório: ncx_paths traz o caminho real do zip."""
        epub_bytes = _make_minimal_epub(
            title="T",
            language="en",
            spine_items=[("ch1", "Text/ch1.xhtml", "application/xhtml+xml")],
            has_ncx=True,
        )
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            assert data.ncx_paths == ["OEBPS/toc.ncx"]
            for path in data.ncx_paths:
                assert path in data.files
        finally:
            os.unlink(tmp_path)

    def test_ncx_from_spine_toc_attribute(self):
        """NCX referenciado só por spine@toc também entra em ncx_paths."""
        epub_bytes = _make_minimal_epub(
            title="T",
            language="en",
            spine_items=[("ch1", "text/chap1.xhtml", "application/xhtml+xml")],
            has_ncx=True,
        )
        # reinsere o NCX no manifest/spine[@toc] sem itemref
        epub_bytes = _replace_in_zip(
            epub_bytes,
            "OEBPS/content.opf",
            _opf_with_toc_only(),
        )
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            assert data.ncx_paths == ["OEBPS/toc.ncx"]
            assert "OEBPS/toc.ncx" in data.files
        finally:
            os.unlink(tmp_path)


# ── Testes: write_epub ──────────────────────────────────────────────────────

class TestWriteEpub:
    """Testes de escrita de EPUB."""

    def test_mimetype_first_stored(self):
        """mimetype é a 1ª entrada do zip com ZIP_STORED."""
        epub_bytes = _make_minimal_epub()
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
        finally:
            os.unlink(tmp_path)
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            out_path = tmp.name
        write_epub(out_path, data)

        with zipfile.ZipFile(out_path, "r") as z:
            names = z.namelist()
            assert names[0] == "mimetype"
            info = z.getinfo("mimetype")
            assert info.compress_type == zipfile.ZIP_STORED
            for name in names[1:]:
                if name == "mimetype":
                    continue
                assert z.getinfo(name).compress_type == zipfile.ZIP_DEFLATED
        os.unlink(out_path)

    def test_overrides(self):
        """Overrides substituem conteúdo sem alterar estrutura."""
        epub_bytes = _make_minimal_epub()
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp2:
                out_path = tmp2.name
            new_opf = b'<?xml version="1.0"?><package></package>'
            write_epub(out_path, data, overrides={"OEBPS/content.opf": new_opf})
            with zipfile.ZipFile(out_path, "r") as z:
                assert z.read("OEBPS/content.opf") == new_opf
            os.unlink(out_path)
        finally:
            os.unlink(tmp_path)

    def test_overrides_do_not_duplicate_names(self):
        """Overrides substituem a entrada original — sem nomes duplicados."""
        epub_bytes = _make_minimal_epub()
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            original_names = list(data.files.keys())
            new_opf = b'<?xml version="1.0"?><package></package>'
            new_chap = b"<html><body><p>Traduzido</p></body></html>"
            overrides = {
                "OEBPS/content.opf": new_opf,
                "OEBPS/text/chap1.xhtml": new_chap,
            }
            with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp2:
                out_path = tmp2.name
            write_epub(out_path, data, overrides=overrides)

            with zipfile.ZipFile(out_path, "r") as z:
                names = z.namelist()
                # mesma quantidade de entradas do original
                assert len(names) == len(original_names)
                # nenhum nome duplicado
                assert len(names) == len(set(names))
                assert names[0] == "mimetype"
                # conteúdo substituído no lugar
                assert z.read("OEBPS/content.opf") == new_opf
                assert z.read("OEBPS/text/chap1.xhtml") == new_chap
                # demais entradas intactas
                for name, content in data.files.items():
                    if name in overrides or name == "mimetype":
                        continue
                    assert z.read(name) == content
            os.unlink(out_path)
        finally:
            os.unlink(tmp_path)

    def test_write_epub_no_duplicate_warning(self, recwarn):
        """write_epub não emite o warning 'Duplicate name' do zipfile."""
        epub_bytes = _make_minimal_epub()
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp2:
                out_path = tmp2.name
            write_epub(
                out_path,
                data,
                overrides={"OEBPS/content.opf": b"<package/>",
                           "OEBPS/text/chap1.xhtml": b"<html/>"},
            )
            os.unlink(out_path)
        finally:
            os.unlink(tmp_path)
        assert not [w for w in recwarn if "Duplicate name" in str(w.message)]


# ── Testes: set_opf_language / set_opf_title ────────────────────────────────

class TestOpfHelpers:
    """Testes de helpers do OPF."""

    def test_set_opf_language(self):
        """set_opf_language altera dc:language e sobrevive a re-parse."""
        epub_bytes = _make_minimal_epub(title="T", language="en")
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            new_opf = set_opf_language(data.opf_bytes, "pt-BR")
            root = etree.fromstring(new_opf)
            lang = root.xpath(
                "string(//*[local-name()='metadata']/*[local-name()='language'])"
            )
            assert lang == "pt-BR"
        finally:
            os.unlink(tmp_path)

    def test_set_opf_title(self):
        """set_opf_title altera dc:title e sobrevive a re-parse."""
        epub_bytes = _make_minimal_epub(title="Old Title", language="en")
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            new_opf = set_opf_title(data.opf_bytes, "Novo Título")
            root = etree.fromstring(new_opf)
            title = root.xpath(
                "string(//*[local-name()='metadata']/*[local-name()='title'])"
            )
            assert title == "Novo Título"
        finally:
            os.unlink(tmp_path)


# ── Testes: translate_ncx ───────────────────────────────────────────────────

class TestTranslateNcx:
    """Testes de tradução do NCX."""

    def test_translate_ncx_roundtrip(self):
        """translate_ncx aplica fn aos nós <text> e preserva estrutura."""
        epub_bytes = _make_minimal_epub(title="Test Book", language="en")
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(epub_bytes)
            tmp_path = tmp.name
        try:
            data = read_epub(tmp_path)
            ncx_bytes = _find_ncx_file(data)
            translated = translate_ncx(ncx_bytes, lambda t: t.upper())
            root = etree.fromstring(translated)
            ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
            doc_title = root.xpath("//ncx:docTitle//ncx:text/text()", namespaces=ns)[0]
            nav_label = root.xpath("//ncx:navLabel//ncx:text/text()", namespaces=ns)[0]
            assert doc_title == "TEST BOOK"
            assert nav_label == "CHAPTER 1"
            # playOrder preservado
            nav_point = root.xpath("//ncx:navPoint", namespaces=ns)[0]
            assert nav_point.get("playOrder") == "1"
            assert nav_point.get("id") == "np1"
        finally:
            os.unlink(tmp_path)


# ── Testes: parse/serialize XHTML ──────────────────────────────────────────

class TestXhtmlParseSerialize:
    """Testes de parse e serialização XHTML."""

    def test_preserves_doctype(self):
        """parse/serialize preserva doctype original."""
        xhtml = (
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><title>T</title></head>'
            '<body><p>Hi</p></body>'
            '</html>\n'
        )
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        result = serialize_xhtml(tree, doctype)
        assert b'<!DOCTYPE html PUBLIC' in result

    def test_preserves_inline_tags(self):
        """Tags inline (b, span c/ attrs, br) sobrevivem ao ciclo."""
        xhtml = (
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            '<body>'
            '<p><b class="bold" id="b1">bold</b></p>'
            '<span style="color:red">colored</span>'
            '<br/>'
            '</body>'
            '</html>\n'
        )
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        result = serialize_xhtml(tree, doctype)
        # b com class e id
        assert b'class="bold"' in result
        assert b'id="b1"' in result
        # span com style
        assert b'style=' in result
        # br
        assert b'<br' in result.lower()

    def test_preserves_entities(self):
        """Entidades XML sobrevivem ao ciclo parse/serialize."""
        # XML válido: XML decl antes do DOCTYPE
        xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            '<body><p>1 &amp; 2</p></body>'
            '</html>\n'
        )
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        result = serialize_xhtml(tree, doctype)
        # O & é preservado na árvore
        ns = {"x": "http://www.w3.org/1999/xhtml"}
        p = tree.getroot().xpath(".//x:p", namespaces=ns)[0]
        assert p.text == "1 & 2"
        # &#160; (NBSP) preservado na árvore
        xhtml2 = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            '<body><p>A&#160;B</p></body>'
            '</html>\n'
        )
        tree2, doctype2 = parse_xhtml(xhtml2.encode("utf-8"))
        result2 = serialize_xhtml(tree2, doctype2)
        p2 = tree2.getroot().xpath(".//x:p", namespaces=ns)[0]
        assert p2.text == "A\u00a0B"

    def test_default_doctype_xhtml11(self):
        """doctype None → XHTML 1.1 default."""
        xhtml = '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">' \
                '<body><p>Hi</p></body></html>'
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        assert doctype is None
        result = serialize_xhtml(tree, doctype)
        assert b'XHTML 1.1' in result

    def test_html5_doctype(self):
        """DOCTYPE html5 → '<!DOCTYPE html>'."""
        xhtml = '<!DOCTYPE html>\n<html><head><title>T</title></head>' \
                '<body><p>Hi</p></body></html>'
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        assert doctype == "<!DOCTYPE html>"
        result = serialize_xhtml(tree, doctype)
        assert b'<!DOCTYPE html>' in result

    def test_xml_declaration_before_doctype(self):
        """Declaração XML vem PRIMEIRO, doctype depois (ordem exigida pelo XML)."""
        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            '<body><p>Hi</p></body>'
            '</html>\n'
        )
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        result = serialize_xhtml(tree, doctype)
        # começa pela declaração XML…
        assert result.startswith(b'<?xml')
        # …e o doctype vem DEPOIS dela
        assert result.index(b'<!DOCTYPE') > result.index(b'<?xml')
        assert result.index(b'<!DOCTYPE') < result.index(b'<html')

    def test_output_is_parseable_by_lxml(self):
        """Saída do serialize_xhtml é parseada pelo lxml sem erro."""
        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            '<body><p>Hi</p></body>'
            '</html>\n'
        )
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        result = serialize_xhtml(tree, doctype)
        # lxml estrito (sem recover) — não pode levantar XMLSyntaxError
        root = etree.fromstring(result)
        assert root.tag == "{http://www.w3.org/1999/xhtml}html"
        # e o ElementTree da stdlib também
        assert ET.fromstring(result).tag == "{http://www.w3.org/1999/xhtml}html"

    def test_doctype_none_html_without_ns_uses_html5(self):
        """doctype None + <html> sem namespace → doctype HTML5."""
        xhtml = '<html><head><title>T</title></head><body><p>Hi</p></body></html>'
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        assert doctype is None
        result = serialize_xhtml(tree, doctype)
        assert result.startswith(b'<?xml')
        assert b'<!DOCTYPE html>' in result
        # HTML5 puro: sem "PUBLIC"
        assert b'PUBLIC' not in result
        # e continua parseável
        assert etree.fromstring(result).tag == "html"

    def test_doctype_none_with_ns_uses_xhtml11(self):
        """doctype None + <html> com namespace XHTML → doctype XHTML 1.1."""
        xhtml = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hi</p></body></html>'
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        assert doctype is None
        result = serialize_xhtml(tree, doctype)
        assert b'XHTML 1.1' in result
        assert result.startswith(b'<?xml')
        assert etree.fromstring(result).tag == "{http://www.w3.org/1999/xhtml}html"

    def test_preserves_doctype_with_internal_newlines(self):
        """Doctype original é preservado exatamente, com quebras de linha internas."""
        doctype = (
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"\n'
            '  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
        )
        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            + doctype
            + '\n<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hi</p></body></html>\n'
        )
        tree, captured = parse_xhtml(xhtml.encode("utf-8"))
        assert captured == doctype
        result = serialize_xhtml(tree, captured)
        assert doctype.encode("utf-8") in result
        # o doctype continua íntegro e depois da declaração
        assert result.startswith(b'<?xml')
        assert result.index(b'<!DOCTYPE') > result.index(b'<?xml')

    def test_roundtrip_equivalent_tree(self):
        """parse_xhtml(serialize_xhtml(tree, doctype)) devolve árvore equivalente."""
        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">'
            '<head><title>Título</title></head>'
            '<body><p class="x">Oi <b> Mundo</b></p></body>'
            '</html>\n'
        )
        tree, doctype = parse_xhtml(xhtml.encode("utf-8"))
        result = serialize_xhtml(tree, doctype)

        tree2, doctype2 = parse_xhtml(result)
        assert doctype2 == doctype
        # árvores equivalentes (mesma serialização da raiz)
        assert etree.tostring(tree2.getroot()) == etree.tostring(tree.getroot())
        # e a segunda serialização é byte-idêntica à primeira
        assert serialize_xhtml(tree2, doctype2) == result


# ── Testes: EpubData ────────────────────────────────────────────────────────

class TestDataclass:
    """Testes de estrutura de dados."""

    def test_epubdata_defaults(self):
        """EpubData com defaults é instanciável."""
        data = EpubData()
        assert data.title == ""
        assert data.language == ""
        assert data.spine == []
        assert data.text_files == []
        assert data.ncx_paths == []
        assert data.all_items == {}
