/**
 * Visualizador do log do job.
 *
 * Botão "Ver log" que abre um modal (overlay escuro) com o conteúdo do log
 * num `<pre>` monoespaçado. O log tem **auto-scroll**: quando o conteúdo
 * muda, a rolagem vai para o fim — assim o usuário acompanha a tradução sem
 * precisar arrastar a barra.
 *
 * O componente cuida da **apresentação**; quem busca o log é o App, que
 * passa a função `carregar` (ex.: `() => buscarLog(jobId)` de `src/api.ts`).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'

import type { LogResponse } from '../types'

/** Props do visualizador de log. */
export interface LogViewerProps {
  /** Busca o log do job (devolve `{ text }`). */
  carregar: () => Promise<LogResponse>
  /** Abre o modal de fora do componente (opcional). */
  abertoExterno?: boolean
  /** Rótulo do botão que abre o modal. */
  rotulo?: string
}

/**
 * Modal com o log do job e auto-scroll.
 */
export default function LogViewer({
  carregar,
  abertoExterno = false,
  rotulo = 'Ver log',
}: LogViewerProps): ReactElement {
  const [aberto, setAberto] = useState(false)
  const [texto, setTexto] = useState('')
  const [carregando, setCarregando] = useState(false)
  const [erro, setErro] = useState<string | null>(null)

  /** Área rolável do log (vai para o fim a cada mudança de conteúdo). */
  const refArea = useRef<HTMLDivElement | null>(null)
  /** Último valor de `abertoExterno` (para detectar a virada para `true`). */
  const externoAnterior = useRef(abertoExterno)

  /** Busca o log e mostra o modal. */
  const abrir = useCallback(async (): Promise<void> => {
    setAberto(true)
    setCarregando(true)
    setErro(null)
    try {
      const resposta = await carregar()
      setTexto(resposta.text ?? '')
    } catch (falha) {
      setTexto('')
      setErro(
        falha instanceof Error
          ? falha.message
          : 'Não foi possível carregar o log do job.',
      )
    } finally {
      setCarregando(false)
    }
  }, [carregar])

  /** Abre o modal quando o pai pede (virada de `false` para `true`). */
  useEffect(() => {
    if (abertoExterno && !externoAnterior.current) {
      void abrir()
    }
    externoAnterior.current = abertoExterno
  }, [abertoExterno, abrir])

  /** Auto-scroll: sempre que o conteúdo muda, vai para o fim. */
  useEffect(() => {
    if (!aberto) {
      return
    }
    const area = refArea.current
    if (area) {
      area.scrollTop = area.scrollHeight
    }
  }, [texto, aberto, carregando])

  /** Esc fecha o modal. */
  useEffect(() => {
    if (!aberto) {
      return
    }
    function aoTeclar(evento: KeyboardEvent): void {
      if (evento.key === 'Escape') {
        setAberto(false)
      }
    }
    window.addEventListener('keydown', aoTeclar)
    return () => {
      window.removeEventListener('keydown', aoTeclar)
    }
  }, [aberto])

  return (
    <>
      <button
        type="button"
        onClick={() => {
          void abrir()
        }}
        className="rounded-md border border-borda bg-surface px-3 py-1.5 text-xs font-medium text-texto transition-colors hover:border-acento hover:text-acento"
        title="Abrir o log do job"
      >
        {rotulo}
      </button>

      {aberto && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Log do job"
          onClick={(evento) => {
            // Clique no overlay (fora da caixa) fecha o modal.
            if (evento.target === evento.currentTarget) {
              setAberto(false)
            }
          }}
        >
          <div className="flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-lg border border-borda bg-surface shadow-2xl">
            <header className="flex items-center justify-between gap-3 border-b border-borda px-4 py-3">
              <h2 className="text-sm font-semibold text-texto">Log do job</h2>
              <button
                type="button"
                onClick={() => setAberto(false)}
                className="rounded-md border border-borda px-2.5 py-1 text-xs text-suave transition-colors hover:border-acento hover:text-acento"
              >
                Fechar
              </button>
            </header>

            <div ref={refArea} className="min-h-0 flex-1 overflow-auto bg-fundo p-4">
              {carregando && (
                <p className="mb-2 text-xs text-suave">Carregando o log…</p>
              )}
              {erro && <p className="mb-2 text-xs text-erro">{erro}</p>}
              <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-texto">
                {texto && texto.length > 0
                  ? texto
                  : carregando || erro
                    ? ''
                    : 'O log está vazio.'}
              </pre>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
