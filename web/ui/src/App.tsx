/**
 * App do Tradutor de EPUB (TASK-029).
 *
 * Integra os **sete componentes já prontos** de `src/components/`:
 *
 * 1. **Faixa 1 — cabeçalho**: nome do app, `ProviderSelector`, upload do
 *    `.epub` (botão "Traduzir"), upload do glossário e "Ver log".
 * 2. **Faixa 2 — progresso**: `ProgressBar` + `MetricsPanel` (só aparece
 *    quando há job ativo).
 * 3. **Faixa 3 — área principal**: `ChapterNav` + `SideBySide` +
 *    `IntegrityPanel`.
 *
 * O estado chega pelo **SSE** (`GET /api/jobs/{id}/events`): cada evento é
 * acumulado num `useRef` e o React só é atualizado a cada ~100 ms
 * (*throttle*), para a barra não re-renderizar a 60 fps.
 *
 * Decisões:
 *
 * - O `EventSource` fica num `useRef` e é fechado no *cleanup* do `useEffect`
 *   (nada de conexão aberta depois do unmount).
 * - A pausa só tem efeito **entre lotes** (um lote leva ~90 s no LM Studio):
 *   por isso existe o estado "pausando…", que só vira "pausado" quando chega
 *   o evento `state: paused`.
 * - A `api_key` nunca é renderizada em texto puro (quem cuida disso é o
 *   `ProviderSelector`).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChangeEvent, ReactElement } from 'react'

import {
  baixarJob,
  buscarLog,
  buscarPreview,
  cancelarJob,
  conectarEventos,
  criarJob,
  descartarCache,
  listarCapitulos,
  obterJob,
  pausarJob,
  retomarJob,
  urlDoStyle,
} from './api'
import type {
  BlockStatus,
  Chapter,
  IntegrityItem,
  JobConfig,
  JobState,
  LogResponse,
  ProviderConfig,
  Report,
  SseEvent,
} from './types'
import ChapterNav from './components/ChapterNav'
import IntegrityPanel from './components/IntegrityPanel'
import LogViewer from './components/LogViewer'
import MetricsPanel from './components/MetricsPanel'
import ProgressBar from './components/ProgressBar'
import ProviderSelector from './components/ProviderSelector'
import SideBySide from './components/SideBySide'

/** Intervalo mínimo entre duas atualizações do React (ms). */
const INTERVALO_ATUALIZACAO_MS = 100

/** Tamanho do lote enviado ao back-end (blocos por chamada ao modelo). */
const TAMANHO_LOTE = 5

/** Temperatura da tradução (0 = determinístico, 1 = criativo). */
const TEMPERATURA = 0.25

/** Idioma de destino. */
const IDIOMA_DESTINO = 'pt-BR'

/** Motivo pelo qual a interface está esperando um evento do back-end. */
type MotivoEspera = 'pausa' | 'retomada'

/** Estado do job mostrado na interface (espelho do acumulador do SSE). */
interface EstadoJob {
  /** Estado do job (running, paused, done, error, cancelled). */
  estado: JobState
  /** Espera em andamento: pausa pedida ou retomada pedida. */
  aguardando: MotivoEspera | null
  /** Mensagem de status (pausado, cancelado, concluído…). */
  mensagem: string | null
  /** Mensagem de erro (em pt-BR). */
  erro: string | null
  /** Total de blocos do livro. */
  total: number
  /** Blocos já concluídos. */
  done: number
  /** Blocos traduzidos pelo modelo. */
  translated: number
  /** Blocos reusados do cache. */
  cached: number
  /** Blocos que falharam. */
  failed: number
  /** Blocos descartados. */
  discarded: number
  /** Segundos decorridos. */
  elapsed_s: number
  /** Estimativa de término (segundos) ou `null`. */
  eta_s: number | null
  /** Arquivo (capítulo) em processamento. */
  arquivo: string | null
  /** Bloco em processamento (1-based). */
  bloco: number | null
  /** `detail` do último evento de cada status (tooltip dos contadores). */
  detalhes: Partial<Record<BlockStatus, string>>
  /** Relatório final do tradutor. */
  relatorio: Report | null
  /** Itens da verificação de integridade. */
  integridade: IntegrityItem[]
  /** Nome do arquivo de saída. */
  nomeSaida: string | null
  /** `true` quando o EPUB traduzido já existe no disco. */
  temSaida: boolean
}

/** Estado inicial do job (nada aconteceu ainda). */
function estadoInicial(): EstadoJob {
  return {
    estado: 'running',
    aguardando: null,
    mensagem: null,
    erro: null,
    total: 0,
    done: 0,
    translated: 0,
    cached: 0,
    failed: 0,
    discarded: 0,
    elapsed_s: 0,
    eta_s: null,
    arquivo: null,
    bloco: null,
    detalhes: {},
    relatorio: null,
    integridade: [],
    nomeSaida: null,
    temSaida: false,
  }
}

/** Extrai uma mensagem em pt-BR de um erro qualquer. */
function mensagemDeErro(falha: unknown, padrao: string): string {
  if (falha instanceof Error && falha.message && falha.message.length > 0) {
    return falha.message
  }
  return padrao
}

/**
 * Converte o conteúdo de um glossário em `{termo: tradução}`.
 *
 * O parser é escolhido pelo **CONTEÚDO**, nunca pelo nome do arquivo: o
 * `File.name` pode vir como `C:\fakepath\glossario.json`, `glossario.txt`
 * ou de qualquer outra forma, e um JSON mal detectado viraria um glossário
 * vazio (ou cheio de lixo) no prompt do modelo.
 *
 * - **JSON**: `JSON.parse` é tentado primeiro (o objeto precisa ser
 *   `{termo: tradução}`).
 * - **YAML simples**: uma linha `termo: tradução` por linha; linhas vazias e
 *   comentários (`#`) são ignorados.
 */
function lerGlossario(conteudo: string): Record<string, string> {
  const termos: Record<string, string> = {}

  // (1) JSON — tenta sempre primeiro, venha o arquivo de onde vier.
  try {
    const dados: unknown = JSON.parse(conteudo)
    if (dados && typeof dados === 'object' && !Array.isArray(dados)) {
      for (const [chave, valor] of Object.entries(dados as Record<string, unknown>)) {
        const termo = chave.trim()
        if (termo.length === 0) {
          continue
        }
        termos[termo] = String(valor)
      }
      return termos
    }
    // JSON válido, mas não é um objeto (ex.: uma lista): cai no YAML.
  } catch {
    // Não é JSON: cai no parser YAML simples.
  }

  // (2) YAML simples
  for (const linha of conteudo.split(/\r?\n/)) {
    const limpa = linha.trim()
    if (limpa.length === 0 || limpa.startsWith('#')) {
      continue
    }
    const separador = limpa.indexOf(':')
    if (separador <= 0) {
      continue
    }
    const termo = limpa
      .slice(0, separador)
      .trim()
      .replace(/^["']|["']$/g, '')
    const traducao = limpa
      .slice(separador + 1)
      .trim()
      .replace(/^["']|["']$/g, '')
    if (termo.length === 0) {
      continue
    }
    termos[termo] = traducao
  }
  return termos
}

/** Busca o CSS do próprio EPUB (usado no preview lado a lado). */
async function buscarCssDoEpub(jobId: string): Promise<string> {
  const resposta = await fetch(urlDoStyle(jobId))
  if (!resposta.ok) {
    throw new Error('Não foi possível carregar a folha de estilo do livro.')
  }
  return await resposta.text()
}

/** Classe Tailwind dos campos de formulário. */
const CLASSE_CAMPO =
  'w-full rounded-md border border-borda bg-fundo px-3 py-2 text-sm text-texto ' +
  'placeholder:text-suave/60 focus:border-acento focus:outline-none ' +
  'disabled:cursor-not-allowed disabled:opacity-60'

/** Classe Tailwind dos campos de arquivo (com o botão nativo estilizado). */
const CLASSE_ARQUIVO =
  'w-full rounded-md border border-borda bg-fundo px-3 py-2 text-sm text-texto ' +
  'file:mr-3 file:rounded-md file:border-0 file:bg-borda file:px-3 file:py-1.5 ' +
  'file:text-xs file:font-medium file:text-texto hover:file:bg-suave/25'

/** Classe Tailwind dos botões secundários. */
const CLASSE_BOTAO =
  'rounded-md border border-borda bg-fundo px-3 py-1.5 text-xs font-medium text-texto ' +
  'transition-colors hover:border-acento hover:text-acento ' +
  'disabled:cursor-not-allowed disabled:opacity-50'

/** Classe Tailwind do botão principal ("Traduzir"). */
const CLASSE_BOTAO_PRINCIPAL =
  'rounded-md border border-acento/60 bg-acento/10 px-3 py-2 text-sm font-medium ' +
  'text-acento transition-colors hover:bg-acento/20 ' +
  'disabled:cursor-not-allowed disabled:opacity-50'

/**
 * App completo: cabeçalho + barra de progresso + área principal.
 */
export default function App(): ReactElement {
  /* ------------------------------------------------------------------ */
  /* Provedor de modelos                                                 */
  /* ------------------------------------------------------------------ */
  const [provedor, setProvedor] = useState<ProviderConfig>({
    provider: 'lmstudio',
    base_url: 'http://localhost:1234/v1',
    api_key: '',
    model: '',
  })

  /** Recebe o patch do `ProviderSelector` (componente controlado). */
  const alterarProvedor = useCallback((patch: Partial<ProviderConfig>): void => {
    setProvedor((atual) => ({ ...atual, ...patch }))
  }, [])

  /* ------------------------------------------------------------------ */
  /* Upload do livro e do glossário                                       */
  /* ------------------------------------------------------------------ */
  const [arquivo, setArquivo] = useState<File | null>(null)
  const [nomeSaida, setNomeSaida] = useState('traducao_resultado.epub')
  const [enviando, setEnviando] = useState(false)
  const [erroUpload, setErroUpload] = useState<string | null>(null)

  const [glossario, setGlossario] = useState<Record<string, string>>({})
  const [nomeGlossario, setNomeGlossario] = useState<string | null>(null)
  const [erroGlossario, setErroGlossario] = useState<string | null>(null)

  /* ------------------------------------------------------------------ */
  /* Job (estado espelhado pelo SSE)                                      */
  /* ------------------------------------------------------------------ */
  const [jobId, setJobId] = useState<string | null>(null)
  const [ui, setUi] = useState<EstadoJob>(estadoInicial())

  /** Acumulador do SSE: a fonte da verdade entre dois *flushes*. */
  const acumuladoRef = useRef<EstadoJob>(estadoInicial())
  /** Timer do *throttle* (100 ms). */
  const temporizadorRef = useRef<number | null>(null)
  /** Conexão SSE aberta (fechada no *cleanup*). */
  const fonteRef = useRef<EventSource | null>(null)
  /** `true` quando já avisamos que a conexão caiu. */
  const avisouQuedaRef = useRef(false)
  const [avisoConexao, setAvisoConexao] = useState<string | null>(null)

  /* ------------------------------------------------------------------ */
  /* Capítulos e preview                                                  */
  /* ------------------------------------------------------------------ */
  const [capitulos, setCapitulos] = useState<Chapter[]>([])
  const [capitulo, setCapitulo] = useState<string | null>(null)
  const [carregandoCapitulos, setCarregandoCapitulos] = useState(false)
  const [erroCapitulos, setErroCapitulos] = useState<string | null>(null)

  const [htmlOriginal, setHtmlOriginal] = useState('')
  const [htmlTraduzido, setHtmlTraduzido] = useState('')
  const [css, setCss] = useState('')
  const [carregandoPreview, setCarregandoPreview] = useState(false)
  const [erroPreview, setErroPreview] = useState<string | null>(null)
  /** Contador que força o recarregamento do preview. */
  const [noncePreview, setNoncePreview] = useState(0)

  /* ------------------------------------------------------------------ */
  /* Throttle: aplica o acumulado no React no máximo a cada 100 ms       */
  /* ------------------------------------------------------------------ */
  const aplicar = useCallback((): void => {
    if (temporizadorRef.current !== null) {
      return
    }
    temporizadorRef.current = window.setTimeout(() => {
      temporizadorRef.current = null
      setUi({ ...acumuladoRef.current })
    }, INTERVALO_ATUALIZACAO_MS)
  }, [])

  // Limpa o timer do throttle quando o App desmonta.
  useEffect(
    () => () => {
      if (temporizadorRef.current !== null) {
        window.clearTimeout(temporizadorRef.current)
        temporizadorRef.current = null
      }
    },
    [],
  )

  /** Fecha a conexão SSE (job terminal). */
  const encerrarFluxo = useCallback((): void => {
    const fonte = fonteRef.current
    if (fonte) {
      fonte.close()
      fonteRef.current = null
    }
  }, [])

  /* ------------------------------------------------------------------ */
  /* Eventos do SSE                                                      */
  /* ------------------------------------------------------------------ */
  const aoReceberEvento = useCallback(
    (evento: SseEvent): void => {
      const atual = acumuladoRef.current

      if (avisouQuedaRef.current) {
        avisouQuedaRef.current = false
        setAvisoConexao(null)
      }

      switch (evento.type) {
        case 'progress': {
          atual.total = evento.total
          atual.done = evento.done
          atual.translated = evento.translated
          atual.cached = evento.cached
          atual.failed = evento.failed
          atual.discarded = evento.discarded
          atual.elapsed_s = evento.elapsed_s
          atual.eta_s = evento.eta_s
          atual.arquivo = evento.file
          atual.bloco = evento.block_index
          if (evento.detail && evento.detail.length > 0) {
            atual.detalhes = { ...atual.detalhes, [evento.status]: evento.detail }
          }
          // Um evento de progresso significa que o job está vivo: some com
          // o erro anterior (ex.: a queda da conexão já se resolveu).
          atual.erro = null
          // A retomada vale na hora: o próximo evento de progresso confirma.
          // Já a pausa só vale ENTRE lotes — os blocos do lote em andamento
          // ainda geram eventos de progresso, então "pausando…" continua
          // até chegar o evento "state: paused".
          if (atual.aguardando === 'retomada') {
            atual.aguardando = null
            atual.estado = 'running'
            atual.mensagem = null
          }
          break
        }

        case 'state': {
          atual.estado = evento.state
          if (evento.state === 'paused') {
            atual.aguardando = null
            const preservados = evento.done ?? atual.done
            atual.mensagem = `Pausado — ${preservados} blocos preservados`
          } else if (evento.state === 'cancelled') {
            atual.aguardando = null
            atual.mensagem =
              evento.message ?? 'Cancelado — o progresso salvo no cache foi preservado.'
          } else if (evento.state === 'done') {
            atual.aguardando = null
            atual.mensagem = 'Tradução concluída.'
          } else if (evento.state === 'error') {
            atual.aguardando = null
          }
          if (evento.state === 'done' || evento.state === 'error' || evento.state === 'cancelled') {
            encerrarFluxo()
          }
          break
        }

        case 'done': {
          atual.estado = 'done'
          atual.aguardando = null
          atual.mensagem = 'Tradução concluída.'
          if (evento.report) {
            atual.relatorio = evento.report
          }
          if (evento.integrity && evento.integrity.length > 0) {
            atual.integridade = evento.integrity
          }
          atual.temSaida = evento.has_output ?? true
          encerrarFluxo()
          break
        }

        case 'error': {
          atual.estado = 'error'
          atual.aguardando = null
          atual.erro = evento.message ?? evento.error ?? 'Erro inesperado durante a tradução.'
          encerrarFluxo()
          break
        }
      }

      aplicar()
    },
    [aplicar, encerrarFluxo],
  )

  /* ------------------------------------------------------------------ */
  /* Conexão SSE (abre com o job, fecha no cleanup)                       */
  /* ------------------------------------------------------------------ */
  useEffect(() => {
    if (!jobId) {
      return
    }
    const fonte = conectarEventos(
      jobId,
      aoReceberEvento,
      () => {
        // A conexão caiu. Se o job já terminou, não há o que reconectar:
        // o EventSource ficaria reconectando em laço.
        if (acumuladoRef.current.estado === 'done'
          || acumuladoRef.current.estado === 'error'
          || acumuladoRef.current.estado === 'cancelled') {
          encerrarFluxo()
          return
        }
        // Caso contrário, o próprio EventSource tenta reconectar.
        if (!avisouQuedaRef.current) {
          avisouQuedaRef.current = true
          setAvisoConexao('Conexão com o servidor perdida — tentando reconectar…')
        }
      },
    )
    fonteRef.current = fonte

    return () => {
      fonte.close()
      if (fonteRef.current === fonte) {
        fonteRef.current = null
      }
    }
  }, [jobId, aoReceberEvento])

  /* ------------------------------------------------------------------ */
  /* Capítulos                                                            */
  /* ------------------------------------------------------------------ */
  const carregarCapitulos = useCallback(async (id: string): Promise<void> => {
    setCarregandoCapitulos(true)
    setErroCapitulos(null)
    try {
      const lista = await listarCapitulos(id)
      setCapitulos(lista)
      setCapitulo(lista.length > 0 ? lista[0].path : null)
    } catch (falha) {
      setCapitulos([])
      setCapitulo(null)
      setErroCapitulos(
        mensagemDeErro(falha, 'Não foi possível carregar os capítulos do livro.'),
      )
    } finally {
      setCarregandoCapitulos(false)
    }
  }, [])

  /* ------------------------------------------------------------------ */
  /* Preview do capítulo selecionado                                      */
  /* ------------------------------------------------------------------ */
  useEffect(() => {
    if (!jobId || !capitulo) {
      return
    }
    let cancelado = false
    setCarregandoPreview(true)
    setErroPreview(null)
    // O preview do capítulo anterior não pode ficar na tela enquanto o
    // novo carrega (a rolagem sincronizada ficaria desalinhada).
    setHtmlOriginal('')
    setHtmlTraduzido('')

    void (async (): Promise<void> => {
      try {
        const [original, traduzido, folha] = await Promise.all([
          buscarPreview(jobId, capitulo, 'original'),
          buscarPreview(jobId, capitulo, 'translated'),
          buscarCssDoEpub(jobId),
        ])
        if (cancelado) {
          return
        }
        setHtmlOriginal(original)
        setHtmlTraduzido(traduzido)
        setCss(folha)
        setErroPreview(null)
      } catch (falha) {
        if (cancelado) {
          return
        }
        setErroPreview(mensagemDeErro(falha, 'Não foi possível carregar o capítulo.'))
      } finally {
        if (!cancelado) {
          setCarregandoPreview(false)
        }
      }
    })()

    return () => {
      cancelado = true
    }
  }, [jobId, capitulo, noncePreview])

  /* ------------------------------------------------------------------ */
  /* Fotografia final do job (nome da saída + integridade)                */
  /* ------------------------------------------------------------------ */
  useEffect(() => {
    if (!jobId || ui.estado !== 'done') {
      return
    }
    let cancelado = false

    void (async (): Promise<void> => {
      try {
        const foto = await obterJob(jobId)
        if (cancelado) {
          return
        }
        const atual = acumuladoRef.current
        if (foto.output_name) {
          atual.nomeSaida = foto.output_name
        }
        atual.temSaida = foto.has_output
        if (foto.integrity && foto.integrity.length > 0) {
          atual.integridade = foto.integrity
        }
        if (foto.report) {
          atual.relatorio = foto.report
        }
        aplicar()
      } catch {
        // O evento "done" já traz o essencial: a falha aqui é cosmética.
      }
    })()

    return () => {
      cancelado = true
    }
  }, [jobId, ui.estado, aplicar])

  /* ------------------------------------------------------------------ */
  /* Ações                                                                */
  /* ------------------------------------------------------------------ */

  /** Envia o .epub e cria o job (abre o SSE pelo efeito do `jobId`). */
  const iniciarTraducao = useCallback(async (): Promise<void> => {
    if (!arquivo) {
      setErroUpload('Escolha um arquivo .epub para traduzir.')
      return
    }
    // O modelo pode ficar vazio: o back-end resolve o modelo carregado
    // no provedor. Se o provedor não tiver nenhum, o erro chega pelo SSE.

    setEnviando(true)
    setErroUpload(null)

    const config: JobConfig = {
      provider: provedor.provider,
      base_url: provedor.base_url,
      api_key: provedor.api_key,
      model: provedor.model,
      target_lang: IDIOMA_DESTINO,
      batch_size: TAMANHO_LOTE,
      temperature: TEMPERATURA,
      glossary: glossario,
      output_name: nomeSaida.trim().length > 0 ? nomeSaida.trim() : 'traducao_resultado.epub',
    }

    try {
      const criado = await criarJob(arquivo, config)
      acumuladoRef.current = estadoInicial()
      setUi(estadoInicial())
      setAvisoConexao(null)
      setJobId(criado.job_id)
      await carregarCapitulos(criado.job_id)
      // Fotografia inicial: cobre o caso de o job terminar ANTES de o SSE
      // abrir (com um provedor muito rápido, o "done" pode já ter acontecido).
      try {
        const foto = await obterJob(criado.job_id)
        const atual = acumuladoRef.current
        atual.estado = foto.state
        atual.total = foto.total
        atual.done = foto.done
        atual.translated = foto.translated
        atual.cached = foto.cached
        atual.failed = foto.failed
        atual.discarded = foto.discarded
        atual.elapsed_s = foto.elapsed_s
        atual.eta_s = foto.eta_s
        atual.arquivo = foto.current_file
        atual.bloco = foto.current_block
        atual.erro = foto.error
        atual.relatorio = foto.report
        atual.integridade = foto.integrity
        atual.nomeSaida = foto.output_name
        atual.temSaida = foto.has_output
        setUi({ ...atual })
      } catch {
        // Sem a fotografia inicial, o SSE preenche o estado de qualquer forma.
      }
    } catch (falha) {
      setErroUpload(mensagemDeErro(falha, 'Não foi possível iniciar a tradução.'))
    } finally {
      setEnviando(false)
    }
  }, [arquivo, provedor, glossario, nomeSaida, carregarCapitulos])

  /** Pede a pausa (vale entre lotes: o lote atual ainda termina). */
  const aoPausar = useCallback(async (): Promise<void> => {
    if (!jobId) {
      return
    }
    const atual = acumuladoRef.current
    atual.aguardando = 'pausa'
    aplicar()
    try {
      await pausarJob(jobId)
    } catch (falha) {
      atual.aguardando = null
      atual.erro = mensagemDeErro(falha, 'Não foi possível pausar a tradução.')
      aplicar()
    }
  }, [jobId, aplicar])

  /** Retoma a tradução: os blocos já traduzidos voltam do cache. */
  const aoRetomar = useCallback(async (): Promise<void> => {
    if (!jobId) {
      return
    }
    const atual = acumuladoRef.current
    const estadoAnterior = atual.estado
    atual.aguardando = 'retomada'
    atual.estado = 'running'
    atual.mensagem = null
    aplicar()
    try {
      await retomarJob(jobId)
    } catch (falha) {
      atual.aguardando = null
      atual.estado = estadoAnterior
      atual.erro = mensagemDeErro(falha, 'Não foi possível retomar a tradução.')
      aplicar()
    }
  }, [jobId, aplicar])

  /** Cancela a tradução (o cache gravado é preservado). */
  const aoCancelar = useCallback(async (): Promise<void> => {
    if (!jobId) {
      return
    }
    try {
      await cancelarJob(jobId)
      const atual = acumuladoRef.current
      atual.mensagem = 'Cancelando… — o progresso salvo no cache foi preservado.'
      aplicar()
    } catch (falha) {
      const atual = acumuladoRef.current
      atual.erro = mensagemDeErro(falha, 'Não foi possível cancelar a tradução.')
      aplicar()
    }
  }, [jobId, aplicar])

  /** Descarta o cache do job (ação separada do cancelamento). */
  const aoDescartarCache = useCallback(async (): Promise<void> => {
    if (!jobId) {
      return
    }
    if (!window.confirm('Descartar o cache? Todo o progresso desta traducao sera perdido.')) {
      return
    }
    try {
      await descartarCache(jobId)
      const atual = acumuladoRef.current
      atual.mensagem = 'Cache descartado — a próxima tradução refaz todos os blocos.'
      aplicar()
    } catch (falha) {
      const atual = acumuladoRef.current
      atual.erro = mensagemDeErro(falha, 'Não foi possível descartar o cache.')
      aplicar()
    }
  }, [jobId, aplicar])

  /** Baixa o EPUB traduzido. */
  const aoBaixar = useCallback(async (): Promise<void> => {
    if (!jobId) {
      return
    }
    try {
      await baixarJob(jobId, ui.nomeSaida ?? nomeSaida)
    } catch (falha) {
      const atual = acumuladoRef.current
      atual.erro = mensagemDeErro(falha, 'Não foi possível baixar o EPUB traduzido.')
      aplicar()
    }
  }, [jobId, ui.nomeSaida, nomeSaida, aplicar])

  /** Log do job (o `LogViewer` cuida do modal e do auto-scroll). */
  const carregarLog = useCallback(async (): Promise<LogResponse> => {
    if (!jobId) {
      throw new Error('Ainda não há tradução em andamento — o log fica disponível após o upload.')
    }
    return await buscarLog(jobId)
  }, [jobId])

  /** Escolhe o arquivo .epub. */
  const aoEscolherArquivo = useCallback((evento: ChangeEvent<HTMLInputElement>): void => {
    setArquivo(evento.target.files?.[0] ?? null)
    setErroUpload(null)
  }, [])

  /** Lê o glossário (YAML simples ou JSON) no browser. */
  const aoEscolherGlossario = useCallback(
    async (evento: ChangeEvent<HTMLInputElement>): Promise<void> => {
      const entrada = evento.target
      const arquivoGlossario = entrada.files?.[0]
      // Permite reenviar o mesmo arquivo depois.
      entrada.value = ''
      if (!arquivoGlossario) {
        return
      }
      try {
        const conteudo = await arquivoGlossario.text()
        const termos = lerGlossario(conteudo)
        setGlossario(termos)
        setNomeGlossario(arquivoGlossario.name)
        setErroGlossario(null)
        if (Object.keys(termos).length === 0) {
          setErroGlossario(
            'O glossário foi lido, mas nenhum termo foi encontrado — use JSON ' +
              '({"termo": "tradução"}) ou YAML simples (termo: tradução).',
          )
        }
      } catch {
        setGlossario({})
        setNomeGlossario(arquivoGlossario.name)
        setErroGlossario('Não foi possível ler o glossário (o arquivo não pôde ser lido?).')
      }
    },
    [],
  )

  /* ------------------------------------------------------------------ */
  /* Valores derivados                                                    */
  /* ------------------------------------------------------------------ */
  /** Percentual concluído (0 a 100). */
  const percentual = ui.total > 0 ? (ui.done / ui.total) * 100 : 0
  /** Título do capítulo selecionado. */
  const tituloCapitulo =
    capitulos.find((item) => item.path === capitulo)?.title ?? 'Capítulo'
  /** Quantidade de termos do glossário carregado. */
  const totalTermos = Object.keys(glossario).length
  /** `true` quando o EPUB traduzido pode ser baixado. */
  const podeBaixar = ui.estado === 'done' && ui.temSaida
  /** `true` quando há job (a faixa do progresso só existe com job). */
  const temJob = jobId !== null

  return (
    <div className="flex min-h-full flex-col bg-fundo text-texto">
      {/* ============================================================= */}
      {/* FAIXA 1 — CABEÇALHO                                            */}
      {/* ============================================================= */}
      <header className="border-b border-borda bg-surface">
        <div className="mx-auto max-w-7xl px-4 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <span
                className="inline-block h-2.5 w-2.5 rounded-full bg-acento"
                aria-hidden="true"
              />
              <h1 className="text-lg font-semibold tracking-tight">Tradutor de EPUB</h1>
              <span className="hidden text-sm text-suave sm:inline">
                tradução local com modelos OpenAI-compatíveis
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {podeBaixar && (
                <button
                  type="button"
                  onClick={() => {
                    void aoBaixar()
                  }}
                  className="rounded-md border border-sucesso/60 bg-sucesso/10 px-3 py-1.5 text-xs font-medium text-sucesso transition-colors hover:bg-sucesso/20"
                  title="Baixar o EPUB traduzido"
                >
                  Baixar EPUB
                </button>
              )}
              <LogViewer carregar={carregarLog} rotulo="Ver log" />
            </div>
          </div>

          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <ProviderSelector
                provider={provedor.provider}
                base_url={provedor.base_url}
                api_key={provedor.api_key}
                model={provedor.model}
                onChange={alterarProvedor}
              />
            </div>

            {/* Upload do livro + glossário */}
            <section className="rounded-lg border border-borda bg-surface p-4">
              <h2 className="text-sm font-semibold text-texto">Livro</h2>

              <label htmlFor="campo-epub" className="mb-1 mt-3 block text-xs font-medium text-suave">
                Arquivo .epub
              </label>
              <input
                id="campo-epub"
                type="file"
                accept=".epub,application/epub+zip"
                onChange={aoEscolherArquivo}
                className={CLASSE_ARQUIVO}
              />

              <label htmlFor="campo-saida" className="mb-1 mt-3 block text-xs font-medium text-suave">
                Nome do arquivo de saída
              </label>
              <input
                id="campo-saida"
                type="text"
                value={nomeSaida}
                spellCheck={false}
                onChange={(evento) => setNomeSaida(evento.target.value)}
                className={CLASSE_CAMPO}
                placeholder="traducao_resultado.epub"
              />

              <button
                type="button"
                onClick={() => {
                  void iniciarTraducao()
                }}
                disabled={enviando || !arquivo}
                className={`${CLASSE_BOTAO_PRINCIPAL} mt-3 w-full`}
                title="Envia o livro e inicia a tradução"
              >
                {enviando ? 'Enviando…' : 'Traduzir'}
              </button>

              {erroUpload && <p className="mt-2 text-xs text-erro">{erroUpload}</p>}

              {/* Glossário de nomes próprios */}
              <div className="mt-4 border-t border-borda pt-3">
                <label
                  htmlFor="campo-glossario"
                  className="mb-1 block text-xs font-medium text-suave"
                >
                  Glossário de nomes próprios
                </label>
                <input
                  id="campo-glossario"
                  type="file"
                  accept=".yaml,.yml,.json,.txt"
                  onChange={(evento) => {
                    void aoEscolherGlossario(evento)
                  }}
                  className={CLASSE_ARQUIVO}
                />
                <p className="mt-1 text-[11px] leading-relaxed text-suave">
                  Nomes próprios não devem ser traduzidos. Exemplo:{' '}
                  <code className="text-texto">Man-God: Man-God</code>. Aceita JSON
                  ou YAML simples — o formato é reconhecido pelo conteúdo do arquivo.
                </p>
                {nomeGlossario && (
                  <p className="mt-1 text-[11px] text-suave">
                    {nomeGlossario} — {totalTermos}{' '}
                    {totalTermos === 1 ? 'termo carregado' : 'termos carregados'}
                    {totalTermos > 0 && (
                      <>
                        {' '}
                        · {totalTermos}{' '}
                        {totalTermos === 1
                          ? 'termo será enviado ao modelo'
                          : 'termos serão enviados ao modelo'}
                      </>
                    )}
                  </p>
                )}
                {erroGlossario && <p className="mt-1 text-[11px] text-erro">{erroGlossario}</p>}
              </div>
            </section>
          </div>
        </div>
      </header>

      {/* ============================================================= */}
      {/* FAIXA 2 — BARRA DE PROGRESSO                                   */}
      {/* ============================================================= */}
      {temJob && (
        <section className="border-b border-borda bg-fundo">
          <div className="mx-auto max-w-7xl space-y-4 px-4 py-4">
            <ProgressBar
              percent={percentual}
              eta_s={ui.eta_s}
              elapsed_s={ui.elapsed_s}
              estado={ui.estado}
              pausando={ui.aguardando === 'pausa'}
              onPausar={() => {
                void aoPausar()
              }}
              onRetomar={() => {
                void aoRetomar()
              }}
              onCancelar={() => {
                void aoCancelar()
              }}
            />

            <MetricsPanel
              translated={ui.translated}
              cached={ui.cached}
              failed={ui.failed}
              discarded={ui.discarded}
              detalhes={ui.detalhes}
            />

            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-xs text-suave">
                {ui.arquivo ? (
                  <>
                    Capítulo atual: <span className="text-texto">{ui.arquivo}</span> — bloco{' '}
                    <span className="text-texto">{ui.bloco ?? 0}</span> de{' '}
                    <span className="text-texto">{ui.total}</span>
                  </>
                ) : (
                  'Preparando a tradução…'
                )}
              </p>
              <button
                type="button"
                onClick={() => {
                  void aoDescartarCache()
                }}
                className={CLASSE_BOTAO}
                title="Apaga o cache deste job: todo o progresso é perdido"
              >
                Descartar cache
              </button>
            </div>

            <p className="text-[11px] leading-relaxed text-suave">
              Cada lote de {TAMANHO_LOTE} blocos leva cerca de 90 s no LM Studio: a barra só
              avança quando o lote termina — isso é normal, a tradução continua trabalhando.
            </p>

            {ui.aguardando === 'pausa' && (
              <p className="text-xs text-aviso">
                Pausando… — o lote que está em andamento ainda termina antes de parar.
              </p>
            )}
            {ui.aguardando === 'retomada' && (
              <p className="text-xs text-aviso">
                Retomando… — os blocos já traduzidos voltam do cache.
              </p>
            )}
            {ui.mensagem && <p className="text-xs text-suave">{ui.mensagem}</p>}
            {ui.erro && <p className="text-xs text-erro">{ui.erro}</p>}
            {avisoConexao && <p className="text-xs text-aviso">{avisoConexao}</p>}
          </div>
        </section>
      )}

      {/* ============================================================= */}
      {/* FAIXA 3 — ÁREA PRINCIPAL                                        */}
      {/* ============================================================= */}
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        {!temJob ? (
          <div className="rounded-lg border border-borda bg-surface p-6">
            <h2 className="text-sm font-semibold text-texto">Como usar</h2>
            <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm text-suave">
              <li>Escolha o provedor de modelos (LM Studio local ou API compatível com OpenAI).</li>
              <li>Selecione o arquivo .epub e clique em “Traduzir”.</li>
              <li>
                Acompanhe o progresso em tempo real; um lote leva cerca de 90 s, então a barra
                fica parada de vez em quando.
              </li>
              <li>Ao final, baixe o EPUB traduzido e confira a verificação de integridade.</li>
            </ol>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
            <div className="min-w-0">
              <ChapterNav
                capitulos={capitulos}
                ativo={capitulo}
                onSelecionar={(caminho) => setCapitulo(caminho)}
                carregando={carregandoCapitulos}
              />
              {erroCapitulos && <p className="mt-2 text-xs text-erro">{erroCapitulos}</p>}
            </div>

            <div className="min-w-0 space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold text-texto">{tituloCapitulo}</h2>
                <button
                  type="button"
                  onClick={() => setNoncePreview((valor) => valor + 1)}
                  className={CLASSE_BOTAO}
                  title="Recarrega o preview (o lado traduzido usa o cache atual)"
                >
                  Atualizar preview
                </button>
              </div>

              <SideBySide
                htmlOriginal={htmlOriginal}
                htmlTraduzido={htmlTraduzido}
                css={css}
                carregando={carregandoPreview}
                erro={erroPreview}
              />

              {(ui.estado === 'done' || ui.integridade.length > 0) && (
                <IntegrityPanel itens={ui.integridade} />
              )}
            </div>
          </div>
        )}
      </main>

      <footer className="border-t border-borda px-4 py-3 text-center text-xs text-suave">
        Tradutor de EPUB — tudo roda localmente.
      </footer>
    </div>
  )
}
