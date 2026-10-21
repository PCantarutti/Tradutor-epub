# FORJA — Tradutor de EPUB via LM Studio

**O que é**: CLI em Python que traduz livros .epub com modelos locais do LM Studio (API compatível OpenAI), preservando a estrutura do EPUB: mimetype primeiro no zip (ZIP_STORED), imagens/CSS byte-idênticos, tags inline, índice (NCX/nav), título e dc:language.

**Camadas** (2026-10-21): (1) CLI em Python (`epub_translator/`) — validada; (2) **web/**: back-end FastAPI fino (`web/server.py`) que encapsula o pipeline + front-end React/Vite/TS/Tailwind (`web/ui/`).

**Stack**: Python 3.12 no container (3.13 na máquina do usuário), sem framework no CLI. Deps: requests, lxml, PyYAML, rich, pytest. Camada web: fastapi, uvicorn, python-multipart (httpx/starlette já vêm como dependência).

**Como rodar/testar**:
- Suíte: `python -m pytest -q` (deps: `pip install -q -r requirements.txt`) — **405 testes** (2026-10-21)
- Script de entrega: `python traduzir.py` (traduz só `OEBPS/Text/section-0005.html` de `validacao.epub` → `saida/traducao_resultado.epub` + `saida/traducao_resultado.log`; faz a verificação final (a)-(g) e imprime o relatório).
- Opções úteis: `--only CAMINHO` (traduz só esse arquivo do zip), `--limit-batches N` (teste rápido), `--verify-only` (só verifica), `--mock`, `--no-cache`, `--no-glossary`.
- Pipeline sem servidor: `python epub_translator.py validacao.epub --mock --limit 12 --output saida/out.epub`
- Real: `python epub_translator.py livro.epub --target pt-BR --model <modelo>` (LM Studio em http://localhost:1234/v1)
- **CRÍTICO**: pytest está em `/cache/pyuser/bin` que NÃO está no PATH do container. Sempre use `python -m pytest` (NUNCA `pytest` sozinho).
- **Camada web**: back-end `python -m uvicorn web.server:app --port 8000` (serve também `web/ui/dist` se existir); front-end em dev `cd web/ui; npm run dev` (proxy `/api` → 8000). Build: `cd web/ui; npm run build`.

**Estrutura de pastas** (organizada em 2026-10-21):
- `web/` — **camada web**: `server.py` (FastAPI: rotas, SSE, jobs em disco), `jobs.py` (modelo de job em disco + barramento de eventos), `preview.py` (capítulos, títulos via NCX, HTML de preview sanitizado), `ui/` (Vite+React+TS+Tailwind), `jobs/<id>/` (dados de cada job).
- `saida/` — **TODAS** as saídas de tradução: EPUB gerado, `.log` e `.cache.json`. A pasta é criada automaticamente (`write_epub` e `BlockCache` fazem `makedirs`).
- `fixtures/` — EPUBs de entrada usados em teste/exemplo: `fixed_input.epub`, `test_input.epub`. Não são referenciados por nenhum teste.
- `glossarios/` — glossários `{termo: tradução}` versionáveis: `mushoku_tensei.json` (nomes próprios, 43 termos, cada um mapeado para si mesmo) + `README.md` de uso. O `traduzir.py` usa `mushoku_tensei.json` por padrão (`GLOSSARIO_PADRAO` + `carregar_glossario()`); o mesmo arquivo serve para `--glossary` da CLI do pacote e para o upload da interface web (que já aceita JSON).
- `validacao.epub` — na raiz, é a entrada de validação (somente leitura).
- Default do CLI do pacote: `saida/<src>_translated.epub`, ao lado do arquivo de origem.

**Módulos** (`epub_translator/`): `epub_io.py` (leitura/escrita EPUB), `lmstudio_client.py` (API + MockClient + `ChatProvider` Protocol), `blocks.py` (marcadores inline), `prompts.py` (prompt + parser), `cache.py` (cache JSON atômico), `translator.py` (orquestração), `cli.py` (argparse). Entry: `epub_translator.py` na raiz.

**TASK-021 (2026-10-21) — `ChatProvider` Protocol**: `epub_translator/lmstudio_client.py` expõe `ChatProvider(Protocol)` com `@runtime_checkable` e exatamente 3 métodos: `list_models() -> list[str]`, `resolve_model() -> str`, `chat(messages, temperature=0.25, max_tokens=None) -> str`. `LMStudioClient` e `MockClient` JÁ cumprem o contrato (nenhuma classe nova criada, nenhuma assinatura alterada) — conformidade declarada na docstring. O back-end web deve tipar/injetar provedores contra este Protocol. Testes do protocolo ficam DENTRO de `tests/test_lmstudio_client.py` (nunca criar `tests/test_chat_provider.py`).

**TASK-031 (2026-10-21) — glossário em arquivo JSON**: a constante `GLOSSARIO` saiu do `traduzir.py` (não existe mais — `not hasattr(traduzir, "GLOSSARIO")` é testado). O glossário de nomes próprios agora vive em `glossarios/mushoku_tensei.json` (**43** chaves únicas, cada uma mapeada para si mesma; a tupla antiga tinha 44 entradas com "Talisman" duplicado). `traduzir.carregar_glossario(caminho=None)` lê JSON com `json.load`, devolve `{}` (com aviso no log, sem quebrar) para arquivo ausente / JSON inválido / conteúdo que não é objeto, e normaliza para `{str(k): str(v)}`. `traduzir.py` ganhou `--glossary CAMINHO` (precedência sobre o padrão; caminho explícito inexistente → erro em pt-BR e exit 2) e mantém `--no-glossary`. O `epub_translator/cli.py --glossary` NÃO mudou de implementação (`yaml.safe_load` já lê JSON) — só o texto do help.

**BUG CORRIGIDO (2026-10-21) — o cliente nunca enviava a chave de API**: `LMStudioClient` guardava `self.api_key` mas NAO mandava o header `Authorization` — as chamadas saíam sem autenticação. O LM Studio local não exige chave (por isso o bug passou despercebido), mas Ollama Cloud / OpenAI / qualquer provedor com chave respondia **HTTP 401**. Agora existe `auth_headers()`: com chave, envia `{"Authorization": "Bearer <chave>"}`; sem chave, NENHUM header (o LM Studio local continua funcionando). As mensagens de erro também foram reescritas: citam a `base_url` real em vez do texto fixo "LM Studio", e o **401 não é mais retentado** (credencial inválida não melhora com retry) — a mensagem é "Erro HTTP 401 (sem autorização) em {base_url} — a chave de API está ausente ou expirada...". A Ollama Cloud usa `https://ollama.com/v1` (URL que o usuario digitou e que está CORRETA — o que faltava era o header).

**Glossário pela interface web (2026-10-21)**: o `lerGlossario` do `App.tsx` passou a decidir o parser pelo **CONTEÚDO** (`JSON.parse` primeiro, YAML simples como fallback) em vez da extensão do nome do arquivo. O `ProviderSelector` não impõe nenhuma restrição à `base_url` (qualquer provedor é aceito). Provado de ponta a ponta: upload pela interface → 43 termos no `job.json` → 43 linhas na seção "GLOSSÁRIO:" do prompt realmente enviado ao modelo.

**ARMADILHA DE MEDIÇÃO (2026-10-21)**: para contar as chaves de um dicionário JSON lido no PowerShell, `ConvertFrom-Json` devolve um hashtable e `$obj.PSObject.Properties.Name.Count` devolve **0** (o Count de uma coleção de strings não é o de uma coleção de objetos). Use Python para contar — a UI parecia "não enviar o glossário" quando nao enviava.

**TASK-027 (2026-10-21) — teste e2e da camada web**: `tests/test_web_e2e.py` (4 testes) sobe um **servidor OpenAI-compatível falso** (`http.server.ThreadingHTTPServer` em porta livre) e aponta a `base_url` da config para ele — é a única forma de injetar o provedor pela rota `POST /api/jobs` (o `JobManager.criar/iniciar` não recebe cliente da rota; `iniciar(job, client=...)` aceita, mas a rota chama sem cliente). O falso devolve `GET /v1/models` → `{"data":[{"id":"modelo-falso"}]}` e `POST /v1/chat/completions` → `{"choices":[{"message":{"content": "[i]\n<texto>"}}]}` (mesmo formato do prompt, senão o `parse_response` falha). Detalhes: `fixtures/test_input.epub` = 2 capítulos (13 + 3 blocos, 16 no total); o `pause_cb` do tradutor é consultado **entre lotes**, então a pausa só é determinística com `batch_size=1`; `limit_batches` deve cobrir TODOS os lotes do capítulo, senão `translated + cached == total` não fecha; o `snapshot` do job só tem `cached > 0` depois do `resume` (o `iniciar` zera os contadores).

**Entregue e validado**: 1275 blocos traduzidos em `validacao.epub` (12 arquivos do spine), 0 falhas; EPUB de saída com 51 entradas, mimetype 1ª ZIP_STORED, binários byte-idênticos, 229 `<p>` e 198 `<span>` preservados, 0 marcadores `[[n]]` remanescentes, `dc:language=pt-BR`, `dc:title` intacto, NCX íntegro. Cache validado: 2ª execução reusa 1155/1275 blocos.

**Rodada `traduzir.py` (2026-10-21) — ENTREGUE**: `OEBPS/Text/section-0005.html`

**REPOSITÓRIO PÚBLICO (2026-10-21)**: `https://github.com/PCantarutti/Tradutor-epub` (branch `main`). O código-fonte INTEIRO foi versionado pela primeira vez (62 arquivos, ~0,8 MB) em 2 commits: `5e92ced` (`.gitignore` + `.gitattributes` + código) e `4969465` (README + validação do EPUB de entrada no `traduzir.py`).

- **`.gitignore`**: ignora `__pycache__/`, `.pytest_cache/`, `node_modules/`, `web/ui/dist/`, `saida/`, `web/jobs/`, `.forja/` (memória interna do agente), segredos (`.env*`, `config.yaml`, `config.local.*`) e `validacao.epub` + `*.epub` (com exceção `!fixtures/*.epub`, que SÃO versionados).
- **ARMADILHA DO `lib/`**: o padrão `lib/` do Python ignorava `web/ui/src/lib/util.ts` (código do front-end!). Os padrões Python que podem colidir com o front-end precisam ser **ancorados na raiz** (`/lib/`, `/dist/`, `/build/`).
- **`.gitattributes`**: `* text=auto eol=lf` (o `core.autocrlf=true` do Windows fazia cada `git diff` mostrar o arquivo inteiro como alterado) + binários marcados como `binary` (o tradutor exige imagens/CSS byte-idênticos).
- **`validacao.epub` NÃO é versionado** (volume 15 de *Mushoku Tensei*, ~17 MB, copyright de Seven Seas). O README avisa e o `traduzir.py` agora valida a existência do EPUB de entrada ANTES de traduzir (exit 2 com mensagem apontando `--src`), em vez de estourar um `Errno 2` cru.
- **`.forja/` é ignorado** — memória interna do agente, não vai para o GitHub. `FORJA.md` (na raiz) É versionado. de `validacao.epub` traduzido para pt-BR com `qwen/qwen3.6-35b-a3b` no LM Studio local. 229 blocos: 1 descarte por marcador inválido na 1ª rodada (o modelo perdeu 1 dos 2 marcadores do bloco 82) e 228 traduzidos; a 2ª execução reusou 228 do cache, re-traduziu o bloco 82 e ficou **100% traduzido**. Verificação final 12/12 OK. Latência real: ~85s por lote de 5 blocos (46 lotes, ~68 min). Saída em `saida/traducao_resultado.epub`. Suíte: **239 testes**.

**Convenções**:
- lxml para TODO XML (XHTML/OPF/NCX) — sem BeautifulSoup.
- Tags inline viram marcadores `[[n]]`/`[[/n]]` no prompt; reconstrução por "replay" sobre a árvore original (preserva tag+atributos).
- Fio de tradução: blocos numerados `[i]` (1-based) — não JSON (mais robusto p/ modelos locais).
- **Formato de bloco no prompt: `[i]\n<texto>` — SEM prefixo de tag.** Fonte única: `blocks.format_block(index, text)`.
- **Protocolo de separadores: `prompts.separator_index(line)`** — casa `^\[(\d+)\]$` ou `^(\d+)$`. O `MockClient` importa ESSE helper; nunca reimplementar o regex em outro arquivo.
- `parse_response` aceita índices não consecutivos; erro só quando não há separador numerado.
- Cache JSON `<output>.cache.json` (atômico), key = sha256(bloco|target|model|source|glossary|context|**file_path**).
- **A chave do cache inclui o caminho do arquivo** (`file_path`): o índice do bloco reinicia a cada arquivo, então dois capítulos com blocos de mesmo texto (ex.: copyright repetido) colidiam sem isso.
- **Ordem da serialização XHTML**: declaração XML → doctype → conteúdo. Doctype antes da declaração faz o lxml recusar o doc.
- **OPF alterado só chega à saída via `overrides[data.opf_path]`** (`data.opf_path` = chave real do zip, ex. `OEBPS/content.opf`).
- **href do manifest OPF é relativo ao diretório do OPF** — `epub_io.resolve_href(opf_dir, href)` converte para a chave real do zip.
- Erros/avisos em pt-BR. Type hints em tudo; comentários só nos pontos não óbvios.
- **Camada web** (ver `.forja/knowledge/frontend.md`): tema escuro, React 18 + Vite + TS + Tailwind **v3** (config JS clássico) + SSE. Sem biblioteca de UI. Textos da interface em pt-BR.
- **SSE sem deadlock**: o thread do tradutor ESCREVE eventos numa lista com `threading.Lock`; o gerador SSE (async, no event loop) LÊ e faz `await asyncio.sleep(0.15)`. **Nunca** usar `asyncio.Queue` cross-thread e **nunca** `queue.get()` bloqueante dentro do async (trava o event loop inteiro).
- **`TestClient` do FastAPI TRAVA em stream SSE** (verificado empiricamente em 2026-10-21): o teste do SSE deve consumir o **gerador de eventos direto** (`asyncio.run` + `anext`), não passar pelo HTTP.
- **Camada web = encapsuladora, não reimplementação**: o server.py só orquestra o pipeline existente (`translate_epub`, `EpubReader`, `key_for`, `traduzir.verificar_epub`). Não reescrever lógica de EPUB.
- **Preview**: alinhar parágrafo-a-parágrafo exige `data-block-index` 1-based nos elementos de bloco. Estratégia VALIDADA empiricamente: parse 2x do XHTML → 1ª parse só para extrair os `getpath()` dos elementos via `extract_blocks`; 2ª parse (limpa) recebe os `data-block-index` → `serialize_xhtml`. Depois `replay(blocks, tree)` reconstrói as tags inline (o texto do bloco traz os marcadores `[[n]]`).
- **Título dos capítulos vem do NCX** (`data.ncx_paths` → `<navPoint><navLabel><text>` + `<content src>`); o manifesto OPF NÃO tem título (validado em `validacao.epub`).
- **Log por job**: `_setup_logging` adiciona um `FileHandler` ao logger `epub_translator` por output e **nunca remove** os antigos → com vários jobs, cada arquivo de log recebe as linhas de todos. O back-end deve remover os handlers de FileHandler de outros jobs antes de iniciar um novo.
- **Pausa é ENTRE LOTES**: o `pause_cb` do tradutor só é consultado no fim de um lote. Com o LM Studio real, um lote de 5 blocos leva ~90 s e um de 1 bloco ~70 s — a pausa só efetua depois disso. A UI mostra "pausando…" nesse intervalo (campo `pausando` do snapshot).
- **Contadores do job**: `traduzidos + cache + falhas + descartados == done == total`. O `done` vem do `ProgressEvent.done` (o tradutor conta os 4 desfechos).

**Armadilhas conhecidas**:
- `cache.key_for` usa o índice do bloco **dentro do lote** (`idx+1` reinicia a cada lote) → colisões entre lotes. Efeito: `translated_blocks + cached_blocks == total_blocks` (não `translated == total`).
- `parse_response` usa `.strip()` → parágrafos só-NBSP viram string vazia. O número de tags é preservado; compare contagens, não texto byte a byte.
- Log do pipeline vai para `<output>.log`; em teste, aponte `cache_path` para tmp_path e feche o `FileHandler` no teardown.
- O container **não alcança** o LM Studio do host (`localhost:1234` é isolado) — testes usam `MockClient`.
- **O `MockClient` mascara os marcadores `[[n]]`/`[[/n]]` durante o transform** (por design). Para testar descarte por marcador inválido, use o `ClienteRespostaCrua` de `tests/test_correcoes.py` — um transform normal NUNCA corrompe os marcadores.
- O LM Studio separa o raciocínio em `choices[0].message.reasoning_content`; o `content` vem limpo. Ainda assim, `_strip_think_blocks` remove `<think>`/`</think>` do texto.
- `"reasoning": {"effort": "minimal"}` no corpo reduz a latência de ~96s para ~60s por lote (medido no qwen3.6-35b-a3b). Se o servidor responder 400, o cliente refaz sem o campo.
- Glossário de nomes próprios é **obrigatório** para este livro: sem ele o modelo traduz "Man-God" para "Deus-Homem". Desde TASK-031 ele **não vive mais no código**: fica em `glossarios/mushoku_tensei.json` (43 termos, `termo → termo`), carregado por `traduzir.carregar_glossario()`.

**Bugs corrigidos** (não reintroduzir): TASK-011 (caminhos OPF + duplicatas no zip), TASK-013 (ordem dos marcadores inline + validação por bloco + replay usando texto traduzido), TASK-014 (protocolo mock↔parser), TASK-015 (ordem declaração XML/doctype), TASK-016 (prefixo `p: ` vazando), TASK-017 (OPF descartado).

**Bugs corrigidos em 2026-10-21** (rodada `traduzir.py`):
- cache hit não aplicava a tradução ao bloco → o EPUB saía em inglês na 2ª execução. Agora `b.text = hit`.
- a chave do `cache.put` era calculada sobre o texto JÁ traduzido → nunca batia com a do `get`. Agora a chave é calculada antes e guardada em `keys`.
- faltava validar os marcadores NA RESPOSTA do modelo → agora `blocks.compare_markers(original, traduzido)`; bloco com marcador faltando/duplicado/fora de ordem é **descartado** (contador `Report.discarded_blocks`) e logado como `DESCARTADO`.
- `max_chars_per_batch` era código morto → `build_batches()` respeita batch_size E max_chars.
- faltava filtro por arquivo → `TranslationConfig.only` (caminho exato no zip).
- o NCX era re-serializado sem traduzir nada → agora sai byte-idêntico.
- `resolve_model()` fazia GET /v1/models a cada bloco → resolvido UMA vez (memorizado no cliente).
- `_setup_logging` duplicava handler → sem duplicidade.
- o log não registrava por bloco → agora registra arquivo, índice, status e motivo.
- resposta vazia para bloco não-vazio → conta como falha, mantém o original.
- o índice do bloco na chave do cache reiniciava por arquivo → a chave agora inclui `file_path`.

**README — camada web documentada (2026-10-21)**: a seção "Interface visual (web)" do `README.md` agora traz `python -m uvicorn web.server:app --port 8000` (back-end que serve a interface na **mesma origem** quando `web/ui/dist/` existe), o modo dev (`npm run dev` em `web/ui` → 5173, com proxy de `/api` para a 8000), o build de produção e o troubleshooting. Validado de ponta a ponta: build do front-end, interface na 8000, proxy do Vite, e um job completo pela API (16/16 blocos, 0 falhas, EPUB baixável com `mimetype` como 1ª entrada ZIP_STORED).

**FORMATO DO PROMPT (armadilha ao mockar um provedor)**: o `build_user` manda o separador `[i]` **sozinho numa linha** e o texto do bloco na linha seguinte (`epub_translator/prompts.py:60-65`). Um provedor falso que devolva `[i] texto` na mesma linha produz blocos vazios e o job termina em `error` ("o modelo devolveu bloco vazio"). O mesmo formato precisa ser devolvido na resposta.
