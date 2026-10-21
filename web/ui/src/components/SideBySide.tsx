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
 * O HTML do capítulo chega **sanitizado** pelo back-end (sem `script`, sem
 * `style`, sem handlers `on*` e sem URLs `javascript:`/`data:`), por isso
 * pode ser injetado com `dangerouslySetInnerHTML`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent, ReactElement, RefObject, UIEvent } from 'react'

import { aplicarEscopo, aplicarRazao, calcularRazao } from '../lib/util'
import type { PreviewSide } from '../types'

/** Props do painel lado a lado. */
export interface SideBySideProps {
  /** HTML sanitizado do capítulo original. */
  htmlOriginal: string
  /** HTML sanitizado do capítulo traduzido. */
  htmlTraduzido: string
  /** CSS do próprio EPUB (injetado e escopado dentro dos painéis). */
  css?: string
  /** `true` enquanto os previews estão sendo carregados. */
  carregando?: boolean
  /** Mensagem de erro a mostrar no lugar do preview. */
  erro?: string | null
}

/** Classe CSS usada como raiz do escopo do CSS do EPUB. */
const RAIZ_ORIGINAL = 'preview-raiz-original'
const RAIZ_TRADUZIDO = 'preview-raiz-traduzido'

/** Classe (definida em index.css) do parágrafo destacado. */
const CLASSE_DESTACADO = 'bloco-destacado'

/** Cor de texto do tema (fundo escuro). */
const COR_TEXTO = '#e5edf5'
/** Cor de texto sobre fundos claros do livro (ex.: caixas de comentário). */
const COR_TEXTO_ESCURO = '#0b0f14'

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
  return { r: 11, g: 15, b: 20, a: 1 }
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
    r: 11,
    g: 15,
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
  /** HTML sanitizado do capítulo. */
  html: string
  /** CSS do EPUB já escopado nesta raiz. */
  css: string
  /** Rótulo do painel ("Original" / "Traduzido"). */
  rotulo: string
  /** Lado do preview. */
  lado: PreviewSide
  /** Índice do bloco destacado (1-based) ou `null`. */
  blocoAtivo: number | null
  /** Avisa o destaque de parágrafo. */
  aoDestacar: (indice: number | null) => void
  /** Avisa a rolagem deste painel. */
  aoRolar: (evento: UIEvent<HTMLDivElement>) => void
  /** Elemento com rolagem (o pai sincroniza os dois). */
  refPainel: RefObject<HTMLDivElement>
  /** Mensagem exibida quando não há conteúdo. */
  vazio: string
}

/** Um dos dois painéis do preview. */
function Painel({
  html,
  css,
  rotulo,
  lado,
  blocoAtivo,
  aoDestacar,
  aoRolar,
  refPainel,
  vazio,
}: PainelProps): ReactElement {
  /** Índice do bloco → elemento do DOM (montado a cada HTML novo). */
  const mapa = useRef<Map<number, HTMLElement>>(new Map())
  /** Elemento atualmente destacado (para remover a classe). */
  const destacado = useRef<HTMLElement | null>(null)
  /** Raiz do conteúdo injetado (para o ajuste de legibilidade). */
  const refRaiz = useRef<HTMLDivElement | null>(null)

  const classeRaiz = lado === 'original' ? RAIZ_ORIGINAL : RAIZ_TRADUZIDO

  // (1) HTML novo: monta o mapa índice → elemento e ajusta a legibilidade.
  useEffect(() => {
    const raiz = refRaiz.current
    mapa.current = new Map()
    destacado.current = null
    if (!raiz) {
      return
    }
    raiz.querySelectorAll<HTMLElement>('[data-block-index]').forEach((elemento) => {
      const indice = Number(elemento.getAttribute('data-block-index'))
      if (!Number.isFinite(indice)) {
        return
      }
      mapa.current.set(indice, elemento)
    })
    ajustarLegibilidade(raiz)
  }, [html, css])

  // (2) Destaque: marca o bloco ativo neste painel.
  useEffect(() => {
    if (destacado.current) {
      destacado.current.classList.remove(CLASSE_DESTACADO)
      destacado.current = null
    }
    if (blocoAtivo === null) {
      return
    }
    const elemento = mapa.current.get(blocoAtivo)
    if (elemento) {
      elemento.classList.add(CLASSE_DESTACADO)
      destacado.current = elemento
    }
  }, [blocoAtivo, html])

  // (3) Eventos de mouse: delegates no container (o HTML é injetado, o React
  //     não conhece os elementos dentro dele).
  const blocoDoEvento = useCallback(
    (evento: ReactMouseEvent<HTMLDivElement>): HTMLElement | null => {
      const alvo = evento.target
      if (!(alvo instanceof Element)) {
        return null
      }
      return alvo.closest('[data-block-index]')
    },
    [],
  )

  const indiceDoBloco = useCallback((bloco: HTMLElement | null): number | null => {
    if (!bloco) {
      return null
    }
    const indice = Number(bloco.getAttribute('data-block-index'))
    return Number.isFinite(indice) ? indice : null
  }, [])

  const aoPassarMouse = useCallback(
    (evento: ReactMouseEvent<HTMLDivElement>): void => {
      const indice = indiceDoBloco(blocoDoEvento(evento))
      if (indice !== null) {
        aoDestacar(indice)
      }
    },
    [aoDestacar, blocoDoEvento, indiceDoBloco],
  )

  const aoSairDoMouse = useCallback(
    (evento: ReactMouseEvent<HTMLDivElement>): void => {
      const bloco = blocoDoEvento(evento)
      if (!bloco) {
        return
      }
      const destino = evento.relatedTarget
      if (destino instanceof Element && bloco.contains(destino)) {
        // Saiu de um elemento interno, mas continua dentro do mesmo parágrafo.
        return
      }
      aoDestacar(null)
    },
    [aoDestacar, blocoDoEvento],
  )

  const aoClicar = useCallback(
    (evento: ReactMouseEvent<HTMLDivElement>): void => {
      // O preview é somente leitura: links do livro não devem navegar.
      if (evento.target instanceof Element && evento.target.closest('a')) {
        evento.preventDefault()
      }
      const indice = indiceDoBloco(blocoDoEvento(evento))
      if (indice !== null) {
        aoDestacar(indice)
      }
    },
    [aoDestacar, blocoDoEvento, indiceDoBloco],
  )

  return (
    <section className="flex min-w-0 flex-col">
      <header className="mb-2 flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-texto">{rotulo}</h3>
        <span className="text-xs uppercase tracking-wide text-suave">
          {lado === 'original' ? 'texto de origem' : 'tradução'}
        </span>
      </header>

      <div
        ref={refPainel}
        onScroll={aoRolar}
        onMouseOver={aoPassarMouse}
        onMouseOut={aoSairDoMouse}
        onClick={aoClicar}
        className="h-[60vh] overflow-y-auto overscroll-contain rounded-lg border border-borda bg-fundo p-4"
      >
        {/* CSS do próprio EPUB, com os seletores escopados neste painel. */}
        <style>{css}</style>
        <div
          ref={refRaiz}
          className={`${classeRaiz} mx-auto max-w-2xl px-1 py-2 font-serif leading-relaxed text-texto`}
        >
          {html.length > 0 ? (
            <div dangerouslySetInnerHTML={{ __html: html }} />
          ) : (
            <p className="py-6 text-center text-sm text-suave">{vazio}</p>
          )}
        </div>
      </div>
    </section>
  )
}

/**
 * Preview lado a lado do capítulo: original à esquerda, traduzido à direita.
 */
export default function SideBySide({
  htmlOriginal,
  htmlTraduzido,
  css = '',
  carregando = false,
  erro = null,
}: SideBySideProps): ReactElement {
  const refOriginal = useRef<HTMLDivElement | null>(null)
  const refTraduzido = useRef<HTMLDivElement | null>(null)
  const [blocoAtivo, setBlocoAtivo] = useState<number | null>(null)

  /** Guarda de loop: `true` enquanto uma sincronização está em andamento. */
  const sincronizando = useRef(false)
  /** Marca da última sincronização (proteção extra contra loop). */
  const ultima = useRef<{ alvo: HTMLElement; razao: number } | null>(null)

  const cssOriginal = useMemo(
    () => aplicarEscopo(css, `.${RAIZ_ORIGINAL}`),
    [css],
  )
  const cssTraduzido = useMemo(
    () => aplicarEscopo(css, `.${RAIZ_TRADUZIDO}`),
    [css],
  )

  /**
   * Copia a **razão** de rolagem da origem para o destino.
   *
   * O `scrollTop` cru nunca é copiado: as duas colunas têm alturas diferentes
   * e isso desalinha o texto. A guarda `sincronizando` impede o laço (o
   * scroll programático do destino dispara um evento de scroll que chamaria
   * a sincronização de novo).
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
      // A guarda só é liberada no frame seguinte: o evento de scroll do
      // destino é disparado nesse frame e precisa ser ignorado.
      requestAnimationFrame(() => {
        sincronizando.current = false
      })
    })
  }, [])

  /**
   * Treata a rolagem de um painel: se o evento não foi provocado por nós,
   * sincroniza o outro painel pela razão.
   */
  const aoRolar = useCallback(
    (evento: UIEvent<HTMLDivElement>, destino: HTMLElement | null): void => {
      const elemento = evento.currentTarget
      if (sincronizando.current) {
        return
      }
      const marca = ultima.current
      if (marca && marca.alvo === elemento) {
        const razaoAtual = calcularRazao(
          elemento.scrollTop,
          elemento.scrollHeight,
          elemento.clientHeight,
        )
        if (razaoAtual !== null && Math.abs(razaoAtual - marca.razao) < 0.005) {
          // É o scroll que nós mesmos provocamos: ignora.
          return
        }
      }
      sincronizar(elemento, destino)
    },
    [sincronizar],
  )

  const aoRolarOriginal = useCallback(
    (evento: UIEvent<HTMLDivElement>): void => {
      aoRolar(evento, refTraduzido.current)
    },
    [aoRolar],
  )

  const aoRolarTraduzido = useCallback(
    (evento: UIEvent<HTMLDivElement>): void => {
      aoRolar(evento, refOriginal.current)
    },
    [aoRolar],
  )

  // Capítulo novo: o destaque antigo não faz sentido.
  useEffect(() => {
    setBlocoAtivo(null)
  }, [htmlOriginal, htmlTraduzido])

  if (erro) {
    return (
      <div className="rounded-lg border border-erro/40 bg-surface p-4 text-sm text-erro">{erro}</div>
    )
  }

  if (carregando && htmlOriginal.length === 0 && htmlTraduzido.length === 0) {
    return (
      <div className="rounded-lg border border-borda bg-surface p-6 text-center text-sm text-suave">
        Carregando o capítulo…
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Painel
        html={htmlOriginal}
        css={cssOriginal}
        rotulo="Original"
        lado="original"
        blocoAtivo={blocoAtivo}
        aoDestacar={setBlocoAtivo}
        aoRolar={aoRolarOriginal}
        refPainel={refOriginal}
        vazio="Sem conteúdo original para este capítulo."
      />
      <Painel
        html={htmlTraduzido}
        css={cssTraduzido}
        rotulo="Traduzido"
        lado="translated"
        blocoAtivo={blocoAtivo}
        aoDestacar={setBlocoAtivo}
        aoRolar={aoRolarTraduzido}
        refPainel={refTraduzido}
        vazio="Sem tradução para este capítulo (ainda não traduzido?)."
      />
    </div>
  )
}
