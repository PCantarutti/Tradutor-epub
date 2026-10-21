#!/usr/bin/env python
"""Traduz um capítulo de EPUB usando o modelo rodando no LM Studio local.

Uso típico (destaca da tarefa):

    python traduzir.py

Traduz apenas ``OEBPS/Text/section-0005.html`` de ``validacao.epub`` para
português do Brasil e grava ``saida/traducao_resultado.epub`` +
``saida/traducao_resultado.log``. O cache JSON vai junto, em
``saida/traducao_resultado.epub.cache.json``.

O script é RESUMÍVEL: o cache JSON guarda cada bloco já traduzido, então
rodar de novo não re-traduz o que já está pronto.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import requests
from lxml import etree

from epub_translator.lmstudio_client import (
    LMStudioClient,
    LMStudioError,
    MockClient,
    NoModelError,
    ServerNotRunningError,
)
from epub_translator.translator import TranslationConfig, translate_epub

# ── Constantes da tarefa ─────────────────────────────────────────────────────

# Pasta onde ficam as saídas de tradução (EPUB gerado, log e cache).
PASTA_SAIDA = "saida"

DEFAULT_SRC = "validacao.epub"
DEFAULT_OUTPUT = f"{PASTA_SAIDA}/traducao_resultado.epub"
DEFAULT_LOG = f"{PASTA_SAIDA}/traducao_resultado.log"
DEFAULT_MODEL = "qwen/qwen3.6-35b-a3b"
DEFAULT_TARGET = "pt-BR"
DEFAULT_ONLY = "OEBPS/Text/section-0005.html"
DEFAULT_BASE_URL = "http://localhost:1234/v1"

# Marcadores de tag inline que não podem sobrar no texto final.
RE_MARCADOR = re.compile(r"\[\[/?\d+\]\]")

# Extensões que precisam sair byte-idênticas (imagens e CSS).
EXTENSOES_BINARIAS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".css")

# Glossário de NOMES PRÓPRIOS usado por padrão: termo → termo. Reforça a
# regra 2 do prompt ("Preserve NOMES PRÓPRIOS"), que sozinho não impede o
# modelo de traduzir títulos como "Man-God" (observado na calibração contra
# o LM Studio). O glossário NÃO fica no código: vive num arquivo JSON
# versionável, reaproveitado pela CLI do pacote e pelo upload da interface.
GLOSSARIO_PADRAO = Path(__file__).resolve().parent / "glossarios" / "mushoku_tensei.json"

logger = logging.getLogger("traduzir")


# ── Glossário ────────────────────────────────────────────────────────────────


def carregar_glossario(caminho: str | Path | None = None) -> dict[str, str]:
    """Carrega um glossário {termo: tradução} de um arquivo JSON.

    O glossário é OPCIONAL: qualquer problema (arquivo ausente, JSON
    inválido ou conteúdo que não seja um objeto) é registrado como aviso e
    a tradução segue sem glossário.

    Args:
        caminho: Caminho do arquivo JSON; None usa o glossário padrão
            (``glossarios/mushoku_tensei.json``).

    Returns:
        Mapeamento {termo: tradução}; {} se o glossário não puder ser lido.
    """
    alvo = Path(caminho) if caminho is not None else GLOSSARIO_PADRAO

    if not alvo.exists():
        logger.warning(
            "Glossário não encontrado: %s — seguindo sem glossário.", alvo
        )
        return {}

    try:
        with alvo.open(encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Não foi possível ler o glossário %s (%s) — seguindo sem glossário.",
            alvo,
            exc,
        )
        return {}

    if not isinstance(dados, dict):
        logger.warning(
            "O glossário %s deve ser um objeto JSON {termo: tradução} — "
            "seguindo sem glossário.",
            alvo,
        )
        return {}

    glossario = {str(k): str(v) for k, v in dados.items()}
    logger.info("Glossário carregado de %s (%d termos)", alvo, len(glossario))
    return glossario


# ── Verificação final ────────────────────────────────────────────────────────


@dataclass
class ResultadoVerificacao:
    """Resultado de um item da verificação final."""

    item: str
    ok: bool
    detalhe: str = ""


@dataclass
class Verificacao:
    """Conjunto de resultados da verificação final do EPUB."""

    resultados: list[ResultadoVerificacao] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True quando todos os itens passaram."""
        return all(r.ok for r in self.resultados)

    def add(self, item: str, ok: bool, detalhe: str = "") -> None:
        """Registra o resultado de um item."""
        self.resultados.append(ResultadoVerificacao(item, ok, detalhe))

    def linhas(self) -> list[str]:
        """Linhas formatadas do relatório de verificação."""
        saida = []
        for r in self.resultados:
            marca = "OK  " if r.ok else "FALHA"
            linha = f"[{marca}] {r.item}"
            if r.detalhe:
                linha += f" — {r.detalhe}"
            saida.append(linha)
        return saida


def _contar_tags(xhtml_bytes: bytes) -> tuple[int, int]:
    """Conta <p> e <span> de um XHTML (parse estrito, sem recover)."""
    root = etree.fromstring(xhtml_bytes)
    return (
        len(root.xpath("//*[local-name()='p']")),
        len(root.xpath("//*[local-name()='span']")),
    )


def _textos_opf(opf_bytes: bytes) -> tuple[str, str]:
    """Devolve (dc:title, dc:language) do OPF."""
    root = etree.fromstring(opf_bytes)
    titulo = root.xpath(
        "string(//*[local-name()='metadata']/*[local-name()='title'])"
    )
    idioma = root.xpath(
        "string(//*[local-name()='metadata']/*[local-name()='language'])"
    )
    return titulo, idioma


def verificar_epub(
    original_path: str,
    saida_path: str,
    alvo: str,
    idioma_alvo: str,
) -> Verificacao:
    """Roda a verificação final do EPUB traduzido.

    Itens verificados:
        (a) mesmas contagens de <p> e <span> do original;
        (b) nenhum marcador [[n]] sobrando no texto final;
        (c) dc:language = idioma alvo e dc:title intacto;
        (d) imagens e CSS byte a byte iguais aos do original;
        (e) mimetype é a primeira entrada e está ZIP_STORED;
        (f) nenhuma entrada duplicada; total de entradas preservado;
        (g) o zip abre sem erro e o XHTML traduzido é XML válido.

    Args:
        original_path: Caminho do EPUB original.
        saida_path: Caminho do EPUB traduzido.
        alvo: Caminho do XHTML traduzido dentro do zip.
        idioma_alvo: Idioma esperado no dc:language.

    Returns:
        ``Verificacao`` com o resultado de cada item.
    """
    v = Verificacao()

    if not os.path.exists(saida_path):
        v.add("EPUB de saída existe", False, f"arquivo não encontrado: {saida_path}")
        return v
    v.add("EPUB de saída existe", True, saida_path)

    with zipfile.ZipFile(original_path) as z_orig, zipfile.ZipFile(saida_path) as z_out:
        nomes_orig = z_orig.namelist()
        nomes_out = z_out.namelist()

        # ── (g1) zip íntegro ──
        corrompido = z_out.testzip()
        v.add(
            "ZIP abre sem erro",
            corrompido is None,
            "nenhuma entrada corrompida" if corrompido is None
            else f"entrada corrompida: {corrompido}",
        )

        # ── (e) mimetype primeiro e ZIP_STORED ──
        if nomes_out and nomes_out[0] == "mimetype":
            info = z_out.getinfo("mimetype")
            conteudo = z_out.read("mimetype")
            ok_mt = (
                info.compress_type == zipfile.ZIP_STORED
                and conteudo == b"application/epub+zip"
            )
            detalhe = (
                f"primeira entrada, compress_type={info.compress_type} "
                f"(0=STORED), conteúdo {conteudo!r}"
            )
        else:
            ok_mt = False
            detalhe = f"primeira entrada é {nomes_out[0] if nomes_out else '(vazio)'!r}"
        v.add("mimetype é a primeira entrada e está ZIP_STORED", ok_mt, detalhe)

        # ── (f) sem duplicadas, mesmo total de entradas ──
        duplicadas = len(nomes_out) != len(set(nomes_out))
        v.add(
            "Nenhuma entrada duplicada no ZIP",
            not duplicadas,
            f"{len(nomes_out)} entradas (original: {len(nomes_orig)})",
        )
        v.add(
            "Total de entradas preservado",
            len(nomes_out) == len(nomes_orig),
            f"saída={len(nomes_out)} original={len(nomes_orig)}",
        )

        # ── (d) binários byte-idênticos ──
        binarios = [
            n for n in nomes_orig if n.lower().endswith(EXTENSOES_BINARIAS)
        ]
        divergentes = [
            n for n in binarios if z_out.read(n) != z_orig.read(n)
        ]
        v.add(
            "Imagens e CSS byte a byte idênticos",
            not divergentes,
            f"{len(binarios)} arquivos comparados, {len(divergentes)} divergentes"
            + (f" ({', '.join(divergentes)})" if divergentes else ""),
        )

        # ── (a) contagem de tags ──
        if alvo in nomes_out and alvo in nomes_orig:
            p_out, span_out = _contar_tags(z_out.read(alvo))
            p_orig, span_orig = _contar_tags(z_orig.read(alvo))
            v.add(
                "Mesma quantidade de <p> e <span> do original",
                (p_out, span_out) == (p_orig, span_orig),
                f"saída: {p_out} <p> / {span_out} <span> | "
                f"original: {p_orig} <p> / {span_orig} <span>",
            )
        else:
            v.add(
                "Mesma quantidade de <p> e <span> do original",
                False,
                f"alvo {alvo} não está no zip de saída",
            )

        # ── (b) nenhum marcador sobrando ──
        sobrando: list[str] = []
        for nome in nomes_out:
            if not nome.endswith((".html", ".xhtml")):
                continue
            texto = z_out.read(nome).decode("utf-8", errors="replace")
            if RE_MARCADOR.search(texto):
                sobrando.append(nome)
        v.add(
            "Nenhum marcador [[n]] sobrando no texto final",
            not sobrando,
            "nenhum marcador encontrado" if not sobrando
            else f"marcadores remanescentes em: {', '.join(sobrando)}",
        )

        # ── (g2) XHTML traduzido é XML válido ──
        if alvo in nomes_out:
            try:
                etree.fromstring(z_out.read(alvo))
                v.add("XHTML traduzido é XML válido", True, "parse estrito OK")
            except etree.XMLSyntaxError as exc:
                v.add("XHTML traduzido é XML válido", False, str(exc))
        else:
            v.add("XHTML traduzido é XML válido", False, f"{alvo} ausente")

        # ── (c) OPF: dc:language e dc:title ──
        opf_path = None
        for nome in nomes_out:
            if nome.lower().endswith(".opf"):
                opf_path = nome
                break
        if opf_path:
            titulo_out, idioma_out = _textos_opf(z_out.read(opf_path))
            if opf_path in nomes_orig:
                titulo_orig, _ = _textos_opf(z_orig.read(opf_path))
            else:
                titulo_orig = titulo_out
            v.add(
                "OPF com dc:language = idioma alvo",
                idioma_out == idioma_alvo,
                f"dc:language={idioma_out!r} (esperado {idioma_alvo!r})",
            )
            v.add(
                "OPF com dc:title intacto",
                titulo_out == titulo_orig and bool(titulo_out),
                f"dc:title={titulo_out!r}",
            )
        else:
            v.add("OPF com dc:language = idioma alvo", False, "nenhum .opf no zip")
            v.add("OPF com dc:title intacto", False, "nenhum .opf no zip")

        # ── extra: NCX byte-idêntico ──
        ncx = [n for n in nomes_orig if n.lower().endswith(".ncx")]
        if ncx:
            divergentes_ncx = [n for n in ncx if z_out.read(n) != z_orig.read(n)]
            v.add(
                "NCX byte-idêntico ao original",
                not divergentes_ncx,
                f"{len(ncx)} arquivo(s) NCX comparados",
            )

    return v


# ── Verificação do LM Studio ─────────────────────────────────────────────────


def checar_lm_studio(base_url: str, modelo: str | None) -> list[str]:
    """Verifica se o LM Studio está no ar e devolve a lista de modelos.

    Args:
        base_url: URL base da API (ex.: http://localhost:1234/v1).
        modelo: Modelo desejado, ou None para não exigir um específico.

    Returns:
        Lista de IDs de modelos disponíveis.

    Raises:
        ServerNotRunningError: Se o servidor não responder.
        NoModelError: Se nenhum modelo estiver carregado.
    """
    url = f"{base_url.rstrip('/')}/models"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.ConnectionError as exc:
        raise ServerNotRunningError(
            f"O LM Studio não está rodando em {base_url} — abra o LM Studio "
            "e inicie o servidor na aba Server."
        ) from exc
    except requests.Timeout as exc:
        raise ServerNotRunningError(
            f"Timeout ao conectar em {base_url} — verifique se o servidor "
            "está acessível."
        ) from exc
    except requests.HTTPError as exc:
        raise LMStudioError(
            f"Erro HTTP {resp.status_code} ao listar modelos em {url}."
        ) from exc

    data = resp.json()
    modelos = [m.get("id", "") for m in data.get("data", [])]

    if not modelos:
        raise NoModelError(
            "Nenhum modelo carregado no LM Studio — carregue um modelo "
            "antes de traduzir."
        )

    if modelo and modelo not in modelos:
        logger.warning(
            "O modelo %s não está na lista de modelos disponíveis. "
            "Modelos disponíveis: %s",
            modelo,
            ", ".join(modelos),
        )

    return modelos


# ── Configuração do logging ─────────────────────────────────────────────────


def configurar_log(caminho_log: str) -> None:
    """Configura o log em arquivo (e remove handlers anteriores)."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    Path(caminho_log).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(caminho_log, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    # O log do pacote vai para o mesmo arquivo.
    pkg = logging.getLogger("epub_translator")
    for handler in list(pkg.handlers):
        pkg.removeHandler(handler)
        handler.close()
    pkg.addHandler(handler)
    pkg.setLevel(logging.DEBUG)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Monta o parser de argumentos."""
    p = argparse.ArgumentParser(
        prog="traduzir.py",
        description=(
            "Traduz um capítulo de EPUB usando o modelo do LM Studio local "
            "e verifica a integridade do EPUB resultante."
        ),
    )
    p.add_argument("--src", default=DEFAULT_SRC, help="EPUB de entrada.")
    p.add_argument("--output", "-o", default=DEFAULT_OUTPUT, help="EPUB de saída.")
    p.add_argument("--log", default=DEFAULT_LOG, help="Arquivo de log.")
    p.add_argument("--model", "-m", default=DEFAULT_MODEL, help="Modelo do LM Studio.")
    p.add_argument("--target", "-t", default=DEFAULT_TARGET, help="Idioma alvo.")
    p.add_argument(
        "--only",
        default=DEFAULT_ONLY,
        help="Arquivo (caminho exato no zip) a traduzir; '' = todos.",
    )
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="URL da API.")
    p.add_argument("--batch-size", type=int, default=5, help="Blocos por requisição.")
    p.add_argument(
        "--max-chars", type=int, default=8000, help="Máximo de caracteres por lote."
    )
    p.add_argument(
        "--timeout", type=float, default=300.0, help="Timeout por requisição (s)."
    )
    p.add_argument(
        "--limit-batches",
        type=int,
        default=None,
        help="Traduzir no máximo N lotes (para teste rápido).",
    )
    p.add_argument("--no-cache", action="store_true", help="Desabilita o cache JSON.")
    p.add_argument(
        "--cache-path", default=None, help="Caminho do cache; None = <output>.cache.json."
    )
    p.add_argument("--mock", action="store_true", help="Usa MockClient (sem LM Studio).")
    p.add_argument(
        "--verify-only",
        action="store_true",
        help="Só verifica um EPUB já gerado (não traduz).",
    )
    p.add_argument(
        "--no-glossary",
        action="store_true",
        help="Não envia glossário de nomes próprios.",
    )
    p.add_argument(
        "--glossary",
        default=None,
        help=(
            "Caminho de um glossário JSON {termo: tradução}; padrão: "
            f"{GLOSSARIO_PADRAO.name} (pasta glossarios/)."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada do script de tradução.

    Args:
        argv: Argumentos da linha de comando (None = sys.argv[1:]).

    Returns:
        Código de saída (0 = sucesso).
    """
    args = _build_parser().parse_args(argv)

    # Garante que as pastas de saída existam (EPUB, log e cache).
    for caminho in (args.output, args.log, args.cache_path):
        if caminho:
            pasta = os.path.dirname(caminho)
            if pasta:
                Path(pasta).mkdir(parents=True, exist_ok=True)

    configurar_log(args.log)
    logger.info("=" * 70)
    logger.info("Tradução iniciada: %s -> %s", args.src, args.output)
    logger.info(
        "Modelo=%s | Idioma=%s | Arquivo=%s | Lote=%d blocos",
        args.model,
        args.target,
        args.only or "(todos)",
        args.batch_size,
    )

    # ── Modo verify-only ──
    if args.verify_only:
        v = verificar_epub(args.src, args.output, args.only or DEFAULT_ONLY, args.target)
        print("\n=== Verificação do EPUB ===")
        for linha in v.linhas():
            print(linha)
        for linha in v.linhas():
            logger.info("VERIFICACAO %s", linha)
        print()
        print("RESULTADO DA VERIFICAÇÃO:", "TUDO OK" if v.ok else "HOUVE FALHAS")
        return 0 if v.ok else 1

    # ── Glossário ──
    # O glossário é opcional: um caminho explícito que não existe é erro
    # (o usuário pediu um arquivo que não está lá); já o glossário padrão
    # ausente apenas desliga o glossário, com aviso no log.
    glossary: dict[str, str] | None = None
    if not args.no_glossary:
        caminho_glossario = args.glossary or GLOSSARIO_PADRAO
        if args.glossary and not Path(caminho_glossario).exists():
            print(
                f"ERRO: glossário não encontrado: {caminho_glossario}",
                file=sys.stderr,
            )
            logger.error("Glossário não encontrado: %s", caminho_glossario)
            return 2
        glossary = carregar_glossario(caminho_glossario)

    # ── Verificação do LM Studio ──
    if not args.mock:
        try:
            modelos = checar_lm_studio(args.base_url, args.model)
        except (ServerNotRunningError, NoModelError, LMStudioError) as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            logger.error("Falha na verificação do LM Studio: %s", exc)
            return 2
        print(f"LM Studio OK em {args.base_url} — {len(modelos)} modelo(s) carregado(s).")
        logger.info("Modelos disponíveis: %s", ", ".join(modelos))

    # ── Execução ──
    cfg = TranslationConfig(
        src=args.src,
        output=args.output,
        target_lang=args.target,
        model=args.model,
        base_url=args.base_url,
        batch_size=args.batch_size,
        max_chars_per_batch=args.max_chars,
        use_cache=not args.no_cache,
        cache_path=args.cache_path,
        only=args.only or None,
        limit_batches=args.limit_batches,
        temperature=0.25,
        glossary=glossary,
    )

    if args.mock:
        client: LMStudioClient | MockClient = MockClient()
    else:
        client = LMStudioClient(
            base_url=args.base_url,
            model=args.model,
            timeout=args.timeout,
        )

    print(f"Traduzindo {cfg.src} -> {cfg.output}")
    print(f"  Modelo: {cfg.model}")
    print(f"  Idioma: {cfg.target_lang}")
    print(f"  Arquivo: {cfg.only or '(todos do spine)'}")
    print("")

    try:
        report = translate_epub(cfg.src, cfg.output, cfg, client=client)
    except Exception as exc:
        print(f"ERRO durante a tradução: {exc}", file=sys.stderr)
        logger.exception("Falha durante a tradução")
        return 1

    # ── Relatório ──
    print("")
    print("=== Resultado da tradução ===")
    print(f"  Blocos traduzidos:   {report.translated_blocks}")
    print(f"  Blocos do cache:     {report.cached_blocks}")
    print(f"  Blocos com falha:    {report.failed_blocks}")
    print(f"  Blocos descartados:  {report.discarded_blocks}")
    print(f"  Total de blocos:     {report.total_blocks}")
    print(f"  Arquivos traduzidos: {', '.join(report.files_processed) or '(nenhum)'}")

    logger.info(
        "RESUMO: %d traduzidos, %d cache, %d falhas, %d descartados, %d total",
        report.translated_blocks,
        report.cached_blocks,
        report.failed_blocks,
        report.discarded_blocks,
        report.total_blocks,
    )

    # ── Verificação final ──
    v = verificar_epub(
        args.src, args.output, args.only or DEFAULT_ONLY, args.target
    )
    print("")
    print("=== Verificação do EPUB ===")
    for linha in v.linhas():
        print(linha)
        logger.info("VERIFICACAO %s", linha)

    print("")
    print("RESULTADO DA VERIFICAÇÃO:", "TUDO OK" if v.ok else "HOUVE FALHAS")
    logger.info(
        "Verificação final: %s", "TUDO OK" if v.ok else "HOUVE FALHAS"
    )

    return 0 if v.ok else 1


if __name__ == "__main__":
    sys.exit(main())
