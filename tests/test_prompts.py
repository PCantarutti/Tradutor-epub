"""Testes para epub_translator/prompts.py."""

from __future__ import annotations

import pytest

from epub_translator.blocks import Block
from epub_translator.prompts import (
    MalformedResponseError,
    build_system,
    build_user,
    parse_response,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _block(text: str = "texto", tag_name: str = "p") -> Block:
    """Cria um Block sintético para testes de prompt."""
    return Block(tag_name=tag_name, element=None, synthetic=False, text=text)


# ── Testes parse_response ────────────────────────────────────────────────────

class TestParseResponse:
    """Testes do parser de resposta."""

    def test_formato_com_colchetes(self) -> None:
        """parse_response devolve os pares (i, texto) do formato [i]."""
        result = parse_response("[1]\nola\n[2]\nmundo")
        assert result == [(1, "ola"), (2, "mundo")]

    def test_indices_nao_consecutivos_sao_aceitos(self) -> None:
        """Um lote que começa no bloco 3 devolve 3 e 4 (sem exigir 1..N)."""
        result = parse_response("[3]\nola\n[4]\nmundo")
        assert result == [(3, "ola"), (4, "mundo")]

    def test_texto_solto_levanta_erro(self) -> None:
        """Resposta sem nenhum separador numerado levanta MalformedResponseError."""
        with pytest.raises(MalformedResponseError):
            parse_response("texto solto")

    def test_formato_cru_ainda_funciona(self) -> None:
        """Separador cru '1' (sem colchetes) continua aceito."""
        result = parse_response("1\nola\n2\nmundo")
        assert result == [(1, "ola"), (2, "mundo")]

    def test_indices_fora_de_ordem_levanta_erro(self) -> None:
        """Índices não crescentes continuam inválidos."""
        with pytest.raises(MalformedResponseError):
            parse_response("[2]\nbloco2\n[1]\nbloco1")

    def test_indices_repetidos_levanta_erro(self) -> None:
        """Índices repetidos são inválidos (não são estritamente crescentes)."""
        with pytest.raises(MalformedResponseError):
            parse_response("[1]\nbloco1\n[1]\nbloco1b")

    def test_texto_multilinha(self) -> None:
        """Texto multilinha dentro de um bloco é preservado."""
        result = parse_response("[1]\nlinha1\nlinha2")
        assert result == [(1, "linha1\nlinha2")]

    def test_separador_deve_casar_exatamente(self) -> None:
        """Linha com prefixo/sufito extra não vira separador."""
        with pytest.raises(MalformedResponseError):
            parse_response("[1] texto\nconteudo")
        with pytest.raises(MalformedResponseError):
            parse_response("bloco [1]\nconteudo")


# ── Testes build_system / build_user ────────────────────────────────────────

class TestBuildPrompts:
    """Testes de construção dos prompts."""

    def test_build_system_menciona_formato_de_resposta(self) -> None:
        """O prompt de sistema explica o formato [i] esperado."""
        system = build_system(target_lang="pt-BR")
        assert "[i]" in system
        assert "pt-BR" in system

    def test_build_system_descreve_bloco_sem_prefixo_de_tag(self) -> None:
        """O prompt de sistema descreve '[i]\\n<texto>' (sem prefixo de tag)."""
        system = build_system(target_lang="pt-BR")
        assert "<texto original>" in system
        assert "<texto traduzido>" in system
        assert "prefixo de" in system
        # o prompt não deve enseñar o modelo a devolver um prefixo de tag
        assert "p: " not in system

    def test_build_system_preserva_regras_de_fidelidade(self) -> None:
        """Fidelidade, marcadores [[n]] e glossário continuam no prompt."""
        system = build_system(target_lang="pt-BR", glossary={"cat": "gato"})
        assert "MARCADORES" in system
        assert "[[n]]" in system
        assert "TAGS INLINE" in system
        assert "GLOSSÁRIO" in system

    def test_build_system_com_glossario(self) -> None:
        """O glossário aparece no prompt de sistema."""
        system = build_system(target_lang="pt-BR", glossary={"cat": "gato"})
        assert "cat" in system
        assert "gato" in system

    def test_build_user_usa_separadores_com_colchetes(self) -> None:
        """build_user emite '[i]' como separador de bloco, sem prefixo de tag."""
        user = build_user([_block("primeiro"), _block("segundo")])
        assert user.splitlines()[0] == "[1]"
        assert "[2]" in user
        assert "primeiro" in user
        assert "segundo" in user
        assert "p:" not in user

    def test_build_user_nao_inclui_prefixo_de_tag(self) -> None:
        """O texto do bloco é enviado puro (sem 'p: '), que vazaria na saída."""
        user = build_user([_block("primeiro", tag_name="p")])
        assert user == "[1]\nprimeiro"

    def test_build_user_com_contexto_anterior(self) -> None:
        """O contexto anterior vem antes dos blocos."""
        user = build_user([_block("primeiro")], prev_context="contexto")
        assert user.startswith("contexto")
        assert "[1]" in user
