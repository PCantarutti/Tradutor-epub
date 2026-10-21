"""Cliente OpenAI-compatível (LM Studio, Ollama, OpenAI, vLLM…) + MockClient.

O cliente fala com qualquer servidor que exponha a API da OpenAI
(`/v1/models` e `/v1/chat/completions`). A chave de API, quando informada,
é enviada no header ``Authorization: Bearer <chave>``; sem chave, NENHUM
header de autenticação é enviado (servidores locais não exigem).

Protocolo de blocos numerados (usado pelo MockClient e esperado pelos prompts):
    Os blocos de entrada são demarcados por linhas que casam exatamente `^[i]$`
    (ou, na forma crua aceita para retrocompatibilidade, `^i$`), onde `i` é um
    inteiro 1-based. O conteúdo de cada bloco vai do fim da linha do separador
    até o próximo separador (ou fim do texto).
    A resposta do MockClient devolve os mesmos separadores `[i]` com o texto
    de cada bloco (com transform aplicada ao texto cru).
"""

from __future__ import annotations

import random
import re
import time
import logging
from functools import partial
from typing import Callable, Protocol, runtime_checkable

import requests

from .prompts import separator_index

logger = logging.getLogger("epub_translator")


# Marcadores de tag inline [[n]]/[[/n]] — protegidos durante o transform
_MARKER_TOKEN_RE = re.compile(r"\[\[/?\d+\]\]")


# ── Erros ────────────────────────────────────────────────────────────────────

class LMStudioError(Exception):
    """Erro base para operações com o servidor OpenAI-compatível."""


class ServerNotRunningError(LMStudioError):
    """O servidor OpenAI-compatível não está disponível."""


class NoModelError(LMStudioError):
    """Nenhum modelo disponível no servidor."""


# ── Protocol ─────────────────────────────────────────────────────────────────

@runtime_checkable
class ChatProvider(Protocol):
    """Contrato mínimo de um provedor de chat OpenAI-compatível.

    Qualquer servidor com a API `/v1/chat/completions` (LM Studio, llama.cpp,
    Ollama, vLLM, Together, etc.) pode ser usado pelo pipeline desde que
    implemente estes três métodos.
    """

    def list_models(self) -> list[str]:
        """Retorna a lista de IDs de modelos disponíveis."""
        ...

    def resolve_model(self) -> str:
        """Retorna o ID do modelo a usar."""
        ...

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.25,
        max_tokens: int | None = None,
    ) -> str:
        """Envia as mensagens e retorna o conteúdo da resposta."""
        ...


# ── Cliente ──────────────────────────────────────────────────────────────────

class LMStudioClient:
    """Cliente OpenAI-compatível (LM Studio, Ollama, OpenAI, vLLM, ...).

    Implementa o protocolo :class:`ChatProvider`.

    Args:
        base_url: URL da API (padrão http://localhost:1234/v1).
        model: Nome do modelo a usar. Se None, resolve automaticamente.
        api_key: Chave de API (padrão 'lm-studio'). Se vazia/None, NENHUM
            header de autenticação é enviado (servidores locais que não
            exigem chave continuam funcionando).
        timeout: Timeout em segundos para requests (padrão 300).
        max_retries: Número máximo de tentativas (padrão 3).
        backoff_base: Base do backoff exponencial (padrão 2.0).
        session: Session requests injetável (para testes).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:1234/v1",
        model: str | None = None,
        api_key: str = "lm-studio",
        timeout: float = 300.0,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        session: requests.Session | None = None,
        reasoning_effort: str | None = "minimal",
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.reasoning_effort = reasoning_effort
        self._session = session or requests.Session()
        # O modelo é resolvido UMA vez e reaproveitado: resolver a cada
        # requisição custaria um GET /v1/models por bloco traduzido.
        self._resolved_model: str | None = None

    # ── list_models ────────────────────────────────────────────────────────

    def list_models(self) -> list[str]:
        """Retorna a lista de IDs de modelos disponíveis no servidor."""
        try:
            resp = self._session.get(
                f"{self.base_url}/models",
                headers=self.auth_headers(),
                timeout=self.timeout,
            )
            if resp.status_code == 401:
                # Credencial inválida: repetir não resolve — falha na hora.
                raise LMStudioError(self._mensagem_401())
            resp.raise_for_status()
            data = resp.json()
            return [m["id"] for m in data.get("data", [])]
        except requests.ConnectionError as exc:
            raise ServerNotRunningError(self._mensagem_conexao()) from exc
        except requests.Timeout as exc:
            raise ServerNotRunningError(self._mensagem_conexao()) from exc

    # ── resolve_model ──────────────────────────────────────────────────────

    def resolve_model(self) -> str:
        """Retorna o modelo a usar.

        Se self.model foi fornecido, retorna ele (mas valida se há modelos
        no servidor). Se None, retorna o primeiro da lista.

        O resultado é memorizado: a lista de modelos é buscada uma única vez.
        """
        if self._resolved_model is not None:
            return self._resolved_model

        models = self.list_models()
        if not models:
            raise NoModelError(
                f"Nenhum modelo carregado no servidor em {self.base_url} "
                "— carregue um modelo"
            )
        if self.model is not None:
            self._resolved_model = self.model
        else:
            self._resolved_model = models[0]
        return self._resolved_model

    # ── chat ───────────────────────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.25,
        max_tokens: int | None = None,
    ) -> str:
        """Envia mensagens e retorna o conteúdo da resposta.

        Retry com backoff exponencial em ConnectionError, Timeout e HTTPError
        5xx/429. Após max_retries, re-raise como LMStudioError.
        Remove blocos <think>...</think> do conteúdo final.
        """
        model = self.resolve_model()
        body: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        # 'reasoning.effort' reduz o tempo de resposta dos modelos Qwen3 com
        # raciocínio (medido: ~96s -> ~60s por lote, sem perda de qualidade).
        # Servidores que não conhecem o campo respondem 400 — nesse caso
        # caímos no corpo simples e seguimos traduzindo.
        use_reasoning = self.reasoning_effort is not None
        if use_reasoning:
            body["reasoning"] = {"effort": self.reasoning_effort}

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._session.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers=self.auth_headers(),
                    timeout=self.timeout,
                )

                if resp.status_code == 401:
                    # Chave ausente/expirada: repetir a mesma requisição não
                    # resolve — levanta o erro de imediato (sem backoff).
                    raise LMStudioError(self._mensagem_401())

                # Se for erro de conexão, re-raise imediatamente
                if resp.status_code == 429:
                    # rate limit: espera e retry
                    wait = self._backoff(attempt)
                    time.sleep(wait)
                    continue

                if resp.status_code == 400 and use_reasoning:
                    # O servidor não aceita 'reasoning': refaz sem ele.
                    logger.info(
                        "Servidor recusou 'reasoning.effort'; refazendo sem ele."
                    )
                    body.pop("reasoning", None)
                    use_reasoning = False
                    wait = self._backoff(attempt)
                    time.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    # server error: retry
                    if attempt >= self.max_retries - 1:
                        raise LMStudioError(
                            self._mensagem_http(resp.status_code)
                        )
                    wait = self._backoff(attempt)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    raise LMStudioError("resposta vazia")
                result = self._strip_think_blocks(content)
                if not result.strip():
                    raise LMStudioError("resposta vazia")
                return result

            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    wait = self._backoff(attempt)
                    time.sleep(wait)
                continue

            except requests.HTTPError as exc:
                # Se já tentou todas as vezes, re-raise
                if attempt >= self.max_retries - 1:
                    raise LMStudioError(
                        self._mensagem_http(resp.status_code)
                    ) from exc
                wait = self._backoff(attempt)
                time.sleep(wait)
                continue

        # Esgotou retries
        if last_exc is not None:
            raise ServerNotRunningError(self._mensagem_conexao()) from last_exc
        raise LMStudioError("Falha ao obter resposta do servidor")

    # ── Helpers ────────────────────────────────────────────────────────────

    def auth_headers(self) -> dict[str, str]:
        """Cabeçalhos de autenticação da requisição.

        Devolve ``{"Authorization": "Bearer <api_key>"}`` quando há chave de
        API; devolve um dict VAZIO quando a chave é vazia/None — assim
        servidores locais que não exigem autenticação (LM Studio) recebem a
        requisição sem header de Authorization.

        A chave NUNCA aparece em log nem em mensagem de erro.
        """
        chave = (self.api_key or "").strip()
        if not chave:
            return {}
        return {"Authorization": f"Bearer {chave}"}

    def _mensagem_401(self) -> str:
        """Mensagem de erro para HTTP 401 (sem autorização)."""
        return (
            f"Erro HTTP 401 (sem autorização) em {self.base_url} — a chave de "
            "API está ausente ou expirada. Confira a chave no seletor de "
            "provedor e use 'Testar conexão' para validar."
        )

    def _mensagem_conexao(self) -> str:
        """Mensagem de erro para conexão/timeout (cita a base_url real)."""
        return (
            f"Não foi possível conectar em {self.base_url} — verifique se o "
            "servidor está no ar e se a base_url está correta (o servidor não "
            "está rodando ou a requisição expirou)."
        )

    def _mensagem_http(self, status: int) -> str:
        """Mensagem de erro para status HTTP (cita a base_url real)."""
        return f"Erro HTTP {status} do servidor em {self.base_url}."

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Calcula tempo de espera com backoff exponencial + jitter."""
        base = 2.0 ** attempt
        jitter = random.uniform(0, 1)
        return base + jitter

    @staticmethod
    def _strip_think_blocks(content: str) -> str:
        """Remove TODOS os blocos <thinking>...</thinking> e
        <think>...</think> do conteúdo.

        Modelos Qwen3 com raciocínio às vezes emitem esse raciocínio dentro
        do texto da resposta; ele precisa sair ANTES do parse dos blocos.
        """
        # Remove <thinking>...</thinking> (com tags)
        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
        # Remove <think>...</think> (sem tags de abertura)
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        # Remove a tag de abertura sobrando
        content = re.sub(r"<thinking>", "", content)
        # Remove tags soltas de fechamento
        content = re.sub(r"</thinking>", "", content)
        return content

    @property
    def is_mock(self) -> bool:
        return False


# ── MockClient ───────────────────────────────────────────────────────────────

class MockClient:
    """Cliente mock para testes, com protocolo de blocos numerados.

    Implementa o protocolo :class:`ChatProvider`.

    Protocolo de blocos:
        - Entrada: a ÚLTIMA mensagem 'user' contém blocos demarcados por
          linhas que casam exatamente `^[i]$` (forma crua `^i$` também aceita).
        - O conteúdo de cada bloco vai do fim da linha do separador até
          o próximo separador (ou fim do texto). O texto antes do primeiro
          separador é descartado.
        - Saída: devolve os mesmos separadores `[i]` com o texto de cada
          bloco (com *transform* aplicada ao texto cru, padrão identity).
          Os marcadores `[[n]]`/`[[/n]]` são preservados intactos.

    Args:
        transform: Função opcional aplicada ao texto de cada bloco.
    """

    def __init__(self, transform: Callable[[str], str] | None = None) -> None:
        self._transform = transform or (lambda x: x)

    def list_models(self) -> list[str]:
        return ["mock-model"]

    def resolve_model(self) -> str:
        return "mock-model"

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.25,
        max_tokens: int | None = None,
    ) -> str:
        # Pega a última mensagem 'user'
        last_user = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user = msg.get("content", "")
                break
        if last_user is None:
            last_user = ""

        # Divide em blocos por separadores '[i]' (ou 'i' na forma crua)
        blocks = self._split_blocks(last_user)

        # Reconstrói no MESMO formato do prompt: '[i]' + texto do bloco
        parts: list[str] = []
        for idx, content in blocks:
            parts.append(f"[{idx}]")
            parts.append(self._transform_text(content))

        return "\n".join(parts)

    @staticmethod
    def _split_blocks(text: str) -> list[tuple[int, str]]:
        """Divide *text* em blocos (índice, conteúdo) pelos separadores.

        Aceita o formato ``[i]`` (pedido no prompt) e o formato cru ``i``.
        Linhas antes do primeiro separador são descartadas.
        """
        blocks: list[tuple[int, str]] = []
        current_idx: int | None = None
        current_lines: list[str] = []

        for line in text.split("\n"):
            separator = separator_index(line)
            if separator is not None:
                if current_idx is not None:
                    blocks.append((current_idx, "\n".join(current_lines)))
                current_idx = separator
                current_lines = []
            elif current_idx is not None:
                current_lines.append(line)

        if current_idx is not None:
            blocks.append((current_idx, "\n".join(current_lines)))

        return blocks

    def _transform_text(self, text: str) -> str:
        """Aplica o transform ao texto preservando os marcadores [[n]]/[[/n]].

        Os marcadores são trocados por placeholders antes do transform e
        restaurados depois — assim o transform nunca os corrompe.
        """
        markers: list[str] = []

        def _mask(match: re.Match[str]) -> str:
            markers.append(match.group(0))
            return f"\x00{len(markers) - 1}\x00"

        masked = _MARKER_TOKEN_RE.sub(_mask, text)
        result = self._transform(masked)
        for i, marker in enumerate(markers):
            result = result.replace(f"\x00{i}\x00", marker)
        return result

    @property
    def is_mock(self) -> bool:
        return True
