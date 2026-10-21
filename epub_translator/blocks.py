"""Extração de blocos de texto, marcadores [[n]]/[[/n]], replay e validação."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from lxml import etree


# ── Tags inline que viram marcadores ────────────────────────────────────────

INLINE_TAGS: frozenset[str] = frozenset((
    "b", "i", "em", "strong", "span", "sub", "sup", "small", "big",
    "u", "s", "mark", "a", "code", "abbr", "cite", "dfn", "var",
    "samp", "kbd", "ins", "del", "rb", "rt", "rtc", "rp", "ruby",
    "wbr", "br", "img",
))

# ── Tags de bloco ────────────────────────────────────────────────────────────

BLOCK_TAGS: frozenset[str] = frozenset((
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
    "figcaption", "dt", "dd", "td", "th", "caption", "summary", "address",
))

# ── Tags para pular ─────────────────────────────────────────────────────────

SKIP_TAGS: frozenset[str] = frozenset((
    "script", "style", "head", "noscript", "template", "svg", "pre",
))

# ── Marcadores ──────────────────────────────────────────────────────────────

# Grupo 1: "/" (fechamento) ou vazio (abertura). Grupo 2: o ID.
_MARKER_RE = re.compile(r"\[\[(/?)(\d+)\]\]")


# ── Helpers de namespace ─────────────────────────────────────────────────────

def _localname(tag: str) -> str:
    """Retorna o nome local de uma tag, removendo namespace URI."""
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


# ── Bloco ────────────────────────────────────────────────────────────────────

@dataclass
class Block:
    """Um bloco de texto extraído de um elemento de bloco.

    Attributes:
        tag_name: Nome local da tag de bloco (ex: 'p', 'h1').
        element: O elemento lxml original (None para blocos sintéticos).
        synthetic: True se é um bloco sintético (texto órfão sem tag de bloco).
        inline_tags: Mapeamento de marcador_id → (tag_name, attrs_dict).
        text: Texto com marcadores [[n]]/[[/n]] substituindo tags inline.
        _orig: Mapeamento de marcador_id → cópia profunda do elemento original.
        parent: Elemento que hospeda o texto de um bloco sintético (é o
            elemento cujo ``.tail`` contém o texto órfão); None em blocos
            normais.
    """
    tag_name: str
    element: etree._Element | None
    synthetic: bool
    inline_tags: dict[int, tuple[str, dict[str, str]]] = field(default_factory=dict)
    text: str = ""
    _orig: dict[int, etree._Element] = field(default_factory=dict)
    parent: etree._Element | None = None


# ── Validação ────────────────────────────────────────────────────────────────

def validate_markers(text: str) -> None:
    """Valida os marcadores [[n]]/[[/n]] de UM bloco.

    A numeração dos marcadores é global por documento, então os IDs não
    precisam ser contíguos. O que é exigido, por bloco:

    1. cada ``[[n]]`` tem um ``[[/n]]`` correspondente, com aninhamento
       correto (abre/fecha em pilha, sem cruzar);
    2. os IDs, na ordem de primeira aparição, são estritamente crescentes;
    3. texto sem marcadores é válido.

    Args:
        text: Texto (original ou traduzido) de um único bloco.

    Raises:
        ValueError: Se houver desbalanceamento, aninhamento cruzado ou IDs
            fora de ordem crescente.
    """
    stack: list[int] = []
    first_seen: list[int] = []
    seen: set[int] = set()

    for m in _MARKER_RE.finditer(text):
        marker_id = int(m.group(2))

        if m.group(1) == "/":
            if not stack:
                raise ValueError(
                    f"Marcadores desbalanceados: fechamento [[/{marker_id}]] "
                    "sem abertura correspondente"
                )
            expected = stack.pop()
            if expected != marker_id:
                raise ValueError(
                    f"Marcadores desbalanceados: esperava [[/{expected}]] "
                    f"mas encontrou [[/{marker_id}]]"
                )
            continue

        stack.append(marker_id)

        if marker_id not in seen:
            seen.add(marker_id)
            if first_seen and marker_id <= first_seen[-1]:
                raise ValueError(
                    f"Marcadores fora de ordem: [[{marker_id}]] aparece "
                    f"depois de [[{first_seen[-1]}]]"
                )
            first_seen.append(marker_id)

    if stack:
        raise ValueError(
            "Marcadores desbalanceados: faltam fechamentos para "
            f"{sorted(stack)}"
        )


# ── Comparação de marcadores entre original e tradução ─────────────────────

def _marker_sequence(text: str) -> list[tuple[str, int]]:
    """Extrai a sequência de marcadores de *text* como lista de (tipo, id).

    O tipo é ``'abertura'`` ou ``'fechamento'``.
    """
    return [
        ("fechamento" if m.group(1) == "/" else "abertura", int(m.group(2)))
        for m in _MARKER_RE.finditer(text)
    ]


def compare_markers(original: str, translated: str) -> tuple[bool, str]:
    """Compara os marcadores [[n]]/[[/n]] do original com os da tradução.

    O requisito é que a tradução devolva os marcadores **byte a byte, na mesma
    posição e na mesma ordem**. Como o texto traduzido tem palavras diferentes
    (a posição em caracteres muda), a comparação é feita sobre a SEQUÊNCIA de
    marcadores: mesma quantidade, mesmos tipos (abertura/fechamento), mesmos
    IDs e na mesma ordem.

    Além disso, a tradução precisa continuar com aninhamento balanceado e IDs
    de primeira aparição estritamente crescentes (como exige
    ``validate_markers``).

    Args:
        original: Texto original do bloco (com marcadores).
        translated: Texto traduzido devolvido pelo modelo (com marcadores).

    Returns:
        Tupla ``(ok, motivo)``. ``ok`` é True quando a tradução é aceitável;
        caso contrário, ``motivo`` descreve o problema (em pt-BR) e a tradução
        deve ser DESCARTADA (não aplicada na árvore).
    """
    seq_orig = _marker_sequence(original)
    seq_trad = _marker_sequence(translated)

    if len(seq_orig) != len(seq_trad):
        return (
            False,
            f"quantidade de marcadores diferente: original tem {len(seq_orig)}, "
            f"tradução tem {len(seq_trad)}",
        )

    if seq_orig != seq_trad:
        # Mostra a primeira posição em que as sequências divergem.
        for i, (a, b) in enumerate(zip(seq_orig, seq_trad)):
            if a != b:
                return (
                    False,
                    f"marcador {i + 1} divergente: original {a[0]}[[{a[1]}]], "
                    f"tradução {b[0]}[[{b[1]}]]",
                )
        return (False, "sequência de marcadores divergente do original")

    # Marcadores presentes: a tradução ainda precisa estar balanceada e em
    # ordem crescente de IDs de primeira aparição.
    try:
        validate_markers(translated)
    except ValueError as exc:
        return (False, f"marcadores inválidos na tradução: {exc}")

    return (True, "")


# ── Extração de blocos ──────────────────────────────────────────────────────

def extract_blocks(tree: etree._ElementTree | etree._Element) -> list[Block]:
    """Extrai blocos de texto de uma árvore XHTML.

    Para cada elemento de bloco (p, h1-h6, etc.), extrai o texto com
    tags inline substituídas por marcadores [[n]]/[[/n]].
    Textos órfãos (fora de elementos de bloco) viram blocos sintéticos.

    A árvore é modificada in-place: tags inline são substituídas por
    marcadores no texto e no tail.

    Args:
        tree: Árvore lxml de um documento XHTML.

    Returns:
        Lista de Block com texto contendo marcadores.
    """
    blocks: list[Block] = []
    seq = [1]  # lista para mutabilidade em nested function

    if hasattr(tree, "getroot"):
        root = tree.getroot()
    else:
        root = tree

    def _process(el: etree._Element) -> None:
        tag = _localname(el.tag)

        if tag in SKIP_TAGS:
            return

        if tag in BLOCK_TAGS:
            inline_tags: dict[int, tuple[str, dict[str, str]]] = {}
            orig: dict[int, etree._Element] = {}
            parts: list[str] = []

            # Texto antes do primeiro filho
            if el.text:
                parts.append(el.text)

            # Percorre filhos inline e modifica a árvore em uma única passagem
            for child in list(el):
                _process_inline(child, inline_tags, orig, parts)

            blocks.append(Block(
                tag_name=tag,
                element=el,
                synthetic=False,
                inline_tags=inline_tags,
                text="".join(parts),
                _orig=orig,
            ))
        else:
            # Processa filhos recursivamente
            for child in el:
                _process(child)

        # Texto órfão: é o tail do elemento (vem depois do elemento na ordem
        # do documento) e não está dentro de nenhuma tag de bloco.
        if el.tail and el.tail.strip():
            blocks.append(Block(
                tag_name="",
                element=None,
                synthetic=True,
                inline_tags={},
                text=el.tail,
                _orig={},
                parent=el,
            ))

    def _process_inline(el: etree._Element,
                        inline_tags: dict[int, tuple[str, dict[str, str]]],
                        orig: dict[int, etree._Element],
                        parts: list[str]) -> None:
        """Percorre *el* e seus filhos inline, coletando tags inline e
        substituindo-as por marcadores [[n]]/[[/n]] na árvore E no texto.

        Esta função é chamada apenas uma vez por elemento, dentro do bloco
        que o contém, evitando duplicação de marcadores.

        A ordem do texto segue a ordem do documento: o marcador de ABERTURA
        e o texto do elemento vêm ANTES dos filhos, e o marcador de FECHAMENTO
        e o tail vêm DEPOIS.
        """
        tag = _localname(el.tag)

        if tag in SKIP_TAGS:
            return

        if tag in INLINE_TAGS:
            marker_id = seq[0]
            seq[0] += 1
            attrs = {k: v for k, v in el.attrib.items()}
            inline_tags[marker_id] = (tag, attrs)
            orig[marker_id] = copy.deepcopy(el)

            # Salva texto/tail originais antes de modificar
            orig_text = el.text
            orig_tail = el.tail

            # Modifica in-place: .text recebe marcador + texto original
            el.text = f"[[{marker_id}]]"
            if orig_text:
                el.text += orig_text

            # Abre o marcador ANTES dos filhos (pai antes do filho)
            parts.append(f"[[{marker_id}]]")
            if orig_text:
                parts.append(orig_text)

            # Filhos recursivamente
            for child in list(el):
                _process_inline(child, inline_tags, orig, parts)

            # Modifica in-place: .tail recebe marcador de fechamento + tail original
            el.tail = f"[[/{marker_id}]]"
            if orig_tail:
                el.tail += orig_tail

            # Fecha o marcador DEPOIS dos filhos
            parts.append(f"[[/{marker_id}]]")
            if orig_tail:
                parts.append(orig_tail)
        else:
            # Não-inline: apenas coleta texto
            if el.text:
                parts.append(el.text)
            for child in el:
                _process_inline(child, inline_tags, orig, parts)
            if el.tail:
                parts.append(el.tail)

    _process(root)
    return blocks


# ── to_marker_text ───────────────────────────────────────────────────────────

def format_block(index: int, text: str) -> str:
    """Formata um bloco no formato ``[i]\\n<texto>``.

    Fonte única de verdade do formato de bloco usado no prompt (e na chave
    do cache). O prefixo da tag de bloco (ex.: ``p: ``) NÃO é incluído:
    o separador ``[i]`` já identifica o bloco e o prefixo vazaria para o
    texto traduzido final (bug TASK-016).

    Args:
        index: Índice 1-based do bloco.
        text: Texto (original ou traduzido) do bloco.

    Returns:
        String ``'[i]\\n<texto>'``.
    """
    return f"[{index}]\n{text}"


def to_marker_text(blocks: list[Block]) -> str:
    """Formata blocos como texto numerado para o prompt.

    Retorna: f'[{i}]\\n{texto}' para cada bloco (via `format_block`).

    O nome da tag de bloco (ex.: 'p', 'h1') fica apenas no debug: incluí-lo
    no texto faria o prefixo vazar para o texto traduzido final.
    """
    parts: list[str] = []
    for i, b in enumerate(blocks, start=1):
        parts.append(format_block(i, b.text))
    return "\n".join(parts)


# ── Replay ───────────────────────────────────────────────────────────────────

def replay(blocks: list[Block], original_tree: etree._ElementTree | etree._Element) -> etree.Element:
    """Reconstrói o conteúdo dos blocos a partir do TEXTO traduzido.

    Para cada bloco com elemento, as tags inline são reconstruídas a partir
    dos marcadores [[n]]/[[/n]] presentes em ``block.text`` (que deve conter
    o texto traduzido). Cada tag inline recriada usa a MESMA tag e os MESMOS
    atributos do elemento original guardado em ``block._orig``.

    Blocos sintéticos (element=None) não têm elemento a reconstruir: o texto
    traduzido é aplicado no ``.tail`` do elemento dono (quando existir).

    Args:
        blocks: Lista de Block com ``text`` já traduzido.
        original_tree: Árvore lxml original (modificada in-place).

    Returns:
        Elemento raiz da árvore reconstruída.
    """
    if hasattr(original_tree, "getroot"):
        root = original_tree.getroot()
    else:
        root = original_tree

    for block in blocks:
        if block.element is not None:
            _rebuild_block(block)
        elif block.synthetic and block.parent is not None:
            # Texto órfão vive no .tail do elemento dono
            block.parent.tail = block.text

    return root


def _rebuild_block(block: Block) -> None:
    """Reconstroi o conteúdo de um elemento de bloco a partir de block.text.

    O elemento é esvaziado (filhos e .text) e repovoado com o texto
    traduzido, reinsertando as tags inline indicadas pelos marcadores.
    Tags inline que não aparecem no texto traduzido são descartadas.
    """
    el = block.element
    if el is None:
        return

    for child in list(el):
        el.remove(child)
    el.text = None

    stack: list[tuple[int, etree._Element]] = []

    for kind, marker_id, literal in _tokenize(block.text):
        if kind == "text":
            _append_text(stack[-1][1] if stack else el, literal)
        elif kind == "open":
            orig = block._orig.get(marker_id) if marker_id is not None else None
            if orig is None:
                # Marcador sem original conhecido: o texto interno continua
                # no nível do pai.
                continue
            new_el = _clone_inline(orig)
            (stack[-1][1] if stack else el).append(new_el)
            stack.append((marker_id, new_el))
        else:  # close
            if stack and stack[-1][0] == marker_id:
                stack.pop()


def _clone_inline(orig: etree._Element) -> etree._Element:
    """Cria um novo elemento inline com a mesma tag e os mesmos atributos.

    O nsmap é preservado para que prefixos (ex.: ``epub:type``) continuem
    válidos, mas o conteúdo (filhos/texto) é descartado — quem repovoá-lo
    é o replay, a partir do texto traduzido.
    """
    new_el = etree.Element(orig.tag, nsmap=orig.nsmap)
    for key, value in orig.attrib.items():
        new_el.set(key, value)
    return new_el


def _tokenize(text: str) -> Iterator[tuple[str, int | None, str]]:
    """Quebra o texto em pedaços: texto literal, abertura e fechamento.

    Yields:
        Tuplas (tipo, id, texto) com tipo em {'text', 'open', 'close'}.
    """
    pos = 0
    for m in _MARKER_RE.finditer(text):
        if m.start() > pos:
            yield ("text", None, text[pos:m.start()])
        marker_id = int(m.group(2))
        yield ("close" if m.group(1) == "/" else "open", marker_id, "")
        pos = m.end()
    if pos < len(text):
        yield ("text", None, text[pos:])


def _append_text(container: etree._Element, text: str) -> None:
    """Acrescenta *text* ao ponto de inserção de *container*.

    Se o container já tem filhos, o texto vai para o .tail do último filho
    (é assim que o XML representa texto depois de um elemento); senão vai
    para o .text do container.
    """
    if not text:
        return
    if len(container):
        last = container[-1]
        last.tail = (last.tail or "") + text
    else:
        container.text = (container.text or "") + text
