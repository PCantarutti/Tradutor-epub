/**
 * Utilidades do preview: razão de rolagem e escopo do CSS do EPUB.
 *
 * Nenhuma dependência de DOM aqui: as funções recebem números (ou um objeto
 * com `scrollTop`/`scrollHeight`/`clientHeight`), o que as torna fáceis de
 * verificar fora do browser.
 */

/** Objeto com as propriedades de rolagem usadas pelas funções abaixo. */
export interface ElementoRolavel {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

/**
 * Razão de rolagem de um elemento: 0 = topo, 1 = fim.
 *
 * Devolve `null` quando não há área rolável (conteúdo curto demais) ou quando
 * o cálculo não resulta em número finito.
 */
export function calcularRazao(
  scrollTop: number,
  scrollHeight: number,
  clientHeight: number,
): number | null {
  const rolavel = scrollHeight - clientHeight
  if (rolavel <= 0) {
    return null
  }
  const razao = scrollTop / rolavel
  if (!Number.isFinite(razao)) {
    return null
  }
  return Math.max(0, Math.min(1, razao))
}

/** Aplica uma razão de rolagem (0 a 1) a um elemento. */
export function aplicarRazao(destino: ElementoRolavel, razao: number): void {
  const rolavel = destino.scrollHeight - destino.clientHeight
  if (rolavel <= 0) {
    return
  }
  destino.scrollTop = razao * rolavel
}

/* ------------------------------------------------------------------ */
/* Escopo do CSS do EPUB                                               */
/* ------------------------------------------------------------------ */

/** At-rules cujo conteúdo volta a ser escopado recursivamente. */
const AT_RECURSAO = new Set(['@media', '@supports', '@container', '@layer'])

/** Remove os comentários `/* ... *\/` do CSS. */
function semComentarios(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

/**
 * Prefixa os seletores de uma regra com a raiz do painel.
 *
 * `html`, `:root` e `body` viram a própria raiz (o CSS do livro costuma
 * estilizar o `body`); todo o resto ganha o prefixo da raiz, para o CSS do
 * EPUB não vazar para a interface do app.
 */
function escoparSeletores(seletores: string, raiz: string): string {
  return seletores
    .split(',')
    .map((seletor) => seletor.trim())
    .filter((seletor) => seletor.length > 0)
    .map((seletor) => {
      if (seletor === 'html' || seletor === ':root' || seletor === 'body') {
        return raiz
      }
      if (seletor.startsWith('html ')) {
        return `${raiz} ${seletor.slice(5)}`
      }
      if (seletor.startsWith('body ')) {
        return `${raiz} ${seletor.slice(5)}`
      }
      if (seletor.startsWith(':root ')) {
        return `${raiz} ${seletor.slice(6)}`
      }
      return `${raiz} ${seletor}`
    })
    .join(', ')
}

/**
 * Escopa o CSS do EPUB dentro de um container.
 *
 * O CSS do livro (`GET /api/jobs/{id}/style.css`) é global por natureza:
 * regras como `body { margin: 0 1pt }` ou `h1 { font-size: 2em }` mudariam
 * a interface do app. Aqui cada seletor ganha o prefixo da raiz do painel —
 * exceto os at-rules cujo conteúdo não é uma lista de seletores
 * (`@font-face`, `@keyframes`, `@page`), que são preservados como estão.
 *
 * @param css   CSS do EPUB.
 * @param raiz  Seletor da raiz do painel (ex.: `".preview-pagina-original"`).
 */
export function aplicarEscopo(css: string, raiz: string): string {
  const limpo = semComentarios(css)
    .replace(/@import[^;]*;/gi, '')
    .replace(/@charset[^;]*;/gi, '')

  let resultado = ''
  let buffer = ''
  let indice = 0

  while (indice < limpo.length) {
    const caractere = limpo[indice]

    if (caractere === '{') {
      const preludio = buffer.trim()
      buffer = ''
      let profundidade = 1
      let cursor = indice + 1
      while (cursor < limpo.length && profundidade > 0) {
        if (limpo[cursor] === '{') {
          profundidade += 1
        } else if (limpo[cursor] === '}') {
          profundidade -= 1
        }
        cursor += 1
      }
      const conteudo = limpo.slice(indice + 1, cursor - 1)

      if (preludio.startsWith('@')) {
        const nome = preludio.split(/\s+/)[0].toLowerCase()
        if (AT_RECURSAO.has(nome)) {
          resultado += `${preludio} { ${aplicarEscopo(conteudo, raiz)}}`
        } else {
          // @font-face, @keyframes, @page…: conteúdo preservado.
          resultado += `${preludio} {${conteudo}}`
        }
      } else if (preludio.length > 0) {
        resultado += `${escoparSeletores(preludio, raiz)} {${conteudo}}`
      }

      indice = cursor
      continue
    }

    if (caractere === '}') {
      buffer = ''
      indice += 1
      continue
    }

    buffer += caractere
    indice += 1
  }

  return resultado
}
