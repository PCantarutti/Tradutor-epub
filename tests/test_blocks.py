"""Testes para blocks.py e prompts.py."""

from __future__ import annotations

import pytest
from lxml import etree

from epub_translator.blocks import (
    Block,
    extract_blocks,
    replay,
    to_marker_text,
    validate_markers,
)
from epub_translator.lmstudio_client import MockClient
from epub_translator.prompts import (
    build_system,
    build_user,
    parse_response,
    MalformedResponseError,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_xhtml(html: str) -> etree._Element:
    """Cria uma árvore XHTML a partir de HTML simples.
    Retorna o elemento root (html) diretamente.
    """
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">{html}</html>'
    return etree.fromstring(xml.encode("utf-8"))


def _serialize(el: etree._Element) -> str:
    """Serializa um elemento para string."""
    return etree.tostring(el, encoding="unicode", pretty_print=False)


# ── Testes extract_blocks ────────────────────────────────────────────────────

class TestExtractBlocks:
    """Testes para extract_blocks."""

    def test_para_com_texto_simples(self) -> None:
        """Parágrafo com texto simples vira um bloco."""
        body = _parse_xhtml("<p>Olá mundo</p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        assert not blocks[0].synthetic
        assert blocks[0].tag_name == "p"
        assert blocks[0].text == "Olá mundo"
        assert blocks[0].inline_tags == {}

    def test_para_com_tags_inline(self) -> None:
        """Parágrafo com tags inline gera marcadores."""
        body = _parse_xhtml("<p>Texto <b>negrito</b> e <i>italico</i></p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        assert len(block.inline_tags) == 2
        assert "[[" in block.text
        assert "[[/" in block.text

    def test_para_com_sup(self) -> None:
        """Tag sup gera marcador."""
        body = _parse_xhtml("<p>Texto<sup>1</sup>nota</p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        assert len(block.inline_tags) == 1
        assert "[[1]]" in block.text

    def test_para_com_a_href(self) -> None:
        """Tag <a> com href gera marcador preservando atributo."""
        body = _parse_xhtml('<p>Link <a href="http://ex.com">clique</a> aqui</p>')
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        assert len(block.inline_tags) == 1
        tag_name, attrs = block.inline_tags[1]
        assert tag_name == "a"
        assert attrs.get("href") == "http://ex.com"

    def test_multiplas_tags_aninhadas(self) -> None:
        """Tags aninhadas geram marcadores corretos."""
        body = _parse_xhtml("<p><b>texto <i>aninhado</i></b></p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        assert len(block.inline_tags) == 2

    def test_ordem_marcadores_aninhados_pai_antes_do_filho(self) -> None:
        """Tags inline aninhadas: pai antes do filho, IDs em ordem crescente."""
        body = _parse_xhtml("<p>a <b>b <i>c</i> d</b> e</p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        # O marcador do pai (<b>) abre antes do marcador do filho (<i>)
        assert block.text == "a [[1]]b [[2]]c[[/2]] d[[/1]] e"
        assert block.inline_tags[1][0] == "b"
        assert block.inline_tags[2][0] == "i"
        # A validação por bloco aceita o resultado
        validate_markers(block.text)

    def test_ordem_marcadores_blocos_seguintes_continuam_crescente(self) -> None:
        """A numeração é global: o 2º bloco continua depois do 1º."""
        body = _parse_xhtml("<p><b>um</b></p><p><i>dois</i></p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 2
        assert blocks[0].text == "[[1]]um[[/1]]"
        assert blocks[1].text == "[[2]]dois[[/2]]"
        for b in blocks:
            validate_markers(b.text)

    def test_bloco_com_tag_interna_preserva_ordem_do_documento(self) -> None:
        """Um <br> interno não embaralha a ordem dos marcadores."""
        body = _parse_xhtml(
            "<p><span><span>Chapter 1:<br/>The Diary (Part 1)</span></span></p>"
        )
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        # pai (1) -> filho (2): IDs crescentes na ordem de aparição
        assert block.text.index("[[1]]") < block.text.index("[[2]]")
        assert block.text.index("[[2]]") < block.text.index("[[/2]]")
        assert block.text.index("[[/2]]") < block.text.index("[[/1]]")
        validate_markers(block.text)

    def test_multiplas_tags_sequenciais(self) -> None:
        """Tags sequenciais geram marcadores em ordem crescente."""
        body = _parse_xhtml("<p><b>um</b> <i>dois</i> <em>tres</em></p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        assert len(block.inline_tags) == 3
        # Verifica ordem: 1=b, 2=i, 3=em
        assert block.inline_tags[1][0] == "b"
        assert block.inline_tags[2][0] == "i"
        assert block.inline_tags[3][0] == "em"

    def test_tags_skip_preservidas(self) -> None:
        """Tags <script> e <style> são ignoradas."""
        body = _parse_xhtml("<p>Texto<script>alert(1)</script><b>negrito</b></p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        assert "alert" not in block.text
        assert len(block.inline_tags) == 1

    def test_tags_skip_pre_preservidas(self) -> None:
        """Tags <pre> são ignoradas."""
        body = _parse_xhtml("<p>Texto<pre>codigo</pre><b>negrito</b></p>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        assert "codigo" not in block.text
        assert len(block.inline_tags) == 1

    def test_div_ignorado(self) -> None:
        """Tags <div> não são blocos nem tags inline — texto é preservado."""
        body = _parse_xhtml("<div><p>Texto</p></div>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        assert blocks[0].text == "Texto"

    def test_h1_tag_name(self) -> None:
        """Elemento <h1> é detectado corretamente."""
        body = _parse_xhtml("<h1>Título</h1>")
        blocks = extract_blocks(body)
        assert len(blocks) == 1
        assert blocks[0].tag_name == "h1"
        assert blocks[0].text == "Título"

    def test_li_tag_name(self) -> None:
        """Elemento <li> é detectado corretamente."""
        body = _parse_xhtml("<ul><li>Item um</li><li>Item dois</li></ul>")
        blocks = extract_blocks(body)
        assert len(blocks) == 2
        assert blocks[0].tag_name == "li"
        assert blocks[0].text == "Item um"
        assert blocks[1].tag_name == "li"
        assert blocks[1].text == "Item dois"


# ── Testes to_marker_text ────────────────────────────────────────────────────

class TestToMarkerText:
    """Testes para to_marker_text."""

    def test_bloco_simples(self) -> None:
        """Bloco simples gera formato [1]\\n<texto> (sem prefixo de tag)."""
        blocks = [Block(tag_name="p", element=None, synthetic=False, text="olá")]
        result = to_marker_text(blocks)
        assert result == "[1]\nolá"
        assert "p:" not in result

    def test_multiplas_tags(self) -> None:
        """Múltiplos blocos geram marcadores sequenciais, sem prefixo."""
        blocks = [
            Block(tag_name="p", element=None, synthetic=False, text="bloco1"),
            Block(tag_name="h1", element=None, synthetic=False, text="titulo"),
        ]
        result = to_marker_text(blocks)
        assert result == "[1]\nbloco1\n[2]\ntitulo"
        assert "p:" not in result
        assert "h1:" not in result

    def test_bloco_sintetico(self) -> None:
        """Bloco sintético (sem tag de bloco) também vai sem prefixo."""
        blocks = [Block(tag_name="", element=None, synthetic=True, text="órfão")]
        result = to_marker_text(blocks)
        assert result == "[1]\nórfão"
        assert "text:" not in result


# ── Testes round-trip (prompt -> parse -> replay) ───────────────────────────

class TestRoundTripSemPrefixoDeTag:
    """Regressão do bug TASK-016: o prefixo de tag não pode vazar para o
    texto final do EPUB."""

    def test_round_trip_preserva_texto_exato(self) -> None:
        """Bloco 'p' com texto 'Ola mundo' volta EXATAMENTE como 'Ola mundo'."""
        body = _parse_xhtml("<p>Ola mundo</p>")
        blocks = extract_blocks(body)
        assert blocks[0].tag_name == "p"

        prompt = to_marker_text(blocks)
        assert prompt == "[1]\nOla mundo"
        assert "p:" not in prompt

        parsed = parse_response(prompt)
        assert parsed == [(1, "Ola mundo")]

        blocks[0].text = parsed[0][1]
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        assert p.text == "Ola mundo"
        assert "p:" not in (p.text or "")

    def test_round_trip_nao_vaza_marcadores_inline(self) -> None:
        """Marcadores [[n]] são consumidos pelo replay, não vazam no texto."""
        body = _parse_xhtml("<p>Hello <b>world</b></p>")
        blocks = extract_blocks(body)

        prompt = to_marker_text(blocks)
        assert prompt == "[1]\nHello [[1]]world[[/1]]"

        parsed = parse_response(prompt)
        assert parsed == [(1, "Hello [[1]]world[[/1]]")]

        blocks[0].text = parsed[0][1]
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        serialized = _serialize(p)
        assert "[[" not in serialized
        assert "]]" not in serialized
        assert "<b>world</b>" in serialized

    def test_round_trip_com_mock_nao_acrescenta_prefixo(self) -> None:
        """O eco do MockClient (que devolve o bloco como veio) sai limpo."""
        client = MockClient()
        body = _parse_xhtml("<p>Ola mundo</p>")
        blocks = extract_blocks(body)
        messages = [
            {"role": "system", "content": build_system("pt-BR")},
            {"role": "user", "content": build_user(blocks)},
        ]
        parsed = parse_response(client.chat(messages))
        assert parsed == [(1, "Ola mundo")]

        blocks[0].text = parsed[0][1]
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        assert p.text == "Ola mundo"


# ── Testes replay ────────────────────────────────────────────────────────────

class TestReplay:
    """Testes para replay."""

    def test_replay_preserva_tag_simples(self) -> None:
        """Replay reconstrói tag <b> sem atributos."""
        body = _parse_xhtml("<p>Texto <b>negrito</b> aqui</p>")
        blocks = extract_blocks(body)
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        serialized = _serialize(p)
        assert "<b>" in serialized

    def test_replay_preserva_tag_com_atributo(self) -> None:
        """Replay reconstrói tag <a> com atributo href."""
        body = _parse_xhtml('<p><a href="http://ex.com">link</a></p>')
        blocks = extract_blocks(body)
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        serialized = _serialize(p)
        assert "href=" in serialized
        assert "http://ex.com" in serialized

    def test_replay_preserva_multiplas_tags(self) -> None:
        """Replay reconstrói múltiplas tags."""
        body = _parse_xhtml("<p><b>um</b> <i>dois</i></p>")
        blocks = extract_blocks(body)
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        serialized = _serialize(p)
        assert "<b>" in serialized
        assert "<i>" in serialized

    def test_replay_retorna_element(self) -> None:
        """Replay retorna o elemento raiz."""
        body = _parse_xhtml("<p>texto</p>")
        blocks = extract_blocks(body)
        result = replay(blocks, body)
        assert result is not None
        from epub_translator.blocks import _localname
        assert _localname(result.tag) == "html"

    def test_replay_insere_texto_traduzido(self) -> None:
        """Replay usa o texto traduzido (b.text) no lugar do original."""
        body = _parse_xhtml("<p>Hello world</p>")
        blocks = extract_blocks(body)
        blocks[0].text = "Olá mundo"
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        assert p.text == "Olá mundo"
        assert "Hello world" not in _serialize(p)

    def test_replay_insere_texto_traduzido_dentro_de_inline(self) -> None:
        """O texto traduzido fica DENTRO da tag inline, com o mesmo atributo."""
        body = _parse_xhtml('<p>Link <a href="http://ex.com">click</a> aqui</p>')
        blocks = extract_blocks(body)
        blocks[0].text = "Link [[1]]clique traduzido[[/1]] aqui"
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        a = p.find("{http://www.w3.org/1999/xhtml}a")
        assert a is not None
        assert a.text == "clique traduzido"
        assert a.get("href") == "http://ex.com"
        assert "clique traduzido" in _serialize(p)

    def test_replay_reconstroi_tags_aninhadas(self) -> None:
        """Tags aninhadas são reconstruídas na ordem do documento."""
        body = _parse_xhtml("<p>a <b>b <i>c</i> d</b> e</p>")
        blocks = extract_blocks(body)
        blocks[0].text = "a [[1]]B [[2]]C[[/2]] D[[/1]] e"
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        b_el = p.find("{http://www.w3.org/1999/xhtml}b")
        assert b_el is not None
        i_el = b_el.find("{http://www.w3.org/1999/xhtml}i")
        assert i_el is not None
        assert i_el.text == "C"
        assert b_el.text == "B "
        assert len(b_el) == 1

    def test_replay_remove_inline_nao_presente_no_texto(self) -> None:
        """Tags inline ausentes do texto traduzido são descartadas."""
        body = _parse_xhtml("<p>keep <b>drop</b> <i>also drop</i></p>")
        blocks = extract_blocks(body)
        blocks[0].text = "keep only"
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        serialized = _serialize(p)
        assert "<b>" not in serialized
        assert "<i>" not in serialized
        assert "drop" not in serialized
        assert p.text == "keep only"

    def test_replay_sem_marcadores_deixa_so_texto(self) -> None:
        """Sem marcadores, o elemento fica apenas com o texto traduzido."""
        body = _parse_xhtml("<p>Hello <b>world</b></p>")
        blocks = extract_blocks(body)
        blocks[0].text = "Olá"
        root = replay(blocks, body)
        p = root.find(".//{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        assert len(p) == 0
        assert p.text == "Olá"

    def test_replay_bloco_sintetico_vai_para_o_tail_do_pai(self) -> None:
        """Bloco sintético: o texto traduzido vai para o .tail do elemento pai."""
        body = _parse_xhtml("<div><p>paragrafo</p>texto orfao</div>")
        blocks = extract_blocks(body)
        assert len(blocks) == 2
        assert blocks[1].synthetic
        blocks[1].text = "ORFAO TRADUZIDO"
        root = replay(blocks, body)
        div = root.find(".//{http://www.w3.org/1999/xhtml}div")
        assert div is not None
        p = div.find("{http://www.w3.org/1999/xhtml}p")
        assert p is not None
        assert p.tail == "ORFAO TRADUZIDO"


# ── Testes validate_markers ──────────────────────────────────────────────────

class TestValidateMarkers:
    """Testes para validate_markers."""

    def test_texto_sem_marcadores(self) -> None:
        """Texto sem marcadores passa na validação."""
        validate_markers("Olá mundo")

    def test_marcadores_balanceados(self) -> None:
        """Marcadores balanceados passam na validação."""
        validate_markers("[[1]]texto[[/1]]")

    def test_multiplas_tags_balanceadas(self) -> None:
        """Múltiplas tags balanceadas passam na validação."""
        validate_markers("[[1]]a[[/1]][[2]]b[[/2]]")

    def test_marcador_desbalanceado_fechamento_extra(self) -> None:
        """Fechamento sem abertura correspondente levanta ValueError."""
        with pytest.raises(ValueError, match="desbalancea"):
            validate_markers("[[1]]texto[[/2]]")

    def test_marcador_desbalanceado_abertura_extra(self) -> None:
        """Abertura sem fechamento correspondente levanta ValueError."""
        with pytest.raises(ValueError, match="desbalancea"):
            validate_markers("[[1]][[2]]texto")

    def test_marcadores_com_gap_sao_validos(self) -> None:
        """IDs não contíguos são válidos: a numeração é global por documento."""
        validate_markers("[[1]]texto[[/1]][[3]]texto[[/3]]")

    def test_marcadores_fora_de_ordem(self) -> None:
        """Marcadores fora de ordem crescente levanta ValueError."""
        with pytest.raises(ValueError, match="ordem"):
            validate_markers("[[2]]texto[[/2]][[1]]texto[[/1]]")

    def test_ids_nao_contiguos_passam(self) -> None:
        """IDs não contíguos (ex.: 3 e 7) passam na validação."""
        validate_markers("[[3]]x[[/3]][[7]]y[[/7]]")

    def test_ordem_crescente_preservada(self) -> None:
        """IDs em ordem estritamente crescente passam na validação."""
        validate_markers("[[2]]a[[/2]][[5]]b[[/5]][[9]]c[[/9]]")

    def test_ordem_errada_levanta_erro(self) -> None:
        """IDs fora de ordem crescente levantam ValueError."""
        with pytest.raises(ValueError, match="ordem"):
            validate_markers("[[7]]x[[/7]][[3]]y[[/3]]")

    def test_desbalanceado_levanta_erro(self) -> None:
        """Marcador desbalanceado levanta ValueError."""
        with pytest.raises(ValueError, match="desbalancea"):
            validate_markers("[[1]]texto sem fechamento")

    def test_aninhamento_cruzado_levanta_erro(self) -> None:
        """Aninhamento cruzado (abre 1, abre 2, fecha 1) levanta ValueError."""
        with pytest.raises(ValueError, match="desbalancea"):
            validate_markers("[[1]]a[[2]]b[[/1]][[/2]]")

    def test_aninhamento_correto_pass(self) -> None:
        """Aninhamento correto (pai/filho) passa na validação."""
        validate_markers("[[1]]a [[2]]b[[/2]][[/1]]")


# ── Testes build_system ──────────────────────────────────────────────────────

class TestBuildSystem:
    """Testes para build_system."""

    def test_prompt_basico(self) -> None:
        """Prompt básico contém regras e idioma alvo."""
        prompt = build_system("pt-BR")
        assert "pt-BR" in prompt
        assert "REGRAS" in prompt
        assert "traduzir" in prompt.lower()

    def test_prompt_com_glossario(self) -> None:
        """Prompt com glossário inclui glossário."""
        glossary = {"hello": "olá", "world": "mundo"}
        prompt = build_system("pt-BR", glossary=glossary)
        assert "GLOSSÁRIO" in prompt
        assert "hello → olá" in prompt
        assert "world → mundo" in prompt

    def test_prompt_com_contexto(self) -> None:
        """Prompt com contexto inclui contexto."""
        prompt = build_system("pt-BR", book_context="Capítulo 1: Introdução")
        assert "CONTEXTO" in prompt
        assert "Capítulo 1: Introdução" in prompt

    def test_prompt_soma_regras(self) -> None:
        """Prompt contém todas as regras essenciais."""
        prompt = build_system("pt-BR")
        assert "NOMES PRÓPRIOS" in prompt
        assert "MARCADORES" in prompt
        assert "TAGS INLINE" in prompt


# ── Testes build_user ────────────────────────────────────────────────────────

class TestBuildUser:
    """Testes para build_user."""

    def test_mensagem_com_bloco(self) -> None:
        """Mensagem com um bloco tem separador [1]."""
        blocks = [Block(tag_name="p", element=None, synthetic=False, text="texto")]
        msg = build_user(blocks)
        assert "[1]" in msg
        assert "texto" in msg

    def test_mensagem_com_multiplas_tags(self) -> None:
        """Mensagem com múltiplos blocos tem todos os separadores."""
        blocks = [
            Block(tag_name="p", element=None, synthetic=False, text="texto1"),
            Block(tag_name="p", element=None, synthetic=False, text="texto2"),
        ]
        msg = build_user(blocks)
        assert "[1]" in msg
        assert "[2]" in msg
        assert "texto1" in msg
        assert "texto2" in msg

    def test_mensagem_com_prev_context(self) -> None:
        """Mensagem com contexto anterior inclui o contexto."""
        blocks = [Block(tag_name="p", element=None, synthetic=False, text="texto")]
        msg = build_user(blocks, prev_context="contexto")
        assert "contexto" in msg


# ── Testes parse_response ────────────────────────────────────────────────────

class TestParseResponse:
    """Testes para parse_response."""

    def test_resposta_simples(self) -> None:
        """Resposta simples é parseada corretamente."""
        text = "1\ntraducao"
        result = parse_response(text)
        assert result == [(1, "traducao")]

    def test_resposta_multipla(self) -> None:
        """Resposta com múltiplos blocos é parseada corretamente."""
        text = "1\nbloco1\n\n2\nbloco2"
        result = parse_response(text)
        assert result == [(1, "bloco1"), (2, "bloco2")]

    def test_resposta_multiline(self) -> None:
        """Resposta com texto multilinha é parseada corretamente."""
        text = "1\nlinha1\nlinha2"
        result = parse_response(text)
        assert result == [(1, "linha1\nlinha2")]

    def test_resposta_vazia_levanta_erro(self) -> None:
        """Resposta sem blocos levanta MalformedResponseError."""
        with pytest.raises(MalformedResponseError):
            parse_response("sem blocos")

    def test_resposta_sem_numeros_levanta_erro(self) -> None:
        """Resposta sem números de bloco levanta MalformedResponseError."""
        with pytest.raises(MalformedResponseError):
            parse_response("texto sem separadores")

    def test_resposta_indices_fora_de_ordem_levanta_erro(self) -> None:
        """Resposta com índices fora de ordem levanta MalformedResponseError."""
        with pytest.raises(MalformedResponseError):
            parse_response("2\nbloco2\n1\nbloco1")
