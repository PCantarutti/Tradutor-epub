# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Uma pessoa só: o dono do repositório, na própria máquina. Traduz livros em .epub (light novels, como *Mushoku Tensei*) do inglês para pt-BR para ler. Usa no computador (desktop/notebook), nunca no celular.

## Product Purpose

Traduzir um EPUB inteiro com um modelo de linguagem (LM Studio local ou API compatível com OpenAI, ex.: Ollama Cloud) preservando a estrutura do livro — tags inline, imagens, CSS, índice e metadados. Sucesso = um EPUB legível em pt-BR, com 0 blocos faltando e integridade 12/12.

## Positioning

Ferramenta pessoal, local, de um usuário. Não é produto público nem SaaS.

## Operating Context

- Uma tradução leva de dezenas de minutos a mais de uma hora (2.700+ blocos, lotes de 5). O usuário inicia e volta de tempos em tempos para conferir — **acompanhar o progresso é o momento mais importante da interface**.
- Depois: revisar o original e o traduzido lado a lado, retraduzir só os blocos que falharam (o cache reaproveita o resto) e baixar o EPUB.
- Back-end FastAPI local (`web/server.py`) com SSE de progresso; front-end React + Vite + TypeScript + Tailwind v3 em `web/ui/`.

## Capabilities and Constraints

- Provedor: LM Studio local ou qualquer API compatível com OpenAI; a chave de API fica salva só no navegador (o servidor nunca grava a chave em disco).
- Glossário de nomes próprios (JSON ou YAML simples).
- Pausar (entre lotes), retomar, cancelar, descartar cache, retraduzir falhas, reabrir traduções anteriores.
- Contadores: traduzidos, cache, falhas, descartados; verificação de integridade com 12 itens; log por job.
- Toda a interface em pt-BR.

## Product Principles

1. O progresso tem que ser legível de relance, mesmo voltando depois de 20 minutos.
2. Nada se perde: cache, retomada e retradução de falhas são de primeira classe.
3. Falha visível e acionável — dizer onde falhou e oferecer o conserto.
4. Ferramenta, não vitrine: densidade e clareza acima de decoração.
