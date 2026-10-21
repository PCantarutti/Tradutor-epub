# Front-end (web/ui)

Interface web do Tradutor de EPUB: React 18 + Vite + TypeScript + Tailwind v3,
falando com o back-end FastAPI (`web/server.py`) por `/api`.

A documentação completa de uso (incluindo como subir o back-end com
`python -m uvicorn web.server:app --port 8000`) está no
[README da raiz](../../README.md#interface-visual-web).

## Comandos

```bash
npm install       # instala as dependências
npm run dev       # dev server em http://localhost:5173 (proxy de /api -> :8000)
npm run build     # tsc -b && vite build  -> gera dist/ (servido pelo back-end)
npm run preview   # serve o build de dist/
```

O proxy só existe em desenvolvimento: em produção o próprio back-end serve
`web/ui/dist` na mesma origem, então as chamadas relativas a `/api` funcionam
sem configuração adicional.

## Estrutura

| Arquivo | Responsabilidade |
|---|---|
| `src/types.ts` | Tipos do contrato da API (job, eventos SSE, capítulos, config). |
| `src/api.ts` | Cliente HTTP tipado + `conectarEventos` (SSE via `EventSource`). |
| `src/App.tsx` | Layout da aplicação (a UI completa entra na tarefa seguinte). |
| `src/index.css` | Diretivas do Tailwind + base do tema escuro. |

## Tema

Paleta definida em `tailwind.config.js`: fundo `#0b0f14`, surface `#111823`,
borda `#1f2a37`, texto `#e5edf5`, texto suave `#8fa3b8`, acento `#38bdf8`,
sucesso `#34d399`, aviso `#fbbf24`, erro `#f87171`.
