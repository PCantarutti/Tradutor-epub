/**
 * Log do job em tempo real.
 *
 * Lê o log em incrementos (`GET /log?offset=`): só as linhas novas chegam a
 * cada 1,5 s enquanto a tradução roda. Rola sozinho para o fim apenas
 * quando o usuário já está no fim — subir para ler uma linha antiga não é
 * interrompido.
 */
import { useEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'

import { buscarLog } from '../api'

/** Máximo de linhas mantidas na tela (as mais antigas saem). */
const MAX_LINHAS = 3000
const INTERVALO_MS = 1500

interface Linha {
  id: number
  hora: string
  nivel: 'INFO' | 'WARNING' | 'ERROR' | 'DEBUG' | ''
  texto: string
}

const RE_LINHA = /^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] [\w.]+: (.*)$/

let proximoId = 0

function analisar(bruta: string): Linha {
  const m = RE_LINHA.exec(bruta)
  proximoId += 1
  if (!m) {
    return { id: proximoId, hora: '', nivel: '', texto: bruta }
  }
  const nivel = (['INFO', 'WARNING', 'ERROR', 'DEBUG'].includes(m[2]) ? m[2] : '') as Linha['nivel']
  return { id: proximoId, hora: m[1], nivel, texto: m[3] }
}

const COR_NIVEL: Record<Linha['nivel'], string> = {
  INFO: 'text-texto/80',
  WARNING: 'text-aviso',
  ERROR: 'text-erro',
  DEBUG: 'text-suave',
  '': 'text-texto/80',
}

export default function LogAoVivo({
  jobId,
  rodando,
  soProblemas,
}: {
  jobId: string
  rodando: boolean
  soProblemas: boolean
}): ReactElement {
  const [linhas, setLinhas] = useState<Linha[]>([])
  const [erro, setErro] = useState<string | null>(null)
  const offset = useRef(0)
  const resto = useRef('')
  const refLista = useRef<HTMLDivElement | null>(null)
  const noFim = useRef(true)

  // Job novo: recomeça do zero.
  useEffect(() => {
    offset.current = 0
    resto.current = ''
    setLinhas([])
  }, [jobId])

  useEffect(() => {
    let ativo = true
    // Leitura em andamento: a próxima só começa quando esta termina
    // (senão duas leituras partem do mesmo offset e duplicam linhas).
    let lendo = false
    const ler = async (): Promise<void> => {
      if (lendo) {
        return
      }
      lendo = true
      try {
        const resposta = await buscarLog(jobId, offset.current)
        if (!ativo) {
          return
        }
        setErro(null)
        if (resposta.offset < offset.current) {
          // Log recriado: recomeça.
          resto.current = ''
          setLinhas([])
        }
        offset.current = resposta.offset
        if (!resposta.text) {
          return
        }
        const partes = (resto.current + resposta.text).split(/\r?\n/)
        resto.current = partes.pop() ?? ''
        const novas = partes.filter((p) => p.length > 0).map(analisar)
        if (novas.length > 0) {
          setLinhas((atuais) => [...atuais, ...novas].slice(-MAX_LINHAS))
        }
      } catch {
        if (ativo) {
          setErro('Não foi possível ler o log agora — tentando de novo.')
        }
      } finally {
        lendo = false
      }
    }
    void ler()
    if (!rodando) {
      return () => {
        ativo = false
      }
    }
    const timer = window.setInterval(() => void ler(), INTERVALO_MS)
    return () => {
      ativo = false
      window.clearInterval(timer)
    }
  }, [jobId, rodando])

  // Segue o fim só se o usuário já estava lá.
  useEffect(() => {
    const lista = refLista.current
    if (lista && noFim.current) {
      lista.scrollTop = lista.scrollHeight
    }
  }, [linhas, soProblemas])

  const visiveis = soProblemas
    ? linhas.filter((l) => l.nivel === 'ERROR' || l.nivel === 'WARNING')
    : linhas

  return (
    <div
      ref={refLista}
      onScroll={(evento) => {
        const el = evento.currentTarget
        noFim.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
      }}
      className="h-full overflow-y-auto px-4 py-2 font-mono text-[12px] leading-[1.6]"
      aria-live="off"
    >
      {erro && <p className="text-aviso">{erro}</p>}
      {visiveis.length === 0 && !erro && (
        <p className="text-suave">
          {soProblemas ? 'Nenhum aviso ou erro até agora.' : 'O log aparece aqui assim que a tradução começa.'}
        </p>
      )}
      {visiveis.map((linha) => (
        <div key={linha.id} className="flex gap-3 whitespace-pre-wrap break-words">
          <span className="num shrink-0 text-suave">{linha.hora}</span>
          <span className={`min-w-0 flex-1 ${COR_NIVEL[linha.nivel]}`}>{linha.texto}</span>
        </div>
      ))}
    </div>
  )
}
