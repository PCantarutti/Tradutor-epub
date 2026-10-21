"""Testes da interface de linha de comando (CLI)."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from epub_translator.cli import _build_parser, main
from epub_translator.lmstudio_client import MockClient


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_dummy_epub(path: Path) -> None:
    """Cria um EPUB mínimo válido para testes."""
    with zipfile.ZipFile(path, "w") as zf:
        # mimetype (ZIP_STORED, primeiro)
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        # META-INF/container.xml
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
            "</container>",
        )
        # OEBPS/content.opf — href usa caminho relativo correto
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
            '<manifest><item id="ch1" href="OEBPS/ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="toc" href="OEBPS/toc.ncx" media-type="application/x-dtbncx+xml"/>'
            "</manifest>"
            '<spine><itemref idref="ch1"/></spine>'
            "</package>",
        )
        # OEBPS/ch1.xhtml
        zf.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            "<head><title>Chapter 1</title></head>"
            "<body><p>Hello world</p></body>"
            "</html>",
        )
        # NCX
        zf.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            '<head><meta name="dtb:depth" content="1"/></head>'
            '<docTitle><text>Test Book</text></docTitle>'
            '<navMap><navPoint id="1"><navLabel><text>Chapter 1</text></navLabel>'
            '<content src="OEBPS/ch1.xhtml"/></navPoint></navMap>'
            "</ncx>",
        )


@pytest.fixture()
def tmp_epub(tmp_path: Path) -> Path:
    """EPUB dummy em diretório temporário."""
    epub_path = tmp_path / "test.epub"
    _make_dummy_epub(epub_path)
    return epub_path


# ── Testes: --help ───────────────────────────────────────────────────────────


class TestHelp:
    """Testes do --help."""

    def test_help_exits_cleanly(self) -> None:
        """--help deve exibir ajuda e sair com código 0."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_help_output_contains_expected_sections(self, capsys: pytest.CaptureFixture) -> None:
        """A saída de --help deve conter seções esperadas."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "epub_translator" in captured.out.lower() or "Traduz" in captured.out
        assert "--src" in captured.out or "src" in captured.out
        assert "--target" in captured.out
        assert "--output" in captured.out
        assert "--mock" in captured.out
        assert "--list-models" in captured.out


# ── Testes: --mock cria output ───────────────────────────────────────────────


class TestMockOutput:
    """Testes com --mock gerando arquivo de saída."""

    def test_mock_creates_output_epub(self, tmp_epub: Path, tmp_path: Path) -> None:
        """--mock deve criar o EPUB de saída."""
        output = str(tmp_path / "output.epub")
        main([str(tmp_epub), "--mock", "--output", output])
        assert Path(output).exists(), "Arquivo de saída não foi criado"
        # Verifica que é um ZIP válido
        assert zipfile.is_zipfile(output), "Saída não é um arquivo ZIP/EPUB válido"

    def test_mock_default_output_name(self, tmp_epub: Path, tmp_path: Path) -> None:
        """Saída padrão deve ficar na pasta saida/ ao lado do src."""
        expected = str(
            tmp_epub.parent / "saida" / (tmp_epub.stem + "_translated" + tmp_epub.suffix)
        )
        main([str(tmp_epub), "--mock"])
        assert Path(expected).exists(), f"Saída padrão '{expected}' não foi criada"

    def test_mock_with_limit(self, tmp_epub: Path, tmp_path: Path) -> None:
        """--mock com --limit deve funcionar."""
        output = str(tmp_path / "limited.epub")
        main([str(tmp_epub), "--mock", "--output", output, "--limit", "1"])
        assert Path(output).exists(), "Saída com limit não foi criada"
        assert zipfile.is_zipfile(output)


# ── Testes: --list-models com MockClient ─────────────────────────────────────


class TestListModels:
    """Testes de --list-models usando MockClient."""

    def test_list_models_with_mock(self, tmp_epub: Path, tmp_path: Path,
                                    capsys: pytest.CaptureFixture) -> None:
        """--list-models com --mock deve listar modelos mock e sair."""
        output = str(tmp_path / "out.epub")
        main([str(tmp_epub), "--mock", "--list-models", "--output", output])
        captured = capsys.readouterr()
        assert "mock-model" in captured.out.lower()

    def test_list_models_no_models(self, tmp_epub: Path, tmp_path: Path,
                                    capsys: pytest.CaptureFixture) -> None:
        """--list-models sem modelos deve exibir aviso."""
        output = str(tmp_path / "out.epub")
        with patch("epub_translator.cli.LMStudioClient") as MockCls:
            mock_instance = MagicMock()
            mock_instance.list_models.return_value = []
            MockCls.return_value = mock_instance
            main([str(tmp_epub), "--list-models", "--output", output])
            captured = capsys.readouterr()
            assert "nenhum modelo" in captured.out.lower() or "no model" in captured.out.lower()


# ── Testes: argumentos default ───────────────────────────────────────────────


class TestDefaults:
    """Testes dos valores padrão."""

    def test_default_target(self) -> None:
        """--target padrão deve ser pt-BR."""
        parser = _build_parser()
        args = parser.parse_args(["dummy.epub"])
        assert args.target == "pt-BR"

    def test_default_output(self) -> None:
        """--output padrão deve ser <src>_translated.epub."""
        parser = _build_parser()
        args = parser.parse_args(["livro.epub"])
        assert args.output is None  # main() calcula o default

    def test_default_batch_size(self) -> None:
        """--batch-size padrão deve ser 5."""
        parser = _build_parser()
        args = parser.parse_args(["dummy.epub"])
        assert args.batch_size == 5

    def test_default_temperature(self) -> None:
        """--temperature padrão deve ser 0.25."""
        parser = _build_parser()
        args = parser.parse_args(["dummy.epub"])
        assert args.temperature == 0.25

    def test_default_base_url(self) -> None:
        """--base-url padrão deve ser http://localhost:1234/v1."""
        parser = _build_parser()
        args = parser.parse_args(["dummy.epub"])
        assert args.base_url == "http://localhost:1234/v1"

    def test_default_max_chars(self) -> None:
        """--max-chars padrão deve ser 8000."""
        parser = _build_parser()
        args = parser.parse_args(["dummy.epub"])
        assert args.max_chars == 8000

    def test_default_max_attempts(self) -> None:
        """--max-attempts padrão deve ser 3."""
        parser = _build_parser()
        args = parser.parse_args(["dummy.epub"])
        assert args.max_attempts == 3


# ── Testes: --no-cache ───────────────────────────────────────────────────────


class TestNoCache:
    """Testes de --no-cache."""

    def test_no_cache_flag(self) -> None:
        """--no-cache deve desabilitar cache."""
        parser = _build_parser()
        args = parser.parse_args(["dummy.epub", "--no-cache"])
        assert args.no_cache is True
        # --use-cache ainda é True por padrão, mas --no-cache prevalece no código

    def test_no_cache_in_main(self, tmp_epub: Path, tmp_path: Path) -> None:
        """--no-cache na tradução deve funcionar."""
        output = str(tmp_path / "nocache.epub")
        main([str(tmp_epub), "--mock", "--output", output, "--no-cache"])
        assert Path(output).exists()
        assert zipfile.is_zipfile(output)


# ── Testes: --glossary ───────────────────────────────────────────────────────


class TestGlossary:
    """Testes de --glossary."""

    def test_glossary_yaml(self, tmp_epub: Path, tmp_path: Path) -> None:
        """--glossary com YAML válido deve funcionar."""
        glossary_path = tmp_path / "glossary.yaml"
        glossary_path.write_text("hello: oi\nworld: mundo\n", encoding="utf-8")
        output = str(tmp_path / "gloss.epub")
        main([str(tmp_epub), "--mock", "--output", output, "--glossary", str(glossary_path)])
        assert Path(output).exists()
        assert zipfile.is_zipfile(output)

    def test_glossary_missing_file(self, tmp_epub: Path) -> None:
        """--glossary com arquivo inexistente deve sair com erro."""
        with pytest.raises(SystemExit) as exc_info:
            main([str(tmp_epub), "--glossary", "/nonexistent/glossary.yaml"])
        assert exc_info.value.code != 0

    def test_glossary_json(self, tmp_epub: Path, tmp_path: Path) -> None:
        """--glossary com JSON válido deve funcionar (JSON é YAML válido)."""
        glossary_path = tmp_path / "glossary.json"
        glossary_path.write_text(
            '{"hello": "oi", "world": "mundo"}', encoding="utf-8"
        )
        output = str(tmp_path / "gloss_json.epub")
        main([str(tmp_epub), "--mock", "--output", output, "--glossary", str(glossary_path)])
        assert Path(output).exists()
        assert zipfile.is_zipfile(output)

    def test_glossary_json_do_projeto(self, tmp_epub: Path, tmp_path: Path) -> None:
        """--glossary aceita o JSON de glossarios/ do próprio projeto."""
        raiz = Path(__file__).resolve().parent.parent
        glossary_path = raiz / "glossarios" / "mushoku_tensei.json"
        assert glossary_path.exists(), f"glossário ausente: {glossary_path}"
        output = str(tmp_path / "gloss_projeto.epub")
        main([str(tmp_epub), "--mock", "--output", output, "--glossary", str(glossary_path)])
        assert Path(output).exists()
        assert zipfile.is_zipfile(output)


# ── Testes: --bilingual ──────────────────────────────────────────────────────


class TestBilingual:
    """Testes de --bilingual."""

    def test_bilingual_creates_output(self, tmp_epub: Path, tmp_path: Path) -> None:
        """--bilingual deve criar saída com texto original."""
        output = str(tmp_path / "bilingual.epub")
        main([str(tmp_epub), "--mock", "--output", output, "--bilingual"])
        assert Path(output).exists()
        # Verifica que o XHTML traduzido contém o comentário ORIGINAL
        with zipfile.ZipFile(output) as zf:
            content = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
            assert "ORIGINAL" in content
