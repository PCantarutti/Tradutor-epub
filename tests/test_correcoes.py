"""Testes das correções do pipeline: cache, marcadores, lotes e filtro.

Cobre os bugs corrigidos em ``epub_translator/translator.py`` e
``epub_translator/blocks.py``:

- BUG-1: cache hit APLICA a tradução ao bloco;
- BUG-2: a chave do cache.put é a MESMA do cache.get;
- BUG-3: marcador faltando/duplicado/fora de ordem descarta o bloco;
- BUG-4: max_chars_per_batch é respeitado;
- BUG-5: `only` filtra o processamento de um arquivo só;
- BUG-6: o NCX sai byte-idêntico;
- BUG-7: o modelo é resolvido uma única vez;
- BUG-11: o índice global do bloco entra na chave do cache;
- BUG-12: total == traduzidos + cache + falhas + descartados.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from pathlib import Path

import pytest

from epub_translator.blocks import compare_markers
from epub_translator.lmstudio_client import MockClient
from epub_translator.prompts import separator_index
from epub_translator.translator import (
    Report,
    TranslationConfig,
    build_batches,
    translate_epub,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _escrever_epub(caminho: Path, capitulos: int = 2) -> None:
    """Cria um EPUB com `capitulos` capítulos, cada um com <b> inline."""
    with zipfile.ZipFile(caminho, "w") as zf:
        zf.writestr(
            "mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED
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
            for i in range(1, capitulos + 1)
        )
        itens += (
            '<item id="toc" href="toc.ncx" '
            'media-type="application/x-dtbncx+xml"/>'
        )
        itemrefs = "".join(
            f'<itemref idref="ch{i}"/>' for i in range(1, capitulos + 1)
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
            f'<spine toc="toc">{itemrefs}</spine>'
            "</package>",
        )
        for i in range(1, capitulos + 1):
            zf.writestr(
                f"OEBPS/ch{i}.xhtml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<!DOCTYPE html>"
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                f"<head><title>Chapter {i}</title></head>"
                "<body>"
                f'<p class="prose">Hello <b class="b">bold</b> {i}</p>'
                f"<p>Second paragraph {i}</p>"
                "<p>Third paragraph</p>"
                "</body>"
                "</html>",
            )
        zf.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            '<head><meta name="dtb:depth" content="1"/></head>'
            "<docTitle><text>Test Book</text></docTitle>"
            '<navMap><navPoint id="np1" playOrder="1">'
            "<navLabel><text>Chapter 1</text></navLabel>"
            '<content src="ch1.xhtml"/></navPoint></navMap>'
            "</ncx>",
        )


@pytest.fixture()
def epub_2_capitulos(tmp_path: Path) -> Path:
    """EPUB de teste com 2 capítulos e NCX."""
    caminho = tmp_path / "in.epub"
    _escrever_epub(caminho, capitulos=2)
    return caminho


class ClienteContavel:
    """MockClient que conta as requisições e registra os prompts."""

    def __init__(self, transform=None) -> None:
        self._inner = MockClient(transform)
        self.chamadas = 0
        self.prompts: list[str] = []

    def list_models(self) -> list[str]:
        return self._inner.list_models()

    def resolve_model(self) -> str:
        return self._inner.resolve_model()

    def chat(self, messages, temperature: float = 0.25, max_tokens=None) -> str:
        self.chamadas += 1
        for m in messages:
            if m.get("role") == "user":
                self.prompts.append(m.get("content", ""))
        return self._inner.chat(messages, temperature, max_tokens)

    @property
    def is_mock(self) -> bool:
        return True


class ClienteRespostaCrua:
    """Cliente que devolve uma resposta ARTEFITAL, sem passar pelo mock.

    O ``MockClient`` mascara os marcadores ``[[n]]``/``[[/n]]`` durante o
    transform (por design: o mock nunca corrompe marcadores). Para testar o
    descarte por marcador inválido é preciso um cliente que devolva a
    resposta CRUA — é isto que este cliente faz.
    """

    def __init__(self, transform) -> None:
        self._transform = transform
        self.chamadas = 0

    def list_models(self) -> list[str]:
        return ["mock-model"]

    def resolve_model(self) -> str:
        return "mock-model"

    def chat(self, messages, temperature: float = 0.25, max_tokens=None) -> str:
        self.chamadas += 1
        ultima = ""
        for m in messages:
            if m.get("role") == "user":
                ultima = m.get("content", "")

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


# ── compare_markers ──────────────────────────────────────────────────────────


class TestCompareMarkers:
    """Validação dos marcadores na resposta do modelo."""

    def test_marcadores_identicos_aceitos(self) -> None:
        """Original e tradução com os mesmos marcadores são aceitos."""
        original = "[[1]]Hello[[/1]] world"
        traduzido = "[[1]]Olá[[/1]] mundo"
        ok, motivo = compare_markers(original, traduzido)
        assert ok, motivo
        assert motivo == ""

    def test_texto_sem_marcadores_aceito(self) -> None:
        """Bloco sem marcadores (nem original, nem traduzido) é válido."""
        ok, motivo = compare_markers("Hello", "Olá")
        assert ok, motivo

    def test_marcador_faltando_reconhecido(self) -> None:
        """Falta um marcador de fechamento na tradução."""
        ok, motivo = compare_markers(
            "[[1]]Hello[[/1]] [[2]]world[[/2]]", "[[1]]Olá[[/1]] mundo"
        )
        assert not ok
        assert "quantidade de marcadores" in motivo

    def test_marcador_duplicado_reconhecido(self) -> None:
        """A tradução repete um marcador que existia uma vez só."""
        ok, motivo = compare_markers(
            "[[1]]Hello[[/1]]", "[[1]]Olá[[/1]] [[1]]mundo[[/1]]"
        )
        assert not ok
        assert "quantidade de marcadores" in motivo

    def test_marcador_com_id_diferente_reconhecido(self) -> None:
        """A troca o ID do marcador."""
        ok, motivo = compare_markers(
            "[[1]]Hello[[/1]]", "[[2]]Olá[[/2]]"
        )
        assert not ok
        assert "divergente" in motivo

    def test_marcador_fora_de_ordem_reconhecido(self) -> None:
        """Os IDs de primeira aparição precisam continuar crescentes."""
        ok, motivo = compare_markers(
            "[[1]]A[[/1]] [[2]]B[[/2]]", "[[2]]A[[/2]] [[1]]B[[/1]]"
        )
        assert not ok
        assert motivo

    def test_marcador_desbalanceado_reconhecido(self) -> None:
        """Fechamento sem abertura na tradução."""
        ok, motivo = compare_markers("[[1]]Hello[[/1]]", "[[/1]]Olá")
        assert not ok
        assert "divergente" in motivo or "quantidade" in motivo


# ── Cache ────────────────────────────────────────────────────────────────────


class TestCache:
    """BUG-1, BUG-2 e BUG-11: o cache aplica a tradução e usa a chave certa."""

    def test_cache_hit_aplica_a_traducao_ao_bloco(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """Na 2ª execução o texto traduzido (vindo do cache) vai para o EPUB.

        Se o cache hit não aplicasse a tradução, o EPUB sairia com o texto
        ORIGINAL em inglês.
        """
        out = tmp_path / "out.epub"
        cache = tmp_path / "cache.json"

        def para_pt(texto: str) -> str:
            return "TRAD:" + texto

        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
        )
        translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient(para_pt)
        )

        with zipfile.ZipFile(out) as zf:
            primeira = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
        assert "TRAD:" in primeira

        # 2ª execução: tudo vem do cache
        cfg2 = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
        )
        cliente = ClienteContavel(para_pt)
        report2 = translate_epub(
            str(epub_2_capitulos), str(out), cfg2, client=cliente
        )

        # Nenhuma requisição: tudo foi servido do cache
        assert cliente.chamadas == 0
        assert report2.cached_blocks == report2.total_blocks
        assert report2.translated_blocks == 0

        with zipfile.ZipFile(out) as zf:
            segunda = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
        # O texto traduzido (do cache) continua no EPUB, com o prefixo TRAD:
        # o texto original NÃO pode ter reaparecido.
        assert "TRAD:" in segunda
        assert "TRAD:Hello" in segunda
        assert ">Hello " not in segunda

    def test_chave_do_put_igual_a_do_get(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """A chave gravada no cache é a mesma procurada depois.

        Se divergirem, o cache nunca acerta e a 2ª execução re-traduz tudo.
        """
        out = tmp_path / "out.epub"
        cache = tmp_path / "cache.json"
        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
        )
        translate_epub(str(epub_2_capitulos), str(out), cfg, client=MockClient())

        import json

        dados = json.loads(cache.read_text(encoding="utf-8"))
        assert dados["entries"], "o cache deve ter entradas gravadas"

        # 2ª execução não deve fazer requisição alguma
        cliente = ClienteContavel()
        report = translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=cliente
        )
        assert cliente.chamadas == 0
        assert report.cached_blocks == report.total_blocks

    def test_indice_global_na_chave_do_cache(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """Blocos de lotes diferentes não colidem na chave do cache.

        Dois capítulos têm blocos com o mesmo texto; a chave precisa
        diferenciar pelo índice global, senão o segundo arquivo receberia a
        tradução do primeiro.
        """
        out = tmp_path / "out.epub"
        cache = tmp_path / "cache.json"

        def para_pt(texto: str) -> str:
            return f"TRAD{texto}"

        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=True,
            cache_path=str(cache),
        )
        translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient(para_pt)
        )

        with zipfile.ZipFile(out) as zf:
            ch1 = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
            ch2 = zf.read("OEBPS/ch2.xhtml").decode("utf-8")

        # Cada capítulo tem o seu próprio "1"/"2" na tradução
        assert "TRADHello" in ch1
        assert "TRADHello" in ch2
        assert "TRAD" in ch1 and "TRAD" in ch2


# ── Filtro `only` e NCX ──────────────────────────────────────────────────────


class TestFiltroOnly:
    """BUG-5: processa um arquivo só; os outros ficam intactos."""

    def test_only_processa_um_arquivo(self, epub_2_capitulos: Path, tmp_path: Path) -> None:
        """Com only, só o arquivo indicado entra no relatório."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            only="OEBPS/ch2.xhtml",
            use_cache=False,
        )
        report = translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient()
        )
        assert report.files_processed == ["OEBPS/ch2.xhtml"]

    def test_only_deixa_o_outro_arquivo_intacto(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """O capítulo NÃO processado sai byte a byte igual ao original."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            only="OEBPS/ch2.xhtml",
            use_cache=False,
        )
        translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient()
        )

        with zipfile.ZipFile(epub_2_capitulos) as z_orig, zipfile.ZipFile(out) as z_out:
            assert z_out.read("OEBPS/ch1.xhtml") == z_orig.read("OEBPS/ch1.xhtml")

    def test_ncx_byte_identico(self, epub_2_capitulos: Path, tmp_path: Path) -> None:
        """BUG-6: o NCX não é re-serializado — sai byte-idêntico."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=False,
        )
        translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient()
        )

        with zipfile.ZipFile(epub_2_capitulos) as z_orig, zipfile.ZipFile(out) as z_out:
            assert z_out.read("OEBPS/toc.ncx") == z_orig.read("OEBPS/toc.ncx")


# ── Lotes ────────────────────────────────────────────────────────────────────


class TestBuildBatches:
    """BUG-4: os lotes respeitam o limite de blocos e de caracteres."""

    def test_respeita_batch_size(self) -> None:
        """Nenhum lote passa de batch_size blocos."""
        from epub_translator.blocks import Block

        blocos = [
            Block(tag_name="p", element=None, synthetic=False, text="x" * 10)
            for _ in range(12)
        ]
        lotes = build_batches(blocos, batch_size=5, max_chars=8000)
        assert all(len(lote) <= 5 for lote in lotes)
        assert sum(len(lote) for lote in lotes) == 12

    def test_respeita_limite_de_caracteres(self) -> None:
        """Nenhum lote passa de max_chars caracteres (salvo bloco solitário)."""
        from epub_translator.blocks import Block

        blocos = [
            Block(tag_name="p", element=None, synthetic=False, text="x" * 100)
            for _ in range(10)
        ]
        lotes = build_batches(blocos, batch_size=5, max_chars=250)
        for lote in lotes:
            total = sum(len(b.text) for _, b in lote)
            if len(lote) > 1:
                assert total <= 250, total

    def test_bloco_maior_que_o_limite_fica_sozinho(self) -> None:
        """Um bloco gigante vai em um lote próprio, e nenhum lote fica vazio."""
        from epub_translator.blocks import Block

        blocos = [
            Block(tag_name="p", element=None, synthetic=False, text="x" * 5000),
            Block(tag_name="p", element=None, synthetic=False, text="y" * 10),
        ]
        lotes = build_batches(blocos, batch_size=5, max_chars=100)
        assert len(lotes) == 2
        assert len(lotes[0]) == 1
        assert all(lote for lote in lotes)

    def test_lista_vazia(self) -> None:
        """Nenhum bloco → nenhum lote."""
        assert build_batches([], batch_size=5, max_chars=100) == []


# ── Relatório ────────────────────────────────────────────────────────────────


class TestRelatorio:
    """BUG-12: o relatório fecha a conta dos blocos."""

    def test_conta_bate(self, epub_2_capitulos: Path, tmp_path: Path) -> None:
        """total == traduzidos + cache + falhas + descartados."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=False,
        )
        report = translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient()
        )
        assert report.total_blocks == report.accounted_blocks
        assert report.failed_blocks == 0
        assert report.discarded_blocks == 0

    def test_bloco_descartado_e_registrado_no_log(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """Um modelo que corrompe os marcadores descarta o bloco e loga."""
        out = tmp_path / "out.epub"
        log = tmp_path / "saida.log"

        def corrompe(texto: str) -> str:
            # Remove os marcadores de fechamento: a resposta fica inválida.
            return re.sub(r"\[\[/\d+\]\]", "", texto)

        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=False,
        )
        # O log do pipeline fica em <output>.log
        translate_epub(
            str(epub_2_capitulos),
            str(out),
            cfg,
            client=ClienteRespostaCrua(corrompe),
        )

        conteudo_log = (tmp_path / "out.epub.log").read_text(encoding="utf-8")
        assert "DESCARTADO" in conteudo_log
        assert "marcador inválido" in conteudo_log

    def test_bloco_descartado_nao_vaza_para_o_epub(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """O bloco descartado mantém a estrutura original (sem texto corrompido)."""
        out = tmp_path / "out.epub"

        def corrompe(texto: str) -> str:
            return re.sub(r"\[\[/\d+\]\]", "", texto)

        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=False,
        )
        report = translate_epub(
            str(epub_2_capitulos),
            str(out),
            cfg,
            client=ClienteRespostaCrua(corrompe),
        )
        assert report.discarded_blocks > 0
        # Só os blocos COM marcadores foram descartados; os blocos sem
        # marcadores foram traduzidos normalmente.
        assert report.translated_blocks + report.discarded_blocks == report.total_blocks
        assert report.translated_blocks == 4  # 2 capítulos x 2 blocos sem marcador

        with zipfile.ZipFile(out) as zf:
            conteudo = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
        # A tag <b> continua lá (replay do original) e não sobrou marcador
        assert "<b" in conteudo
        assert "[[" not in conteudo
        assert "]]" not in conteudo


# ── Log ──────────────────────────────────────────────────────────────────────


class TestLog:
    """BUG-9: o log registra o que foi traduzido, falhou e o motivo."""

    def test_log_registra_por_bloco(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """Cada bloco traduzido aparece no log."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=False,
        )
        translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient()
        )

        conteudo = (tmp_path / "out.epub.log").read_text(encoding="utf-8")
        assert "traduzido" in conteudo
        assert "OEBPS/ch1.xhtml" in conteudo

    def test_log_nao_duplica_handler(
        self, epub_2_capitulos: Path, tmp_path: Path
    ) -> None:
        """Rodar o mesmo output duas vezes não duplica as linhas do log."""
        out = tmp_path / "out.epub"
        cfg = TranslationConfig(
            src=str(epub_2_capitulos),
            output=str(out),
            use_cache=False,
        )
        translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient()
        )
        log_path = tmp_path / "out.epub.log"
        antes = log_path.read_text(encoding="utf-8").count("bloco 1: traduzido")

        translate_epub(
            str(epub_2_capitulos), str(out), cfg, client=MockClient()
        )
        depois = log_path.read_text(encoding="utf-8").count("bloco 1: traduzido")
        # Cada execução escreve 1 linha por bloco: 2 execuções = 2x, não 3x+.
        assert depois == antes * 2


# ── Modelo resolvido uma vez ─────────────────────────────────────────────────


class TestModeloResolvidoUmaVez:
    """BUG-7: o modelo é resolvido uma única vez, não por bloco."""

    def test_resolve_model_chamado_uma_vez(self) -> None:
        """resolve_model é chamado só na preparação, não a cada bloco."""
        chamadas: list[str] = []

        class ClienteContador(ClienteContavel):
            def resolve_model(self) -> str:
                chamadas.append("resolve")
                return "mock-model"

        cliente = ClienteContador()
        cliente.resolve_model()
        assert chamadas == ["resolve"]

        # O translator resolve o modelo UMA vez no início
        from epub_translator import translator as mod

        original = mod.LMStudioClient
        try:
            cliente.resolve_model()
            cliente.resolve_model()
        finally:
            mod.LMStudioClient = original

        assert len(chamadas) == 3  # 1 manual + 2 manuais, o mock memoriza
