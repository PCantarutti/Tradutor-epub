/**
 * Tipos do contrato da API do back-end (web/server.py).
 * Fonte única: qualquer mudança no back-end precisa ser refletida aqui.
 */

/** Estado do job de tradução. */
export type JobState = 'running' | 'paused' | 'done' | 'error' | 'cancelled'

/** Provedores compatíveis com a API da OpenAI. */
export type ProviderKind = 'lmstudio' | 'openai-compatible'

/** Lado do preview: original ou traduzido. */
export type PreviewSide = 'original' | 'translated'

/** Situação de um bloco no evento de progresso. */
export type BlockStatus = 'traduzido' | 'cache' | 'falhou' | 'descartado'

/** Relatório final do tradutor (`Report` em epub_translator/translator.py). */
export interface Report {
  total_blocks: number
  translated_blocks: number
  cached_blocks: number
  failed_blocks: number
  discarded_blocks: number
  files_processed: string[]
  [chave: string]: unknown
}

/** Evento SSE de progresso de um bloco. */
export interface ProgressEvent {
  type: 'progress'
  file: string
  block_index: number
  total: number
  done: number
  elapsed_s: number
  status: BlockStatus
  detail: string | null
  translated: number
  cached: number
  failed: number
  discarded: number
  eta_s: number | null
}

/** Evento SSE de mudança de estado do job. */
export interface StateEvent {
  type: 'state'
  state: JobState
  /** Mensagem do back-end (ex.: "Pausado — 12 bloco(s) preservado(s)"). */
  message?: string | null
  detail?: string | null
  total?: number
  done?: number
  translated?: number
  cached?: number
  failed?: number
  discarded?: number
  elapsed_s?: number
  eta_s?: number | null
}

/** Evento SSE de conclusão do job. */
export interface DoneEvent {
  type: 'done'
  job_id?: string
  state?: JobState
  total?: number
  done?: number
  translated?: number
  cached?: number
  failed?: number
  discarded?: number
  elapsed_s?: number
  eta_s?: number | null
  report?: Report | null
  integrity?: IntegrityItem[]
  output_name?: string | null
  has_output?: boolean
  error?: string | null
}

/** Evento SSE de erro do job. */
export interface ErrorEvent {
  type: 'error'
  /** Mensagem de erro em pt-BR (o back-end manda em `message`). */
  message?: string | null
  /** Campo legado (mantido por compatibilidade). */
  error?: string | null
  detail?: string | null
}

/** União de todos os eventos emitidos pelo SSE do back-end. */
export type SseEvent = ProgressEvent | StateEvent | DoneEvent | ErrorEvent

/** Item da verificação de integridade do EPUB gerado. */
export interface IntegrityItem {
  item: string
  ok: boolean
  detail: string | null
}

/** Fotografia do job em um dado instante. */
export interface JobSnapshot {
  job_id: string
  state: JobState
  total: number
  done: number
  translated: number
  cached: number
  failed: number
  discarded: number
  elapsed_s: number
  eta_s: number | null
  current_file: string | null
  current_block: number | null
  error: string | null
  report: Report | null
  model: string | null
  output_name: string | null
  has_output: boolean
  integrity: IntegrityItem[]
}

/** Capítulo do EPUB, na ordem do spine. */
export interface Chapter {
  index: number
  path: string
  title: string
  blocks: number
  /** Blocos ainda sem tradução no cache (falharam ou não foram traduzidos). */
  pending?: number
  /** Um caractere por bloco: "1" = já traduzido no cache. */
  mask?: string
}

/** Resposta de `POST /api/providers/models`. */
export interface ProviderModelsResponse {
  models: string[]
  error?: string
}

/** Dados do provedor de modelos (base_url + chave). */
export interface ProviderConfig {
  provider: ProviderKind
  base_url: string
  api_key: string
  model: string
}

/** Configuração enviada ao back-end na criação de um job. */
export interface JobConfig {
  provider: ProviderKind
  base_url: string
  api_key: string
  model: string
  target_lang: string
  batch_size: number
  temperature: number
  glossary: Record<string, string>
  output_name: string
  only?: string
  limit_batches?: number
}

/** Resposta de `POST /api/jobs`. */
export interface JobCreated {
  job_id: string
  state: JobState
}

/** Resposta das rotas de controle (pausar/retomar/cancelar/descartar cache). */
export interface ControlResponse {
  paused?: boolean
  cancelled?: boolean
  discarded?: boolean
  state?: JobState
}

/** Resposta de `GET /api/jobs/{id}/log`. */
export interface LogResponse {
  text: string
  /** Tamanho do log em bytes: passe-o na próxima chamada. */
  offset: number
}
