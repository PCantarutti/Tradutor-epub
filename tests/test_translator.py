"""Testes de integração do translator (pipeline prompt -> parse -> replay)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from epub_translator.epub_io import set_opf_title
from epub_translator.lmstudio_client import MockClient
from epub_translator.translator import TranslationConfig, translate_epub


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_dummy_epub(path: Path) -> None:
    """Cria um EPUB mínimo com um parágrafo, um <b> inline e um <span>."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles>'
            "</container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="uid">urn:uuid:00000000-0000-0000-0000-000000000001</dc:identifier>'
            '<dc:language>en</dc:language>'
            '<dc:title>Test Book</dc:title>'
            '<meta property="dcterms:modified">2025-01-01T00:00:00Z</meta>'
            "</metadata>"
            '<manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest>"
            '<spine><itemref idref="ch1"/></spine>'
            "</package>",
        )
        zf.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            "<head><title>Chapter 1</title></head>"
            "<body>"
            '<p class="prose">Hello <b class="b">bold</b> world</p>'
            "<p>Second paragraph</p>"
            "</body>"
            "</html>",
        )


@pytest.fixture()
def dummy_epub(tmp_path: Path) -> Path:
    """EPUB dummy em diretório temporário."""
    path = tmp_path / "in.epub"
    _make_dummy_epub(path)
    return path


def _make_doctype_epub(path: Path) -> None:
    """Cria um EPUB com OPF realisticamente completo (doctype, NCX, nav)."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles>'
            "</container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE package PUBLIC "-//OEBPS//DTD Package 2007-03-01//EN" '
            '"http://www.idpf.org/2007/opf/opf.dtd">\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="uid">urn:uuid:00000000-0000-0000-0000-000000000001</dc:identifier>'
            '<dc:language>en</dc:language>'
            '<dc:title>Test Book</dc:title>'
            '<meta property="dcterms:modified">2025-01-01T00:00:00Z</meta>'
            "</metadata>"
            '<manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            "</manifest>"
            '<spine toc="toc"><itemref idref="ch1"/></spine>'
            "</package>",
        )
        zf.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            "<head><title>Chapter 1</title></head>"
            "<body><p>Hello world</p></body>"
            "</html>",
        )
        zf.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            '<head><meta name="dtb:depth" content="1"/></head>'
            '<docTitle><text>Test Book</text></docTitle>'
            '<navMap><navPoint id="np1" playOrder="1">'
            '<navLabel><text>Chapter 1</text></navLabel>'
            '<content src="ch1.xhtml"/></navPoint></navMap>'
            "</ncx>",
        )


@pytest.fixture()
def doctype_epub(tmp_path: Path) -> Path:
    """EPUB com doctype no OPF e NCX no diretório temporário."""
    path = tmp_path / "doctype.epub"
    _make_doctype_epub(path)
    return path


# ── Testes ───────────────────────────────────────────────────────────────────

class TestTranslateEpub:
    """Testes do pipeline completo (prompt -> parse -> replay -> EPUB)."""

    def test_texto_final_nao_tem_prefixo_de_tag(
        self, dummy_epub: Path, tmp_path: Path
    ) -> None:
        """O prefixo de tag ('p: ') não pode aparecer no texto final."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(dummy_epub),
            output=str(out),
            use_cache=False,
        )
        report = translate_epub(str(dummy_epub), str(out), cfg, client=MockClient())

        assert report.failed_blocks == 0
        assert report.translated_blocks == 2

        with zipfile.ZipFile(out) as zf:
            content = zf.read("OEBPS/ch1.xhtml").decode("utf-8")

        # O texto dos parágrafos não pode começar com 'p: '
        textos = re.findall(r"<p[^>]*>(.*?)</p>", content, re.S)
        assert textos
        for texto in textos:
            assert not re.match(r"^\s*(p|h1|h2|h3|li|td|th|text):\s", texto), texto

    def test_tags_inline_preservadas_no_epub_final(
        self, dummy_epub: Path, tmp_path: Path
    ) -> None:
        """As tags inline originais continuam no EPUB com os mesmos atributos."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(dummy_epub),
            output=str(out),
            use_cache=False,
        )
        translate_epub(str(dummy_epub), str(out), cfg, client=MockClient())

        with zipfile.ZipFile(out) as zf:
            content = zf.read("OEBPS/ch1.xhtml").decode("utf-8")

        assert '<b class="b">bold</b>' in content
        assert "Hello" in content
        assert "Second paragraph" in content

    def test_marcadores_inline_nao_vazam_para_o_epub(
        self, dummy_epub: Path, tmp_path: Path
    ) -> None:
        """Marcadores [[n]] são consumidos no replay e não vazam no EPUB."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(dummy_epub),
            output=str(out),
            use_cache=False,
        )
        translate_epub(str(dummy_epub), str(out), cfg, client=MockClient())

        with zipfile.ZipFile(out) as zf:
            content = zf.read("OEBPS/ch1.xhtml").decode("utf-8")

        assert "[[" not in content
        assert "]]" not in content


# ── Testes: OPF de saída (dc:language / dc:title) ───────────────────────────

class TestOpfOutput:
    """Testes do OPF gravado no EPUB de saída."""

    @staticmethod
    def _read_opf(epub: Path) -> bytes:
        """Lê o OPF do EPUB (caminho real da entrada no zip)."""
        with zipfile.ZipFile(epub) as zf:
            return zf.read("OEBPS/content.opf")

    def test_dc_language_atualizado_no_epub_de_saida(
        self, dummy_epub: Path, tmp_path: Path
    ) -> None:
        """O OPF gravado na saída tem dc:language = target_lang."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(dummy_epub),
            output=str(out),
            target_lang="pt-BR",
            use_cache=False,
        )
        translate_epub(str(dummy_epub), str(out), cfg, client=MockClient())

        opf = self._read_opf(out)
        root = etree.fromstring(opf)  # parse estrito: não pode falhar
        lang = root.xpath(
            "string(//*[local-name()='metadata']/*[local-name()='language'])"
        )
        assert lang == "pt-BR"

    def test_dc_title_preservado_no_epub_de_saida(
        self, dummy_epub: Path, tmp_path: Path
    ) -> None:
        """O dc:title original é preservado (não há tradução de título)."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(dummy_epub),
            output=str(out),
            target_lang="pt-BR",
            use_cache=False,
        )
        translate_epub(str(dummy_epub), str(out), cfg, client=MockClient())

        root = etree.fromstring(self._read_opf(out))
        title = root.xpath(
            "string(//*[local-name()='metadata']/*[local-name()='title'])"
        )
        assert title == "Test Book"

    def test_set_opf_title_nao_e_chamado_sem_traducao(
        self, dummy_epub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem tradução de título, set_opf_title não deve ser chamado.

        Reescrever o dc:title com o valor original é um no-op que ainda
        reserializa o OPF — o título original deve ficar intacto no EPUB.
        """
        calls: list[str] = []

        def _spy(opf_bytes: bytes, title: str) -> bytes:
            calls.append(title)
            return set_opf_title(opf_bytes, title)

        monkeypatch.setattr(
            "epub_translator.translator.set_opf_title", _spy
        )

        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(dummy_epub),
            output=str(out),
            target_lang="pt-BR",
            use_cache=False,
        )
        translate_epub(str(dummy_epub), str(out), cfg, client=MockClient())

        assert calls == []

    def test_opf_de_saida_continua_xml_valido(
        self, dummy_epub: Path, tmp_path: Path
    ) -> None:
        """O OPF de saída é XML válido e preserva manifest, spine e metadata."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(dummy_epub),
            output=str(out),
            target_lang="pt-BR",
            use_cache=False,
        )
        translate_epub(str(dummy_epub), str(out), cfg, client=MockClient())

        opf = self._read_opf(out)

        # (a) parse estrito com lxml — sem XMLSyntaxError
        root = etree.fromstring(opf)
        assert root.tag == "{http://www.idpf.org/2007/opf}package"

        # (b) o resto do metadata continua lá
        identifier = root.xpath(
            "string(//*[local-name()='metadata']/*[local-name()='identifier'])"
        )
        assert identifier == "urn:uuid:00000000-0000-0000-0000-000000000001"

        # (c) manifest e spine preservados
        manifest = root.xpath("//*[local-name()='manifest']/*[local-name()='item']")
        assert [item.get("href") for item in manifest] == ["ch1.xhtml"]
        spine = root.xpath("//*[local-name()='spine']/*[local-name()='itemref']")
        assert [item.get("idref") for item in spine] == ["ch1"]

    def test_opf_de_saida_preserva_doctype_e_estrutura(
        self, doctype_epub: Path, tmp_path: Path
    ) -> None:
        """O OPF gravado mantém o doctype e a declaração XML no lugar."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(doctype_epub),
            output=str(out),
            target_lang="pt-BR",
            use_cache=False,
        )
        translate_epub(str(doctype_epub), str(out), cfg, client=MockClient())

        with zipfile.ZipFile(out) as zf:
            opf = zf.read("OEBPS/content.opf")

        # declaração XML primeiro, como exige o XML
        assert opf.startswith(b"<?xml")
        # doctype do OPF preservado
        assert b"<!DOCTYPE package" in opf
        assert b"http://www.idpf.org/2007/opf/opf.dtd" in opf
        # e o lxml continua parseando o documento inteiro
        root = etree.fromstring(opf)
        assert root.xpath(
            "string(//*[local-name()='metadata']/*[local-name()='language'])"
        ) == "pt-BR"

    def test_ncx_de_saida_permanece_integro(
        self, doctype_epub: Path, tmp_path: Path
    ) -> None:
        """O NCX é gravado sem corrupção (parseável, com a estrutura intacta)."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(doctype_epub),
            output=str(out),
            target_lang="pt-BR",
            use_cache=False,
        )
        translate_epub(str(doctype_epub), str(out), cfg, client=MockClient())

        with zipfile.ZipFile(out) as zf:
            ncx = zf.read("OEBPS/toc.ncx")

        root = etree.fromstring(ncx)
        ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
        assert root.xpath("//ncx:docTitle//ncx:text/text()", namespaces=ns) == [
            "Test Book"
        ]
        assert root.xpath("//ncx:navLabel//ncx:text/text()", namespaces=ns) == [
            "Chapter 1"
        ]
        nav_point = root.xpath("//ncx:navPoint", namespaces=ns)[0]
        assert nav_point.get("id") == "np1"
        assert nav_point.get("playOrder") == "1"
