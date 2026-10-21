/**
 * Navegação de capítulos do EPUB.
 *
 * A lista vem do back-end (`GET /api/jobs/{id}/chapters`) **na ordem do
 * spine** — esta ordem é respeitada aqui, sem reordenar nada. Cada item
 * mostra o título do capítulo (vem do NCX) e quantos blocos de texto ele
 * tem; o capítulo ativo fica destacado.
 */
import type { ReactElement } from 'react'

import type { Chapter } from '../types'

/** Props da navegação de capítulos. */
export interface ChapterNavProps {
  /** Capítulos na ordem devolvida pelo back-end (ordem do spine). */
  capitulos: Chapter[]
  /** Caminho (`path`) do capítulo ativo, ou `null` se nenhum. */
  ativo: string | null
  /** Chamado com o caminho do capítulo escolhido. */
  onSelecionar: (path: string) => void
  /** `true` enquanto a lista está sendo carregada. */
  carregando?: boolean
}

/**
 * Navegação de capítulos: lista rolável com o capítulo ativo destacado.
 */
export default function ChapterNav({
  capitulos,
  ativo,
  onSelecionar,
  carregando = false,
}: ChapterNavProps): ReactElement {
  if (carregando && capitulos.length === 0) {
    return (
      <section className="rounded-lg border border-borda bg-surface p-4">
        <h2 className="mb-3 text-sm font-semibold text-texto">Capítulos</h2>
        <p className="text-sm text-suave">Carregando os capítulos…</p>
      </section>
    )
  }

  if (capitulos.length === 0) {
    return (
      <section className="rounded-lg border border-borda bg-surface p-4">
        <h2 className="mb-3 text-sm font-semibold text-texto">Capítulos</h2>
        <p className="text-sm text-suave">Nenhum capítulo encontrado neste EPUB.</p>
      </section>
    )
  }

  return (
    <section className="rounded-lg border border-borda bg-surface p-4">
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-texto">Capítulos</h2>
        <span className="text-xs text-suave">
          {capitulos.length} {capitulos.length === 1 ? 'capítulo' : 'capítulos'}
        </span>
      </div>

      <ul className="max-h-[60vh] space-y-1 overflow-y-auto pr-1">
        {capitulos.map((capitulo) => {
          const selecionado = capitulo.path === ativo
          return (
            <li key={capitulo.path}>
              <button
                type="button"
                onClick={() => onSelecionar(capitulo.path)}
                aria-current={selecionado ? 'true' : undefined}
                className={[
                  'w-full rounded-md border px-3 py-2 text-left transition-colors',
                  selecionado
                    ? 'border-acento bg-acento/10 text-texto'
                    : 'border-transparent text-suave hover:border-borda hover:bg-fundo hover:text-texto',
                ].join(' ')}
              >
                <span className="flex items-baseline gap-2">
                  <span className="text-[11px] tabular-nums text-suave/80">
                    {capitulo.index + 1}.
                  </span>
                  <span
                    className={`min-w-0 flex-1 truncate text-sm ${selecionado ? 'font-medium' : ''}`}
                    title={capitulo.title}
                  >
                    {capitulo.title}
                  </span>
                </span>
                <span className="mt-0.5 block pl-6 text-[11px] text-suave/80">
                  {capitulo.blocks} {capitulo.blocks === 1 ? 'bloco' : 'blocos'}
                </span>
              </button>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
