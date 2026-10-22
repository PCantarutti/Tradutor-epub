---
name: Tradutor de EPUB
description: Console escuro de acompanhamento de tradução, com o livro inteiro como um campo de barras ao vivo.
colors:
  fundo: "#0f1114"
  surface: "#15181c"
  elevado: "#1b1f25"
  borda: "#262b33"
  texto: "#dde2e8"
  suave: "#8d96a2"
  sucesso: "#7cc4a4"
  cache: "#6f87a6"
  cache-barra: "#566c88"
  aviso: "#e5a34a"
  erro: "#e57373"
  atual: "#c9a6f2"
  por-traduzir: "#2b3139"
  nao-traduzido: "#333a44"
typography:
  display:
    fontFamily: "Cascadia Mono, Cascadia Code, Consolas, ui-monospace, monospace"
    fontSize: "54px"
    fontWeight: 300
    lineHeight: 1
    letterSpacing: "-0.03em"
    fontFeature: "tnum"
  display-unit:
    fontFamily: "Cascadia Mono, Cascadia Code, Consolas, ui-monospace, monospace"
    fontSize: "26px"
    fontWeight: 300
    lineHeight: 1
  headline:
    fontFamily: "Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
  body:
    fontFamily: "Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  label:
    fontFamily: "Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    letterSpacing: "0.08em"
  caption:
    fontFamily: "Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
  numeric:
    fontFamily: "Cascadia Mono, Cascadia Code, Consolas, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
    fontFeature: "tnum"
  numeric-alert:
    fontFamily: "Cascadia Mono, Cascadia Code, Consolas, ui-monospace, monospace"
    fontSize: "15px"
    fontWeight: 400
    fontFeature: "tnum"
  reading:
    fontFamily: "Georgia, Cambria, Times New Roman, serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.7
rounded:
  sm: "4px"
  full: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  2xl: "24px"
components:
  button-primario:
    backgroundColor: "{colors.elevado}"
    textColor: "{colors.texto}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "32px"
  button-neutro:
    backgroundColor: "{colors.elevado}"
    textColor: "{colors.texto}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "32px"
  button-perigo:
    backgroundColor: "transparent"
    textColor: "{colors.suave}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "32px"
  button-perigo-hover:
    textColor: "{colors.erro}"
  button-aviso:
    textColor: "{colors.aviso}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "32px"
  button-sucesso:
    textColor: "{colors.sucesso}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "32px"
  button-atual:
    textColor: "{colors.atual}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "32px"
  input:
    backgroundColor: "{colors.fundo}"
    textColor: "{colors.texto}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "32px"
  tab-doca:
    textColor: "{colors.suave}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "28px"
  tab-doca-ativa:
    backgroundColor: "{colors.elevado}"
    textColor: "{colors.texto}"
  list-item-ativo:
    backgroundColor: "{colors.elevado}"
    textColor: "{colors.texto}"
    padding: "8px 16px"
  tooltip:
    backgroundColor: "{colors.elevado}"
    textColor: "{colors.texto}"
    typography: "{typography.numeric}"
    rounded: "{rounded.sm}"
    padding: "4px 8px"
---

# Design System: Tradutor de EPUB

## Overview

**Creative North Star: "O Console de Bancada"**

Um console de desenvolvedor em grafite, feito para ser lido de relance por quem volta à máquina vinte minutos depois. A tela inteira é uma bancada de painéis costurados por linhas de 1px: trilho de traduções à esquerda, instrumento no topo (percentual em mono e o campo de barras), capítulos e preview lado a lado no meio, log na doca inferior. Nada flutua, nada é cartão; a estrutura é o próprio grid.

A cor não decora: ela só codifica estado. O cinza grafite é o silêncio de base e cada matiz tem um significado único (verde traduzido, aço cache, vermelho falha real, âmbar descarte, violeta "acontecendo agora"). Por isso a ação primária é neutra e elevada, não azul: se um botão fosse colorido, ele competiria com o estado do livro. A densidade é alta (corpo de 13px, controles de 32px), porque é ferramenta, não vitrine.

Três vozes tipográficas dividem o trabalho: Segoe UI fala a interface, Cascadia Mono fala números e log, Georgia fala o livro. Elas nunca trocam de papel.

**Key Characteristics:**
- Painéis planos separados por costuras de 1px (`borda`), sem sombra e sem cartão.
- Cor exclusivamente semântica de estado; o primário é neutro.
- O campo de barras é o maior objeto do viewport do job.
- Números sempre em mono tabular; texto do livro sempre em serifa.
- Movimento mínimo e funcional (pulso do bloco atual, brilho de chegada), desligado por `prefers-reduced-motion`.

## Colors

Um grafite frio quase neutro (matiz ~256) com cinco cores de estado saturadas o bastante para ler num traço de 2px.

### Primary
- **Neutro Elevado** (`elevado`): o primário do sistema não tem matiz. A ação principal ("Traduzir", "Retomar", "Nova tradução") usa o fundo elevado com borda `suave` a 60% e peso 600; o que a distingue é contraste de borda e peso, nunca cor.

### Secondary
- **Violeta Agora** (`atual`): exclusivo do que está acontecendo neste instante: o traço do bloco em tradução (pulsando), o ponto do capítulo em tradução, o ponto do job rodando, o botão "Seguir tradução" ativo e o realce do bloco atual no preview (fundo violeta a 16% + contorno de 1px a 80%, deslocado 3px).

### Tertiary (cores de estado)
- **Verde Traduzido** (`sucesso`): bloco traduzido nesta rodada, régua de progresso do capítulo, integridade OK, "Baixar EPUB" quando nada falta. Um bloco recém-chegado acende do branco e assenta no verde em ~1,1 s no campo; no preview, um brilho verde a 22% some em 1,6 s.
- **Aço Cache** (`cache` para texto e legenda, `cache-barra` para o traço no campo): bloco vindo do cache. O traço usa o aço mais escuro para que o verde "agora" se destaque sobre o "já tinha".
- **Âmbar Descarte** (`aviso`): blocos descartados, ação "Retraduzir N blocos", estado pausado, conexão perdida, avisos no log.
- **Vermelho Falha** (`erro`): falha real; e, só depois de um job concluído, o que ficou "sem tradução".

### Neutral
- **Grafite** (`fundo`): fundo da aplicação, dos campos de formulário, do preview e do log.
- **Superfície** (`surface`): trilho, faixa do instrumento e barra da doca; um degrau acima do fundo.
- **Elevado** (`elevado`): item selecionado em listas, aba ativa da doca, botões, tooltip.
- **Costura** (`borda`): todas as divisões de painel (1px), bordas de controle, trilho da régua do capítulo, polegar da barra de rolagem.
- **Texto** (`texto`): texto principal; variações a 80–85% para itens não selecionados e linhas INFO do log.
- **Suave** (`suave`): rótulos, metadados, títulos de painel, unidades ("%"), marca de início de capítulo no campo.
- **Por Traduzir** (`por-traduzir`): bloco pendente enquanto a tradução roda.
- **Não Traduzido** (`nao-traduzido`): bloco pendente num job interrompido (cancelado ou com erro): não tentado, portanto não é falha.

### Named Rules
**The Cor É Estado Rule.** Toda cor fora da escala de grafite significa um estado de bloco ou de job. Nenhuma cor é usada para marca, ênfase decorativa ou "destaque". Se uma cor nova não corresponde a um estado, ela não entra.

**The Vermelho Honesto Rule.** Vermelho só para falha real. Pendente durante a execução é `por-traduzir`; pendente num job interrompido é `nao-traduzido`; só depois de "Concluído" o que falta vira vermelho "sem tradução". O mesmo vale para preview (`[data-pending]` neutro a 45–55% de opacidade, ondulado vermelho só com `preview-concluido`), lista de capítulos e legenda.

**The Sem Azul Rule.** Não existe azul de destaque. O aço do cache é o único matiz frio e é um estado, não um acento.

## Typography

**Display Font:** Cascadia Mono (com Cascadia Code, Consolas, ui-monospace)
**Body Font:** Segoe UI Variable Text (com Segoe UI, system-ui)
**Label/Mono Font:** Cascadia Mono para números e log; Georgia (com Cambria, Times New Roman) para o texto do livro.

**Character:** a interface fala em Segoe UI pequena e densa; a mono só aparece onde há número ou máquina, e então fala alto (54px light). A serifa fica reservada ao livro, para que o original e a tradução pareçam texto de leitura, não dado.

### Hierarchy
- **Display** (300, 54px, line-height 1, -0.03em, mono tabular): o percentual na margem numérica do instrumento. O "%" vai em 26px `suave`. Único uso.
- **Headline** (600, 15px): título do livro na barra do job, "Nova tradução", títulos de seção ("Livro", "Provedor").
- **Body** (400, 13px): todo o texto de interface; tamanho base do `body`.
- **Caption** (400, 12px): rótulos de campo, legenda do campo, lista de tempos (restante, decorrido, ritmo), linhas de ajuda.
- **Label** (600, 11px, 0.08em, MAIÚSCULAS, `suave`): título de painel ("Traduções", "Capítulos", "Original", "Tradução"). É o cabeçalho do próprio painel, dentro de uma faixa de 36px; nunca um sobretítulo acima de outro título.
- **Numeric** (400, 12px mono tabular, line-height 1.6): contagens, tempos, log, tooltip do campo, detalhes de integridade (11px).
- **Numeric Alert** (400, 15px mono tabular): "N falhas" / "N descartados" / "N sem tradução" logo abaixo do percentual.
- **Reading** (400, 15px Georgia, line-height 1.7, largura máxima 68ch): o HTML do capítulo nos dois painéis do preview, com o CSS do próprio EPUB escopado por cima.

### Named Rules
**The Três Vozes Rule.** Segoe para interface, Cascadia Mono só para números e log, Georgia só para o texto do livro. Rótulo em mono ou número em Segoe é erro.

**The Número Parado Rule.** Todo número que muda ao vivo usa algarismos tabulares (`font-variant-numeric: tabular-nums`) para não "dançar".

## Layout

Aplicação de tela cheia, sem rolagem de página, montada em grid e dividida por costuras de 1px.

- **Trilho** (248px fixos, `surface`, costura à direita): marca e nome (48px), "Nova tradução", lista de traduções, provedor em uso no rodapé.
- **Principal do job**, em quatro linhas: barra do job (48px), **instrumento**, **bancada** (ocupa o resto), **doca**.
- **Instrumento**: margem numérica de 216px à esquerda (percentual, contagens de alerta, tempos) + campo de barras na largura restante, com a legenda-contador logo abaixo. O campo é o maior objeto do viewport do job: altura-alvo de 176px, faixas que encolhem de 40px até 16px para caber nela, 6px entre faixas.
- **Bancada**: capítulos (272px) + preview em duas colunas iguais (original | tradução) separadas por costura.
- **Doca**: barra de abas de 36px; conteúdo de 176px (log, integridade, provedor), recolhível.
- **Nova tradução**: duas seções lado a lado (Livro | Provedor), máx. 84rem, separadas por uma costura feita de `gap: 1px` sobre fundo `borda`.

Ritmo vertical: barras de 48px, cabeçalhos de painel de 36px, controles de 32px, abas de 28px. Espaçamento interno em passos de 4px (4, 8, 12, 16, 20, 24); listas usam 8px × 16px por item, seções de formulário 24px. Plataforma é desktop; não há layout móvel.

### Named Rules
**The Campo Primeiro Rule.** No viewport do job, o campo de barras é o maior objeto. Nenhum painel novo pode empurrá-lo para baixo da dobra ou reduzi-lo abaixo de faixas de 16px.

**The Rolagem Intocável Rule.** Atualizações ao vivo nunca reposicionam a rolagem do usuário: o preview troca só os blocos cujo HTML mudou; o log só segue o fim se o usuário já estava no fim. Só uma ação explícita ("Seguir tradução", clique num traço) move a vista, e rolar à mão desliga o "Seguir".

## Elevation & Depth

Sistema plano. A profundidade vem de três degraus tonais (`fundo` → `surface` → `elevado`) e de costuras de 1px, não de sombra. A única sombra do sistema pertence ao que realmente flutua sobre o conteúdo: a dica do campo de barras.

### Shadow Vocabulary
- **Dica flutuante** (`box-shadow: 0 4px 12px rgba(0,0,0,0.45)`): tooltip do campo de barras. Nada mais recebe sombra.

### Named Rules
**The Costura Rule.** Painéis se separam por uma linha de 1px `borda` ou por um degrau tonal. Nunca sombra, nunca cartão com margem, nunca borda lateral grossa colorida.

## Shapes

Geometria reta e técnica. Cantos de 4px em controles (botões, campos, abas, tooltip, zona de soltar); painéis e seções não têm canto, encostam uns nos outros. Círculos só para pontos de estado (6–10px). O campo de barras é feito de traços de 2px de largura com passo de 3px, e cada início de capítulo ganha uma marca de 1px × 4px em `suave` acima da faixa (a "régua" do livro). A régua de progresso do capítulo é uma linha de 2px. A legenda repete o traço: 3px × 12px por cor.

## Components

### Buttons
Compactos e sóbrios; a variante comunica pela borda e pela cor de estado, nunca por preenchimento sólido.
- **Shape:** cantos de 4px, altura 32px (36px no "Traduzir"), padding horizontal 12px, ícone de 14px com 8px de vão, peso 500.
- **Primário:** fundo `elevado`, borda `suave` a 60%, texto `texto` peso 600; hover leva a borda a `texto` a 70%.
- **Neutro:** fundo `elevado`, borda `borda`; hover leva a borda a `suave` a 50%.
- **Perigo ("Cancelar"):** transparente, texto `suave`; vira vermelho só no hover. Fica afastado 12px dos demais.
- **Estado (aviso / sucesso / atual):** borda da cor a 50–60%, fundo da cor a 10–15%, texto na cor; hover sobe o fundo para 20–25%. Usados só quando a ação é o próprio estado: "Retraduzir N blocos" (âmbar), "Baixar EPUB" com 0 faltando (verde), "Seguindo a tradução" (violeta, `aria-pressed`).
- **Focus:** contorno global de 1px `texto` com 1px de afastamento. **Disabled:** 40% de opacidade.

### Inputs / Fields
- **Style:** altura 32px, fundo `fundo`, borda 1px `borda`, cantos de 4px, padding 10px; rótulo 12px `suave` acima, a 6px.
- **Focus:** a borda passa a `suave`; sem anel colorido.
- **Error / Disabled:** mensagem de 12px em `erro` abaixo do campo; desabilitado a 60%.
- **Zona de soltar o EPUB:** borda tracejada de 1px, cantos de 4px; arrastando → borda `texto` a 70% sobre `elevado`; arquivo escolhido → borda `sucesso` a 50% sobre verde a 5%.

### Navigation
- **Listas do trilho e de capítulos:** linhas de largura total, 8px × 16px, sem divisórias. Selecionado = fundo `elevado` e texto pleno; hover = `elevado` a 60%; não selecionado com texto a 80–85%. Cada linha leva ponto de estado, título truncado e uma segunda linha de metadados em 11px `suave`.
- **Abas da doca:** 28px, ícone + rótulo; ativa em `elevado`, inativa em `suave` com hover para `texto`. Clicar a aba ativa recolhe a doca.

### Campo de Blocos (componente assinatura)
O livro inteiro como um campo de traços, um por bloco, desenhado em canvas. Cor = estado (`por-traduzir`, `cache-barra`, `sucesso`, `erro`, `aviso`; `nao-traduzido` em job interrompido; `erro` para pendentes após concluir). O bloco atual pulsa em `atual` (opacidade 0,55–1); um bloco que chega acende do branco ao verde em 1,1 s. Cursor em mira; passar o mouse mostra "capítulo · bloco N · estado" na dica mono; clicar abre o capítulo no preview centrado naquele bloco. O laço de animação só roda enquanto há algo animando e para com movimento reduzido.

### Legenda-Contador
Abaixo do campo, cada cor aparece com seu traço de 3px, seu rótulo e sua contagem em mono: a legenda é o contador. Contagens zeradas ficam em `texto`; só ganham cor quando há o que contar. À direita, a mensagem de estado (erro, conexão, pausa) ou a dica "clique num traço para abrir o bloco".

### Preview Lado a Lado
Duas colunas de leitura em Georgia 15/1.7, máx. 68ch, com o CSS do EPUB escopado e cores de texto corrigidas para contraste sobre o grafite. Passar o mouse num parágrafo destaca o mesmo bloco nos dois lados (fundo `texto` a 7% + contorno de 1px a 30%). Rolagem sincronizada pela razão, não pelo pixel. O HTML ao vivo é aplicado bloco a bloco.

### Log ao Vivo
Mono 12px, line-height 1.6, hora em `suave` numa coluna fixa e mensagem colorida pelo nível (INFO `texto` 80%, WARNING `aviso`, ERROR `erro`, DEBUG `suave`). Filtro "só avisos e erros".

## Do's and Don'ts

### Do:
- **Do** separar painéis com costura de 1px `borda` ou com um degrau tonal `fundo` → `surface` → `elevado`.
- **Do** reservar toda cor saturada para estado: verde traduzido, aço cache, vermelho falha, âmbar descarte, violeta agora.
- **Do** mostrar números em Cascadia Mono com algarismos tabulares; o percentual em 54px light na margem numérica, falhas e descartes em 15px logo abaixo.
- **Do** manter o campo de barras como o maior objeto do viewport do job (faixas de 16–40px, alvo de 176px).
- **Do** marcar o bloco atual com fundo violeta a 16% + contorno de 1px, no campo e no preview.
- **Do** aplicar atualizações ao vivo sem mover a rolagem do usuário.
- **Do** desligar pulso e brilho com `prefers-reduced-motion`.

### Don't:
- **Don't** usar azul (ou qualquer matiz) como cor de destaque ou de ação primária; o primário é neutro elevado.
- **Don't** pintar de vermelho o que só não foi traduzido ainda ou não foi tentado; vermelho é falha real ou "sem tradução" de job concluído.
- **Don't** usar cartões, sombras de painel ou borda lateral grossa colorida.
- **Don't** colocar sobretítulos (eyebrows) acima de títulos; o rótulo em maiúsculas de 11px é o cabeçalho do painel, não um adorno.
- **Don't** usar mono em rótulos de interface nem serifa fora do texto do livro.
- **Don't** usar glifos de texto como ícones; ícones são SVG de 16px com traço de 1,5px na mesma grade.
