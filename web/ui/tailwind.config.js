/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Tema escuro (ver .forja/knowledge/frontend.md)
        fundo: '#0b0f14',
        surface: '#111823',
        borda: '#1f2a37',
        texto: '#e5edf5',
        suave: '#8fa3b8',
        acento: '#38bdf8',
        sucesso: '#34d399',
        aviso: '#fbbf24',
        erro: '#f87171',
      },
    },
  },
  plugins: [],
}
