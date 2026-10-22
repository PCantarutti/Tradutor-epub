/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Console em grafite: painéis separados por linhas de 1px, cor só para
        // estado (ver o contrato de direção em index.html).
        fundo: '#0f1114',
        surface: '#15181c',
        elevado: '#1b1f25',
        borda: '#262b33',
        texto: '#dde2e8',
        suave: '#8d96a2',
        sucesso: '#7cc4a4',
        cache: '#6f87a6',
        aviso: '#e5a34a',
        erro: '#e57373',
        atual: '#c9a6f2',
      },
      fontFamily: {
        sans: ['"Segoe UI Variable Text"', '"Segoe UI"', 'system-ui', 'sans-serif'],
        mono: ['"Cascadia Mono"', '"Cascadia Code"', 'Consolas', 'ui-monospace', 'monospace'],
        serif: ['Georgia', 'Cambria', '"Times New Roman"', 'serif'],
      },
      keyframes: {
        pulso: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.35' },
        },
      },
      animation: {
        pulso: 'pulso 1.4s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
