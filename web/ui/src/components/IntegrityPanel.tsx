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
  const titulo = `${itens.length - falhas}/${itens.length} verificações OK`

  return (
    <section className="px-5 py-3">
      {carregando && itens.length === 0 && (
        <p className="text-suave">Verificando a integridade do EPUB…</p>
      )}

      {!carregando && itens.length === 0 && (
        <p className="text-suave">A verificação de integridade roda no fim da tradução.</p>
      )}

      {itens.length > 0 && (
        <>
          <p className={`mb-2 ${falhas > 0 ? 'text-erro' : 'text-sucesso'}`}>
            {titulo} — {falhas > 0 ? `${falhas} ${falhas === 1 ? 'falha' : 'falhas'}` : 'tudo certo'}
          </p>
          <ul className="grid grid-cols-1 gap-x-8 xl:grid-cols-2">
            {itens.map((item, indice) => (
              <li key={`${item.item}-${indice}`} className="flex gap-3 border-b border-borda/60 py-1.5">
                <span
                  className={`num w-10 shrink-0 font-mono text-[11px] ${item.ok ? 'text-sucesso' : 'text-erro'}`}
                >
                  {item.ok ? 'OK' : 'FALHA'}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="text-texto">{item.item}</span>
                  {item.detail && item.detail.length > 0 && (
                    <span className="block truncate font-mono text-[11px] text-suave" title={item.detail}>
                      {item.detail}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
