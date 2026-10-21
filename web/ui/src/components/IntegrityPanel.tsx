/**
 * Painel de integridade do EPUB gerado.
 *
 * Mostra o resultado da verificação feita pelo tradutor (`verificar_epub`):
 * cada item vem como `{item, ok, detail}`. Itens OK ficam em verde
 * (`#34d399`), falhas em vermelho (`#f87171`), e o `detail` vai logo abaixo,
 * em texto pequeno e suave.
 */
import type { ReactElement } from 'react'

import type { IntegrityItem } from '../types'

/** Props do painel de integridade. */
export interface IntegrityPanelProps {
  /** Itens da verificação de integridade. */
  itens: IntegrityItem[]
  /** `true` enquanto a verificação está sendo carregada. */
  carregando?: boolean
}

/**
 * Painel de integridade: lista de verificações OK/Falha do EPUB gerado.
 */
export default function IntegrityPanel({
  itens,
  carregando = false,
}: IntegrityPanelProps): ReactElement {
  const falhas = itens.filter((item) => !item.ok).length
  const titulo = itens.length > 0 ? `Integridade (${itens.length - falhas}/${itens.length} OK)` : 'Integridade'

  return (
    <section className="rounded-lg border border-borda bg-surface p-4">
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-texto">{titulo}</h2>
        {itens.length > 0 && (
          <span className={`text-xs ${falhas > 0 ? 'text-erro' : 'text-sucesso'}`}>
            {falhas > 0 ? `${falhas} ${falhas === 1 ? 'falha' : 'falhas'}` : 'todas OK'}
          </span>
        )}
      </div>

      {carregando && itens.length === 0 && (
        <p className="text-sm text-suave">Verificando a integridade do EPUB…</p>
      )}

      {!carregando && itens.length === 0 && (
        <p className="text-sm text-suave">
          A verificação de integridade roda no final da tradução.
        </p>
      )}

      {itens.length > 0 && (
        <ul className="space-y-2">
          {itens.map((item, indice) => (
            <li
              key={`${item.item}-${indice}`}
              className="rounded-md border border-borda/70 bg-fundo px-3 py-2"
            >
              <div className="flex items-start gap-2">
                <span
                  className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                    item.ok ? 'bg-sucesso/15 text-sucesso' : 'bg-erro/15 text-erro'
                  }`}
                >
                  {item.ok ? 'OK' : 'Falha'}
                </span>
                <span className="min-w-0 flex-1 text-sm text-texto">{item.item}</span>
              </div>
              {item.detail && item.detail.length > 0 && (
                <p className="mt-1 pl-1 text-xs leading-relaxed text-suave">{item.detail}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
