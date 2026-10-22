"""Testes do script de entrega ``traduzir.py``.

Cobre a CLI de entrega e a verificação final (itens a-g do enunciado).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from lxml import etree

import traduzir

RAIZ = Path(__file__).resolve().parent.parent
EPUB_VALIDACAO = RAIZ / "validacao.epub"
XHTML_ALVO = "OEBPS/Text/section-0005.html"
ARQUIVO_GLOSSARIO = RAIZ / "glossarios" / "mushoku_tensei.json"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def epub_mock(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Gera um EPUB traduzido com MockClient (tradução identity)."""
    if not EPUB_VALIDACAO.exists():
        pytest.skip(f"EPUB de validação ausente: {EPUB_VALIDACAO}")

    tmp = tmp_path_factory.mktemp("traduzir")
    out = tmp / "mock.epub"
    log = tmp / "mock.log"

    codigo = traduzir.main(
        [
            "--mock",
            "--src",
            str(EPUB_VALIDACAO),
            "--output",
            str(out),
            "--log",
            str(log),
            "--only",
            XHTML_ALVO,
        ]
    )
    assert codigo == 0, "a execução com mock deve terminar com sucesso"
    return out


# ── Verificação final ────────────────────────────────────────────────────────


class TestVerificacaoFinal:
    """A verificação final (a)-(g) do enunciado."""

    def test_todos_itens_passam(self, epub_mock: Path) -> None:
        """A verificação de um EPUB válido passa em todos os itens."""
        v = traduzir.verificar_epub(
            str(EPUB_VALIDACAO), str(epub_mock), XHTML_ALVO, "pt-BR"
        )
        assert v.ok, "\n".join(v.linhas())

    def test_conta_de_paragrafos_e_spans(self, epub_mock: Path) -> None:
        """Item (a): 229 <p> e 198 <span>, igual ao original."""
        with zipfile.ZipFile(epub_mock) as zf:
            saida = zf.read(XHTML_ALVO)
        with zipfile.ZipFile(EPUB_VALIDACAO) as zf:
            original = zf.read(XHTML_ALVO)

        def conta(b: bytes) -> tuple[int, int]:
            root = etree.fromstring(b)
            return (
                len(root.xpath("//*[local-name()='p']")),
                len(root.xpath("//*[local-name()='span']")),
            )

        assert conta(saida) == conta(original) == (229, 198)

    def test_nenhum_marcador_sobrando(self, epub_mock: Path) -> None:
        """Item (b): nenhum [[n]] no texto final."""
        with zipfile.ZipFile(epub_mock) as zf:
            for nome in zf.namelist():
                if nome.endswith((".html", ".xhtml")):
                    texto = zf.read(nome).decode("utf-8")
                    assert "[[" not in texto, nome

    def test_opf_com_idioma_e_titulo(self, epub_mock: Path) -> None:
        """Item (c): dc:language = pt-BR e dc:title intacto."""
        with zipfile.ZipFile(epub_mock) as zf:
            opf = zf.read("OEBPS/content.opf")
        root = etree.fromstring(opf)
        idioma = root.xpath(
            "string(//*[local-name()='metadata']/*[local-name()='language'])"
        )
        titulo = root.xpath(
            "string(//*[local-name()='metadata']/*[local-name()='title'])"
        )
        assert idioma == "pt-BR"
        assert titulo == "Mushoku Tensei: Jobless Reincarnation Vol. 15"

    def test_binarios_byte_identicos(self, epub_mock: Path) -> None:
        """Item (d): imagens e CSS byte a byte iguais."""
        with zipfile.ZipFile(epub_mock) as zf_out:
            with zipfile.ZipFile(EPUB_VALIDACAO) as zf_orig:
                binarios = [
                    n
                    for n in zf_orig.namelist()
                    if n.lower().endswith(traduzir.EXTENSOES_BINARIAS)
                ]
                assert binarios
                for nome in binarios:
                    assert zf_out.read(nome) == zf_orig.read(nome), nome

    def test_mimetype_primeira_entrada_stored(self, epub_mock: Path) -> None:
        """Item (e): mimetype é a primeira entrada e está ZIP_STORED."""
        with zipfile.ZipFile(epub_mock) as zf:
            nomes = zf.namelist()
            info = zf.getinfo("mimetype")
        assert nomes[0] == "mimetype"
        assert info.compress_type == zipfile.ZIP_STORED

    def test_sem_duplicadas_e_total_preservado(self, epub_mock: Path) -> None:
        """Item (f): sem duplicadas e 51 entradas."""
        with zipfile.ZipFile(epub_mock) as zf:
            nomes = zf.namelist()
        with zipfile.ZipFile(EPUB_VALIDACAO) as zf:
            total = len(zf.namelist())
        assert len(nomes) == len(set(nomes))
        assert len(nomes) == total == 51

    def test_zip_e_xhtml_validos(self, epub_mock: Path) -> None:
        """Item (g): zip íntegro e XHTML é XML válido (parse estrito)."""
        with zipfile.ZipFile(epub_mock) as zf:
            assert zf.testzip() is None
            etree.fromstring(zf.read(XHTML_ALVO))  # XMLSyntaxError se inválido


# ── Verificação detecta problemas ────────────────────────────────────────────


class TestVerificacaoDetectaProblemas:
    """A verificação não pode dar "OK" para um EPUB quebrado."""

    def test_arquivo_inexistente_falha(self, tmp_path: Path) -> None:
        """EPUB de saída ausente → verificação falha."""
        v = traduzir.verificar_epub(
            str(EPUB_VALIDACAO),
            str(tmp_path / "nao_existe.epub"),
            XHTML_ALVO,
            "pt-BR",
        )
        assert not v.ok

    def test_idioma_errado_e_detectado(self, epub_mock: Path) -> None:
        """dc:language diferente do esperado → item reprovado."""
        v = traduzir.verificar_epub(
            str(EPUB_VALIDACAO), str(epub_mock), XHTML_ALVO, "en"
        )
        assert not v.ok
        assert any("dc:language" in r.item for r in v.resultados if not r.ok)

    def test_marcador_sobrando_e_detectado(
        self, epub_mock: Path, tmp_path: Path
    ) -> None:
        """Um [[n]] no texto final → item reprovado."""
        corrompido = tmp_path / "corrompido.epub"
        with zipfile.ZipFile(epub_mock) as zf_out:
            conteudo = zf_out.read(XHTML_ALVO).decode("utf-8")
            conteudo = conteudo.replace("</p>", " [[999]]</p>", 1)
            with zipfile.ZipFile(corrompido, "w") as zf:
                for nome in zf_out.namelist():
                    if nome == XHTML_ALVO:
                        zf.writestr(nome, conteudo)
                    else:
                        zf.writestr(nome, zf_out.read(nome))

        v = traduzir.verificar_epub(
            str(EPUB_VALIDACAO), str(corrompido), XHTML_ALVO, "pt-BR"
        )
        assert not v.ok
        assert any("marcador" in r.item for r in v.resultados if not r.ok)


# ── Glossário ────────────────────────────────────────────────────────────────


class TestGlossario:
    """O glossário de nomes próprios vive num arquivo JSON, não no código."""

    def test_arquivo_json_existe_e_e_valido(self) -> None:
        """O glossário padrão existe na pasta glossarios/ e é um objeto JSON."""
        assert ARQUIVO_GLOSSARIO.exists(), (
            f"glossário padrão ausente: {ARQUIVO_GLOSSARIO}"
        )
        with ARQUIVO_GLOSSARIO.open(encoding="utf-8") as f:
            dados = json.load(f)
        assert isinstance(dados, dict)
        assert dados, "o glossário não pode estar vazio"

    def test_termos_principais_presentes(self) -> None:
        """Os nomes próprios do enunciado estão no glossário."""
        glossario = traduzir.carregar_glossario()
        for termo in ("Man-God", "Nanahoshi", "Eris", "Sylphie", "Roxy", "Ariel"):
            assert termo in glossario, termo

    def test_nomes_e_titulos_mapeados_para_si_mesmos(self) -> None:
        """Nomes e títulos de poder não são traduzidos; termos de magia e
        instituições têm tradução fixa; palavras comuns ficam de fora."""
        with ARQUIVO_GLOSSARIO.open(encoding="utf-8") as f:
            dados = json.load(f)
        for nome in ("Rudeus", "Orsted", "Zanoba", "Man-God", "Sword God", "North God"):
            assert dados[nome] == nome, nome
        assert dados["Stone Cannon"] == "Canhão de Pedra"
        for comum in ("Dragon", "Giant", "Labyrinth", "North", "Refugee"):
            assert comum not in dados, comum

    def test_man_god_nao_e_traduzido(self) -> None:
        """'Man-God' precisa sobreviver intacto na tradução."""
        glossario = traduzir.carregar_glossario()
        assert glossario["Man-God"] == "Man-God"

    def test_glossario_saiu_do_codigo(self) -> None:
        """A constante GLOSSARIO não pode mais existir no script."""
        assert not hasattr(traduzir, "GLOSSARIO"), (
            "o glossário deve viver no arquivo JSON, não no código"
        )

    def test_carregar_glossario_usa_o_arquivo(self) -> None:
        """carregar_glossario() devolve exatamente o conteúdo do arquivo."""
        with ARQUIVO_GLOSSARIO.open(encoding="utf-8") as f:
            dados = json.load(f)
        glossario = traduzir.carregar_glossario()
        assert glossario == {str(k): str(v) for k, v in dados.items()}

    def test_carregar_glossario_caminho_inexistente(self, tmp_path: Path) -> None:
        """Arquivo inexistente → {} e nenhuma exceção."""
        glossario = traduzir.carregar_glossario(tmp_path / "nao_existe.json")
        assert glossario == {}

    def test_carregar_glossario_json_invalido(self, tmp_path: Path) -> None:
        """JSON inválido → {} e nenhuma exceção."""
        invalido = tmp_path / "invalido.json"
        invalido.write_text("{isto não é json", encoding="utf-8")
        assert traduzir.carregar_glossario(invalido) == {}

    def test_carregar_glossario_json_nao_objeto(self, tmp_path: Path) -> None:
        """JSON que não é objeto → {} e nenhuma exceção."""
        lista = tmp_path / "lista.json"
        lista.write_text('["Man-God", "Eris"]', encoding="utf-8")
        assert traduzir.carregar_glossario(lista) == {}


# ── Glossário na CLI do script ───────────────────────────────────────────────


class TestGlossarioNaCli:
    """A CLI do ``traduzir.py`` usa o glossário do arquivo."""

    def test_glossario_padrao_entra_no_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem --no-glossary, o prompt leva o glossário do arquivo JSON."""
        from epub_translator.prompts import build_system

        capturado: dict[str, object] = {}

        def fake_build_system(target_lang: str, glossary=None, book_context=None) -> str:
            capturado["glossary"] = glossary
            return build_system(target_lang, glossary=None, book_context=None)

        monkeypatch.setattr("epub_translator.translator.build_system", fake_build_system)

        codigo = traduzir.main(
            [
                "--mock",
                "--src",
                str(EPUB_VALIDACAO),
                "--output",
                str(tmp_path / "com_glossario.epub"),
                "--log",
                str(tmp_path / "com_glossario.log"),
                "--only",
                XHTML_ALVO,
            ]
        )
        assert codigo == 0
        glossario = capturado["glossary"]
        assert isinstance(glossario, dict)
        assert glossario.get("Man-God") == "Man-God"

    def test_no_glossary_desliga(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-glossary não manda glossário nenhum."""
        from epub_translator.prompts import build_system

        capturado: dict[str, object] = {}

        def fake_build_system(target_lang: str, glossary=None, book_context=None) -> str:
            capturado["glossary"] = glossary
            return build_system(target_lang, glossary=None, book_context=None)

        monkeypatch.setattr(
            "epub_translator.translator.build_system", fake_build_system
        )

        codigo = traduzir.main(
            [
                "--mock",
                "--no-glossary",
                "--src",
                str(EPUB_VALIDACAO),
                "--output",
                str(tmp_path / "sem_glossario.epub"),
                "--log",
                str(tmp_path / "sem_glossario.log"),
                "--only",
                XHTML_ALVO,
            ]
        )
        assert codigo == 0
        assert capturado["glossary"] is None

    def test_glossary_inexistente_falha(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """--glossary apontando para arquivo inexistente sai com erro."""
        codigo = traduzir.main(
            [
                "--mock",
                "--glossary",
                str(tmp_path / "nao_existe.json"),
                "--output",
                str(tmp_path / "erro.epub"),
                "--log",
                str(tmp_path / "erro.log"),
            ]
        )
        assert codigo != 0
        assert "glossário não encontrado" in capsys.readouterr().err.lower()

    def test_glossary_json_valido_e_aceito(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """--glossary com o JSON do projeto é aceito e traduz sem erro."""
        codigo = traduzir.main(
            [
                "--mock",
                "--glossary",
                str(ARQUIVO_GLOSSARIO),
                "--src",
                str(EPUB_VALIDACAO),
                "--output",
                str(tmp_path / "glossario_json.epub"),
                "--log",
                str(tmp_path / "glossario_json.log"),
                "--only",
                XHTML_ALVO,
            ]
        )
        assert codigo == 0, capsys.readouterr().err
        assert (tmp_path / "glossario_json.epub").exists()


# ── Verificação do LM Studio ─────────────────────────────────────────────────


class TestChecarLmStudio:
    """A checagem do LM Studio dá mensagens claras em pt-BR."""

    def test_servidor_fora_do_ar(self) -> None:
        """Porta sem servidor → ServerNotRunningError com mensagem clara."""
        from epub_translator.lmstudio_client import ServerNotRunningError

        with pytest.raises(ServerNotRunningError) as exc:
            traduzir.checar_lm_studio("http://localhost:59999/v1", "qualquer")
        assert "não está rodando" in str(exc.value)

    def test_mensagem_para_servidor_fora_do_ar(
        self, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        """A CLI sai com código 2 e mensagem em pt-BR."""
        codigo = traduzir.main(
            [
                "--base-url",
                "http://localhost:59999/v1",
                "--output",
                str(tmp_path / "nao_usado.epub"),
                "--log",
                str(tmp_path / "nao_usado.log"),
            ]
        )
        assert codigo == 2
        erro = capsys.readouterr().err
        assert "LM Studio" in erro


# ── Verificação de contagem do relatório ─────────────────────────────────────


class TestRelatorioConta:
    """O relatório do pipeline fecha a conta."""

    def test_total_bate_com_as_partes(self) -> None:
        """total == traduzidos + cache + falhas + descartados."""
        from epub_translator.translator import Report

        r = Report(
            total_blocks=10,
            translated_blocks=4,
            cached_blocks=3,
            failed_blocks=2,
            discarded_blocks=1,
        )
        assert r.accounted_blocks == r.total_blocks
