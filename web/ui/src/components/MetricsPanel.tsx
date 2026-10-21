/**
 * Painel de métricas da tradução (TASK-026).
 *
 * Quatro contadores separados — **Traduzidos**, **Cache**, **Falhas** e
 * **Descartados** — com o número grande e o rótulo. Cada contador tem um
 * `title` (tooltip) com o `detail` do **último evento** daquele status: é o
 * que o usuário precisa para ver na hora que algo foi recusado e por quê
 * (ex.: "marcador [[3]] perdido na resposta do modelo").
 *
 * As cores seguem o tema: verde (`#34d399`) para traduzidos, ciano
 * (`#38bdf8`) para o cache, vermelho (`#f87171`) para falhas e amarelo
 * (`#fbbf24`) para descartados.
 */
import type { ReactElement } from 'react'

import type { BlockStatus } from '../types'

/** Props do painel de métricas. */
export interface MetricsPanelProps {
  /** Blocos traduzidos pelo modelo. */
  translated: number
  /** Blocos reusados do cache. */
  cached: number
  /** Blocos que falharam. */
  failed: number
  /** Blocos descartados (resposta do modelo recusada). */
  discarded: number
  /** `detail` do último evento de cada status (aparece no tooltip). */
  detalhes?: Partial<Record<BlockStatus, string>>
}

/** Um contador do painel. */
interface Contador {
  /** Status ao qual o contador pertence (chave de `detalhes`). */
  status: BlockStatus
  /** Rótulo visível. */
  rotulo: string
  /** Valor do contador. */
  valor: number
  /** Classe Tailwind da cor do número. */
  cor: string
  /** Classe Tailwind da cor do rótulo. */
  corRotulo: string
}

/**
 * Painel de métricas: traduções, cache, falhas e descartados.
 */
export default function MetricsPanel({
  translated,
  cached,
  failed,
  discarded,
  detalhes = {},
}: MetricsPanelProps): ReactElement {
  const contadores: Contador[] = [
    {
      status: 'traduzido',
      rotulo: 'Traduzidos',
      valor: translated,
      cor: 'text-sucesso',
      corRotulo: 'text-sucesso/80',
    },
    {
      status: 'cache',
      rotulo: 'Cache',
      valor: cached,
      cor: 'text-acento',
      corRotulo: 'text-acento/80',
    },
    {
      status: 'falhou',
      rotulo: 'Falhas',
      valor: failed,
      cor: 'text-erro',
      corRotulo: 'text-erro/80',
    },
    {
      status: 'descartado',
      rotulo: 'Descartados',
      valor: discarded,
      cor: 'text-aviso',
      corRotulo: 'text-aviso/80',
    },
  ]

  return (
    <section className="rounded-lg border border-borda bg-surface p-4">
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-texto">Blocos</h2>
        <span className="text-xs text-suave">
          {translated + cached + failed + discarded} no total
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {contadores.map((contador) => {
          const detalhe = detalhes[contador.status]
          const temDetalhe = typeof detalhe === 'string' && detalhe.length > 0
          return (
            <div
              key={contador.status}
              title={temDetalhe ? detalhe : `Nenhum ${contador.rotulo.toLowerCase()} ainda`}
              className="rounded-md border border-borda/70 bg-fundo px-3 py-2"
            >
              <div
                className={`text-2xl font-semibold leading-none tabular-nums ${contador.cor}`}
              >
                {contador.valor}
              </div>
              <div className={`mt-1 text-xs ${contador.corRotulo}`}>{contador.rotulo}</div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
