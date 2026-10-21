"""Tradução de EPUB via LM Studio com cache e replay de marcadores.

Exporta ``ProgressEvent``, ``TranslationConfig``, ``translate_epub`` e ``Report``.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from lxml import etree

from .blocks import (
    Block,
    compare_markers,
    extract_blocks,
    format_block,
    replay,
    validate_markers,
)
from .cache import BlockCache, glossary_fingerprint, key_for
from .epub_io import (
    EpubReader,
    parse_xhtml,
    serialize_xhtml,
    set_opf_language,
    set_opf_title,
    write_epub,
)
from .lmstudio_client import LMStudioClient, MockClient
from .prompts import MalformedResponseError, build_system, build_user, parse_response

logger = logging.getLogger("epub_translator")


def _opf_title(opf_bytes: bytes) -> str:
    """Devolve o dc:title do OPF (string vazia se não houver).

    Usa exatamente a mesma expressão do ``EpubReader`` para que a
    comparação com ``data.title`` seja coerente (inclusive quando o título
    tem conteúdo misto).
    """
    root = etree.fromstring(opf_bytes)
    return root.xpath(
        "string(//*[local-name()='metadata']/*[local-name()='title'])"
    )


def _setup_logging(output: str) -> None:
    """Configura handler de arquivo para <output>.log.

    Não duplica handler: rodar o mesmo output duas vezes na mesma sessão
    não pode gerar linhas repetidas no log.
    """
    log_path = os.path.abspath(output + ".log")
    for handler in logger.handlers:
        if getattr(handler, "baseFilename", "") == log_path:
            return

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


class TranslationCancelled(RuntimeError):
    """A tradução foi cancelada pelo usuário.

    É uma ``RuntimeError`` (para quem só quer pegar "erro de tradução") que
    o ``translate_epub`` NÃO engole: ao contrário das demais exceções do
    ``progress_cb``, ela aborta a tradução de imediato — o cancelamento não
    espera o fim do lote. O cache já gravado é preservado.
    """


# ── Configuração ──────────────────────────────────────────────────────────────


@dataclass
class ProgressEvent:
    """Evento de progresso emitido ao final de cada bloco processado.

    Attributes:
        file: Caminho da entrada no zip (ex.: ``OEBPS/Text/section-0005.html``).
        block_index: Índice global do bloco dentro do arquivo, 1-based — o
            mesmo índice usado na chave do cache.
        total_blocks: Total de blocos do arquivo que está sendo processado.
        done: Blocos já concluídos no livro todo (traduzidos + cache + falhas
            + descartados).
        total: Total de blocos do livro.
        elapsed_s: Segundos desde o início da tradução.
        status: Desfecho do bloco: ``"traduzido"``, ``"cache"``, ``"falhou"``
            ou ``"descartado"``.
        detail: Motivo do desfecho, quando houver (vazio nos casos de
            sucesso).
    """

    file: str
    block_index: int
    total_blocks: int
    done: int
    total: int
    elapsed_s: float
    status: str
    detail: str = ""


@dataclass
class TranslationConfig:
    """Parâmetros da tradução.

    Attributes:
        src: Caminho do arquivo EPUB de entrada.
        output: Caminho do arquivo EPUB de saída.
        target_lang: Idioma alvo (padrão ``pt-BR``).
        model: Nome do modelo; ``None`` para usar o primeiro disponível.
        base_url: URL da API OpenAI-compatível (padrão LM Studio local).
        batch_size: Blocos por requisição (padrão 5).
        max_chars_per_batch: Limite de caracteres por lote (padrão 8000).
        max_attempts: Tentativas por lote em caso de erro (padrão 3).
        use_cache: Habilita cache JSON (padrão ``True``).
        cache_path: Caminho do cache; ``None`` gera ``<output>.cache.json``.
        limit: Limite de arquivos a traduzir; ``None`` = todos.
        only: Caminho EXATO de uma entrada do zip a traduzir (ex.:
            ``OEBPS/Text/section-0005.html``); ``None`` = todos.
        limit_batches: Limita quantos lotes são traduzidos (para teste
            rápido); ``None`` = todos.
        bilingual: Inclui texto original junto com o traduzido (padrão ``False``).
        temperature: Temperatura do modelo (padrão 0.25).
        glossary: Glossário opcional ``{termo_original: termo_traduzido}``.
        progress_cb: Callback chamado a cada bloco concluído, com um
            ``ProgressEvent``; ``None`` (padrão) desliga o hook.
        pause_cb: Callback consultado ENTRE LOTES; quando devolve ``True``,
            a tradução para de forma limpa e grava um EPUB parcial.
    """

    src: str
    output: str
    target_lang: str = "pt-BR"
    model: str | None = None
    base_url: str = "http://localhost:1234/v1"
    batch_size: int = 5
    max_chars_per_batch: int = 8000
    max_attempts: int = 3
    use_cache: bool = True
    cache_path: str | None = None
    limit: int | None = None
    only: str | None = None
    limit_batches: int | None = None
    bilingual: bool = False
    temperature: float = 0.25
    glossary: dict[str, str] | None = None
    progress_cb: Callable[[ProgressEvent], None] | None = None
    pause_cb: Callable[[], bool] | None = None


# ── Relatório ─────────────────────────────────────────────────────────────────


@dataclass
class Report:
    """Relatório final da tradução.

    Attributes:
        total_blocks: Blocos encontrados no total.
        translated_blocks: Blocos traduzidos com sucesso.
        cached_blocks: Blocos servidos pelo cache.
        failed_blocks: Blocos que falharam após todas as tentativas.
        discarded_blocks: Blocos descartados por marcador inline inválido
            (a tradução do modelo não preservou os marcadores).
        files_processed: Nomes de arquivos traduzidos.
    """

    total_blocks: int = 0
    translated_blocks: int = 0
    cached_blocks: int = 0
    failed_blocks: int = 0
    discarded_blocks: int = 0
    files_processed: list[str] = field(default_factory=list)

    @property
    def accounted_blocks(self) -> int:
        """Total de blocos com destino definido (traduzido/cache/falha/descarte)."""
        return (
            self.translated_blocks
            + self.cached_blocks
            + self.failed_blocks
            + self.discarded_blocks
        )


# ── Montagem de lotes ────────────────────────────────────────────────────────


def build_batches(
    blocks: list[Block],
    batch_size: int,
    max_chars: int,
) -> list[list[tuple[int, Block]]]:
    """Monta lotes respeitando o limite de blocos E o limite de caracteres.

    Cada lote é uma lista de ``(índice_global, bloco)``, com o índice global
    1-based do bloco dentro do arquivo (usado na chave do cache, para não
    colidir entre lotes).

    Um bloco que sozinho ultrapasse ``max_chars`` vai em um lote sozinho:
    nunca um lote vazio, nunca um lote com mais de ``batch_size`` blocos.

    Args:
        blocks: Blocos do arquivo, na ordem do documento.
        batch_size: Máximo de blocos por lote.
        max_chars: Máximo de caracteres somados no lote.

    Returns:
        Lista de lotes.
    """
    batches: list[list[tuple[int, Block]]] = []
    atual: list[tuple[int, Block]] = []
    atual_chars = 0

    for global_index, block in enumerate(blocks, start=1):
        tamanho = len(block.text)

        if atual and (
            len(atual) >= batch_size or atual_chars + tamanho > max_chars
        ):
            batches.append(atual)
            atual = []
            atual_chars = 0

        atual.append((global_index, block))
        atual_chars += tamanho

    if atual:
        batches.append(atual)

    return batches


# ── Motor de tradução ─────────────────────────────────────────────────────────


def translate_epub(
    src: str,
    out: str,
    cfg: TranslationConfig,
    client: LMStudioClient | MockClient | None = None,
) -> Report:
    """Traduz um EPUB preservando estrutura, imagens e metadados.

    Args:
        src: Caminho do EPUB de entrada.
        out: Caminho do EPUB de saída.
        cfg: Configuração da tradução.
        client: Cliente LM Studio; ``None`` cria um padrão.

    Returns:
        ``Report`` com estatísticas da tradução.
    """
    # Configura logging para <output>.log
    _setup_logging(out)

    # Relógio do hook de progresso: mede o tempo desde o início da tradução.
    t0 = time.monotonic()

    # (a) Cliente
    if client is None:
        client = LMStudioClient(
            base_url=cfg.base_url,
            model=cfg.model,
            temperature=cfg.temperature,
        )

    # 2. Cache
    cache: BlockCache | None = None
    if cfg.use_cache:
        cache_file = cfg.cache_path or (cfg.output + ".cache.json")
        cache = BlockCache(cache_file)
        logger.info("Cache carregado de %s (%d entradas)", cache_file, len(cache))

    # 3. Glossary fingerprint
    glossary_hash = ""
    if cfg.glossary:
        glossary_hash = glossary_fingerprint(
            [f"{k}\t{v}" for k, v in cfg.glossary.items()]
        )

    # 4. Prompt de sistema (uma vez) — o glossário vai no prompt para
    # reforçar a regra "preserve NOMES PRÓPRIOS".
    system_prompt = build_system(
        target_lang=cfg.target_lang,
        glossary=cfg.glossary,
    )

    # 5. Leitura do EPUB
    data = EpubReader(src).read()

    # O modelo é resolvido UMA vez (evita um GET /v1/models por bloco).
    resolved_model = cfg.model or client.resolve_model()

    # 6. Arquivos a traduzir (ordem do spine, até limit)
    text_files = list(data.text_files)
    if cfg.only:
        # Filtro por caminho exato da entrada do zip. Mantém a ordem do spine.
        text_files = [f for f in text_files if f == cfg.only]
        if not text_files:
            logger.warning(
                "Nenhum arquivo do spine corresponde a 'only=%s'; "
                "nada será traduzido (o EPUB sai sem alterações de texto).",
                cfg.only,
            )
    if cfg.limit is not None:
        text_files = text_files[: cfg.limit]

    # Total de blocos do LIVRO: passagem leve (só parse, nenhuma chamada ao
    # modelo) para o hook de progresso ter o denominador do progresso.
    total_livro = 0
    for href in text_files:
        try:
            arvore, _ = parse_xhtml(data.files[href])
            total_livro += len(extract_blocks(arvore))
        except Exception as exc:
            logger.warning(
                "Não foi possível contar os blocos de %s: %s", href, exc
            )

    report = Report()
    overrides: dict[str, bytes] = {}
    pausado = False

    def _emitir(
        status: str,
        href: str,
        block_index: int,
        blocos_do_arquivo: int,
        detail: str = "",
    ) -> None:
        """Chama o ``progress_cb`` (se houver) sem derrubar a tradução.

        O hook é best-effort: uma exceção do callback é registrada no log e a
        tradução continua normalmente.
        """
        if cfg.progress_cb is None:
            return
        evento = ProgressEvent(
            file=href,
            block_index=block_index,
            total_blocks=blocos_do_arquivo,
            done=report.accounted_blocks,
            total=total_livro,
            elapsed_s=time.monotonic() - t0,
            status=status,
            detail=detail,
        )
        try:
            cfg.progress_cb(evento)
        except TranslationCancelled:
            # Cancelamento: aborta a tradução de imediato (o cache já
            # gravado é preservado).
            raise
        except Exception as exc:
            logger.warning("Falha no progress_cb: %s", exc)

    def _processar_lote(
        href: str,
        blocks: list[Block],
        batch: list[tuple[int, Block]],
    ) -> None:
        """Processa um lote: cache, tradução, falhas e descartes.

        O corpo é o pipeline original; os ``continue`` que pulavam para o
        próximo lote viram ``return`` (mesmo efeito dentro da função).
        """
        # (f) chave por bloco: calculada sobre o texto ORIGINAL, ANTES
        # da tradução — é a MESMA chave usada no put depois.
        keys: dict[int, str] = {}
        uncached: list[tuple[int, Block]] = []

        for global_index, b in batch:
            k = key_for(
                text=b.text,
                marker_text=format_block(global_index, b.text),
                target=cfg.target_lang,
                model=resolved_model,
                source=src,
                glossary_hash=glossary_hash,
                context="",
                file_path=href,
            )
            keys[global_index] = k
            if cache is not None:
                hit = cache.get(k)
                if hit is not None:
                    # BUG-1: o cache precisa APLICAR a tradução ao bloco,
                    # senão a 2ª execução mantém o texto original.
                    b.text = hit
                    report.cached_blocks += 1
                    logger.info(
                        "%s bloco %d: cache (tradução reusada)",
                        href,
                        global_index,
                    )
                    _emitir("cache", href, global_index, len(blocks))
                    continue
            uncached.append((global_index, b))

        if not uncached:
            return

        # O modelo responde numerando de 1 dentro do lote.
        lote_para_prompt = [b for _, b in uncached]
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user(lote_para_prompt)},
        ]

        # Tentativas com retry
        response_text: str | None = None
        last_error: str = ""
        for attempt in range(cfg.max_attempts):
            try:
                response_text = client.chat(messages, temperature=cfg.temperature)
                break
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Tentativa %d/%d falhou para %s: %s",
                    attempt + 1,
                    cfg.max_attempts,
                    href,
                    exc,
                )
                if attempt < cfg.max_attempts - 1:
                    continue
                logger.error(
                    "Todos os %d attempts falharam para %s",
                    cfg.max_attempts,
                    href,
                )

        if response_text is None:
            for global_index, b in uncached:
                report.failed_blocks += 1
                logger.error(
                    "%s bloco %d: FALHOU — %s",
                    href,
                    global_index,
                    last_error or "erro desconhecido",
                )
                _emitir(
                    "falhou",
                    href,
                    global_index,
                    len(blocks),
                    last_error or "erro desconhecido",
                )
            return

        # parse_response
        try:
            parsed = parse_response(response_text)
        except MalformedResponseError as exc:
            for global_index, b in uncached:
                report.failed_blocks += 1
                logger.error(
                    "%s bloco %d: FALHOU — resposta malformada: %s",
                    href,
                    global_index,
                    exc,
                )
                _emitir(
                    "falhou",
                    href,
                    global_index,
                    len(blocks),
                    f"resposta malformada: {exc}",
                )
            return

        translated_map: dict[int, str] = dict(parsed)

        for position, (global_index, b) in enumerate(uncached):
            local_idx = position + 1  # 1-based dentro do lote
            original_text = b.text

            if local_idx not in translated_map:
                report.failed_blocks += 1
                logger.error(
                    "%s bloco %d: FALHOU — o modelo não devolveu este bloco",
                    href,
                    global_index,
                )
                _emitir(
                    "falhou",
                    href,
                    global_index,
                    len(blocks),
                    "o modelo não devolveu este bloco",
                )
                continue

            translated_text = translated_map[local_idx]

            # Resposta vazia para bloco não-vazio: mantém o original.
            if not translated_text.strip() and original_text.strip():
                report.failed_blocks += 1
                logger.error(
                    "%s bloco %d: FALHOU — o modelo devolveu bloco vazio",
                    href,
                    global_index,
                )
                _emitir(
                    "falhou",
                    href,
                    global_index,
                    len(blocks),
                    "o modelo devolveu bloco vazio",
                )
                continue

            # Validação dos marcadores: a tradução precisa devolver os
            # marcadores byte a byte, na mesma ordem. Se não devolver,
            # o bloco é DESCARTADO (não reconstruído errado).
            ok, motivo = compare_markers(original_text, translated_text)
            if not ok:
                report.discarded_blocks += 1
                logger.error(
                    "%s bloco %d: DESCARTADO — marcador inválido: %s",
                    href,
                    global_index,
                    motivo,
                )
                _emitir(
                    "descartado",
                    href,
                    global_index,
                    len(blocks),
                    f"marcador inválido: {motivo}",
                )
                continue

            b.text = translated_text
            report.translated_blocks += 1
            logger.info(
                "%s bloco %d: traduzido (%d caracteres)",
                href,
                global_index,
                len(translated_text),
            )
            _emitir("traduzido", href, global_index, len(blocks))

            # (j) cache.put com a MESMA chave do get (calculada sobre o
            # texto original) — por isso o guardamos em `keys`.
            if cache is not None:
                cache.put(keys[global_index], translated_text)

    for href in text_files:
        logger.info("Traduzindo arquivo: %s", href)
        report.files_processed.append(href)

        raw = data.files[href]

        # (b) Parse lxml
        tree, doctype = parse_xhtml(raw)

        # (c) extract_blocks
        blocks = extract_blocks(tree)

        # (d) validate_markers no texto original
        for b in blocks:
            validate_markers(b.text)

        report.total_blocks += len(blocks)

        if not blocks:
            logger.warning("Nenhum bloco encontrado em %s", href)
            continue

        # (e) Lotes (respeitam batch_size e max_chars_per_batch)
        lotes = build_batches(blocks, cfg.batch_size, cfg.max_chars_per_batch)
        if cfg.limit_batches is not None:
            lotes = lotes[: cfg.limit_batches]
            logger.info(
                "Limite de %d lote(s) ativo: %d de %d lote(s) serão traduzidos.",
                cfg.limit_batches,
                len(lotes),
                len(
                    build_batches(
                        blocks, cfg.batch_size, cfg.max_chars_per_batch
                    )
                ),
            )

        for batch in lotes:
            _processar_lote(href, blocks, batch)

            # A pausa é pedida ENTRE LOTES (nunca entre blocos): depois de
            # processar o lote inteiro, o pause_cb é consultado. Se pedir
            # pausa, a tradução para de forma limpa — o EPUB parcial é
            # gravado no final (passos (m)-(n)).
            if cfg.pause_cb is not None:
                try:
                    parar = cfg.pause_cb()
                except Exception as exc:
                    logger.warning("Falha no pause_cb: %s", exc)
                    parar = False
                if parar:
                    pausado = True
                    logger.info(
                        "Tradução pausada — %d bloco(s) preservado(s)",
                        report.accounted_blocks,
                    )
                    break

        # (h) replay(blocks, tree)
        result_root = replay(blocks, tree)

        # (i) serialize_xhtml
        serialized = serialize_xhtml(etree.ElementTree(result_root), doctype)

        # (k) bilingual: traduzido += original
        if cfg.bilingual:
            original_text = raw.decode("utf-8")
            translated_text = serialized.decode("utf-8")
            combined = translated_text + "\n\n<!-- ORIGINAL -->\n\n" + original_text
            serialized = combined.encode("utf-8")

        # (l) overrides[text_file] = serialized
        overrides[href] = serialized

        # Pausa solicitada: o arquivo atual já foi gravado (com o que
        # traduziu até a pausa) e os próximos ficam intactos.
        if pausado:
            break

    # (m) OPF: dc:language atualizado + NCX/nav
    # O OPF alterado PRECISA ir para os overrides — sem isso o arquivo
    # original era copiado para a saída e o dc:language continuava antigo.
    opf_bytes = set_opf_language(data.opf_bytes, cfg.target_lang)
    overrides[data.opf_path] = opf_bytes

    # O dc:title NÃO é traduzido (o título original já está no OPF e deve ser
    # preservado byte a byte) — set_opf_title só é chamado se o título mudar.
    if data.title and data.title != _opf_title(opf_bytes):
        opf_bytes = set_opf_title(opf_bytes, data.title)
        overrides[data.opf_path] = opf_bytes

    # O NCX NÃO é traduzido nesta execução: ele é copiado byte a byte do
    # original. Re-serializar sem mudar nada só alteraria os bytes.
    for ncx_path in data.ncx_paths:
        logger.info("NCX mantido intacto (byte-idêntico): %s", ncx_path)

    # (n) write_epub
    write_epub(out, data, overrides)

    logger.info(
        "Tradução concluída: %d blocos, %d traduzidos, %d cache, %d falhas, "
        "%d descartados",
        report.total_blocks,
        report.translated_blocks,
        report.cached_blocks,
        report.failed_blocks,
        report.discarded_blocks,
    )

    return report
