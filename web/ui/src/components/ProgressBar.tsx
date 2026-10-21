/**
 * Barra de progresso da tradução (TASK-026).
 *
 * Mostra o quanto já foi traduzido, o percentual, o **ETA** e o tempo
 * decorrido, além dos botões Pausar / Retomar / Cancelar.
 *
 * Dois cuidados que vêm da realidade do tradutor:
 *
 * 1. **Um lote leva ~89 s no LM Studio.** A barra parece "travada" durante o
 *    lote e só anda quando ele termina — isso é normal, e o texto de ETA
 *    ajuda o usuário a entender que o trabalho continua.
 * 2. **A pausa só tem efeito ENTRE lotes.** Depois de pedir a pausa, o job
 *    ainda termina o lote em andamento (até ~90 s). Por isso existe a prop
 *    `pausando`: enquanto ela for verdadeira, a interface mostra "pausando…"
 *    em vez de fingir que a pausa já valeu.
 */
import type { ReactElement } from 'react'

import type { JobState } from '../types'

/** Props da barra de progresso. */
export interface ProgressBarProps {
  /** Percentual concluído (0 a 100). */
  percent: number
  /** Estimativa de término em segundos; `null` enquanto não dá para estimar. */
  eta_s: number | null
  /** Segundos decorridos desde o início da execução. */
  elapsed_s: number
  /** Estado atual do job. */
  estado: JobState
  /** `true` enquanto a pausa foi pedida mas ainda não valeu (fim do lote). */
  pausando: boolean
  /** Pede a pausa do job. */
  onPausar?: () => void
  /** Retoma o job pausado. */
  onRetomar?: () => void
  /** Cancela o job. */
  onCancelar?: () => void
}

/** Rótulo de cada estado do job (pt-BR). */
const ROTULO_ESTADO: Record<JobState, string> = {
  running: 'Traduzindo',
  paused: 'Pausado',
  done: 'Concluído',
  error: 'Erro',
  cancelled: 'Cancelado',
}

/** Classe Tailwind da cor do preenchimento conforme o estado. */
const COR_ESTADO: Record<JobState, string> = {
  running: 'bg-acento',
  paused: 'bg-aviso',
  done: 'bg-sucesso',
  error: 'bg-erro',
  cancelled: 'bg-suave',
}

/**
 * Formata o tempo decorrido no formato `hh:mm:ss` (ex.: `00:12:35`).
 *
 * @param segundos Segundos decorridos (fracionários são arredondados).
 */
export function formatarTempo(segundos: number): string {
  const total = Math.max(0, Math.floor(segundos))
  const horas = Math.floor(total / 3600)
  const minutos = Math.floor((total % 3600) / 60)
  const rest = total % 60
  return [horas, minutos, rest].map((parte) => String(parte).padStart(2, '0')).join(':')
}

/**
 * Formata o ETA em linguagem natural (ex.: `faltam ~1h 12min`).
 *
 * @param segundos Segundos restantes; `null` devolve `null` (a interface
 *   mostra "estimando…" nesse caso).
 */
export function formatarEta(segundos: number | null): string | null {
  if (segundos === null || !Number.isFinite(segundos)) {
    return null
  }

  const total = Math.max(0, Math.round(segundos))

  if (total < 60) {
    return `faltam ~${total} s`
  }

  const horas = Math.floor(total / 3600)
  let minutos = Math.round((total % 3600) / 60)
  let horasAjustadas = horas
  if (minutos === 60) {
    minutos = 0
    horasAjustadas += 1
  }

  if (horasAjustadas > 0) {
    return minutos > 0
      ? `faltam ~${horasAjustadas}h ${minutos}min`
      : `faltam ~${horasAjustadas}h`
  }

  return `faltam ~${minutos} min`
}

/**
 * Barra de progresso com percentual, ETA, tempo decorrido e controles.
 */
export default function ProgressBar({
  percent,
  eta_s,
  elapsed_s,
  estado,
  pausando,
  onPausar,
  onRetomar,
  onCancelar,
}: ProgressBarProps): ReactElement {
  /** Percentual limitado a 0..100 (evita barra "estourada"). */
  const percentual = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0))
  /** ETA formatado (ou `null` enquanto não há estimativa). */
  const eta = formatarEta(eta_s)
  /** O job ainda está em andamento (pode pausar/cancelar)? */
  const ativo = estado === 'running' || estado === 'paused' || pausando
  /** Mostra o botão de retomar (pausado ou pausa em andamento). */
  const retomar = estado === 'paused' || pausando

  return (
    <section className="rounded-lg border border-borda bg-surface p-4">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex items-baseline gap-2">
          <h2 className="text-sm font-semibold text-texto">Progresso</h2>
          <span className="text-xs uppercase tracking-wide text-suave">
            {ROTULO_ESTADO[estado]}
          </span>
        </div>
        <div className="flex items-baseline gap-4 text-xs text-suave">
          <span className="tabular-nums">
            {percentual.toFixed(1).replace('.', ',')}%
          </span>
          <span>{eta ?? 'estimando…'}</span>
          <span className="tabular-nums" title="Tempo decorrido">
            {formatarTempo(elapsed_s)}
          </span>
        </div>
      </div>

      {/* Barra: fundo escuro + preenchimento na cor do estado. */}
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-fundo"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(percentual)}
        aria-label="Progresso da tradução"
      >
        <div
          className={`h-full rounded-full transition-[width] duration-300 ${COR_ESTADO[estado]}`}
          style={{ width: `${percentual}%` }}
        />
      </div>

      {/* Controles */}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {!retomar && (
          <button
            type="button"
            onClick={onPausar}
            disabled={estado !== 'running' || !onPausar}
            className="rounded-md border border-borda bg-fundo px-3 py-1.5 text-xs font-medium text-texto transition-colors hover:border-aviso hover:text-aviso disabled:cursor-not-allowed disabled:opacity-50"
            title="A pausa só tem efeito entre lotes (o lote atual ainda termina)"
          >
            Pausar
          </button>
        )}

        {retomar && (
          <button
            type="button"
            onClick={onRetomar}
            disabled={pausando || !onRetomar}
            className="rounded-md border border-borda bg-fundo px-3 py-1.5 text-xs font-medium text-texto transition-colors hover:border-acento hover:text-acento disabled:cursor-not-allowed disabled:opacity-50"
            title={
              pausando
                ? 'A pausa foi pedida, mas o lote atual ainda está terminando'
                : 'Retomar a tradução a partir do cache'
            }
          >
            Retomar
            {pausando && <span className="ml-1 text-aviso">pausando…</span>}
          </button>
        )}

        {ativo && (
          <button
            type="button"
            onClick={onCancelar}
            disabled={!onCancelar}
            className="rounded-md border border-borda bg-fundo px-3 py-1.5 text-xs font-medium text-texto transition-colors hover:border-erro hover:text-erro disabled:cursor-not-allowed disabled:opacity-50"
            title="Cancelar a tradução e manter o que já foi salvo no cache"
          >
            Cancelar
          </button>
        )}
      </div>
    </section>
  )
}
