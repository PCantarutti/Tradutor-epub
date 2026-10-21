# Tradutor de EPUB via LM Studio

CLI em Python que traduz livros `.epub` inteiros usando modelos de linguagem locais do **LM Studio** (API compatível com OpenAI), preservando a estrutura do EPUB: `mimetype` como primeira entrada ZIP_STORED, imagens e CSS byte-idênticos, tags inline, índice (NCX/NAV), título e `dc:language`.

Há duas formas de usar:

- **CLI** — `python epub_translator.py livro.epub ...` (linha de comando, ideal para automação).
- **Interface web** — `python -m uvicorn web.server:app --port 8000` (interface visual com progresso em tempo real, preview lado a lado e pausa/retomada). Veja a seção [Interface visual (web)](#interface-visual-web).

## Estrutura de pastas

```
.
├── epub_translator/          # o pacote (leitura/escrita de EPUB, cliente, blocos, cache)
│   ├── blocks.py             # extração de blocos e marcadores [[n]]/[[/n]]
│   ├── cache.py              # cache JSON atômico
│   ├── cli.py                # CLI do pacote
│   ├── epub_io.py            # leitura/escrita de EPUB
│   ├── lmstudio_client.py    # cliente OpenAI-compatível + MockClient
│   ├── prompts.py            # prompt de sistema e parser da resposta
│   └── translator.py         # orquestração do pipeline
├── tests/                    # suíte de testes (pytest)
├── web/                      # camada web (back-end FastAPI + front-end React)
│   ├── server.py             # rotas da API (/api/jobs, SSE, download, preview…)
│   ├── jobs.py               # job em disco + barramento de eventos
│   ├── preview.py            # capítulos, títulos via NCX, HTML de preview
│   ├── jobs/<id>/            # dados de cada job (original, saída, cache, log)
│   └── ui/                   # front-end React + Vite + TypeScript + Tailwind
├── fixtures/                 # EPUBs de entrada usados em testes e exemplos
│   ├── fixed_input.epub
│   └── test_input.epub
├── glossarios/               # glossários {termo: tradução} em JSON
│   ├── mushoku_tensei.json   # nomes próprios de Mushoku Tensei
│   └── README.md             # como usar os glossários
├── saida/                    # TODAS as saídas de tradução geradas
│   ├── <livro>_translated.epub
│   ├── <livro>_translated.epub.log
│   └── <livro>_translated.epub.cache.json
├── epub_translator.py        # entry point do pacote
├── traduzir.py              # script de entrega (capítulo único + verificação final)
├── validacao.epub            # EPUB de validação (somente leitura, NÃO versionado)
├── FORJA.md                  # memória do projeto
└── requirements.txt
```

> ⚠️ **O `validacao.epub` não está no repositório** (é o volume 15 de *Mushoku
> Tensei*, ~17 MB, copyright de Seven Seas Entertainment — não é nosso para
> redistribuir). Os exemplos deste README usam esse nome de arquivo: aponte
> `src` para **qualquer EPUB que você tenha** (`python epub_translator.py
> meu-livro.epub ...`) ou para os EPUBs sintéticos de `fixtures/`, que ARE
> versionados. O `traduzir.py` também espera um `validacao.epub` na raiz —
> ajuste a constante no topo do script se usar outro arquivo.

**Regra da pasta `saida/`**: todo EPUB traduzido, log e cache JSON vai para
`saida/`. A pasta é criada automaticamente se não existir — tanto pelo
`traduzir.py` quanto pela CLI do pacote (`epub_translator.py`), que grava em
`saida/` ao lado do arquivo de origem. O EPUB de **entrada** nunca é
modificado.

## Como funciona

O pipeline opera em etapas:

1. **Leitura** — o EPUB é descompactado como ZIP; todos os arquivos são lidos byte-a-byte.
2. **Extração de blocos** — cada elemento de bloco (p, h1-h6, li, etc.) gera um bloco de texto; tags inline (b, i, em, strong, span, etc.) são substituídas por marcadores `[[n]]`/`[[/n]]` (ex: `[[1]]texto[[/1]]`).
3. **Envio em lotes** — blocos são agrupados em lotes de `--batch-size` blocos (padrão: 5). Cada lote é enviado como mensagem com separadores `[i]` + texto formatado.
4. **Retorno e validação** — a resposta do modelo é parseada por blocos numerados; os marcadores são validados (sem desbalanceamento e em ordem crescente); `replay` reconstrói as tags inline sobre a árvore original.
5. **Escrita** — o EPUB é recompactado com o XHTML traduzido e o `dc:language` atualizado. O `dc:title` e o NCX/nav são **preservados** (não são traduzidos).
6. **Cache de retomada** — cada bloco traduzido é salvo em `<output>.cache.json` (chave = SHA-256 dos inputs). Ao re-executar, os blocos cacheados são reutilizados automaticamente. Use `--no-cache` para ignorar o cache.

## Instalação

```bash
# 1. Python 3.10+
python --version

# 2. Ambiente virtual (recomendado)
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# 3. Dependências
pip install -r requirements.txt
```

**Dependências** (todas em `requirements.txt`): `requests`, `lxml`, `PyYAML`, `rich`,
`pytest` — mais `fastapi`, `uvicorn` e `python-multipart` para a camada web.

**Front-end** (só para a interface visual): `npm` + Node 18+. Rodando em
desenvolvimento, o Vite fica na 5173 e faz proxy de `/api` para a 8000.

## Servidor do LM Studio

O tradutor se conecta ao servidor local do LM Studio. Para configurar:

1. Abra o LM Studio.
2. Na aba **Server**, selecione um modelo carregado.
3. Clique em **Start Server**.
4. O servidor escuta por padrão em `http://localhost:1234`.

**Verificar modelos disponíveis:**

```bash
python epub_translator.py validacao.epub --list-models
```

**Carregar um modelo:** no LM Studio, baixe um modelo na aba Download e carregue-o antes de iniciar o servidor.

## Interface visual (web)

Além da CLI, existe uma **interface web** que faz o mesmo trabalho com
barras de progresso, preview lado a lado e controle de pausa/retomada.

### 1. Instalar o front-end (uma vez)

```bash
cd web/ui
npm install
```

### 2. Subir o back-end (encaminha também a interface)

Na **raiz do projeto**:

```bash
python -m uvicorn web.server:app --port 8000
```

Isso sobe a API em `http://localhost:8000`. Se `web/ui/dist/` já existir
(gerado por `npm run build`), a interface visual é servida **na mesma
origem**: é só abrir **[http://localhost:8000](http://localhost:8000)** no
navegador.

Sem o `dist/`, o back-end responde 404 nas rotas de interface com a mensagem
“O front-end ainda não foi construído (rode `npm run build` em web/ui)”.
Nesse caso, use o modo de desenvolvimento (abaixo).

### 3. Modo de desenvolvimento (front-end com recarga automática)

Dois terminais:

```bash
# Terminal 1 — back-end (na raiz do projeto)
python -m uvicorn web.server:app --port 8000

# Terminal 2 — front-end Vite (em web/ui)
cd web/ui
npm run dev
```

O Vite sobe em **[http://localhost:5173](http://localhost:5173)** e faz proxy
de `/api` para a porta 8000. É o modo para mexer no front-end: as mudanças
recarregam a página sem reiniciar o back-end.

| Modo | Comando | Endereço | Quando usar |
|---|---|---|---|
| Produção (local) | `python -m uvicorn web.server:app --port 8000` | http://localhost:8000 | uso normal, com `npm run build` feito |
| Desenvolvimento | `npm run dev` (em `web/ui`) | http://localhost:5173 | mexer no front-end |

### 4. Gerar o build de produção

```bash
cd web/ui
npm run build     # tsc -b && vite build -> gera web/ui/dist/
```

O `dist/` **não** é versionado (está no `.gitignore`): é gerado a partir do
código-fonte.

### Como usar a interface

1. **Provedor** — no seletor, escolha *LM Studio* ou *API compatível com
   OpenAI* e preencha a URL base (ex.: `http://localhost:1234/v1` ou
   `https://ollama.com/v1`), a chave (só para provedores que exigem) e o
   modelo. O botão **Testar conexão** lista os modelos do servidor.
2. **Livro** — envie o `.epub`, escolha o nome do arquivo de saída e, se
   quiser, envie um glossário de nomes próprios (`.json`, `.yaml` ou `.txt`).
3. **Traduzir** — o progresso chega em tempo real (SSE), com contadores de
   blocos traduzidos, vindos do cache, com falha e descartados.
4. **Controle** — **Pausar**, **Retomar** (o cache é preservado: nada é
   re-traduzido) e **Cancelar** ficam no painel de progresso.
5. **Resultado** — preview **original × traduzido** capítulo a capítulo,
   o painel de **integridade** do EPUB gerado, o **log** e o botão
   **Baixar EPUB**.

Os dados de cada job ficam em `web/jobs/<id>/` (`original.epub`,
`traducao.epub`, `cache.json`, `traducao.epub.log`, `job.json`) — essa pasta
é ignorada pelo Git e pode ser apagada à vontade.

> A chave de API **nunca** é gravada em claro: no `job.json` ela fica
> mascarada e não aparece em nenhuma resposta da API.

## Uso

### Básico

```bash
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo"
```

### Referência de opções

Todas as opções abaixo existem na CLI (`python epub_translator.py --help`).

| Opção | Padrão | O que faz |
|---|---|---|
| `src` (posicional) | — | Caminho do arquivo EPUB de entrada (obrigatório). |
| `--output`, `-o` | `saida/<src>_translated.epub` | Caminho do EPUB traduzido. A pasta é criada se não existir. |
| `--target`, `-t` | `pt-BR` | Idioma alvo. |
| `--model`, `-m` | `None` (primeiro modelo disponível) | Nome do modelo carregado no LM Studio. |
| `--base-url` | `http://localhost:1234/v1` | URL da API OpenAI-compatível. |
| `--batch-size` | `5` | Blocos por requisição ao modelo (tamanho do lote). |
| `--max-chars` | `8000` | Limite de caracteres por lote. **Aceita e valida o valor, mas hoje o corte efetivo dos lotes é definido apenas por `--batch-size`.** |
| `--max-attempts` | `3` | Tentativas por lote quando a requisição falha. |
| `--use-cache` | `True` | Habilita o cache JSON (já é o padrão; use `--no-cache` para desligar). |
| `--no-cache` | `False` | Desabilita o cache JSON: todo bloco é re-traduzido e o cache não é gravado. |
| `--cache-path` | `None` (= `<output>.cache.json`) | Caminho alternativo do arquivo de cache. |
| `--limit` | `None` (todos) | Limita a **N arquivos do spine** (não a N blocos). Os demais arquivos vão para a saída sem tradução. |
| `--bilingual` | `False` | Inclui o XHTML original junto com o traduzido. |
| `--glossary` | `None` | Caminho para arquivo de glossário em YAML ou JSON (termo → tradução). |
| `--mock` | `False` | Usa o `MockClient` (transformação identidade) em vez do LM Studio. |
| `--list-models` | `False` | Lista os modelos disponíveis no servidor e sai. |
| `--temperature` | `0.25` | Temperatura enviada ao modelo. |
| `--config`, `-c` | `None` | Caminho para arquivo de configuração YAML. |
| `--help`, `-h` | — | Mostra a ajuda e sai. |

### Exemplos completos

```bash
# Tradução com modelo específico e idioma alvo
python epub_translator.py livro.epub --target pt-BR --model "Llama-3-70B"

# Usar glossário (arquivo YAML — .json também vale, pois JSON é YAML válido)
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --glossary glossarios/mushoku_tensei.json

# Modo bilingual (traduzido + original no mesmo arquivo)
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --bilingual

# Traduzir apenas os 12 primeiros arquivos do spine
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --limit 12

# Configurar temperatura (padrão: 0.25)
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --temperature 0.3

# Configurar o tamanho do lote em blocos (padrão: 5)
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --batch-size 10

# Configurar as tentativas por lote (padrão: 3)
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --max-attempts 5

# Arquivo de configuração YAML
python epub_translator.py livro.epub --config config.yaml

# Forçar retradução (ignorar o cache)
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --no-cache

# Cache em outro caminho
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --cache-path meu_cache.json

# Servidor do LM Studio em outra porta
python epub_translator.py livro.epub --base-url "http://localhost:12345/v1"

# Arquivo de saída personalizado
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --output livro_pt.epub

# Modo mock (sem servidor LM Studio — para testes)
python epub_translator.py validacao.epub --mock --limit 12 --output saida/out.epub
```

### Modo mock

O `--mock` troca o cliente do LM Studio pelo `MockClient`, que aplica uma **transformação identidade** (devolve o texto original). Serve para exercitar o pipeline completo — extração de blocos, lotes, cache, replay, escrita do EPUB — sem servidor nem modelo carregado:

```bash
python epub_translator.py validacao.epub --mock --limit 12 --output saida/out.epub
```

O `--mock` **não traduz**: o EPUB de saída tem o mesmo texto do original.

### Script de entrega: `traduzir.py`

O `traduzir.py` é o script de uso direto, já configurado para o capítulo de
validação. Sem argumentos, ele traduz `OEBPS/Text/section-0005.html` de
`validacao.epub` para pt-BR e grava tudo em `saida/`:

```bash
python traduzir.py
```

Ele faz três coisas a mais que a CLI do pacote:

1. **Verifica o LM Studio antes de começar** — se o servidor não estiver no ar,
   sai com código 2 e mensagem clara em português; se nenhum modelo estiver
   carregado, avisa e lista os disponíveis.
2. **Manda um glossário de nomes próprios** (Man-God, Nanahoshi, Eris,
   Sylphie, Roxy, Ariel...) para o prompt, para que o modelo não os traduza.
   O glossário vem do arquivo `glossarios/mushoku_tensei.json` (use
   `--glossary` para apontar outro, ou `--no-glossary` para desligar).
3. **Roda a verificação final do EPUB** ao terminar e imprime o resultado:
   contagem de `<p>`/`<span>`, absence de marcadores `[[n]]`, `dc:language`,
   `dc:title`, binários byte-idênticos e `mimetype` como primeira entrada.

Opções úteis:

| Opção | Padrão | O que faz |
|---|---|---|
| `--src` | `validacao.epub` | EPUB de entrada. |
| `--output`, `-o` | `saida/traducao_resultado.epub` | EPUB traduzido. |
| `--log` | `saida/traducao_resultado.log` | Arquivo de log. |
| `--only` | `OEBPS/Text/section-0005.html` | Arquivo do EPUB a traduzir (caminho exato dentro do zip). |
| `--model`, `-m` | `qwen/qwen3.6-35b-a3b` | Modelo do LM Studio. |
| `--target`, `-t` | `pt-BR` | Idioma alvo. |
| `--limit-batches` | `None` | Traduz no máximo N lotes (para teste rápido). |
| `--verify-only` | `False` | Só verifica um EPUB já gerado, sem traduzir. |
| `--mock` | `False` | Usa o MockClient (sem LM Studio). |
| `--no-cache` | `False` | Desabilita o cache JSON. |
| `--glossary` | `glossarios/mushoku_tensei.json` | Caminho de um glossário JSON `{termo: tradução}`. Se o arquivo não existir, a CLI avisa e segue sem glossário. |
| `--no-glossary` | `False` | Desliga o glossário: nenhum termo é enviado no prompt. |

Reverificar o EPUB gerado, a qualquer momento:

```bash
python traduzir.py --verify-only --output saida/traducao_resultado.epub
```

### Retomada automática via cache

O cache é salvo automaticamente em `<output>.cache.json`, na mesma pasta da saída (`saida/livro_pt.epub.cache.json`). Se a tradução for interrompida, basta re-executar o mesmo comando — os blocos já traduzidos são reutilizados e **não são re-traduzidos**.

Comportamento real observado no `validacao.epub` com `--limit 12` (1275 blocos no total):

| Execução | Traduzidos | Cache (servidos) | Falhas |
|---|---|---|---|
| 1ª (cache virgem) | 1271 | 4 | 0 |
| 2ª (mesmo cache) | 120 | 1155 | 0 |

Ou seja, na 2ª execução **1155 dos 1275 blocos vieram do cache** e só 120 foram reenviados ao modelo. As duas colunas do relatório final (`Traduzidos:` e `Cache (servidos):`) sempre somam o total de blocos.

Para forçar a retradução completa (ignorar e não gravar o cache):

```bash
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --no-cache
```

## Glossário

O arquivo de glossário é lido como **YAML**: precisa ser um mapeamento `termo original: tradução`. Como JSON é um YAML válido, um `.json` também funciona.

### Formato YAML (recomendado)

```yaml
termo original: tradução
another term: tradução em pt-BR
```

### Formato JSON (também aceito)

```json
{
  "termo original": "tradução",
  "another term": "tradução em pt-BR"
}
```

### Uso

```bash
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --glossary glossario.yaml
```

O glossário é inserido no prompt de sistema para garantir consistência na tradução. Se o arquivo não for um mapeamento chave-valor, a CLI avisa no terminal e segue **sem** glossário.

### Glossários prontos em `glossarios/`

A pasta `glossarios/` guarda glossários versionáveis, prontos para uso:

| Arquivo | O que é |
|---|---|
| `glossarios/mushoku_tensei.json` | Nomes próprios de *Mushoku Tensei* (43 termos, cada um mapeado **para si mesmo**, ou seja: **não traduzir**). |

O mesmo arquivo serve para as três interfaces:

```bash
# CLI do pacote
python epub_translator.py validacao.epub --glossary glossarios/mushoku_tensei.json

# Script de entrega (usa este arquivo por padrão)
python traduzir.py
python traduzir.py --glossary glossarios/mushoku_tensei.json
python traduzir.py --no-glossary        # desliga o glossário

# Interface web: envie o arquivo no campo "Glossário de nomes próprios"
```

Detalhes em [`glossarios/README.md`](glossarios/README.md).

## Configuração por arquivo (YAML)

Crie um arquivo `config.yaml` na raiz do projeto ou no diretório de execução:

```yaml
# config.yaml
output: "livro_pt.epub"   # caminho do EPUB traduzido
use_cache: true           # habilita/desabilita o cache JSON
cache_path: "meu_cache.json"  # caminho do cache (padrão: <output>.cache.json)
```

Use com:

```bash
python epub_translator.py livro.epub --config config.yaml
```

Os argumentos da linha de comando têm prioridade sobre o arquivo de configuração — exceto `use_cache`, em que o valor do YAML prevalece sobre a flag (só `use_cache: false` no YAML desliga o cache).

**Atenção:** atualmente o YAML só é lido para `output`, `use_cache` e `cache_path`. As demais opções (`--target`, `--model`, `--temperature`, `--batch-size`, `--max-chars`, `--max-attempts`, `--limit`, `--bilingual`, `--base-url`, `--glossary`) são **ignoradas** no YAML e precisam ser passadas na linha de comando.

## Modelos recomendados para tradução local

### Famílias de modelos grandes (multilíngues)

| Família | Modelo | VRAM mínima | Contexto | Observações |
|---|---|---|---|---|
| Llama 3.x | Llama-3.3-70B-Instruct | 80 GB+ | 128K | Melhor qualidade; requer multi-GPU ou servidor de inferência |
| Llama 3.x | Llama-3.1-8B-Instruct | 16 GB | 128K | Boa para hardware consumer; qualidade inferior ao 70B |
| Qwen 2.5 | Qwen2.5-72B-Instruct | 80 GB+ | 128K | Excelente multilíngue; bom em português |
| Qwen 2.5 | Qwen2.5-32B-Instruct | 40 GB | 128K | Bom equilíbrio qualidade/VRAM |
| Mistral | Mistral-Nemo-12B | 16 GB | 128K | Leve e competente para tradução |
| Command-R | Command-R-35B | 40 GB | 128K | Otimizado para RAG e tradução |

### Observações importantes

- **O tamanho do lote precisa caber na janela de contexto** — o lote é definido por `--batch-size` (padrão: 5 blocos). Se o modelo tem janela de 8K tokens, reduza o lote (ex: `--batch-size 2`).
- **VRAM** — modelos acima de 30B geralmente exigem múltiplas GPUs ou quantização (GGUF via LM Studio).
- **Quantização** — modelos GGUF Q4_K_M no LM Studio permitem rodar modelos de 70B em GPUs de 24 GB.
- Para tradução, **temperatura baixa (0.2–0.3)** é recomendada para consistência.

## Troubleshooting

### "Servidor do LM Studio não está rodando"

```
ServerNotRunningError: Servidor do LM Studio não está rodando em http://localhost:1234/v1 — inicie o servidor na aba Server do LM Studio
```

- Verifique se o LM Studio está aberto e o servidor foi iniciado na aba **Server**.
- Confirme que a URL base está correta: `http://localhost:1234/v1`.
- Se o LM Studio usa outra porta, passe `--base-url`:

```bash
python epub_translator.py livro.epub --base-url "http://localhost:12345/v1"
```

### "Nenhum modelo carregado"

```
NoModelError: Nenhum modelo carregado — carregue um modelo no LM Studio
```

- No LM Studio, vá para a aba **My Models** e carregue um modelo.
- Execute `--list-models` para verificar:

```bash
python epub_translator.py validacao.epub --list-models
```

### Contexto insuficiente / requisição muito longa

Se o modelo não consegue processar o lote:

- Reduza o lote: `--batch-size 2` (é o corte efetivo dos lotes hoje).
- Use um modelo com janela de contexto maior.
- O timeout do cliente HTTP é fixo em 300 s e não é configurável pela CLI.

### Resposta malformada / tentativas esgotadas no log

O log da execução fica em `<output>.log`. As mensagens relevantes são:

```
Tentativa 1/3 falhou para OEBPS/Text/section-0005.html: ...
Resposta malformada para OEBPS/Text/section-0005.html: Resposta não contém blocos numerados no formato esperado [i]
```

Isso significa que o modelo devolveu uma resposta que não pôde ser parseada (blocos desbalanceados, formato incorreto). O tradutor tenta até `--max-attempts` vezes (padrão: 3); se persistir, o bloco é mantido no original e contado como falha no relatório.

**Soluções:**
- Aumente `--max-attempts` (ex: `--max-attempts 5`).
- Reduza `--batch-size` para lotes menores.
- Verifique se o modelo suporta bem o idioma alvo.
- Use `--bilingual` para comparar original e tradução lado a lado.

### Cache / retomada

O cache fica em `<output>.cache.json` no mesmo diretório do arquivo de saída. Exemplo:

```
livro_pt.epub          → arquivo traduzido
livro_pt.epub.cache.json  → cache de retomada
```

Para limpar o cache e retraduzir tudo:

```bash
python epub_translator.py livro.epub --target pt-BR --model "meu-modelo" --no-cache
```

### Glossário ignorado

Se a CLI imprimir `Glossário YAML deve ser um mapeamento chave-valor`, o arquivo existe mas não está no formato de mapeamento. Use `termo: tradução` (YAML) ou um `.json` válido.

### "O front-end ainda não foi construído (rode `npm run build` em web/ui)"

A rota da interface devolve essa mensagem quando `web/ui/dist/` não existe —
o back-end serve a API, mas não tem o que mostrar na tela. Rode o build:

```bash
cd web/ui
npm install     # só na primeira vez
npm run build
```

Depois é só abrir http://localhost:8000 de novo. Para desenvolver com
recarregamento automático, use o modo de desenvolvimento (`npm run dev` na
5173) em vez do build.

### A interface abre mas a tradução não começa

- O botão **Traduzir** só habilita depois que um `.epub` é escolhido.
- Clique em **Testar conexão** no painel do provedor: se o ponto ficar
  vermelho, o problema é a URL base, a chave ou o modelo — não o livro.
- Com a Ollama Cloud, a URL é `https://ollama.com/v1` e a chave é
  obrigatória. Com o LM Studio local, a chave fica **vazia**.

### "Job não encontrado" (404)

O job fica em `web/jobs/<id>/`. Se a pasta foi apagada (ou o servidor foi
reiniciado com a pasta limpa), o job não existe mais — crie outro. O
`job.json` permite retomar o job depois de reiniciar o servidor, desde que a
pasta `web/jobs/` tenha sido preservada.

### EPUB de saída não abre no leitor

- Abra o EPUB no **Calibre** primeiro para verificar integridade.
- Se usar **KOReader** ou **Kindle**, certifique-se de que o EPUB é versão 2 ou 3 compatível.
- O tradutor preserva `mimetype` como primeira entrada ZIP_STORED (padrão EPUB).
- Imagens, CSS e fontes são mantidos byte-idênticos.
- Se você usou `--bilingual`, o arquivo traz o XHTML original concatenado ao final e deixa de ser XML bem-formado (veja "Limitações conhecidas").

## Limitações conhecidas

- **`--bilingual` produz XHTML não bem-formado** — o arquivo original é concatenado ao final do XHTML traduzido, o que duplica a declaração XML e quebra leitores XML estritos.
- **`--limit N` conta arquivos, não blocos** — limita a N arquivos de conteúdo do spine (na ordem do spine); os arquivos restantes são copiados sem tradução.
- **`--mock` não traduz** — o MockClient aplica transformação identidade ao texto. Use apenas para testar o pipeline sem um servidor LM Studio.
- **O glossário só é aceito em YAML/JSON** — o formato `termo = tradução` é ignorado.
- **Só `output`, `use_cache` e `cache_path` são lidos do YAML** — as demais opções precisam ir na linha de comando.
- **`--max-chars` não corta os lotes** — hoje o agrupamento é definido só por `--batch-size`.
- **Tags `<script>`, `<style>`, `<pre>` e `<svg>` não são traduzidas** — são preservadas como estão. Já `<code>` é tratada como tag inline: o texto dentro dela é enviado ao modelo e traduzido.
- **O `dc:title` e o NCX/nav não são traduzidos** — são preservados (o índice continua no idioma original).
- **EPUB com estrutura não padrão** — o tradutor depende de `container.xml` e `<spine>` no OPF. EPUBs com estrutura muito customizada podem não ser lidos corretamente.
- **Modelos locais podem ter alucinações** — mesmo com temperatura baixa, modelos pequenos podem inserir texto extra ou quebrar marcadores. Use `--max-attempts` e reduza `--batch-size` se necessário.
- **A tradução é sequencial** — não há concorrência: os lotes são enviados um a um, o que é o comportamento mais estável para o LM Studio.

---

**Licença**: este projeto é fornecido "como está". Use por conta e risco.
