/**
 * Seletor de provedor de modelos (TASK-026).
 *
 * Escolha entre **LM Studio (local)** e qualquer **API compatível com a
 * OpenAI** (OpenAI, Together, Groq, OpenRouter, Ollama…), com os campos
 * `base_url`, `api_key` e `model`.
 *
 * Decisões:
 *
 * 1. **Componente controlado** — o estado (provedor, URL, chave, modelo) mora
 *    no App; aqui só há um `onChange(patch)`. Assim o App pode guardar a
 *    configuração, reenviá-la ao back-end e (depois) persistir.
 * 2. **A chave nunca aparece em texto puro** — o campo é `password` e só vira
 *    `text` quando o usuário clica em "mostrar".
 * 3. **Ponto de status** — verde quando a conexão deu certo, vermelho quando
 *    deu erro, cinza quando ainda não foi testado. A mensagem de erro do
 *    back-end (já em pt-BR) aparece ao lado do ponto, sem quebrar a tela.
 * 4. **Modelo**: depois de um teste bem-sucedido o dropdown é populado com a
 *    lista devolvida pelo provedor; o campo de texto livre ao lado aceita um
 *    modelo que não esteja na lista.
 * 5. **A ``base_url`` é livre** — o app é local e o usuário pode apontar para
 *    QUALQUER provedor compatível com a OpenAI (LM Studio, Ollama local ou na
 *    nuvem, OpenAI, Together, Groq, OpenRouter, vLLM…). Por isso o campo é
 *    ``type="text"`` (o ``type="url"`` do HTML5 marcaria como inválida uma URL
 *    sem esquema, ex.: ``ollama.com/v1``) e **nenhuma validação** barra a URL.
 *
 * O teste usa `listarModelos` de `src/api.ts` (`POST /api/providers/models`),
 * que **não** expõe a `api_key` na resposta.
 */
import { useCallback, useEffect, useId, useState } from 'react'
import type { ChangeEvent, ReactElement } from 'react'

import { listarModelos } from '../api'
import type { ProviderConfig, ProviderKind } from '../types'

/** Resultado de um teste de conexão, devolvido em `onTestar`. */
export interface ResultadoTesteProvedor {
  /** `true` quando o provedor respondeu com a lista de modelos. */
  ok: boolean
  /** Modelos devolvidos pelo provedor (vazio em caso de erro). */
  models: string[]
  /** Mensagem de erro em pt-BR (do back-end ou da própria rede). */
  erro: string | null
}

/** Props do seletor de provedor. */
export interface ProviderSelectorProps {
  /** Provedor escolhido. */
  provider: ProviderKind
  /** Endereço da API do provedor (ex.: `http://localhost:1234/v1`). */
  base_url: string
  /** Chave da API (nunca renderizada em texto puro). */
  api_key: string
  /** Nome do modelo a usar. */
  model: string
  /** Recebe o patch do campo alterado. */
  onChange: (patch: Partial<ProviderConfig>) => void
  /** Chamado depois de cada teste de conexão (sucesso ou falha). */
  onTestar?: (resultado: ResultadoTesteProvedor) => void
}

/** Estado do ponto de status. */
type StatusTeste = 'neutro' | 'testando' | 'ok' | 'erro'

/** `base_url` padrão de cada provedor. */
const BASE_POR_PROVEDOR: Record<ProviderKind, string> = {
  lmstudio: 'http://localhost:1234/v1',
  'openai-compatible': 'https://api.openai.com/v1',
}

/** Exemplos de endpoint aceitos (a lista é ilustrativa: qualquer URL vale). */
const EXEMPLOS_DE_ENDPOINT = [
  'http://localhost:1234/v1',
  'https://ollama.com/v1',
  'https://api.openai.com/v1',
]

/** Rótulo de cada provedor no `<select>`. */
const ROTULO_PROVEDOR: Record<ProviderKind, string> = {
  lmstudio: 'LM Studio (local)',
  'openai-compatible':
    'API compatível com OpenAI (OpenAI, Together, Groq, OpenRouter, Ollama…)',
}

/** Classe Tailwind comum aos campos de formulário. */
const CLASSE_CAMPO =
  'w-full rounded-md border border-borda bg-fundo px-3 py-2 text-sm text-texto ' +
  'placeholder:text-suave/60 focus:border-acento focus:outline-none ' +
  'disabled:cursor-not-allowed disabled:opacity-60'

/** Classe Tailwind comum aos rótulos dos campos. */
const CLASSE_ROTULO = 'mb-1 block text-xs font-medium text-suave'

/**
 * Seletor de provedor: escolha do provedor, endpoint, chave, modelo e teste
 * de conexão.
 */
export default function ProviderSelector({
  provider,
  base_url,
  api_key,
  model,
  onChange,
  onTestar,
}: ProviderSelectorProps): ReactElement {
  /** Estado do teste de conexão (ponto de status + mensagem). */
  const [status, setStatus] = useState<StatusTeste>('neutro')
  /** Modelos devolvidos no último teste bem-sucedido. */
  const [modelos, setModelos] = useState<string[]>([])
  /** Mensagem de erro do último teste (já em pt-BR). */
  const [erro, setErro] = useState<string | null>(null)
  /** `true` quando o usuário pediu para ver a chave em texto puro. */
  const [mostrarChave, setMostrarChave] = useState(false)

  /** Ids únicos para os labels (o componente pode aparecer mais de uma vez). */
  const idBase = useId()
  const idProvedor = `${idBase}-provedor`
  const idBaseUrl = `${idBase}-base-url`
  const idChave = `${idBase}-api-key`
  const idModelo = `${idBase}-modelo`
  const idListaModelo = `${idBase}-lista-modelo`

  // Mudou o endpoint ou a chave: o teste anterior não vale mais.
  useEffect(() => {
    setStatus('neutro')
    setModelos([])
    setErro(null)
  }, [base_url, api_key])

  /**
   * Troca de provedor: ajusta o `base_url` **só quando** ele ainda é o padrão
   * do provedor anterior — um endpoint digitado pelo usuário nunca é
   * sobrescrito (trocar de provedor não pode apagar `https://ollama.com/v1`).
   */
  const trocarProvedor = useCallback(
    (evento: ChangeEvent<HTMLSelectElement>): void => {
      const novo = evento.target.value as ProviderKind
      const customizado = base_url !== BASE_POR_PROVEDOR[provider]
      onChange(
        customizado
          ? { provider: novo }
          : { provider: novo, base_url: BASE_POR_PROVEDOR[novo] },
      )
    },
    [onChange, provider, base_url],
  )

  /** Testa a conexão com o provedor e popula a lista de modelos. */
  const testar = useCallback(async (): Promise<void> => {
    setStatus('testando')
    setErro(null)
    try {
      const resposta = await listarModelos(base_url, api_key)
      const lista = Array.isArray(resposta?.models) ? resposta.models : []
      const mensagem =
        typeof resposta?.error === 'string' && resposta.error.length > 0
          ? resposta.error
          : null

      if (mensagem) {
        setStatus('erro')
        setModelos([])
        setErro(mensagem)
        onTestar?.({ ok: false, models: [], erro: mensagem })
        return
      }

      setModelos(lista)
      setStatus('ok')
      onTestar?.({ ok: true, models: lista, erro: null })
    } catch (falha) {
      const mensagem =
        falha instanceof Error
          ? falha.message
          : 'Não foi possível falar com o provedor.'
      setStatus('erro')
      setModelos([])
      setErro(mensagem)
      onTestar?.({ ok: false, models: [], erro: mensagem })
    }
  }, [base_url, api_key, onTestar])

  /** Cor do ponto de status. */
  const corDoPonto =
    status === 'ok'
      ? 'bg-sucesso'
      : status === 'erro'
        ? 'bg-erro'
        : status === 'testando'
          ? 'animate-pulse bg-suave'
          : 'bg-suave'

  /** Texto do tooltip do ponto de status. */
  const tituloDoPonto =
    status === 'ok'
      ? 'Conexão com o provedor funcionando'
      : status === 'erro'
        ? 'Falha ao falar com o provedor'
        : status === 'testando'
          ? 'Testando a conexão…'
          : 'Conexão ainda não testada'

  /** O modelo atual está na lista devolvida pelo provedor? */
  const modeloNaLista = modelos.includes(model) ? model : ''

  return (
    <section className="rounded-lg border border-borda bg-surface p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-texto">Provedor de modelos</h2>
        <span className="text-xs text-suave">
          LM Studio local ou qualquer API compatível com OpenAI
        </span>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {/* (a) Provedor */}
        <div>
          <label htmlFor={idProvedor} className={CLASSE_ROTULO}>
            Provedor
          </label>
          <select
            id={idProvedor}
            value={provider}
            onChange={trocarProvedor}
            className={CLASSE_CAMPO}
          >
            <option value="lmstudio">{ROTULO_PROVEDOR.lmstudio}</option>
            <option value="openai-compatible">
              {ROTULO_PROVEDOR['openai-compatible']}
            </option>
          </select>
        </div>

        {/* (b) base_url — aceita QUALQUER URL (o app é local) */}
        <div>
          <label htmlFor={idBaseUrl} className={CLASSE_ROTULO}>
            Endereço da API
          </label>
          <input
            id={idBaseUrl}
            type="text"
            value={base_url}
            spellCheck={false}
            autoComplete="off"
            inputMode="url"
            placeholder="http://localhost:1234/v1"
            onChange={(evento) => onChange({ base_url: evento.target.value })}
            className={CLASSE_CAMPO}
            title="Qualquer endpoint compatível com a API da OpenAI (inclusive na nuvem)"
          />
          <p className="mt-1 text-[11px] leading-relaxed text-suave">
            Qualquer URL serve, por exemplo:{' '}
            {EXEMPLOS_DE_ENDPOINT.map((exemplo, indice) => (
              <span key={exemplo}>
                {indice > 0 && ' · '}
                <code className="text-texto">{exemplo}</code>
              </span>
            ))}
          </p>
        </div>

        {/* (b) api_key — password até o usuário pedir para ver */}
        <div>
          <label htmlFor={idChave} className={CLASSE_ROTULO}>
            Chave da API
          </label>
          <div className="relative">
            <input
              id={idChave}
              type={mostrarChave ? 'text' : 'password'}
              value={api_key}
              spellCheck={false}
              autoComplete="off"
              placeholder="deixe vazio se o provedor não exigir"
              onChange={(evento) => onChange({ api_key: evento.target.value })}
              className={`${CLASSE_CAMPO} pr-20`}
            />
            <button
              type="button"
              onClick={() => setMostrarChave((valor) => !valor)}
              className="absolute right-1 top-1/2 -translate-y-1/2 rounded px-2 py-1 text-xs text-suave transition-colors hover:text-acento"
              title={mostrarChave ? 'Ocultar a chave' : 'Mostrar a chave'}
            >
              {mostrarChave ? 'ocultar' : 'mostrar'}
            </button>
          </div>
        </div>

        {/* (b) model — dropdown populado + campo livre */}
        <div>
          <label htmlFor={idModelo} className={CLASSE_ROTULO}>
            Modelo
          </label>
          <select
            value={modeloNaLista}
            disabled={modelos.length === 0}
            onChange={(evento) => onChange({ model: evento.target.value })}
            className={`${CLASSE_CAMPO} mb-1`}
            title={
              modelos.length > 0
                ? 'Modelos disponíveis no provedor'
                : 'Teste a conexão para listar os modelos'
            }
          >
            <option value="">
              {modelos.length > 0
                ? '— escolher um modelo da lista —'
                : '— teste a conexão para listar os modelos —'}
            </option>
            {modelos.map((nome) => (
              <option key={nome} value={nome}>
                {nome}
              </option>
            ))}
          </select>
          <input
            id={idModelo}
            type="text"
            list={idListaModelo}
            value={model}
            spellCheck={false}
            autoComplete="off"
            placeholder="nome do modelo (pode ser um que não está na lista)"
            onChange={(evento) => onChange({ model: evento.target.value })}
            className={CLASSE_CAMPO}
          />
          <datalist id={idListaModelo}>
            {modelos.map((nome) => (
              <option key={nome} value={nome} />
            ))}
          </datalist>
        </div>
      </div>

      {/* (c) Testar conexão + (d) ponto de status */}
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => {
            void testar()
          }}
          disabled={status === 'testando'}
          className="rounded-md border border-acento/60 bg-acento/10 px-3 py-1.5 text-xs font-medium text-acento transition-colors hover:bg-acento/20 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {status === 'testando' ? 'Testando…' : 'Testar conexão'}
        </button>

        <span className="inline-flex items-center gap-2">
          <span
            className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${corDoPonto}`}
            title={tituloDoPonto}
            aria-label={tituloDoPonto}
            role="img"
          />
          <span className="text-xs text-suave">{tituloDoPonto}</span>
        </span>

        {status === 'ok' && (
          <span className="text-xs text-sucesso">
            {modelos.length === 1
              ? '1 modelo disponível'
              : `${modelos.length} modelos disponíveis`}
          </span>
        )}

        {status === 'erro' && erro && (
          <span className="text-xs text-erro">{erro}</span>
        )}
      </div>
    </section>
  )
}
