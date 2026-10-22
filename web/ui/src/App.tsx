/**
 * App do Tradutor de EPUB — console de acompanhamento.
 *
 * Estrutura (tela cheia, painéis separados por linhas de 1px):
 *
 * - **Trilho** à esquerda: "Nova tradução", as traduções já feitas (reabrir
 *   uma delas permite retraduzir só as falhas) e o provedor em uso.
 * - **Instrumento** no topo do job: leitura numérica + campo de barras com um
 *   traço por bloco do livro, ao vivo.
 * - **Bancada**: capítulos à esquerda e o preview original × tradução, que se
 *   atualiza bloco a bloco sem mexer na rolagem.
 * - **Doca** inferior: log ao vivo, integridade e provedor.
 *
 * O estado do job chega pelo SSE (`GET /api/jobs/{id}/events`), é acumulado
 * num `useRef` e aplicado ao React no máximo a cada 100 ms.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ButtonHTMLAttributes, ChangeEvent, DragEvent, ReactElement, ReactNode } from 'react'

import {
  baixarJob,
  buscarPreview,
  cancelarJob,
  conectarEventos,
  criarJob,
  descartarCache,
  listarCapitulos,
  listarJobs,
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
  JobSnapshot,
  JobState,
  ProviderConfig,
  Report,
  SseEvent,
} from './types'
import CampoDeBlocos from './components/CampoDeBlocos'
import type { CapituloDoCampo, EstadoBloco } from './components/CampoDeBlocos'
import ChapterNav from './components/ChapterNav'
import type { ItemCapitulo } from './components/ChapterNav'
import Icone from './components/Icone'
import type { NomeIcone } from './components/Icone'
import IntegrityPanel from './components/IntegrityPanel'
import LogAoVivo from './components/LogAoVivo'
import ProviderSelector from './components/ProviderSelector'
import SideBySide from './components/SideBySide'

/* ======================================================================== */
/* Constantes                                                               */
/* ======================================================================== */

/** Intervalo mínimo entre duas atualizações do React (ms). */
const INTERVALO_ATUALIZACAO_MS = 100
/** Intervalo mínimo entre duas recargas do preview ao vivo (ms). */
const INTERVALO_PREVIEW_MS = 2500
/** Blocos por chamada ao modelo. */
const TAMANHO_LOTE = 5
/** Temperatura da tradução. */
const TEMPERATURA = 0.25
/** Idioma de destino. */
const IDIOMA_DESTINO = 'pt-BR'
/** Chave do localStorage onde o provedor (com a api_key) é lembrado. */
const CHAVE_PROVEDOR_SALVO = 'tradutor-epub:provedor'

type MotivoEspera = 'pausa' | 'retomada'
type AbaDoca = 'log' | 'integridade' | 'provedor'

/* ======================================================================== */
/* Estado do job                                                            */
/* ======================================================================== */

interface EstadoJob {
  estado: JobState
  aguardando: MotivoEspera | null
  mensagem: string | null
  erro: string | null
  total: number
  done: number
  translated: number
  cached: number
  failed: number
  discarded: number
  elapsed_s: number
  eta_s: number | null
  arquivo: string | null
  bloco: number | null
  detalhes: Partial<Record<BlockStatus, string>>
  relatorio: Report | null
  integridade: IntegrityItem[]
  nomeSaida: string | null
  temSaida: boolean
  modelo: string | null
}

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
    modelo: null,
  }
}

/** Copia a fotografia do back-end para o acumulador do SSE. */
function aplicarFotografia(atual: EstadoJob, foto: JobSnapshot): void {
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
  atual.modelo = foto.model
}

/* ======================================================================== */
/* Utilidades                                                               */
/* ======================================================================== */

function mensagemDeErro(falha: unknown, padrao: string): string {
  if (falha instanceof Error && falha.message && falha.message.length > 0) {
    return falha.message
  }
  return padrao
}

/**
 * Converte o conteúdo de um glossário em `{termo: tradução}`. O formato é
 * reconhecido pelo CONTEÚDO (JSON primeiro, YAML simples como reserva),
 * nunca pelo nome do arquivo.
 */
function lerGlossario(conteudo: string): Record<string, string> {
  const termos: Record<string, string> = {}
  try {
    const dados: unknown = JSON.parse(conteudo)
    if (dados && typeof dados === 'object' && !Array.isArray(dados)) {
      for (const [chave, valor] of Object.entries(dados as Record<string, unknown>)) {
        const termo = chave.trim()
        if (termo.length > 0) {
          termos[termo] = String(valor)
        }
      }
      return termos
    }
  } catch {
    // Não é JSON: cai no parser YAML simples.
  }
  for (const linha of conteudo.split(/\r?\n/)) {
    const limpa = linha.trim()
    if (limpa.length === 0 || limpa.startsWith('#')) {
      continue
    }
    const separador = limpa.indexOf(':')
    if (separador <= 0) {
      continue
    }
    const termo = limpa.slice(0, separador).trim().replace(/^["']|["']$/g, '')
    const traducao = limpa.slice(separador + 1).trim().replace(/^["']|["']$/g, '')
    if (termo.length > 0) {
      termos[termo] = traducao
    }
  }
  return termos
}

async function buscarCssDoEpub(jobId: string): Promise<string> {
  const resposta = await fetch(urlDoStyle(jobId))
  if (!resposta.ok) {
    throw new Error('Não foi possível carregar a folha de estilo do livro.')
  }
  return await resposta.text()
}

/** `hh:mm:ss`. */
function relogio(segundos: number): string {
  const s = Math.max(0, Math.round(segundos))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return [h, m, s % 60].map((n) => String(n).padStart(2, '0')).join(':')
}

const formatoNumero = new Intl.NumberFormat('pt-BR')
const n = (valor: number): string => formatoNumero.format(valor)

/** Nome legível do livro a partir do nome do arquivo de saída. */
function nomeDoLivro(saida: string | null | undefined): string {
  if (!saida) {
    return 'Tradução'
  }
  return saida.replace(/\.epub$/i, '').replace(/\s*\(traduzido\)\s*$/i, '')
}

/** Separa o volume do título ("… Volume-16" → "Vol. 16"), para o trilho. */
function partesDoNome(nome: string): { titulo: string; volume: string | null } {
  const m = /[\s\-–—:,]*\b(?:vol(?:ume)?\.?)[\s\-]*(\d+)\b/i.exec(nome)
  if (!m) {
    return { titulo: nome, volume: null }
  }
  const titulo = (nome.slice(0, m.index) + nome.slice(m.index + m[0].length)).trim()
  return { titulo: titulo || nome, volume: `Vol. ${m[1]}` }
}

/** `21/10 18:39` a partir do id do job (`YYYYmmdd-HHMMSS-xxxxxx`). */
function dataDoJob(id: string): string {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(id)
  return m ? `${m[3]}/${m[2]} ${m[4]}:${m[5]}` : id
}

const ROTULO_ESTADO: Record<JobState, string> = {
  running: 'Traduzindo',
  paused: 'Pausado',
  done: 'Concluído',
  error: 'Erro',
  cancelled: 'Cancelado',
}

const COR_PONTO: Record<JobState, string> = {
  running: 'bg-atual',
  paused: 'bg-aviso',
  done: 'bg-sucesso',
  error: 'bg-erro',
  cancelled: 'bg-suave',
}

const ESTADO_DO_EVENTO: Record<BlockStatus, EstadoBloco> = {
  traduzido: 'traduzido',
  cache: 'cache',
  falhou: 'falhou',
  descartado: 'descartado',
}

/* ======================================================================== */
/* Peças de interface                                                       */
/* ======================================================================== */

type Variante = 'primario' | 'neutro' | 'perigo' | 'aviso' | 'sucesso' | 'atual'

const CLASSE_VARIANTE: Record<Variante, string> = {
  primario: 'border-suave/60 bg-elevado font-semibold text-texto hover:border-texto/70',
  neutro: 'border-borda bg-elevado text-texto hover:border-suave/50',
  perigo: 'border-borda bg-transparent text-suave hover:border-erro/70 hover:text-erro',
  aviso: 'border-aviso/50 bg-aviso/10 text-aviso hover:bg-aviso/20',
  sucesso: 'border-sucesso/50 bg-sucesso/10 text-sucesso hover:bg-sucesso/20',
  atual: 'border-atual/60 bg-atual/15 text-atual hover:bg-atual/25',
}

function Botao({
  icone,
  children,
  variante = 'neutro',
  className = '',
  ...props
}: {
  icone?: NomeIcone
  children: ReactNode
  variante?: Variante
} & ButtonHTMLAttributes<HTMLButtonElement>): ReactElement {
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex h-8 items-center gap-2 whitespace-nowrap rounded border px-3 font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${CLASSE_VARIANTE[variante]} ${className}`}
    >
      {icone && <Icone nome={icone} className="h-3.5 w-3.5" />}
      {children}
    </button>
  )
}

const CLASSE_CAMPO =
  'h-8 w-full rounded border border-borda bg-fundo px-2.5 text-texto placeholder:text-suave ' +
  'focus:border-suave focus:outline-none'

/* ======================================================================== */
/* App                                                                      */
/* ======================================================================== */

export default function App(): ReactElement {
  /* ---------------- Provedor (salvo neste navegador, inclusive a chave) --- */
  const [provedor, setProvedor] = useState<ProviderConfig>(() => {
    const padrao: ProviderConfig = {
      provider: 'lmstudio',
      base_url: 'http://localhost:1234/v1',
      api_key: '',
      model: '',
    }
    try {
      const salvo = window.localStorage.getItem(CHAVE_PROVEDOR_SALVO)
      return salvo ? { ...padrao, ...(JSON.parse(salvo) as Partial<ProviderConfig>) } : padrao
    } catch {
      return padrao
    }
  })

  useEffect(() => {
    try {
      window.localStorage.setItem(CHAVE_PROVEDOR_SALVO, JSON.stringify(provedor))
    } catch {
      // Sem armazenamento (aba privada etc.): só não lembra entre sessões.
    }
  }, [provedor])

  const alterarProvedor = useCallback((patch: Partial<ProviderConfig>): void => {
    setProvedor((atual) => ({ ...atual, ...patch }))
  }, [])

  /* ---------------- Formulário de nova tradução --------------------------- */
  const [arquivo, setArquivo] = useState<File | null>(null)
  const [nomeSaida, setNomeSaida] = useState('')
  const [enviando, setEnviando] = useState(false)
  const [erroUpload, setErroUpload] = useState<string | null>(null)
  const [arrastando, setArrastando] = useState(false)
  const [glossario, setGlossario] = useState<Record<string, string>>({})
  const [nomeGlossario, setNomeGlossario] = useState<string | null>(null)
  const [erroGlossario, setErroGlossario] = useState<string | null>(null)

  /* ---------------- Job ---------------------------------------------------- */
  const [jobId, setJobId] = useState<string | null>(null)
  const [ui, setUi] = useState<EstadoJob>(estadoInicial())
  const [nonceSse, setNonceSse] = useState(0)
  const [jobsRecentes, setJobsRecentes] = useState<JobSnapshot[]>([])
  const acumuladoRef = useRef<EstadoJob>(estadoInicial())
  const temporizadorRef = useRef<number | null>(null)
  const fonteRef = useRef<EventSource | null>(null)
  const avisouQuedaRef = useRef(false)
  const [avisoConexao, setAvisoConexao] = useState<string | null>(null)

  /* ---------------- Campo de blocos (estado de cada bloco do livro) ------- */
  const campoRef = useRef<Map<string, EstadoBloco[]>>(new Map())
  const chegadasRef = useRef<Map<string, number>>(new Map())
  const [versaoCampo, setVersaoCampo] = useState(0)

  /* ---------------- Capítulos e preview ----------------------------------- */
  const [capitulos, setCapitulos] = useState<Chapter[]>([])
  const [capitulo, setCapitulo] = useState<string | null>(null)
  const [carregandoCapitulos, setCarregandoCapitulos] = useState(false)
  const [erroCapitulos, setErroCapitulos] = useState<string | null>(null)
  const [htmlOriginal, setHtmlOriginal] = useState('')
  const [htmlTraduzido, setHtmlTraduzido] = useState('')
  const [css, setCss] = useState('')
  const [carregandoPreview, setCarregandoPreview] = useState(false)
  const [erroPreview, setErroPreview] = useState<string | null>(null)
  const [irPara, setIrPara] = useState<{ indice: number; n: number; suave?: boolean } | null>(null)
  /** "Seguir tradução": o preview acompanha o bloco em tradução. */
  const [seguindo, setSeguindo] = useState(false)
  const ultimaRecargaRef = useRef(0)
  const recargaPendenteRef = useRef<number | null>(null)

  /* ---------------- Doca -------------------------------------------------- */
  const [aba, setAba] = useState<AbaDoca>('log')
  const [docaAberta, setDocaAberta] = useState(true)
  const [soProblemas, setSoProblemas] = useState(false)

  /* ======================================================================== */
  /* Throttle do SSE                                                          */
  /* ======================================================================== */
  const aplicar = useCallback((): void => {
    if (temporizadorRef.current !== null) {
      return
    }
    temporizadorRef.current = window.setTimeout(() => {
      temporizadorRef.current = null
      setUi({ ...acumuladoRef.current })
      setVersaoCampo((v) => v + 1)
    }, INTERVALO_ATUALIZACAO_MS)
  }, [])

  useEffect(
    () => () => {
      if (temporizadorRef.current !== null) {
        window.clearTimeout(temporizadorRef.current)
      }
      if (recargaPendenteRef.current !== null) {
        window.clearTimeout(recargaPendenteRef.current)
      }
    },
    [],
  )

  const encerrarFluxo = useCallback((): void => {
    fonteRef.current?.close()
    fonteRef.current = null
  }, [])

  /* ======================================================================== */
  /* Preview: carga inicial do capítulo e recarga ao vivo da tradução         */
  /* ======================================================================== */
  const capituloRef = useRef<string | null>(null)
  capituloRef.current = capitulo

  /** Recarrega só o lado traduzido (o painel aplica bloco a bloco). */
  const recarregarTraducao = useCallback(async (): Promise<void> => {
    const id = jobIdRef.current
    const cap = capituloRef.current
    if (!id || !cap) {
      return
    }
    ultimaRecargaRef.current = Date.now()
    try {
      const html = await buscarPreview(id, cap, 'translated')
      if (capituloRef.current === cap) {
        setHtmlTraduzido(html)
      }
    } catch {
      // A próxima recarga tenta de novo.
    }
  }, [])

  /** Agenda uma recarga respeitando o intervalo mínimo. */
  const agendarRecarga = useCallback((): void => {
    if (recargaPendenteRef.current !== null) {
      return
    }
    const espera = Math.max(0, INTERVALO_PREVIEW_MS - (Date.now() - ultimaRecargaRef.current))
    recargaPendenteRef.current = window.setTimeout(() => {
      recargaPendenteRef.current = null
      void recarregarTraducao()
    }, espera)
  }, [recarregarTraducao])

  const jobIdRef = useRef<string | null>(null)
  jobIdRef.current = jobId

  /* ======================================================================== */
  /* Eventos do SSE                                                           */
  /* ======================================================================== */
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
          atual.erro = null
          if (atual.aguardando === 'retomada') {
            atual.aguardando = null
            atual.estado = 'running'
            atual.mensagem = null
          }
          // Campo: o bloco ganha o estado do evento.
          const blocos = campoRef.current.get(evento.file)
          if (blocos && evento.block_index >= 1 && evento.block_index <= blocos.length) {
            const novo = ESTADO_DO_EVENTO[evento.status]
            // "cache" de um bloco já traduzido antes não precisa de brilho.
            if (novo === 'traduzido') {
              chegadasRef.current.set(`${evento.file}#${evento.block_index}`, performance.now())
            }
            blocos[evento.block_index - 1] = novo
          }
          // Preview ao vivo do capítulo aberto.
          if (evento.file === capituloRef.current && evento.status === 'traduzido') {
            agendarRecarga()
          }
          break
        }

        case 'state': {
          atual.estado = evento.state
          if (evento.state === 'paused') {
            atual.aguardando = null
            atual.mensagem = `Pausado — ${n(evento.done ?? atual.done)} blocos preservados no cache.`
          } else if (evento.state === 'cancelled') {
            atual.aguardando = null
            atual.mensagem = evento.message ?? 'Cancelado — o que já foi traduzido ficou no cache.'
          } else if (evento.state === 'done') {
            atual.aguardando = null
            atual.mensagem = null
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
          atual.mensagem = null
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
    [aplicar, encerrarFluxo, agendarRecarga],
  )

  /* SSE: abre com o job, reabre quando o nonce muda (retomada). */
  useEffect(() => {
    if (!jobId) {
      return
    }
    const estado = acumuladoRef.current.estado
    if (nonceSse === 0 && (estado === 'done' || estado === 'error' || estado === 'cancelled')) {
      // Job já encerrado ao ser aberto: nada vai chegar pelo SSE.
      return
    }
    const fonte = conectarEventos(jobId, aoReceberEvento, () => {
      const e = acumuladoRef.current.estado
      if (e === 'done' || e === 'error' || e === 'cancelled') {
        encerrarFluxo()
        return
      }
      if (!avisouQuedaRef.current) {
        avisouQuedaRef.current = true
        setAvisoConexao('Conexão com o servidor perdida — tentando reconectar…')
      }
    })
    fonteRef.current = fonte
    return () => {
      fonte.close()
      if (fonteRef.current === fonte) {
        fonteRef.current = null
      }
    }
  }, [jobId, aoReceberEvento, nonceSse, encerrarFluxo])

  /* ======================================================================== */
  /* Capítulos (e o campo, que nasce das máscaras do cache)                   */
  /* ======================================================================== */
  const carregarCapitulos = useCallback(async (id: string): Promise<void> => {
    setCarregandoCapitulos(true)
    setErroCapitulos(null)
    try {
      const lista = await listarCapitulos(id)
      const mapa = new Map<string, EstadoBloco[]>()
      for (const item of lista) {
        const mascara = item.mask ?? ''
        mapa.set(
          item.path,
          Array.from({ length: item.blocks }, (_, i): EstadoBloco => (mascara[i] === '1' ? 'cache' : 'pendente')),
        )
      }
      campoRef.current = mapa
      chegadasRef.current = new Map()
      setVersaoCampo((v) => v + 1)
      setCapitulos(lista)
      setCapitulo((atual) => {
        if (atual && lista.some((item) => item.path === atual)) {
          return atual
        }
        return (lista.find((item) => item.blocks > 0) ?? lista[0])?.path ?? null
      })
    } catch (falha) {
      setCapitulos([])
      setErroCapitulos(mensagemDeErro(falha, 'Não foi possível carregar os capítulos do livro.'))
    } finally {
      setCarregandoCapitulos(false)
    }
  }, [])

  /* Preview: carga completa ao trocar de capítulo. */
  useEffect(() => {
    if (!jobId || !capitulo) {
      return
    }
    let cancelado = false
    setCarregandoPreview(true)
    setErroPreview(null)
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
        ultimaRecargaRef.current = Date.now()
        setHtmlOriginal(original)
        setHtmlTraduzido(traduzido)
        setCss(folha)
      } catch (falha) {
        if (!cancelado) {
          setErroPreview(mensagemDeErro(falha, 'Não foi possível carregar o capítulo.'))
        }
      } finally {
        if (!cancelado) {
          setCarregandoPreview(false)
        }
      }
    })()
    return () => {
      cancelado = true
    }
  }, [jobId, capitulo])

  /* Fim do job: fotografia final, máscaras do cache e preview atualizado. */
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
        atual.nomeSaida = foto.output_name
        atual.temSaida = foto.has_output
        if (foto.integrity?.length) {
          atual.integridade = foto.integrity
        }
        if (foto.report) {
          atual.relatorio = foto.report
        }
        aplicar()
      } catch {
        // O evento "done" já trouxe o essencial.
      }
    })()
    return () => {
      cancelado = true
    }
  }, [jobId, ui.estado, aplicar])

  /* ======================================================================== */
  /* Traduções recentes                                                       */
  /* ======================================================================== */
  const atualizarRecentes = useCallback(async (): Promise<void> => {
    try {
      setJobsRecentes(await listarJobs())
    } catch {
      // Conveniência: sem a lista a interface continua funcionando.
    }
  }, [])

  useEffect(() => {
    if (!jobId || ui.estado !== 'running') {
      void atualizarRecentes()
    }
  }, [jobId, ui.estado, atualizarRecentes])

  /* ======================================================================== */
  /* Ações                                                                    */
  /* ======================================================================== */
  const abrirJob = useCallback(
    async (id: string): Promise<void> => {
      try {
        const foto = await obterJob(id)
        encerrarFluxo()
        const atual = estadoInicial()
        aplicarFotografia(atual, foto)
        acumuladoRef.current = atual
        setUi({ ...atual })
        setAvisoConexao(null)
        setCapitulo(null)
        setCapitulos([])
        setNonceSse(0)
        setJobId(id)
        await carregarCapitulos(id)
      } catch (falha) {
        setErroUpload(mensagemDeErro(falha, 'Não foi possível abrir esta tradução.'))
      }
    },
    [carregarCapitulos, encerrarFluxo],
  )

  const novaTraducao = useCallback((): void => {
    encerrarFluxo()
    setJobId(null)
    setCapitulos([])
    setCapitulo(null)
    acumuladoRef.current = estadoInicial()
    setUi(estadoInicial())
    void atualizarRecentes()
  }, [encerrarFluxo, atualizarRecentes])

  const iniciarTraducao = useCallback(async (): Promise<void> => {
    if (!arquivo) {
      setErroUpload('Escolha um arquivo .epub para traduzir.')
      return
    }
    setEnviando(true)
    setErroUpload(null)
    const saida = nomeSaida.trim() || arquivo.name.replace(/\.epub$/i, '') + ' (traduzido).epub'
    const config: JobConfig = {
      provider: provedor.provider,
      base_url: provedor.base_url,
      api_key: provedor.api_key,
      model: provedor.model,
      target_lang: IDIOMA_DESTINO,
      batch_size: TAMANHO_LOTE,
      temperature: TEMPERATURA,
      glossary: glossario,
      output_name: saida,
    }
    try {
      const criado = await criarJob(arquivo, config)
      encerrarFluxo()
      acumuladoRef.current = estadoInicial()
      acumuladoRef.current.nomeSaida = saida
      setUi({ ...acumuladoRef.current })
      setAvisoConexao(null)
      setCapitulo(null)
      setNonceSse(0)
      setJobId(criado.job_id)
      setArquivo(null)
      await carregarCapitulos(criado.job_id)
      try {
        const foto = await obterJob(criado.job_id)
        aplicarFotografia(acumuladoRef.current, foto)
        setUi({ ...acumuladoRef.current })
      } catch {
        // O SSE preenche o estado de qualquer forma.
      }
      void atualizarRecentes()
    } catch (falha) {
      setErroUpload(mensagemDeErro(falha, 'Não foi possível iniciar a tradução.'))
    } finally {
      setEnviando(false)
    }
  }, [arquivo, nomeSaida, provedor, glossario, carregarCapitulos, encerrarFluxo, atualizarRecentes])

  /** Retoma (pausado) ou retraduz o que falta (parado): o cache cobre o resto. */
  const retomar = useCallback(
    async (mensagem: string): Promise<void> => {
      if (!jobId) {
        return
      }
      const atual = acumuladoRef.current
      const anterior = atual.estado
      atual.aguardando = 'retomada'
      atual.estado = 'running'
      atual.erro = null
      atual.mensagem = mensagem
      // Pendentes voltam a ser "pendentes" (não falhas) durante a nova rodada.
      for (const blocos of campoRef.current.values()) {
        blocos.forEach((estado, i) => {
          if (estado === 'falhou' || estado === 'descartado') {
            blocos[i] = 'pendente'
          }
        })
      }
      aplicar()
      try {
        await retomarJob(jobId, provedor.api_key || undefined)
        setNonceSse((v) => v + 1)
      } catch (falha) {
        atual.aguardando = null
        atual.estado = anterior
        atual.mensagem = null
        atual.erro =
          mensagemDeErro(falha, 'Não foi possível retomar.') +
          ' Se o servidor foi reiniciado, confira a chave em Provedor.'
        aplicar()
      }
    },
    [jobId, aplicar, provedor.api_key],
  )

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
      atual.erro = mensagemDeErro(falha, 'Não foi possível pausar.')
      aplicar()
    }
  }, [jobId, aplicar])

  const aoCancelar = useCallback(async (): Promise<void> => {
    if (!jobId || !window.confirm('Cancelar a tradução? O que já foi traduzido fica no cache.')) {
      return
    }
    try {
      await cancelarJob(jobId)
      acumuladoRef.current.mensagem = 'Cancelando…'
      aplicar()
    } catch (falha) {
      acumuladoRef.current.erro = mensagemDeErro(falha, 'Não foi possível cancelar.')
      aplicar()
    }
  }, [jobId, aplicar])

  const aoDescartarCache = useCallback(async (): Promise<void> => {
    if (
      !jobId ||
      !window.confirm('Descartar o cache? Tudo o que foi traduzido neste job terá de ser refeito.')
    ) {
      return
    }
    try {
      await descartarCache(jobId)
      acumuladoRef.current.mensagem = 'Cache descartado — a próxima rodada refaz todos os blocos.'
      aplicar()
      await carregarCapitulos(jobId)
    } catch (falha) {
      acumuladoRef.current.erro = mensagemDeErro(falha, 'Não foi possível descartar o cache.')
      aplicar()
    }
  }, [jobId, aplicar, carregarCapitulos])

  const aoBaixar = useCallback(async (): Promise<void> => {
    if (!jobId) {
      return
    }
    try {
      await baixarJob(jobId, ui.nomeSaida ?? 'traducao.epub')
    } catch (falha) {
      acumuladoRef.current.erro = mensagemDeErro(falha, 'Não foi possível baixar o EPUB.')
      aplicar()
    }
  }, [jobId, ui.nomeSaida, aplicar])

  const escolherArquivo = useCallback((lista: FileList | null): void => {
    const escolhido = lista?.[0] ?? null
    if (escolhido && !/\.epub$/i.test(escolhido.name)) {
      setErroUpload('Esse arquivo não é um .epub.')
      return
    }
    setArquivo(escolhido)
    setErroUpload(null)
  }, [])

  const aoEscolherGlossario = useCallback(async (evento: ChangeEvent<HTMLInputElement>): Promise<void> => {
    const entrada = evento.target
    const escolhido = entrada.files?.[0]
    entrada.value = ''
    if (!escolhido) {
      return
    }
    try {
      const termos = lerGlossario(await escolhido.text())
      setGlossario(termos)
      setNomeGlossario(escolhido.name)
      setErroGlossario(
        Object.keys(termos).length === 0
          ? 'Nenhum termo encontrado — use JSON ({"termo": "tradução"}) ou YAML simples (termo: tradução).'
          : null,
      )
    } catch {
      setGlossario({})
      setNomeGlossario(escolhido.name)
      setErroGlossario('Não foi possível ler o glossário.')
    }
  }, [])

  /* ======================================================================== */
  /* Derivados                                                                */
  /* ======================================================================== */
  const rodando = ui.estado === 'running' || ui.estado === 'paused'
  const parado = ui.estado === 'done' || ui.estado === 'cancelled' || ui.estado === 'error'

  // Contagens por capítulo, ao vivo (vêm do campo).
  const itensCapitulos: ItemCapitulo[] = useMemo(
    () =>
      capitulos.map((c) => {
        const blocos = campoRef.current.get(c.path) ?? []
        const traduzidos = blocos.filter((e) => e === 'cache' || e === 'traduzido').length
        return {
          path: c.path,
          index: c.index,
          title: c.title,
          blocos: c.blocks,
          traduzidos,
          semTraducao: c.blocks - traduzidos,
        }
      }),
    // versaoCampo: o campo é mutável (ref) e só a versão avisa da mudança.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [capitulos, versaoCampo],
  )

  const capitulosDoCampo: CapituloDoCampo[] = useMemo(
    () =>
      capitulos
        .filter((c) => c.blocks > 0)
        .map((c) => ({ path: c.path, title: c.title, blocos: campoRef.current.get(c.path) ?? [] })),
    [capitulos],
  )

  const semTraducao = itensCapitulos.reduce((soma, c) => soma + c.semTraducao, 0)
  const totalBlocos = itensCapitulos.reduce((soma, c) => soma + c.blocos, 0)
  const prontos = totalBlocos - semTraducao
  const percentual = totalBlocos > 0 ? (prontos / totalBlocos) * 100 : 0
  const podeRetraduzir = parado && ui.aguardando === null && semTraducao > 0
  const podeBaixar = ui.temSaida && parado
  const ritmo = ui.elapsed_s > 30 && ui.translated > 0 ? (ui.translated / ui.elapsed_s) * 60 : null

  // Bloco em tradução: o seguinte ao último concluído (o lote inteiro está no modelo).
  const atual =
    ui.estado === 'running' && ui.arquivo && ui.bloco !== null
      ? { path: ui.arquivo, indice: Math.min(ui.bloco + 1, campoRef.current.get(ui.arquivo)?.length ?? ui.bloco) }
      : null

  // Seguir: abre o capítulo em tradução e leva a vista até o bloco atual.
  const alvoSeguir = seguindo && atual ? `${atual.path}#${atual.indice}` : null
  useEffect(() => {
    if (!alvoSeguir) {
      return
    }
    const [path, indice] = [alvoSeguir.slice(0, alvoSeguir.lastIndexOf('#')), Number(alvoSeguir.slice(alvoSeguir.lastIndexOf('#') + 1))]
    if (path !== capituloRef.current) {
      setCapitulo(path)
    }
    setIrPara({ indice, n: Date.now(), suave: path === capituloRef.current })
  }, [alvoSeguir])

  // Fim da tradução: não há mais o que seguir.
  useEffect(() => {
    if (ui.estado !== 'running') {
      setSeguindo(false)
    }
  }, [ui.estado])

  const capituloAtivo = capitulos.find((c) => c.path === capitulo) ?? null
  const itemAtivo = itensCapitulos.find((c) => c.path === capitulo) ?? null

  /* ======================================================================== */
  /* Render                                                                   */
  /* ======================================================================== */
  return (
    <div className="grid h-full grid-cols-[15.5rem_minmax(0,1fr)] bg-fundo">
      {/* ================================================================ */}
      {/* TRILHO                                                            */}
      {/* ================================================================ */}
      <aside className="flex min-h-0 flex-col border-r border-borda bg-surface">
        <div className="flex h-12 shrink-0 items-center gap-2.5 border-b border-borda px-4">
          <svg viewBox="0 0 18 14" className="h-3.5 w-[18px] text-texto" aria-hidden="true">
            {[0, 2, 3, 5, 8, 9, 11, 14, 16].map((x, i) => (
              <rect key={x} x={x} y="0" width={i % 3 === 0 ? 1.4 : 0.8} height="14" fill="currentColor" />
            ))}
          </svg>
          <span className="font-semibold tracking-tight">Tradutor de EPUB</span>
        </div>

        <div className="border-b border-borda p-3">
          <Botao
            icone="mais"
            variante={jobId ? 'neutro' : 'primario'}
            className="w-full justify-center"
            onClick={novaTraducao}
          >
            Nova tradução
          </Botao>
        </div>

        <div className="flex h-9 shrink-0 items-center justify-between px-4">
          <h2 className="painel-titulo">Traduções</h2>
          <button
            type="button"
            onClick={() => void atualizarRecentes()}
            className="text-suave transition-colors hover:text-texto"
            title="Atualizar a lista"
            aria-label="Atualizar a lista de traduções"
          >
            <Icone nome="atualizar" className="h-3.5 w-3.5" />
          </button>
        </div>
        <ul className="min-h-0 flex-1 overflow-y-auto pb-2">
          {jobsRecentes.length === 0 && (
            <li className="px-4 py-2 text-suave">As traduções que você fizer aparecem aqui.</li>
          )}
          {jobsRecentes.map((job) => {
            const aberto = job.job_id === jobId
            const falhas = job.failed + job.discarded
            const estado = aberto ? ui.estado : job.state
            return (
              <li key={job.job_id}>
                <button
                  type="button"
                  onClick={() => void abrirJob(job.job_id)}
                  className={`block w-full px-4 py-2 text-left transition-colors ${aberto ? 'bg-elevado' : 'hover:bg-elevado/60'}`}
                >
                  <span className="flex items-center gap-2">
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${COR_PONTO[estado]}`} />
                    <span
                      className={`min-w-0 flex-1 truncate ${aberto ? 'text-texto' : 'text-texto/85'}`}
                      title={nomeDoLivro(job.output_name)}
                    >
                      {partesDoNome(nomeDoLivro(job.output_name)).titulo}
                    </span>
                    {partesDoNome(nomeDoLivro(job.output_name)).volume && (
                      <span className="num shrink-0 font-mono text-[11px] text-texto/85">
                        {partesDoNome(nomeDoLivro(job.output_name)).volume}
                      </span>
                    )}
                  </span>
                  <span className="num mt-0.5 flex gap-2 pl-3.5 text-[11px] text-suave">
                    <span>{dataDoJob(job.job_id)}</span>
                    <span>·</span>
                    <span>{ROTULO_ESTADO[estado].toLowerCase()}</span>
                    {falhas > 0 && !aberto && <span className="text-erro">· {n(falhas)} falhas</span>}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>

        <button
          type="button"
          onClick={() => {
            if (jobId) {
              setAba('provedor')
              setDocaAberta(true)
            }
          }}
          className="flex shrink-0 items-center gap-2.5 border-t border-borda px-4 py-3 text-left transition-colors hover:bg-elevado/60"
          title="Provedor de modelos em uso"
        >
          <Icone nome="chave" className="h-3.5 w-3.5 shrink-0 text-suave" />
          <span className="min-w-0 flex-1">
            <span className={`block truncate ${provedor.model ? 'text-texto' : 'text-suave'}`}>
              {provedor.model || 'nenhum modelo escolhido'}
            </span>
            <span className="block truncate font-mono text-[11px] text-suave">
              {provedor.base_url.replace(/^https?:\/\//, '')}
              {provedor.api_key ? ' · chave salva' : ''}
            </span>
          </span>
        </button>
      </aside>

      {/* ================================================================ */}
      {/* PRINCIPAL                                                         */}
      {/* ================================================================ */}
      {!jobId ? (
        <main className="min-h-0 overflow-y-auto">
          <div className="flex h-12 items-center border-b border-borda px-6">
            <h1 className="text-[15px] font-semibold">Nova tradução</h1>
          </div>

          <div className="grid max-w-[84rem] grid-cols-2 gap-px border-b border-borda bg-borda">
            {/* Livro */}
            <section className="bg-fundo p-6">
              <h2 className="text-[15px] font-semibold">Livro</h2>
              <p className="mt-1 text-suave">O EPUB é traduzido inteiro para {IDIOMA_DESTINO}, preservando imagens, estilo e índice.</p>

              <label
                onDragOver={(e: DragEvent) => {
                  e.preventDefault()
                  setArrastando(true)
                }}
                onDragLeave={() => setArrastando(false)}
                onDrop={(e: DragEvent) => {
                  e.preventDefault()
                  setArrastando(false)
                  escolherArquivo(e.dataTransfer.files)
                }}
                className={`mt-5 flex cursor-pointer flex-col items-center justify-center gap-2 rounded border border-dashed px-6 py-10 text-center transition-colors ${
                  arrastando ? 'border-texto/70 bg-elevado' : arquivo ? 'border-sucesso/50 bg-sucesso/5' : 'border-borda hover:border-suave/60'
                }`}
              >
                <input
                  type="file"
                  accept=".epub,application/epub+zip"
                  className="sr-only"
                  onChange={(e) => escolherArquivo(e.target.files)}
                />
                <Icone nome="livro" className={`h-6 w-6 ${arquivo ? 'text-sucesso' : 'text-suave'}`} />
                {arquivo ? (
                  <>
                    <span className="text-texto">{arquivo.name}</span>
                    <span className="num text-[11px] text-suave">
                      {(arquivo.size / 1024 / 1024).toFixed(1).replace('.', ',')} MB · clique para trocar
                    </span>
                  </>
                ) : (
                  <>
                    <span className="text-texto">Arraste o .epub aqui</span>
                    <span className="text-[11px] text-suave">ou clique para escolher</span>
                  </>
                )}
              </label>

              <div className="mt-5 grid grid-cols-2 gap-4">
                <div>
                  <label htmlFor="campo-saida" className="mb-1.5 block text-[12px] text-suave">
                    Nome do arquivo traduzido
                  </label>
                  <input
                    id="campo-saida"
                    type="text"
                    value={nomeSaida}
                    spellCheck={false}
                    onChange={(e) => setNomeSaida(e.target.value)}
                    className={CLASSE_CAMPO}
                    placeholder={arquivo ? arquivo.name.replace(/\.epub$/i, '') + ' (traduzido).epub' : 'automático'}
                  />
                </div>
                <div>
                  <span className="mb-1.5 block text-[12px] text-suave">Glossário de nomes próprios</span>
                  <label className={`${CLASSE_CAMPO} flex cursor-pointer items-center gap-2 hover:border-suave/60`}>
                    <input
                      type="file"
                      accept=".yaml,.yml,.json,.txt"
                      className="sr-only"
                      onChange={(e) => void aoEscolherGlossario(e)}
                    />
                    <span className={`min-w-0 flex-1 truncate ${nomeGlossario ? 'text-texto' : 'text-suave'}`}>
                      {nomeGlossario ?? 'opcional — JSON ou YAML'}
                    </span>
                    {nomeGlossario && (
                      <span className="num shrink-0 text-[11px] text-suave">
                        {Object.keys(glossario).length} termos
                      </span>
                    )}
                  </label>
                  {erroGlossario && <p className="mt-1 text-[12px] text-erro">{erroGlossario}</p>}
                </div>
              </div>

              <div className="mt-6 flex items-center gap-3">
                <Botao
                  variante="primario"
                  icone="retomar"
                  disabled={enviando || !arquivo}
                  onClick={() => void iniciarTraducao()}
                  className="h-9 px-4"
                >
                  {enviando ? 'Enviando…' : 'Traduzir'}
                </Botao>
                <span className="text-[12px] text-suave">
                  Lotes de {TAMANHO_LOTE} blocos · {provedor.model || 'modelo carregado no provedor'}
                </span>
              </div>
              {erroUpload && <p className="mt-3 text-erro">{erroUpload}</p>}
            </section>

            {/* Provedor */}
            <section className="bg-fundo">
              <ProviderSelector
                provider={provedor.provider}
                base_url={provedor.base_url}
                api_key={provedor.api_key}
                model={provedor.model}
                onChange={alterarProvedor}
              />
            </section>
          </div>
        </main>
      ) : (
        <main className="grid min-h-0 grid-rows-[auto_auto_minmax(0,1fr)_auto]">
          {/* ---------------- Barra do job ------------------------------- */}
          <header className="flex h-12 items-center gap-3 border-b border-borda px-5">
            <span className={`h-2 w-2 shrink-0 rounded-full ${COR_PONTO[ui.estado]} ${ui.estado === 'running' ? 'animate-pulso motion-reduce:animate-none' : ''}`} />
            <h1 className="min-w-0 truncate text-[15px] font-semibold" title={ui.nomeSaida ?? ''}>
              {nomeDoLivro(ui.nomeSaida)}
            </h1>
            <span className="shrink-0 text-suave">
              {ui.aguardando === 'pausa'
                ? 'pausando ao fim do lote…'
                : ui.aguardando === 'retomada'
                  ? 'retomando…'
                  : ROTULO_ESTADO[ui.estado].toLowerCase()}
              {ui.modelo ? ` · ${ui.modelo}` : ''}
            </span>

            <div className="ml-auto flex items-center gap-2">
              {ui.estado === 'running' && (
                <Botao
                  icone="seguir"
                  variante={seguindo ? 'atual' : 'neutro'}
                  aria-pressed={seguindo}
                  onClick={() => setSeguindo((v) => !v)}
                  title="O preview abre o capítulo em tradução e acompanha o bloco atual; rolar à mão desliga"
                >
                  {seguindo ? 'Seguindo a tradução' : 'Seguir tradução'}
                </Botao>
              )}
              {ui.estado === 'running' && (
                <Botao icone="pausar" onClick={() => void aoPausar()} disabled={ui.aguardando === 'pausa'}
                  title="A pausa vale entre lotes: o lote atual ainda termina">
                  Pausar
                </Botao>
              )}
              {ui.estado === 'paused' && (
                <Botao icone="retomar" variante="primario" onClick={() => void retomar('Retomando — o que já foi traduzido vem do cache.')}>
                  Retomar
                </Botao>
              )}
              {podeRetraduzir && (
                <Botao
                  icone="refazer"
                  variante="aviso"
                  onClick={() => void retomar('Retraduzindo só os blocos que faltam — o resto vem do cache.')}
                  title="Manda ao modelo só os blocos sem tradução; o resto vem do cache"
                >
                  Retraduzir {n(semTraducao)} {semTraducao === 1 ? 'bloco' : 'blocos'}
                </Botao>
              )}
              {podeBaixar && (
                <Botao icone="baixar" variante={semTraducao === 0 ? 'sucesso' : 'neutro'} onClick={() => void aoBaixar()}>
                  Baixar EPUB
                </Botao>
              )}
              {rodando && (
                <Botao icone="parar" variante="perigo" onClick={() => void aoCancelar()} className="ml-3">
                  Cancelar
                </Botao>
              )}
            </div>
          </header>

          {/* ---------------- Instrumento -------------------------------- */}
          <section className="grid grid-cols-[13.5rem_minmax(0,1fr)] border-b border-borda bg-surface">
            {/* Margem numérica: quanto falta, num relance. */}
            <div className="flex flex-col border-r border-borda px-5 py-4">
              <div>
                <div className="num font-mono text-[54px] font-light leading-none tracking-[-0.03em] text-texto">
                  {percentual.toFixed(1).replace('.', ',')}
                  <span className="text-[26px] text-suave">%</span>
                </div>
                <div className="num mt-2 font-mono text-[12px] text-suave">
                  {n(prontos)} / {n(totalBlocos)} blocos
                </div>
                {(ui.failed > 0 || ui.discarded > 0 || (ui.estado === 'done' && semTraducao > 0)) && (
                  <div className="num mt-2 flex flex-wrap gap-x-3 font-mono text-[15px]">
                    {ui.failed > 0 && <span className="text-erro">{n(ui.failed)} falhas</span>}
                    {ui.discarded > 0 && <span className="text-aviso">{n(ui.discarded)} descartados</span>}
                    {ui.failed === 0 && ui.discarded === 0 && (
                      <span className="text-erro">{n(semTraducao)} sem tradução</span>
                    )}
                  </div>
                )}
              </div>
              <dl className="mt-5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px]">
                {ui.estado === 'running' && (
                  <>
                    <dt className="text-suave">restante</dt>
                    <dd className="num text-right font-mono text-texto">
                      {ui.eta_s !== null ? `~${relogio(ui.eta_s)}` : '—'}
                    </dd>
                  </>
                )}
                <dt className="text-suave">decorrido</dt>
                <dd className="num text-right font-mono text-texto">{relogio(ui.elapsed_s)}</dd>
                {ritmo !== null && (
                  <>
                    <dt className="text-suave">ritmo</dt>
                    <dd className="num text-right font-mono text-texto">{ritmo.toFixed(0)} blocos/min</dd>
                  </>
                )}
              </dl>
            </div>

            <div className="min-w-0 px-5 pb-3 pt-4">
              {capitulosDoCampo.length > 0 ? (
                <CampoDeBlocos
                  capitulos={capitulosDoCampo}
                  versao={versaoCampo}
                  rodando={rodando}
                  concluido={ui.estado === 'done'}
                  atual={atual}
                  chegadas={chegadasRef.current}
                  onEscolher={(path, indice) => {
                    setSeguindo(false)
                    setCapitulo(path)
                    setIrPara({ indice, n: Date.now() })
                  }}
                />
              ) : (
                <div className="h-44 bg-borda/40" />
              )}

              {/* Legenda = contadores: cada cor com a sua contagem. */}
              <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-[12px] text-suave">
                {(
                  [
                    ['bg-sucesso', 'traduzidos agora', n(ui.translated), 'text-sucesso', undefined],
                    ['bg-cache', 'do cache', n(ui.cached), 'text-cache', undefined],
                    ['bg-erro', 'falhas', n(ui.failed), ui.failed > 0 ? 'text-erro' : 'text-texto', ui.detalhes.falhou],
                    ['bg-aviso', 'descartados', n(ui.discarded), ui.discarded > 0 ? 'text-aviso' : 'text-texto', ui.detalhes.descartado],
                    ...(semTraducao > 0
                      ? [[
                          ui.estado === 'done' ? 'bg-erro' : rodando ? 'bg-[#2b3139]' : 'bg-[#333a44]',
                          ui.estado === 'done' ? 'sem tradução' : rodando ? 'por traduzir' : 'não traduzidos',
                          n(semTraducao),
                          ui.estado === 'done' ? 'text-erro' : 'text-texto',
                          undefined,
                        ] as const]
                      : []),
                  ] as const
                ).map(([cor, rotulo, valor, corValor, titulo]) => (
                  <span key={rotulo} className="inline-flex items-center gap-1.5" title={titulo}>
                    <span className={`inline-block h-3 w-[3px] ${cor}`} />
                    {rotulo}
                    <span className={`num font-mono ${corValor}`}>{valor}</span>
                  </span>
                ))}
                {ui.estado === 'running' && (
                  <span className="inline-flex items-center gap-1.5">
                    <span className="inline-block h-3 w-[3px] bg-atual" />
                    em tradução
                  </span>
                )}
                <span className="ml-auto">
                  {ui.mensagem || ui.erro || avisoConexao ? (
                    <span className={ui.erro ? 'text-erro' : avisoConexao ? 'text-aviso' : 'text-texto'}>
                      {ui.erro ?? avisoConexao ?? ui.mensagem}
                    </span>
                  ) : (
                    'clique num traço para abrir o bloco'
                  )}
                </span>
              </div>
            </div>
          </section>

          {/* ---------------- Bancada: capítulos + preview -------------- */}
          <div className="grid min-h-0 grid-cols-[17rem_minmax(0,1fr)]">
            <nav className="flex min-h-0 flex-col border-r border-borda" aria-label="Capítulos">
              <div className="flex h-9 shrink-0 items-center justify-between border-b border-borda px-4">
                <h2 className="painel-titulo">Capítulos</h2>
                <span className="num text-[11px] text-suave">{capitulos.length}</span>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto">
                <ChapterNav
                  itens={itensCapitulos}
                  ativo={capitulo}
                  emTraducao={ui.estado === 'running' ? ui.arquivo : null}
                  rodando={rodando}
                  concluido={ui.estado === 'done'}
                  onSelecionar={setCapitulo}
                  carregando={carregandoCapitulos}
                />
                {erroCapitulos && <p className="px-4 py-2 text-erro">{erroCapitulos}</p>}
              </div>
            </nav>

            <div className="min-h-0 min-w-0">
              {capituloAtivo ? (
                <SideBySide
                  capitulo={capituloAtivo.path}
                  htmlOriginal={htmlOriginal}
                  htmlTraduzido={htmlTraduzido}
                  css={css}
                  carregando={carregandoPreview}
                  erro={erroPreview}
                  blocoAtual={atual && atual.path === capituloAtivo.path ? atual.indice : null}
                  irPara={irPara}
                  pendentes={itemAtivo?.semTraducao ?? 0}
                  aoRolarManual={() => setSeguindo(false)}
                  rodando={rodando}
                  concluido={ui.estado === 'done'}
                />
              ) : (
                <p className="p-6 text-suave">Escolha um capítulo para comparar.</p>
              )}
            </div>
          </div>

          {/* ---------------- Doca --------------------------------------- */}
          <section className="flex min-h-0 flex-col border-t border-borda bg-surface">
            <div className="flex h-9 shrink-0 items-center gap-1 px-2">
              {(
                [
                  ['log', 'log', 'Log'],
                  ['integridade', 'escudo', 'Integridade'],
                  ['provedor', 'chave', 'Provedor'],
                ] as const
              ).map(([id, icone, rotulo]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => {
                    setAba(id)
                    setDocaAberta(aba === id ? !docaAberta : true)
                  }}
                  className={`inline-flex h-7 items-center gap-1.5 rounded px-2.5 transition-colors ${
                    aba === id && docaAberta ? 'bg-elevado text-texto' : 'text-suave hover:text-texto'
                  }`}
                >
                  <Icone nome={icone} className="h-3.5 w-3.5" />
                  {rotulo}
                  {id === 'integridade' && ui.integridade.length > 0 && (
                    <span className={`num text-[11px] ${ui.integridade.every((i) => i.ok) ? 'text-sucesso' : 'text-erro'}`}>
                      {ui.integridade.filter((i) => i.ok).length}/{ui.integridade.length}
                    </span>
                  )}
                </button>
              ))}
              {aba === 'log' && docaAberta && (
                <label className="ml-3 inline-flex cursor-pointer items-center gap-1.5 text-[12px] text-suave">
                  <input
                    type="checkbox"
                    checked={soProblemas}
                    onChange={(e) => setSoProblemas(e.target.checked)}
                    className="accent-suave"
                  />
                  só avisos e erros
                </label>
              )}
              <div className="ml-auto flex items-center gap-1">
                {parado && (
                  <button
                    type="button"
                    onClick={() => void aoDescartarCache()}
                    className="mr-2 text-[12px] text-suave transition-colors hover:text-erro"
                  >
                    Descartar cache
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setDocaAberta((v) => !v)}
                  className="rounded p-1.5 text-suave transition-colors hover:text-texto"
                  aria-label={docaAberta ? 'Recolher o painel' : 'Abrir o painel'}
                  title={docaAberta ? 'Recolher' : 'Abrir'}
                >
                  <Icone nome="seguir" className={`h-3.5 w-3.5 transition-transform ${docaAberta ? '' : 'rotate-180'}`} />
                </button>
              </div>
            </div>
            {docaAberta && (
              <div className="h-44 min-h-0 border-t border-borda bg-fundo">
                {aba === 'log' && <LogAoVivo jobId={jobId} rodando={ui.estado === 'running'} soProblemas={soProblemas} />}
                {aba === 'integridade' && (
                  <div className="h-full overflow-y-auto">
                    <IntegrityPanel itens={ui.integridade} />
                  </div>
                )}
                {aba === 'provedor' && (
                  <div className="h-full overflow-y-auto">
                    <ProviderSelector
                      provider={provedor.provider}
                      base_url={provedor.base_url}
                      api_key={provedor.api_key}
                      model={provedor.model}
                      onChange={alterarProvedor}
                    />
                  </div>
                )}
              </div>
            )}
          </section>
        </main>
      )}
    </div>
  )
}
