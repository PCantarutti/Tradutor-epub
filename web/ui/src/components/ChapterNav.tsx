/**
 * Lista de capítulos do livro, na ordem do spine (como vem do back-end).
 *
 * Cada linha mostra o título (do NCX), uma régua fina com a fração já
 * traduzida e, quando houver, quantos blocos estão sem tradução. O capítulo
 * em tradução agora ganha o ponto violeta.
 */
import { useEffect, useRef } from 'react'
import type { ReactElement } from 'react'

/** Um capítulo na lista, com a contagem ao vivo. */
export interface ItemCapitulo {
  path: string
  index: number
  title: string
  blocos: number
  traduzidos: number
  /** Blocos sem tradução (pendentes, falhas, descartes). */
  semTraducao: number
}

export interface ChapterNavProps {
  itens: ItemCapitulo[]
  ativo: string | null
  /** Capítulo em tradução agora (ou `null`). */
  emTraducao: string | null
  /** `true` enquanto a tradução roda (pendente ≠ falha). */
  rodando: boolean
  /** Job concluído: só aí o que falta é "sem tradução" (vermelho). */
  concluido: boolean
  onSelecionar: (path: string) => void
  carregando?: boolean
}

export default function ChapterNav({
  itens,
  ativo,
  emTraducao,
  rodando,
  concluido,
  onSelecionar,
  carregando = false,
}: ChapterNavProps): ReactElement {
  const refLista = useRef<HTMLUListElement | null>(null)

  // Capítulo ativo trocado (ex.: "Seguir tradução"): mantém-no à vista.
  useEffect(() => {
    refLista.current
      ?.querySelector('[aria-current="true"]')
      ?.scrollIntoView({ block: 'nearest' })
  }, [ativo])

  if (itens.length === 0) {
    return (
      <p className="px-4 py-3 text-suave">
        {carregando ? 'Carregando os capítulos…' : 'Nenhum capítulo encontrado neste EPUB.'}
      </p>
    )
  }

  return (
    <ul ref={refLista} className="py-1">
      {itens.map((item) => {
        const selecionado = item.path === ativo
        const fracao = item.blocos > 0 ? item.traduzidos / item.blocos : 0
        const completo = item.blocos > 0 && item.semTraducao === 0
        return (
          <li key={item.path}>
            <button
              type="button"
              onClick={() => onSelecionar(item.path)}
              aria-current={selecionado ? 'true' : undefined}
              className={[
                'group block w-full px-4 py-2 text-left transition-colors',
                selecionado ? 'bg-elevado' : 'hover:bg-elevado/60',
              ].join(' ')}
            >
              <span className="flex items-baseline gap-2">
                <span className="num w-5 shrink-0 text-right text-[11px] text-suave">
                  {item.index + 1}
                </span>
                <span
                  className={`min-w-0 flex-1 truncate ${selecionado ? 'text-texto' : 'text-texto/80 group-hover:text-texto'}`}
                  title={item.title}
                >
                  {item.title}
                </span>
                {emTraducao === item.path && (
                  <span
                    className="h-1.5 w-1.5 shrink-0 animate-pulso rounded-full bg-atual motion-reduce:animate-none"
                    title="Em tradução agora"
                  />
                )}
              </span>
              <span className="mt-1.5 flex items-center gap-2 pl-7">
                <span className="relative h-[2px] flex-1 bg-borda">
                  <span
                    className={`absolute inset-y-0 left-0 ${completo ? 'bg-sucesso' : 'bg-sucesso/70'}`}
                    style={{ width: `${fracao * 100}%` }}
                  />
                </span>
                <span className="num w-[8.5rem] shrink-0 text-right text-[11px]">
                  {item.blocos === 0 ? (
                    <span className="text-suave">sem texto</span>
                  ) : item.semTraducao > 0 && !rodando ? (
                    <span className={concluido ? 'text-erro' : 'text-texto/85'}>
                      {item.semTraducao} {concluido ? 'sem tradução' : 'não traduzidos'}
                    </span>
                  ) : (
                    <span className={completo ? 'text-sucesso/90' : 'text-suave'}>
                      {item.traduzidos}/{item.blocos}
                    </span>
                  )}
                </span>
              </span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}
