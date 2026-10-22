/**
 * Painel lado a lado: original (esquerda) e traduzido (direita).
 *
 * Três cuidados:
 *
 * 1. **Rolagem sincronizada pela razão** — as duas colunas têm alturas
 *    diferentes (a tradução costuma ser mais longa), então copiar o
 *    `scrollTop` bruto desalinha o texto. O sincronismo é feito pela
 *    *razão* de rolagem (`scrollTop / (scrollHeight - clientHeight)`), com
 *    guarda de loop (`sincronizando` + `requestAnimationFrame`).
 * 2. **Destaque de parágrafo** — o back-end marca cada elemento de bloco com
 *    `data-block-index` (1-based). Passar o mouse (ou clicar) num parágrafo
 *    destaca o parágrafo de mesmo índice nos DOIS painéis.
 * 3. **CSS do próprio EPUB** — o CSS do livro (`GET /api/jobs/{id}/style.css`)
 *    é injetado dentro do painel com os seletores **escopados** no container
 *    do preview, para não vazar para a interface do app.
 *
 * 4. **Tempo real sem pular** — o HTML novo de um capítulo já aberto é
 *    aplicado bloco a bloco (`Painel`): a rolagem nunca volta ao topo.
 *
 * O HTML do capítulo chega **sanitizado** pelo back-end (sem `script`, sem
 * `style`, sem handlers `on*` e sem URLs `javascript:`/`data:`), por isso
 * pode ser injetado no DOM.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent, ReactElement, RefObject, UIEvent } from 'react'

import { aplicarEscopo, aplicarRazao, calcularRazao } from '../lib/util'
import type { PreviewSide } from '../types'


/** Classe CSS usada como raiz do escopo do CSS do EPUB. */
const RAIZ_ORIGINAL = 'preview-raiz-original'
const RAIZ_TRADUZIDO = 'preview-raiz-traduzido'

/** Classe (definida em index.css) do parágrafo destacado. */
const CLASSE_DESTACADO = 'bloco-destacado'
/** Classe (definida em index.css) do bloco em tradução. */
const CLASSE_ATUAL = 'bloco-atual'

/** Cor de texto do tema (fundo escuro). */
const COR_TEXTO = '#dde2e8'
/** Cor de texto sobre fundos claros do livro (ex.: caixas de comentário). */
const COR_TEXTO_ESCURO = '#0f1114'

/** Contraste mínimo aceito entre texto e fundo (abaixo disso, corrigimos). */
const CONTRASTE_MINIMO = 0.4

/** Cor RGBA a partir de uma cor CSS computada (`rgb(...)`/`rgba(...)`). */
interface Cor {
  r: number
  g: number
  b: number
  a: number
}

/** Converte uma cor CSS computada em RGB(A); devolve `null` se não der. */
function lerCor(valor: string): Cor | null {
  const partes = valor.match(/rgba?\(([^)]+)\)/)
  if (!partes) {
    return null
  }
  const numeros = partes[1]
    .split(/[\s,/]+/)
    .filter((parte) => parte.length > 0)
    .map(Number)
  if (numeros.length < 3 || numeros.some((numero) => Number.isNaN(numero))) {
    return null
  }
  return {
    r: numeros[0],
    g: numeros[1],
    b: numeros[2],
    a: numeros.length >= 4 ? numeros[3] : 1,
  }
}

/** Luminância aproximada de uma cor (0 = preto, 1 = branco). */
function luminancia(cor: Cor): number {
  return (0.299 * cor.r + 0.587 * cor.g + 0.114 * cor.b) / 255
}

/** Contraste entre duas cores (1 = igual, ~21 = preto sobre branco). */
function contraste(a: Cor, b: Cor): number {
  const la = luminancia(a)
  const lb = luminancia(b)
  const claro = Math.max(la, lb)
  const escuro = Math.min(la, lb)
  return (claro + 0.05) / (escuro + 0.05)
}

/**
 * Fundo efetivo de um elemento: o primeiro fundo opaco da sua árvore.
 *
 * O CSS do livro às vezes pinta o fundo do próprio parágrafo (caixas de
 * comentário, por exemplo); nesse caso o texto precisa ser escuro.
 */
function fundoEfetivo(elemento: HTMLElement, raiz: HTMLElement): Cor {
  let atual: HTMLElement | null = elemento
  while (atual) {
    const estilo = window.getComputedStyle(atual)
    const cor = lerCor(estilo.backgroundColor)
    if (cor && cor.a > 0.5) {
      return cor
    }
    if (atual === raiz) {
      break
    }
    atual = atual.parentElement
  }
  return { r: 15, g: 17, b: 20, a: 1 }
}

/**
 * Deixa o texto do preview legível sobre o tema escuro.
 *
 * O CSS do livro foi feito para papel: ele pinta o texto de **preto**, o que
 * desapareceria no fundo escuro do app. Aqui, para cada elemento do
 * capítulo, o contraste entre a cor do texto e o fundo efetivo é conferido;
 * quando ele é baixo demais, a cor do texto é trocada pela cor do tema (clara
 * sobre fundo escuro, escura sobre fundo claro do livro). Cores com bom
 * contraste (links azuis, por exemplo) são preservadas.
 */
function ajustarLegibilidade(raiz: HTMLElement): void {
  const estiloRaiz = window.getComputedStyle(raiz)
  const corDoPainel = lerCor(estiloRaiz.backgroundColor) ?? {
    r: 15,
    g: 17,
    b: 20,
    a: 1,
  }

  const alvos: HTMLElement[] = [raiz, ...Array.from(raiz.querySelectorAll<HTMLElement>('*'))]

  for (const elemento of alvos) {
    const estilo = window.getComputedStyle(elemento)
    const corTexto = lerCor(estilo.color)
    if (!corTexto) {
      continue
    }
    const fundo =
      elemento === raiz
        ? corDoPainel
        : fundoEfetivo(elemento, raiz)
    if (contraste(corTexto, fundo) >= CONTRASTE_MINIMO) {
      continue
    }
    const nova = luminancia(fundo) > 0.5 ? COR_TEXTO_ESCURO : COR_TEXTO
    elemento.style.setProperty('color', nova, 'important')
  }
}

/** Props do painel interno (um dos lados). */
interface PainelProps {
  /** Capítulo exibido: trocar de capítulo recria o conteúdo do zero. */
  chave: string
  /** HTML sanitizado do capítulo. */
  html: string
  /** CSS do EPUB já escopado nesta raiz. */
  css: string
  /** Rótulo do painel ("Original" / "Tradução"). */
  rotulo: string
  /** Detalhe à direita do rótulo (ex.: "3 pendentes"). */
  detalhe?: string
  /** Lado do preview. */
  lado: PreviewSide
  /** Índice do bloco destacado (1-based) ou `null`. */
  blocoAtivo: number | null
  /** Bloco sendo traduzido agora (1-based) ou `null`. */
  blocoAtual: number | null
  /** Avisa o destaque de parágrafo. */
  aoDestacar: (indice: number | null) => void
  /** Avisa a rolagem deste painel. */
  aoRolar: (evento: UIEvent<HTMLDivElement>) => void
  /** Elemento com rolagem (o pai sincroniza os dois). */
  refPainel: RefObject<HTMLDivElement>
  /** Mapa índice → elemento, exposto ao pai (para "ir até o bloco"). */
  refMapa: { current: Map<number, HTMLElement> }
  /** Mensagem exibida quando não há conteúdo. */
  vazio: string
  /** O usuário rolou com a roda/teclado (desliga o "seguir"). */
  aoRolarManual?: () => void
}

/**
 * Um dos dois painéis do preview.
 *
 * O conteúdo é aplicado **por bloco**: quando chega um HTML novo do MESMO
 * capítulo (atualização em tempo real), só os elementos `data-block-index`
 * cujo HTML mudou são trocados. O resto do DOM — e portanto a rolagem — fica
 * intacto. Só uma troca de capítulo (ou de estrutura) recria tudo.
 */
function Painel({
  chave,
  html,
  css,
  rotulo,
  detalhe,
  lado,
  blocoAtivo,
  blocoAtual,
  aoDestacar,
  aoRolar,
  refPainel,
  refMapa,
  vazio,
  aoRolarManual,
}: PainelProps): ReactElement {
  /** Elementos atualmente marcados (para remover as classes). */
  const destacado = useRef<HTMLElement | null>(null)
  const marcadoAtual = useRef<HTMLElement | null>(null)
  /** Raiz do conteúdo injetado. */
  const refConteudo = useRef<HTMLDivElement | null>(null)
  /** HTML de origem de cada bloco na última aplicação (base da comparação). */
  const fontes = useRef<Map<number, string>>(new Map())
  /** Capítulo atualmente aplicado. */
  const chaveAplicada = useRef<string | null>(null)

  const classeRaiz = lado === 'original' ? RAIZ_ORIGINAL : RAIZ_TRADUZIDO

  // (1) HTML novo: aplica (inteiro ou por bloco) e remonta o mapa.
  useEffect(() => {
    const raiz = refConteudo.current
    if (!raiz) {
      return
    }

    const modelo = document.createElement('template')
    modelo.innerHTML = html
    const novos = Array.from(modelo.content.querySelectorAll<HTMLElement>('[data-block-index]'))
    const indiceDe = (el: Element): number => Number(el.getAttribute('data-block-index'))

    const mesmaEstrutura =
      chaveAplicada.current === chave &&
      html.length > 0 &&
      novos.length === fontes.current.size &&
      novos.every((el) => fontes.current.has(indiceDe(el)))

    if (!mesmaEstrutura) {
      // Capítulo novo (ou estrutura diferente): recria, preservando a rolagem.
      const painel = refPainel.current
      const topo = painel?.scrollTop ?? 0
      raiz.innerHTML = html
      if (painel && chaveAplicada.current === chave) {
        painel.scrollTop = topo
      }
      fontes.current = new Map(novos.map((el) => [indiceDe(el), el.outerHTML]))
      chaveAplicada.current = chave
    } else {
      // Mesmo capítulo: troca só os blocos que mudaram.
      const atuais = new Map<number, HTMLElement>()
      raiz.querySelectorAll<HTMLElement>('[data-block-index]').forEach((el) => {
        atuais.set(indiceDe(el), el)
      })
      for (const novo of novos) {
        const indice = indiceDe(novo)
        const fonte = novo.outerHTML
        if (fontes.current.get(indice) === fonte) {
          continue
        }
        const antigo = atuais.get(indice)
        if (!antigo || !antigo.isConnected) {
          continue
        }
        const chegouTraducao = antigo.hasAttribute('data-pending') && !novo.hasAttribute('data-pending')
        const copia = novo.cloneNode(true) as HTMLElement
        if (chegouTraducao) {
          copia.classList.add('bloco-novo')
        }
        antigo.replaceWith(copia)
        fontes.current.set(indice, fonte)
      }
    }

    refMapa.current = new Map()
    raiz.querySelectorAll<HTMLElement>('[data-block-index]').forEach((el) => {
      const indice = indiceDe(el)
      if (Number.isFinite(indice)) {
        refMapa.current.set(indice, el)
      }
    })
    destacado.current = null
    marcadoAtual.current = null
    ajustarLegibilidade(raiz)
  }, [html, css, chave, refMapa, refPainel])

  // (2) Destaque e bloco atual: classes nos elementos deste painel.
  useEffect(() => {
    destacado.current?.classList.remove(CLASSE_DESTACADO)
    destacado.current = blocoAtivo === null ? null : refMapa.current.get(blocoAtivo) ?? null
    destacado.current?.classList.add(CLASSE_DESTACADO)

    marcadoAtual.current?.classList.remove(CLASSE_ATUAL)
    marcadoAtual.current = blocoAtual === null ? null : refMapa.current.get(blocoAtual) ?? null
    marcadoAtual.current?.classList.add(CLASSE_ATUAL)
  }, [blocoAtivo, blocoAtual, html, refMapa])

  // (3) Eventos de mouse: delegados no container (o HTML é injetado).
  const indiceDoEvento = useCallback((evento: ReactMouseEvent<HTMLDivElement>): number | null => {
    const alvo = evento.target
    if (!(alvo instanceof Element)) {
      return null
    }
    const bloco = alvo.closest('[data-block-index]')
    if (!bloco) {
      return null
    }
    const indice = Number(bloco.getAttribute('data-block-index'))
    return Number.isFinite(indice) ? indice : null
  }, [])

  return (
    <section className="flex min-h-0 min-w-0 flex-col">
      <header className="flex h-9 shrink-0 items-center justify-between gap-2 border-b border-borda px-4">
        <h3 className="painel-titulo">{rotulo}</h3>
        {detalhe && <span className="num text-[11px] text-suave">{detalhe}</span>}
      </header>

      <div
        ref={refPainel}
        onScroll={aoRolar}
        onWheel={aoRolarManual}
        onKeyDown={(evento) => {
          if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(evento.key)) {
            aoRolarManual?.()
          }
        }}
        tabIndex={0}
        onMouseOver={(evento) => {
          const indice = indiceDoEvento(evento)
          if (indice !== null) {
            aoDestacar(indice)
          }
        }}
        onMouseLeave={() => aoDestacar(null)}
        onClick={(evento) => {
          // O preview é somente leitura: links do livro não devem navegar.
          if (evento.target instanceof Element && evento.target.closest('a')) {
            evento.preventDefault()
          }
        }}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
      >
        {/* CSS do próprio EPUB, com os seletores escopados neste painel. */}
        <style>{css}</style>
        <div className={`${classeRaiz} mx-auto max-w-[68ch] px-6 py-5 font-serif text-[15px] leading-[1.7] text-texto`}>
          <div ref={refConteudo} />
          {html.length === 0 && <p className="py-8 text-center font-sans text-sm text-suave">{vazio}</p>}
        </div>
      </div>
    </section>
  )
}

/** Props do preview lado a lado. */
export interface SideBySideProps {
  /** Caminho do capítulo exibido (trocar recria o conteúdo). */
  capitulo: string
  /** HTML sanitizado do capítulo original. */
  htmlOriginal: string
  /** HTML sanitizado do capítulo traduzido. */
  htmlTraduzido: string
  /** CSS do próprio EPUB (injetado e escopado dentro dos painéis). */
  css?: string
  /** `true` enquanto o capítulo está sendo carregado pela primeira vez. */
  carregando?: boolean
  /** Mensagem de erro a mostrar no lugar do preview. */
  erro?: string | null
  /** Bloco sendo traduzido agora neste capítulo (1-based) ou `null`. */
  blocoAtual?: number | null
  /** Pedido para levar a vista até um bloco (`n` muda a cada pedido). */
  irPara?: { indice: number; n: number; suave?: boolean } | null
  /** Blocos ainda sem tradução neste capítulo. */
  pendentes?: number
  /** O usuário rolou à mão um dos painéis. */
  aoRolarManual?: () => void
  /** `true` enquanto a tradução roda (pendente não é falha). */
  rodando?: boolean
  /** Job concluído: só aí o que falta aparece como falha (vermelho). */
  concluido?: boolean
}

/**
 * Preview lado a lado do capítulo: original à esquerda, tradução à direita,
 * com rolagem sincronizada e atualização ao vivo por bloco.
 */
export default function SideBySide({
  capitulo,
  htmlOriginal,
  htmlTraduzido,
  css = '',
  carregando = false,
  erro = null,
  blocoAtual = null,
  irPara = null,
  pendentes = 0,
  aoRolarManual,
  rodando = false,
  concluido = false,
}: SideBySideProps): ReactElement {
  const refOriginal = useRef<HTMLDivElement | null>(null)
  const refTraduzido = useRef<HTMLDivElement | null>(null)
  const mapaOriginal = useRef<Map<number, HTMLElement>>(new Map())
  const mapaTraduzido = useRef<Map<number, HTMLElement>>(new Map())
  const [blocoAtivo, setBlocoAtivo] = useState<number | null>(null)

  /** Guarda de loop: `true` enquanto uma sincronização está em andamento. */
  const sincronizando = useRef(false)
  /** Marca da última sincronização (proteção extra contra loop). */
  const ultima = useRef<{ alvo: HTMLElement; razao: number } | null>(null)

  const cssOriginal = useMemo(() => aplicarEscopo(css, `.${RAIZ_ORIGINAL}`), [css])
  const cssTraduzido = useMemo(() => aplicarEscopo(css, `.${RAIZ_TRADUZIDO}`), [css])

  /**
   * Copia a **razão** de rolagem da origem para o destino (o `scrollTop` cru
   * desalinharia: as colunas têm alturas diferentes). A guarda impede o laço.
   */
  const sincronizar = useCallback((origem: HTMLElement, destino: HTMLElement | null): void => {
    if (!destino || sincronizando.current) {
      return
    }
    const razao = calcularRazao(origem.scrollTop, origem.scrollHeight, origem.clientHeight)
    if (razao === null) {
      return
    }
    sincronizando.current = true
    ultima.current = { alvo: destino, razao }
    requestAnimationFrame(() => {
      aplicarRazao(destino, razao)
      requestAnimationFrame(() => {
        sincronizando.current = false
      })
    })
  }, [])

  const aoRolar = useCallback(
    (evento: UIEvent<HTMLDivElement>, destino: HTMLElement | null): void => {
      const elemento = evento.currentTarget
      if (sincronizando.current) {
        return
      }
      const marca = ultima.current
      if (marca && marca.alvo === elemento) {
        const razaoAtual = calcularRazao(elemento.scrollTop, elemento.scrollHeight, elemento.clientHeight)
        if (razaoAtual !== null && Math.abs(razaoAtual - marca.razao) < 0.005) {
          return
        }
      }
      sincronizar(elemento, destino)
    },
    [sincronizar],
  )

  // Capítulo novo: o destaque antigo não faz sentido.
  useEffect(() => {
    setBlocoAtivo(null)
  }, [capitulo])

  // "Ir até o bloco": centraliza o bloco nos dois painéis — UMA vez por
  // pedido. Espera o capítulo carregar, mas as atualizações ao vivo seguintes
  // não voltam a rolar a vista.
  const pedidoAplicado = useRef<number | null>(null)
  useEffect(() => {
    if (!irPara || pedidoAplicado.current === irPara.n) {
      return
    }
    const quadro = requestAnimationFrame(() => {
      let aplicou = false
      for (const [painel, mapa] of [
        [refOriginal.current, mapaOriginal.current],
        [refTraduzido.current, mapaTraduzido.current],
      ] as const) {
        const alvo = mapa.get(irPara.indice)
        if (painel && alvo) {
          // Posição relativa ao PAINEL (offsetTop mediria a partir do ancestral
          // posicionado mais próximo, fora do painel com rolagem).
          const deslocamento =
            alvo.getBoundingClientRect().top - painel.getBoundingClientRect().top + painel.scrollTop
          painel.scrollTo({
            top: deslocamento - painel.clientHeight / 2 + alvo.offsetHeight / 2,
            behavior: irPara.suave ? 'smooth' : 'auto',
          })
          aplicou = true
        }
      }
      if (aplicou) {
        pedidoAplicado.current = irPara.n
        if (!irPara.suave) {
          setBlocoAtivo(irPara.indice)
        }
      }
    })
    return () => cancelAnimationFrame(quadro)
  }, [irPara, htmlOriginal, htmlTraduzido])

  if (erro) {
    return <div className="p-6 text-sm text-erro">{erro}</div>
  }

  if (carregando && htmlOriginal.length === 0 && htmlTraduzido.length === 0) {
    return <div className="p-6 text-sm text-suave">Carregando o capítulo…</div>
  }

  return (
    <div className={`grid h-full min-h-0 grid-cols-2 divide-x divide-borda ${rodando ? 'preview-rodando' : ''} ${concluido ? 'preview-concluido' : ''}`}>
      <Painel
        chave={capitulo}
        html={htmlOriginal}
        css={cssOriginal}
        rotulo="Original"
        lado="original"
        blocoAtivo={blocoAtivo}
        blocoAtual={blocoAtual}
        aoDestacar={setBlocoAtivo}
        aoRolar={(evento) => aoRolar(evento, refTraduzido.current)}
        refPainel={refOriginal}
        refMapa={mapaOriginal}
        aoRolarManual={aoRolarManual}
        vazio="Este capítulo não tem texto."
      />
      <Painel
        chave={capitulo}
        html={htmlTraduzido}
        css={cssTraduzido}
        rotulo="Tradução"
        detalhe={
          pendentes > 0
            ? `${pendentes} ${rodando ? 'por traduzir' : concluido ? 'sem tradução' : 'não traduzidos'}`
            : undefined
        }
        lado="translated"
        blocoAtivo={blocoAtivo}
        blocoAtual={blocoAtual}
        aoDestacar={setBlocoAtivo}
        aoRolar={(evento) => aoRolar(evento, refOriginal.current)}
        refPainel={refTraduzido}
        refMapa={mapaTraduzido}
        aoRolarManual={aoRolarManual}
        vazio="Ainda sem tradução para este capítulo."
      />
    </div>
  )
}
