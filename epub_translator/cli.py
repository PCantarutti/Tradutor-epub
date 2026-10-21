"""Command-line interface for epub_translator.

Sintaxe:
    python epub_translator.py <epub> [opções]

Configuração (ordem de prioridade): CLI > YAML > defaults.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from epub_translator.translator import TranslationConfig, translate_epub
from epub_translator.lmstudio_client import LMStudioClient, MockClient

logger = logging.getLogger("epub_translator")
console = Console()


# ── Config YAML ──────────────────────────────────────────────────────────────


def _load_config(path: str) -> dict[str, Any]:
    """Carrega configurações de um arquivo YAML."""
    p = Path(path)
    if not p.exists():
        console.print(f"[yellow]Arquivo de configuração não encontrado: {path}[/]")
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            console.print("[yellow]Arquivo YAML vazio ou inválido.[/]")
            return {}
        return data
    except yaml.YAMLError as exc:
        console.print(f"[red]Erro ao ler YAML: {exc}[/]")
        return {}


# ── Argumentos ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Constroi e retorna o parser de argumentos."""
    parser = argparse.ArgumentParser(
        prog="epub_translator",
        description="Traduz livros EPUB usando modelos locais do LM Studio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python epub_translator.py livro.epub --target pt-BR\n"
            "  python epub_translator.py livro.epub --mock --limit 10\n"
            "  python epub_translator.py livro.epub --list-models\n"
        ),
    )

    parser.add_argument(
        "src",
        help="Caminho do arquivo EPUB de entrada.",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Caminho do EPUB traduzido (padrão: <src>_translated.epub).",
    )
    parser.add_argument(
        "--target", "-t",
        default="pt-BR",
        help="Idioma alvo (padrão: pt-BR).",
    )
    parser.add_argument(
        "--model", "-m",
        default=None,
        help="Nome do modelo; None = usar o primeiro disponível.",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:1234/v1",
        help="URL da API (padrão: http://localhost:1234/v1).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Blocos por requisição (padrão: 5).",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=8000,
        help="Limite de caracteres por lote (padrão: 8000).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Tentativas por lote (padrão: 3).",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        default=True,
        help="Habilita cache JSON (padrão: True).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Desabilita cache JSON.",
    )
    parser.add_argument(
        "--cache-path",
        default=None,
        help="Caminho do cache; None = <output>.cache.json.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limite de arquivos a traduzir; None = todos.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help=(
            "Traduzir apenas este arquivo (caminho exato da entrada do zip, "
            "ex.: OEBPS/Text/section-0005.html)."
        ),
    )
    parser.add_argument(
        "--bilingual",
        action="store_true",
        default=False,
        help="Inclui texto original junto com o traduzido.",
    )
    parser.add_argument(
        "--glossary",
        default=None,
        help=(
            "Caminho para arquivo de glossário em YAML ou JSON "
            "(termo → tradução)."
        ),
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=False,
        help="Usa MockClient para tradução (sem conectar ao LM Studio).",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        default=False,
        help="Lista modelos disponíveis no servidor e sai.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.25,
        help="Temperatura do modelo (padrão: 0.25).",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Caminho para arquivo de configuração YAML.",
    )

    return parser


# ── CLI principal ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """Ponto de entrada principal.

    Args:
        argv: Lista de argumentos (None = sys.argv[1:]).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Carrega config YAML se fornecido
    yaml_cfg: dict[str, Any] = {}
    if args.config:
        yaml_cfg = _load_config(args.config)

    # Prioridade: CLI > YAML > defaults
    use_cache = args.use_cache and not args.no_cache
    if "use_cache" in yaml_cfg:
        use_cache = yaml_cfg["use_cache"]

    cache_path = args.cache_path
    if cache_path is None and "cache_path" in yaml_cfg:
        cache_path = yaml_cfg["cache_path"]

    output = args.output
    if output is None and "output" in yaml_cfg:
        output = yaml_cfg["output"]

    # ── --list-models ─────────────────────────────────────────────────────

    if args.list_models:
        client: LMStudioClient | MockClient
        if args.mock:
            client = MockClient()
        else:
            client = LMStudioClient(
                base_url=args.base_url,
                model=args.model,
            )
        try:
            models = client.list_models()
        except Exception as exc:
            console.print(f"[red]Erro ao listar modelos: {exc}[/]")
            sys.exit(1)
        if models:
            console.print("[green]Modelos disponíveis:[/]")
            for m in models:
                console.print(f"  - {m}")
        else:
            console.print("[yellow]Nenhum modelo disponível.[/]")
        return

    # ── Configuração ──────────────────────────────────────────────────────

    # Glossário
    glossary: dict[str, str] | None = None
    if args.glossary:
        glossary_path = Path(args.glossary)
        if glossary_path.exists():
            with glossary_path.open(encoding="utf-8") as f:
                glossary_data = yaml.safe_load(f)
            if isinstance(glossary_data, dict):
                glossary = {str(k): str(v) for k, v in glossary_data.items()}
            else:
                console.print("[yellow]Glossário YAML deve ser um mapeamento chave-valor.[/]")
        else:
            console.print(f"[red]Glossário não encontrado: {args.glossary}[/]")
            sys.exit(1)

    # Output default: vai para a pasta "saida/" AO LADO do arquivo de
    # origem, para que as traduções fiquem sempre numa pasta dedicada sem
    # misturar com o código nem com o EPUB de entrada.
    if output is None:
        src_path = Path(args.src)
        output = str(
            src_path.parent / "saida" / (src_path.stem + "_translated" + src_path.suffix)
        )
        Path(output).parent.mkdir(parents=True, exist_ok=True)

    cfg = TranslationConfig(
        src=args.src,
        output=output,
        target_lang=args.target,
        model=args.model,
        base_url=args.base_url,
        batch_size=args.batch_size,
        max_chars_per_batch=args.max_chars,
        max_attempts=args.max_attempts,
        use_cache=use_cache,
        cache_path=cache_path,
        limit=args.limit,
        only=args.only,
        bilingual=args.bilingual,
        temperature=args.temperature,
        glossary=glossary,
    )

    # ── Cliente ───────────────────────────────────────────────────────────

    if args.mock:
        client: LMStudioClient | MockClient = MockClient()
    else:
        client = LMStudioClient(
            base_url=cfg.base_url,
            model=cfg.model,
            temperature=cfg.temperature,
        )

    # ── Tradução ──────────────────────────────────────────────────────────

    console.print(f"[cyan]Traduzindo {cfg.src} → {cfg.output}[/]")
    console.print(f"  Modelo: {cfg.model or 'auto'}")
    console.print(f"  Idioma: {cfg.target_lang}")
    console.print(f"  Mock:   {args.mock}")
    console.print("")

    try:
        report = translate_epub(cfg.src, cfg.output, cfg, client=client)
    except Exception as exc:
        console.print(f"[red]Erro durante a tradução: {exc}[/]")
        sys.exit(1)

    # ── Relatório ─────────────────────────────────────────────────────────

    console.print("")
    console.print("[green]Tradução concluída![/]")
    console.print(f"  Blocos total:      {report.total_blocks}")
    console.print(f"  Traduzidos:        {report.translated_blocks}")
    console.print(f"  Cache (servidos):  {report.cached_blocks}")
    console.print(f"  Falhas:            {report.failed_blocks}")
    console.print(f"  Descartados:       {report.discarded_blocks}")
    console.print(f"  Arquivos:          {', '.join(report.files_processed)}")
    console.print(f"  Saída:             {cfg.output}")


if __name__ == "__main__":
    main()
