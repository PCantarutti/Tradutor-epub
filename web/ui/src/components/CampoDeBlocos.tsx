/**
 * Campo de blocos: o livro inteiro como um campo de barras finas, um traço
 * por bloco, em faixas que quebram na largura do painel.
 *
 * - Cor = estado do bloco (pendente, cache, traduzido agora, falha, descarte).
 * - O bloco em tradução pulsa em violeta; o que acabou de chegar acende e
 *   assenta no verde em ~1 s.
 * - Cada início de capítulo ganha um traço mais alto (a "régua" do livro).
 * - Passar o mouse mostra capítulo e bloco; clicar abre o capítulo no preview.
 *
 * Desenhado em `<canvas>` (2.700+ barras em DOM pesariam a cada evento). O
 * laço de animação só roda enquanto há algo animando e para por completo com
 * `prefers-reduced-motion`.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent, ReactElement } from 'react'

/** Estado de um bloco no campo. */
export type EstadoBloco = 'pendente' | 'cache' | 'traduzido' | 'falhou' | 'descartado'

/** Um capítulo no campo: estado de cada bloco, na ordem do documento. */
export interface CapituloDoCampo {
  path: string
  title: string
  blocos: EstadoBloco[]
}

export interface CampoDeBlocosProps {
  capitulos: CapituloDoCampo[]
  /** Muda a cada atualização dos estados (força o redesenho). */
  versao: number
  /** `true` enquanto a tradução roda (pendente = "por traduzir"). */
  rodando: boolean
  /** Job concluído: o que sobrou pendente é "sem tradução" (vermelho). */
  concluido: boolean
  /** Bloco sendo traduzido agora (1-based) ou `null`. */
  atual: { path: string; indice: number } | null
  /** Momento (performance.now) em que cada bloco chegou: `path#indice`. */
  chegadas: Map<string, number>
  /** Clique num bloco. */
  onEscolher: (path: string, indice: number) => void
}

const LARGURA_BARRA = 2
const PASSO = 3
/** Altura total desejada do campo: as faixas encolhem para caber nela. */
const ALTURA_ALVO = 176
const ALTURA_FAIXA_MAX = 40
const ALTURA_FAIXA_MIN = 16
const ESPACO_FAIXA = 6
const MARCA_CAPITULO = 5
const DURACAO_CHEGADA = 1100

const COR: Record<EstadoBloco, string> = {
  pendente: '#2b3139',
  cache: '#566c88',
  traduzido: '#7cc4a4',
  falhou: '#e57373',
  descartado: '#e5a34a',
}
const COR_ATUAL = '#c9a6f2'
/** Pendente num job interrompido (cancelado/erro): não tentado, não falha. */
const COR_NAO_TENTADO = '#333a44'
const COR_MARCA = '#8d96a2'

/** Posição de cada bloco no campo (calculada uma vez por layout). */
interface Celula {
  path: string
  title: string
  indice: number
  x: number
  y: number
  inicioCapitulo: boolean
}

const reduzirMovimento =
  typeof window !== 'undefined' &&
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

/** Mistura duas cores hex (t = 0 → a, 1 → b). */
function misturar(a: string, b: string, t: number): string {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16))
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16))
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * t))
  return `rgb(${c[0]},${c[1]},${c[2]})`
}

export default function CampoDeBlocos({
  capitulos,
  versao,
  rodando,
  concluido,
  atual,
  chegadas,
  onEscolher,
}: CampoDeBlocosProps): ReactElement {
  const refCaixa = useRef<HTMLDivElement | null>(null)
  const refCanvas = useRef<HTMLCanvasElement | null>(null)
  const [largura, setLargura] = useState(0)
  const [dica, setDica] = useState<{ x: number; y: number; texto: string } | null>(null)
  const celulas = useRef<Celula[]>([])

  // Largura do painel (o campo quebra em faixas conforme ela).
  useEffect(() => {
    const caixa = refCaixa.current
    if (!caixa) {
      return
    }
    const observador = new ResizeObserver(([entrada]) => {
      setLargura(Math.floor(entrada.contentRect.width))
    })
    observador.observe(caixa)
    return () => observador.disconnect()
  }, [])

  const porFaixa = Math.max(1, Math.floor((largura + PASSO - LARGURA_BARRA) / PASSO))
  const total = capitulos.reduce((soma, c) => soma + c.blocos.length, 0)
  const faixas = Math.max(1, Math.ceil(total / porFaixa))
  const ALTURA_FAIXA = Math.round(
    Math.min(ALTURA_FAIXA_MAX, Math.max(ALTURA_FAIXA_MIN, (ALTURA_ALVO - (faixas - 1) * ESPACO_FAIXA) / faixas)),
  )
  const altura = faixas * ALTURA_FAIXA + (faixas - 1) * ESPACO_FAIXA + MARCA_CAPITULO

  // Layout das células: muda só quando a estrutura do livro ou a largura muda.
  useEffect(() => {
    const lista: Celula[] = []
    let n = 0
    for (const capitulo of capitulos) {
      capitulo.blocos.forEach((_, i) => {
        const faixa = Math.floor(n / porFaixa)
        lista.push({
          path: capitulo.path,
          title: capitulo.title,
          indice: i + 1,
          x: (n % porFaixa) * PASSO,
          y: MARCA_CAPITULO + faixa * (ALTURA_FAIXA + ESPACO_FAIXA),
          inicioCapitulo: i === 0,
        })
        n += 1
      })
    }
    celulas.current = lista
  }, [capitulos, porFaixa, ALTURA_FAIXA])

  const desenhar = useCallback((): boolean => {
    const canvas = refCanvas.current
    if (!canvas || largura === 0) {
      return false
    }
    const dpr = window.devicePixelRatio || 1
    if (canvas.width !== Math.round(largura * dpr) || canvas.height !== Math.round(altura * dpr)) {
      canvas.width = Math.round(largura * dpr)
      canvas.height = Math.round(altura * dpr)
    }
    const ctx = canvas.getContext('2d')
    if (!ctx) {
      return false
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, largura, altura)

    const agora = performance.now()
    let animando = false
    const estados = new Map(capitulos.map((c) => [c.path, c.blocos]))

    for (const celula of celulas.current) {
      const estado = estados.get(celula.path)?.[celula.indice - 1] ?? 'pendente'
      let cor =
        estado === 'pendente' && !rodando
          ? concluido
            ? COR.falhou
            : COR_NAO_TENTADO
          : COR[estado]
      const chave = `${celula.path}#${celula.indice}`
      const chegou = chegadas.get(chave)
      if (chegou !== undefined && !reduzirMovimento) {
        const t = (agora - chegou) / DURACAO_CHEGADA
        if (t < 1) {
          cor = misturar('#ffffff', COR[estado] ?? cor, Math.min(1, t * t))
          animando = true
        }
      }
      if (atual && atual.path === celula.path && atual.indice === celula.indice) {
        const fase = reduzirMovimento ? 1 : 0.55 + 0.45 * Math.sin(agora / 180)
        ctx.globalAlpha = fase
        cor = COR_ATUAL
        animando = !reduzirMovimento
      }
      ctx.fillStyle = cor
      ctx.fillRect(celula.x, celula.y, LARGURA_BARRA, ALTURA_FAIXA)
      ctx.globalAlpha = 1
      if (celula.inicioCapitulo) {
        ctx.fillStyle = COR_MARCA
        ctx.fillRect(celula.x, celula.y - MARCA_CAPITULO, 1, MARCA_CAPITULO - 1)
      }
    }
    return animando
  }, [capitulos, largura, altura, rodando, concluido, atual, chegadas, ALTURA_FAIXA])

  // Redesenha a cada mudança; mantém o laço só enquanto há animação.
  useEffect(() => {
    let quadro = 0
    const passo = (): void => {
      if (desenhar()) {
        quadro = requestAnimationFrame(passo)
      }
    }
    passo()
    return () => cancelAnimationFrame(quadro)
  }, [desenhar, versao, porFaixa, ALTURA_FAIXA])

  const celulaEm = (evento: ReactMouseEvent<HTMLCanvasElement>): Celula | null => {
    const caixa = evento.currentTarget.getBoundingClientRect()
    const x = evento.clientX - caixa.left
    const y = evento.clientY - caixa.top
    const coluna = Math.floor(x / PASSO)
    const faixa = Math.floor((y - MARCA_CAPITULO) / (ALTURA_FAIXA + ESPACO_FAIXA))
    if (coluna < 0 || coluna >= porFaixa || faixa < 0) {
      return null
    }
    return celulas.current[faixa * porFaixa + coluna] ?? null
  }

  const ROTULO: Record<EstadoBloco, string> = {
    pendente: rodando ? 'por traduzir' : concluido ? 'sem tradução' : 'não traduzido',
    cache: 'traduzido (cache)',
    traduzido: 'traduzido agora',
    falhou: 'falhou',
    descartado: 'descartado',
  }

  return (
    <div ref={refCaixa} className="relative w-full">
      <canvas
        ref={refCanvas}
        style={{ width: largura, height: altura }}
        className="block cursor-crosshair"
        role="img"
        aria-label={`Campo com ${total} blocos do livro; cada traço é um bloco colorido pelo estado`}
        onMouseMove={(evento) => {
          const celula = celulaEm(evento)
          if (!celula) {
            setDica(null)
            return
          }
          const estado =
            capitulos.find((c) => c.path === celula.path)?.blocos[celula.indice - 1] ?? 'pendente'
          setDica({
            x: celula.x,
            y: celula.y,
            texto: `${celula.title} · bloco ${celula.indice} · ${ROTULO[estado]}`,
          })
        }}
        onMouseLeave={() => setDica(null)}
        onClick={(evento) => {
          const celula = celulaEm(evento)
          if (celula) {
            onEscolher(celula.path, celula.indice)
          }
        }}
      />
      {dica && (
        <div
          className="pointer-events-none absolute z-10 -translate-y-full whitespace-nowrap rounded border border-borda bg-elevado px-2 py-1 font-mono text-[11px] text-texto shadow-[0_4px_12px_rgba(0,0,0,0.45)]"
          style={{
            left: Math.min(Math.max(dica.x - 8, 0), Math.max(largura - 320, 0)),
            top: dica.y - 6,
          }}
        >
          {dica.texto}
        </div>
      )}
    </div>
  )
}
