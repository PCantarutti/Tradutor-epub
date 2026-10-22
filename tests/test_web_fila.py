"""Fila de tradução: livros em sequência, cópia do EPUB final para o destino."""

from pathlib import Path

from web.fila import Fila, ItemFila, nome_traduzido, rodar_fila
from web.jobs import JobManager

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "test_input.epub"


def test_fila_traduz_em_sequencia_e_copia(tmp_path: Path) -> None:
    livros = []
    for n in (1, 2):
        caminho = tmp_path / f"Livro Volume-{n}.epub"
        caminho.write_bytes(FIXTURE.read_bytes())
        livros.append(str(caminho))
    destino = tmp_path / "Traduzido"

    fila = Fila(
        itens=[ItemFila(origem=p, nome_saida=nome_traduzido(p)) for p in livros],
        destino=str(destino),
        rodadas_extras=1,
    )
    rodar_fila(fila, JobManager(tmp_path / "jobs"), {"mock": True})

    assert fila.estado == "concluida"
    assert [i.estado for i in fila.itens] == ["concluido", "concluido"]
    assert sorted(p.name for p in destino.iterdir()) == [
        "Livro Volume-1 (traduzido).epub",
        "Livro Volume-2 (traduzido).epub",
    ]
