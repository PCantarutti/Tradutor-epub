/**
 * Cliente HTTP tipado do back-end (web/server.py).
 *
 * Todas as rotas são relativas a `/api`: em desenvolvimento o proxy do Vite
 * encaminha para http://localhost:8000; em produção o próprio back-end serve
 * o build estático de web/ui/dist na mesma origem.
 */
import type {
  Chapter,
  ControlResponse,
  JobCreated,
  JobSnapshot,
  LogResponse,
  PreviewSide,
  ProviderModelsResponse,
  SseEvent,
} from './types'

const API = '/api'

/** Erro devolvido pelo back-end (status HTTP + mensagem em pt-BR). */
export class ErroApi extends Error {
  readonly status: number

  constructor(mensagem: string, status: number) {
    super(mensagem)
    this.name = 'ErroApi'
    this.status = status
  }
}

function urlApi(caminho: string): string {
  return `${API}${caminho}`
}

async function erroDaResposta(resposta: Response): Promise<Error> {
  let detalhe = ''
  try {
    const corpo = await resposta.text()
    if (corpo) {
      try {
        const dados: unknown = JSON.parse(corpo)
        if (dados && typeof dados === 'object' && 'error' in dados) {
          const erro = (dados as { error?: unknown }).error
          if (typeof erro === 'string' && erro) {
            detalhe = erro
          }
        }
      } catch {
        detalhe = corpo.slice(0, 400)
      }
    }
  } catch {
    // Corpo ilegível: segue só com o status.
  }
  const mensagem = detalhe || `Erro ${resposta.status} ao chamar a API.`
  return new ErroApi(mensagem, resposta.status)
}

async function pedir<T>(caminho: string, init?: RequestInit): Promise<T> {
  const resposta = await fetch(urlApi(caminho), init)
  if (!resposta.ok) {
    throw await erroDaResposta(resposta)
  }
  return (await resposta.json()) as T
}

/* ------------------------------------------------------------------ */
/* provedores                                                          */
/* ------------------------------------------------------------------ */

/** Lista os modelos disponíveis no provedor OpenAI-compatível. */
export async function listarModelos(base_url: string, api_key: string): Promise<ProviderModelsResponse> {
  return pedir<ProviderModelsResponse>('/providers/models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ base_url, api_key }),
  })
}

/* ------------------------------------------------------------------ */
/* jobs                                                                */
/* ------------------------------------------------------------------ */

/** Cria um job de tradução: envia o .epub no campo `file` e o config em JSON. */
export async function criarJob(file: File, config: unknown): Promise<JobCreated> {
  const dados = new FormData()
  dados.append('file', file, file.name)
  dados.append('config', JSON.stringify(config))
  return pedir<JobCreated>('/jobs', { method: 'POST', body: dados })
}

/** Fotografia atual do job. */
export async function obterJob(jobId: string): Promise<JobSnapshot> {
  return pedir<JobSnapshot>(`/jobs/${encodeURIComponent(jobId)}`)
}

/** Pausa a tradução (o progresso é preservado no cache). */
export async function pausarJob(jobId: string): Promise<ControlResponse> {
  return pedir<ControlResponse>(`/jobs/${encodeURIComponent(jobId)}/pause`, { method: 'POST' })
}

/** Retoma a tradução a partir do cache. */
export async function retomarJob(jobId: string): Promise<ControlResponse> {
  return pedir<ControlResponse>(`/jobs/${encodeURIComponent(jobId)}/resume`, { method: 'POST' })
}

/** Cancela a tradução. */
export async function cancelarJob(jobId: string): Promise<ControlResponse> {
  return pedir<ControlResponse>(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
}

/** Descarta o cache do job (traduz tudo de novo). */
export async function descartarCache(jobId: string): Promise<ControlResponse> {
  return pedir<ControlResponse>(`/jobs/${encodeURIComponent(jobId)}/discard-cache`, { method: 'POST' })
}

/** Capítulos do EPUB, na ordem do spine. */
export async function listarCapitulos(jobId: string): Promise<Chapter[]> {
  return pedir<Chapter[]>(`/jobs/${encodeURIComponent(jobId)}/chapters`)
}

/** HTML do preview de um capítulo (original ou traduzido). */
export async function buscarPreview(
  jobId: string,
  chapter: string,
  side: PreviewSide,
): Promise<string> {
  const resposta = await fetch(urlDoPreview(jobId, chapter, side))
  if (!resposta.ok) {
    throw await erroDaResposta(resposta)
  }
  return await resposta.text()
}

/** Log textual do job. */
export async function buscarLog(jobId: string): Promise<LogResponse> {
  return pedir<LogResponse>(`/jobs/${encodeURIComponent(jobId)}/log`)
}

/** Baixa o EPUB traduzido como arquivo. */
export async function baixarJob(jobId: string, nomeSugerido?: string): Promise<void> {
  const resposta = await fetch(urlDoDownload(jobId))
  if (!resposta.ok) {
    throw await erroDaResposta(resposta)
  }
  const blob = await resposta.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = nomeSugerido && nomeSugerido.length > 0 ? nomeSugerido : `${jobId}.epub`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

/* ------------------------------------------------------------------ */
/* URLs diretas (para <img>, <iframe>, <link> e downloads)             */
/* ------------------------------------------------------------------ */

/** URL do preview de um capítulo. */
export function urlDoPreview(jobId: string, chapter: string, side: PreviewSide): string {
  const params = new URLSearchParams({ chapter, side })
  return urlApi(`/jobs/${encodeURIComponent(jobId)}/preview?${params.toString()}`)
}

/** URL do EPUB traduzido. */
export function urlDoDownload(jobId: string): string {
  return urlApi(`/jobs/${encodeURIComponent(jobId)}/download`)
}

/** URL de um recurso do EPUB (imagem, CSS) pelo caminho interno do zip. */
export function urlDoAsset(jobId: string, path: string): string {
  const params = new URLSearchParams({ path })
  return urlApi(`/jobs/${encodeURIComponent(jobId)}/asset?${params.toString()}`)
}

/** URL do CSS do próprio EPUB. */
export function urlDoStyle(jobId: string): string {
  return urlApi(`/jobs/${encodeURIComponent(jobId)}/style.css`)
}

/* ------------------------------------------------------------------ */
/* SSE                                                                 */
/* ------------------------------------------------------------------ */

/**
 * Abre o fluxo de eventos do job (`GET /api/jobs/{id}/events`).
 *
 * O back-end envia `data: {json}\n\n` e o tipo do evento (`progress`,
 * `state`, `done`, `error`) vem dentro do JSON, então `onmessage` basta.
 */
export function conectarEventos(
  jobId: string,
  onEvent: (evento: SseEvent) => void,
  onError?: (erro: Event) => void,
): EventSource {
  const fonte = new EventSource(urlApi(`/jobs/${encodeURIComponent(jobId)}/events`))

  fonte.onmessage = (mensagem: MessageEvent<string>) => {
    const bruto = mensagem.data
    if (!bruto) {
      return
    }
    try {
      const evento = JSON.parse(bruto) as SseEvent
      if (evento && typeof evento === 'object' && 'type' in evento) {
        onEvent(evento)
      }
    } catch (erro) {
      console.error('Evento SSE inválido:', erro, bruto)
    }
  }

  fonte.onerror = (erro: Event) => {
    if (onError) {
      onError(erro)
    }
  }

  return fonte
}
