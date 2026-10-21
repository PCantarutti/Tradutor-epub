"""Prompts de sistema e usuário para tradução de blocos."""

from __future__ import annotations

import re
from typing import Any

from .blocks import Block, to_marker_text


# ── Prompt de sistema ───────────────────────────────────────────────────────

def build_system(
    target_lang: str,
    glossary: dict[str, str] | None = None,
    book_context: str | None = None,
) -> str:
    """Constrói o prompt de sistema para o modelo de tradução.

    Args:
        target_lang: Idioma alvo (ex: 'pt-BR').
        glossary: Glossário opcional {termo_original: termo_traduzido}.
        book_context: Contexto do livro (opcional).

    Returns:
        Prompt de sistema formatado.
    """
    lines: list[str] = [
        "Você é um tradutor profissional de livros. Sua tarefa é traduzir "
        "blocos de texto XHTML preservando tags inline e marcadores [[n]].",
        "",
        "REGRAS:",
        "1. Traduza fielmente, sem omissões ou adições.",
        "2. Preserve NOMES PRÓPRIOS (não traduza).",
        "3. Preserve MARCADORES [[n]] e [[/n]] EXATAMENTE como estão.",
        "4. Preserve TAGS INLINE (b, i, em, strong, etc.) e seus ATRIBUTOS.",
        "5. Não adicione comentários, explicações ou texto extra.",
        "6. Mantenha a mesma estrutura de linhas e parágrafos.",
        "7. Use a formatação técnica correta do idioma alvo.",
        "8. Devolva APENAS o conteúdo do bloco: não acrescente prefixo de "
        "tag, rótulos, comentários nem texto extra.",
        f"9. Escreva SOMENTE em {target_lang}: nunca use caracteres chineses, "
        "japoneses ou coreanos.",
        "10. Use cada separador [i] UMA única vez, na ordem recebida, e "
        "devolva TODOS os blocos.",
    ]

    if glossary:
        lines.append("")
        lines.append("GLOSSÁRIO:")
        for term, translation in glossary.items():
            lines.append(f"- {term} → {translation}")

    if book_context:
        lines.append("")
        lines.append("CONTEXTO DO LIVRO:")
        lines.append(book_context)

    lines.append("")
    lines.append(f"IDIOMA ALVO: {target_lang}")
    lines.append("")
    lines.append("FORMATO DE RESPOSTA:")
    lines.append("Cada bloco de entrada é enviado assim:")
    lines.append("[i]")
    lines.append("<texto original>")
    lines.append("")
    lines.append("Responda no MESMO formato, com o texto traduzido:")
    lines.append("[i]")
    lines.append("<texto traduzido>")
    lines.append("")
    lines.append("Exemplo:")
    lines.append("[1]")
    lines.append("Texto traduzido com <b>tags</b> preservadas.")

    return "\n".join(lines)


# ── Mensagem de usuário ─────────────────────────────────────────────────────

def build_user(
    blocks: list[Block],
    prev_context: str | None = None,
) -> str:
    """Constrói a mensagem de usuário com blocos numerados.

    Usa to_marker_text para formatar os blocos.

    Args:
        blocks: Lista de Block com texto contendo marcadores.
        prev_context: Contexto anterior (últimos N blocos).

    Returns:
        Mensagem formatada com blocos numerados.
    """
    parts: list[str] = []

    if prev_context:
        parts.append(prev_context)
        parts.append("")

    parts.append(to_marker_text(blocks))

    return "\n".join(parts)


# ── Parser de resposta ──────────────────────────────────────────────────────

class MalformedResponseError(Exception):
    """Erro quando a resposta do modelo não está no formato esperado."""


# Separadores de bloco aceitos: '[i]' (formato do prompt) e 'i' (forma crua)
_BRACKET_SEPARATOR_RE = re.compile(r"^\**\[(\d+)\]\**:?$")
# '[3] texto' na mesma linha: alguns modelos colam o texto no separador.
_INLINE_SEPARATOR_RE = re.compile(r"^\**\[(\d+)\]\**:?\s+(\S.*)$")
_RAW_SEPARATOR_RE = re.compile(r"^(\d+)$")


def separator_index(line: str) -> int | None:
    """Retorna o índice do bloco se *line* for um separador, senão None.

    Formatos aceitos (casamento EXATO da linha, ignorando espaços nas
    pontas):
        - ``[3]`` — formato pedido no prompt de sistema;
        - ``3`` — formato cru que alguns modelos devolvem.

    Esta é a ÚNICA definição do formato de separador: o MockClient
    (lmstudio_client.py) reutiliza este helper para que mock e parser
    nunca fiquem dessincronizados.
    """
    stripped = line.strip()
    match = _BRACKET_SEPARATOR_RE.match(stripped) or _RAW_SEPARATOR_RE.match(stripped)
    if match:
        return int(match.group(1))
    return None


# Alias privado: nome antigo do helper, mantido por compatibilidade.
_separator_index = separator_index


def parse_response(text: str, expected: int | None = None) -> list[tuple[int, str]]:
    """Parseia a resposta do modelo voltando ao formato [i]\n<texto>.

    Aceita separadores ``[i]`` e ``i``.

    Os índices NÃO precisam ser consecutivos nem começar em 1: um lote
    enviado a partir do bloco 7 pode responder ``7, 8, 9``. O que é
    exigido é: (a) pelo menos um bloco; (b) índices estritamente
    crescentes; (c) primeiro índice >= 1.

    Args:
        text: Texto da resposta do modelo.
        expected: Quantos blocos foram enviados. Se a resposta trouxer
            exatamente esse número de blocos, a numeração é refeita pela
            POSIÇÃO (o modelo às vezes repete um número, ex. ``1,2,3,4,4``,
            mas devolve todos os blocos na ordem).

    Returns:
        Lista de (index, translated_text) para cada bloco.

    Raises:
        MalformedResponseError: Se o formato estiver incorreto.
    """
    lines = text.split("\n")
    result: list[tuple[int, str]] = []
    current_idx: int | None = None
    current_lines: list[str] = []

    for line in lines:
        if line.strip().startswith("```"):
            continue  # cercas de markdown não fazem parte da tradução
        separator = separator_index(line)
        inline = None if separator is not None else _INLINE_SEPARATOR_RE.match(line.strip())
        if inline:
            separator = int(inline.group(1))

        if separator is not None:
            # Salva bloco anterior se existir
            if current_idx is not None:
                result.append((current_idx, "\n".join(current_lines).strip()))

            current_idx = separator
            current_lines = [inline.group(2)] if inline else []
        elif current_idx is not None:
            current_lines.append(line)

    # Salva último bloco
    if current_idx is not None:
        result.append((current_idx, "\n".join(current_lines).strip()))

    # Pedido de UM bloco respondido sem o '[1]': a resposta inteira é o bloco
    # (marcadores e idioma ainda são validados pelo tradutor).
    if not result and expected == 1 and text.strip():
        return [(1, text.strip())]

    if not result:
        raise MalformedResponseError(
            "Resposta não contém blocos numerados no formato esperado [i]"
        )

    if expected is not None and len(result) == expected:
        return [(pos, txt) for pos, (_, txt) in enumerate(result, start=1)]

    # Validação relaxada: índices podem ser não consecutivos (lote que
    # começa no bloco 7 devolve 7, 8, 9), mas precisam ser estritamente
    # crescentes e começar em 1 ou mais.
    indices = [idx for idx, _ in result]
    if indices[0] < 1:
        raise MalformedResponseError(
            f"Índice de bloco inválido: {indices[0]} (esperado >= 1)"
        )
    if any(atual <= anterior for anterior, atual in zip(indices, indices[1:])):
        raise MalformedResponseError(
            f"Índices fora de ordem: {indices}"
        )

    return result
