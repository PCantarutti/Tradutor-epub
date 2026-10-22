/**
 * Ícones da interface: SVG de 16px, traço de 1.5px, mesma grade para todos.
 */
import type { ReactElement } from 'react'

/** Nomes dos ícones disponíveis. */
export type NomeIcone =
  | 'livro'
  | 'mais'
  | 'pausar'
  | 'retomar'
  | 'parar'
  | 'refazer'
  | 'baixar'
  | 'atualizar'
  | 'chave'
  | 'log'
  | 'escudo'
  | 'fechar'
  | 'seguir'
  | 'enviar'

/** Traçados de cada ícone (viewBox 0 0 16 16). */
const TRACOS: Record<NomeIcone, ReactElement> = {
  livro: (
    <>
      <path d="M3 2.5h7.5a2 2 0 0 1 2 2v9H5a2 2 0 0 1-2-2v-9Z" />
      <path d="M3 11.5a2 2 0 0 1 2-2h7.5" />
    </>
  ),
  mais: <path d="M8 3v10M3 8h10" />,
  pausar: <path d="M5.5 3.5v9M10.5 3.5v9" />,
  retomar: <path d="M5 3.2v9.6L12.5 8 5 3.2Z" />,
  parar: <rect x="4" y="4" width="8" height="8" rx="1" />,
  refazer: (
    <>
      <path d="M13 8a5 5 0 1 1-1.5-3.55" />
      <path d="M13 2.5v3h-3" />
    </>
  ),
  baixar: (
    <>
      <path d="M8 2.5v8M4.5 7 8 10.5 11.5 7" />
      <path d="M3 13.5h10" />
    </>
  ),
  atualizar: (
    <>
      <path d="M2.5 8a5.5 5.5 0 0 1 9.4-3.9L13.5 5.5" />
      <path d="M13.5 2.5v3h-3" />
      <path d="M13.5 8a5.5 5.5 0 0 1-9.4 3.9L2.5 10.5" />
      <path d="M2.5 13.5v-3h3" />
    </>
  ),
  chave: (
    <>
      <circle cx="5.5" cy="10.5" r="3" />
      <path d="m7.6 8.4 5.4-5.4M11 5l1.5 1.5" />
    </>
  ),
  log: <path d="M3 4h10M3 8h10M3 12h6" />,
  escudo: (
    <>
      <path d="M8 2 3 4v4c0 3 2.2 5 5 6 2.8-1 5-3 5-6V4L8 2Z" />
      <path d="m5.8 8 1.6 1.6L10.5 6.5" />
    </>
  ),
  fechar: <path d="m4 4 8 8M12 4l-8 8" />,
  seguir: (
    <>
      <path d="M8 2.5v11M4.5 10 8 13.5 11.5 10" />
    </>
  ),
  enviar: (
    <>
      <path d="M8 13.5v-9M4.5 8 8 4.5 11.5 8" />
      <path d="M3 2.5h10" />
    </>
  ),
}

/** Ícone SVG em linha; herda a cor do texto (`currentColor`). */
export default function Icone({
  nome,
  className = 'h-4 w-4',
}: {
  nome: NomeIcone
  className?: string
}): ReactElement {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {TRACOS[nome]}
    </svg>
  )
}
