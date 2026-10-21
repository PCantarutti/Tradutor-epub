"""Testes para epub_translator/lmstudio_client.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from epub_translator.lmstudio_client import (
    ChatProvider,
    LMStudioClient,
    LMStudioError,
    MockClient,
    NoModelError,
    ServerNotRunningError,
)
from epub_translator.prompts import parse_response


# ── FakeSession para testes ─────────────────────────────────────────────────

class FakeSession:
    """Session fake que simula respostas/erros do requests."""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self._raise_on_get: Exception | None = None
        self._raise_on_post: Exception | None = None
        self._get_side_effects: list[Any] = []
        self._post_side_effects: list[Any] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.get_calls.append((url, kwargs))
        if self._raise_on_get is not None:
            exc = self._raise_on_get
            self._raise_on_get = None
            raise exc
        if self._get_side_effects:
            return self._get_side_effects.pop(0)
        raise RuntimeError("FakeSession: nenhum efeito GET configurado")

    def post(self, url: str, **kwargs: Any) -> Any:
        self.post_calls.append((url, kwargs))
        if self._raise_on_post is not None:
            raise self._raise_on_post  # type: ignore[misc]
        if self._post_side_effects:
            return self._post_side_effects.pop(0)
        raise RuntimeError("FakeSession: nenhum efeito POST configurado")


class FakeResponse:
    """Resposta fake do requests."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: dict | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json_data

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise requests.HTTPError(f"HTTP {self.status_code}")


# ── Testes LMStudioClient ────────────────────────────────────────────────────

def test_list_models_success() -> None:
    """list_models retorna IDs de modelos."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "model-a"}, {"id": "model-b"}]})
    ]
    client = LMStudioClient(session=fake)
    models = client.list_models()
    assert models == ["model-a", "model-b"]


def test_list_models_connection_error() -> None:
    """ConnectionError em list_models → ServerNotRunningError."""
    fake = FakeSession()
    fake._raise_on_get = requests.ConnectionError("refused")
    client = LMStudioClient(session=fake)
    with pytest.raises(ServerNotRunningError):
        client.list_models()


def test_list_models_timeout() -> None:
    """Timeout em list_models → ServerNotRunningError."""
    fake = FakeSession()
    fake._raise_on_get = requests.Timeout("timed out")
    client = LMStudioClient(session=fake)
    with pytest.raises(ServerNotRunningError):
        client.list_models()


def test_resolve_model_given() -> None:
    """Se model fornecido, retorna ele."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "model-a"}]})
    ]
    client = LMStudioClient(session=fake, model="my-model")
    assert client.resolve_model() == "my-model"


def test_resolve_model_auto() -> None:
    """Sem model fornecido, retorna primeiro da lista."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "auto-1"}, {"id": "auto-2"}]})
    ]
    client = LMStudioClient(session=fake)
    assert client.resolve_model() == "auto-1"


def test_resolve_model_no_models() -> None:
    """list_models vazio → NoModelError."""
    fake = FakeSession()
    fake._get_side_effects = [FakeResponse(json_data={"data": []})]
    client = LMStudioClient(session=fake)
    with pytest.raises(NoModelError):
        client.resolve_model()


def test_chat_success() -> None:
    """Chat bem-sucedido retorna conteúdo."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={
                "choices": [{"message": {"content": "resposta test"}}]
            }
        )
    ]
    client = LMStudioClient(session=fake, model="test-model")
    result = client.chat([{"role": "user", "content": "ola"}])
    assert result == "resposta test"


def test_chat_connection_error_becomes_server_not_running() -> None:
    """ConnectionError no chat → ServerNotRunningError."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._raise_on_post = requests.ConnectionError("refused")
    client = LMStudioClient(session=fake, model="test-model")
    with pytest.raises(ServerNotRunningError):
        client.chat([{"role": "user", "content": "ola"}])


def test_chat_http_error_retries_then_fails() -> None:
    """HTTP 500 retry e falha final."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    # 3 respostas 500 (max_retries=3)
    fake._post_side_effects = [
        FakeResponse(status_code=500, json_data={}),
        FakeResponse(status_code=500, json_data={}),
        FakeResponse(status_code=500, json_data={}),
    ]
    client = LMStudioClient(session=fake, model="test-model", max_retries=3)
    with pytest.raises(LMStudioError, match="Erro HTTP 500"):
        client.chat([{"role": "user", "content": "ola"}])
    # Deveria ter tentado 3 vezes
    assert len(fake.post_calls) == 3


def test_chat_http_429_retries() -> None:
    """HTTP 429 retry."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    # 2x 429, depois sucesso
    fake._post_side_effects = [
        FakeResponse(status_code=429, json_data={}),
        FakeResponse(status_code=429, json_data={}),
        FakeResponse(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        ),
    ]
    client = LMStudioClient(session=fake, model="test-model", max_retries=3)
    result = client.chat([{"role": "user", "content": "ola"}])
    assert result == "ok"
    assert len(fake.post_calls) == 3


def test_chat_retry_then_success() -> None:
    """Retry até sucesso."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    # 1x falha, depois sucesso
    fake._post_side_effects = [
        FakeResponse(status_code=500, json_data={}),
        FakeResponse(
            json_data={"choices": [{"message": {"content": "final"}}]}
        ),
    ]
    client = LMStudioClient(session=fake, model="test-model", max_retries=3)
    result = client.chat([{"role": "user", "content": "ola"}])
    assert result == "final"
    assert len(fake.post_calls) == 2


def test_strip_think_blocks() -> None:
    """Blocos <thinking>...</thinking> são removidos do conteúdo."""
    content = "<thinking>\nPensando...\n</thinking>\n\nResposta final."
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={"choices": [{"message": {"content": content}}]}
        )
    ]
    client = LMStudioClient(session=fake, model="test-model")
    result = client.chat([{"role": "user", "content": "ola"}])
    assert "Pensando" not in result
    assert "Resposta final" in result


def test_strip_multiple_think_blocks() -> None:
    """Múltiplos blocos think removidos."""
    content = "<thinking>\nPensando 1\n</thinking>\n\n<thinking>\nPensando 2\n</thinking>\n\nResposta."
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={"choices": [{"message": {"content": content}}]}
        )
    ]
    client = LMStudioClient(session=fake, model="test-model")
    result = client.chat([{"role": "user", "content": "ola"}])
    assert "Pensando" not in result


def test_empty_content_after_strip() -> None:
    """Conteúdo vazio após strip de think → LMStudioError."""
    content = "<thinking>\n</thinking>\n<thinking>\n</thinking>"
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={"choices": [{"message": {"content": content}}]}
        )
    ]
    client = LMStudioClient(session=fake, model="test-model")
    with pytest.raises(LMStudioError, match="resposta vazia"):
        client.chat([{"role": "user", "content": "ola"}])


def test_empty_content_none() -> None:
    """Content None → LMStudioError."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={"choices": [{"message": {"content": None}}]}
        )
    ]
    client = LMStudioClient(session=fake, model="test-model")
    with pytest.raises(LMStudioError, match="resposta vazia"):
        client.chat([{"role": "user", "content": "ola"}])


def test_chat_with_max_tokens() -> None:
    """max_tokens é incluído no body."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
    ]
    client = LMStudioClient(session=fake, model="test-model")
    client.chat([{"role": "user", "content": "ola"}], max_tokens=100)
    _, kwargs = fake.post_calls[0]
    assert kwargs["json"]["max_tokens"] == 100


def test_chat_without_max_tokens_excludes_key() -> None:
    """Sem max_tokens, chave não deve estar no body."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
    ]
    client = LMStudioClient(session=fake, model="test-model")
    client.chat([{"role": "user", "content": "ola"}])
    _, kwargs = fake.post_calls[0]
    assert "max_tokens" not in kwargs["json"]


def test_lmstudio_error_hierarchy() -> None:
    """ServerNotRunningError e NoModelError são subclasses de LMStudioError."""
    assert issubclass(ServerNotRunningError, LMStudioError)
    assert issubclass(NoModelError, LMStudioError)


def test_error_messages_ptbr() -> None:
    """Mensagens de erro em pt-BR."""
    fake = FakeSession()
    fake._raise_on_get = requests.ConnectionError("refused")
    client = LMStudioClient(session=fake)
    with pytest.raises(ServerNotRunningError, match="não está rodando"):
        client.list_models()

    fake2 = FakeSession()
    fake2._get_side_effects = [FakeResponse(json_data={"data": []})]
    client2 = LMStudioClient(session=fake2)
    with pytest.raises(NoModelError, match="Nenhum modelo carregado"):
        client2.resolve_model()


# ── Testes: header de autenticação e mensagens de erro ─────────────────────

def test_list_models_envia_header_authorization() -> None:
    """Com api_key, o GET /models leva 'Authorization: Bearer <chave>'."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "model-a"}]})
    ]
    client = LMStudioClient(
        base_url="https://ollama.com/v1",
        api_key="sk-chave-secreta",
        session=fake,
    )
    client.list_models()

    assert len(fake.get_calls) == 1
    _, kwargs = fake.get_calls[0]
    headers = kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer sk-chave-secreta"


def test_chat_envia_header_authorization() -> None:
    """Com api_key, o POST /chat/completions leva o mesmo header."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
    ]
    client = LMStudioClient(
        base_url="https://ollama.com/v1",
        model="test-model",
        api_key="sk-chave-secreta",
        session=fake,
    )
    client.chat([{"role": "user", "content": "ola"}])

    assert len(fake.post_calls) == 1
    _, kwargs = fake.post_calls[0]
    headers = kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer sk-chave-secreta"


def test_sem_chave_nao_envia_header_authorization() -> None:
    """Sem api_key, NENHUMA requisição leva header Authorization."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(
            json_data={"choices": [{"message": {"content": "ok"}}]}
        )
    ]
    client = LMStudioClient(
        base_url="http://localhost:1234/v1",
        model="test-model",
        api_key="",
        session=fake,
    )
    client.chat([{"role": "user", "content": "ola"}])

    for _url, kwargs in fake.get_calls:
        assert "Authorization" not in (kwargs.get("headers") or {})
    for _url, kwargs in fake.post_calls:
        assert "Authorization" not in (kwargs.get("headers") or {})


def test_api_key_none_nao_envia_header_authorization() -> None:
    """api_key None também não gera header Authorization."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "model-a"}]})
    ]
    client = LMStudioClient(
        base_url="http://localhost:1234/v1",
        api_key=None,  # type: ignore[arg-type]
        session=fake,
    )
    client.list_models()

    _, kwargs = fake.get_calls[0]
    assert "Authorization" not in (kwargs.get("headers") or {})


def test_401_no_chat_nao_e_retentado() -> None:
    """401 vira erro em pt-BR, sem 'LM Studio', e NÃO é retentado."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [FakeResponse(status_code=401, json_data={})]
    client = LMStudioClient(
        base_url="https://ollama.com/v1",
        model="test-model",
        api_key="sk-chave-secreta",
        max_retries=3,
        session=fake,
    )

    with pytest.raises(LMStudioError) as excinfo:
        client.chat([{"role": "user", "content": "ola"}])

    mensagem = str(excinfo.value)
    assert "401" in mensagem
    assert "autorização" in mensagem or "chave" in mensagem
    assert "LM Studio" not in mensagem
    # A chave NUNCA aparece na mensagem de erro.
    assert "sk-chave-secreta" not in mensagem
    # Credencial inválida não melhora com retry: UMA única chamada.
    assert len(fake.post_calls) == 1


def test_401_no_list_models_nao_e_retentado() -> None:
    """401 no GET /models também vira erro em pt-BR (sem retry)."""
    fake = FakeSession()
    fake._get_side_effects = [FakeResponse(status_code=401, json_data={})]
    client = LMStudioClient(
        base_url="https://ollama.com/v1",
        api_key="sk-chave-secreta",
        session=fake,
    )

    with pytest.raises(LMStudioError) as excinfo:
        client.list_models()

    mensagem = str(excinfo.value)
    assert "401" in mensagem
    assert "autorização" in mensagem or "chave" in mensagem
    assert "LM Studio" not in mensagem
    assert "sk-chave-secreta" not in mensagem
    assert len(fake.get_calls) == 1


def test_mensagem_de_conexao_cita_base_url() -> None:
    """A mensagem de erro de conexão cita a base_url real."""
    fake = FakeSession()
    fake._raise_on_get = requests.ConnectionError("refused")
    client = LMStudioClient(
        base_url="https://exemplo.invalido/v1",
        session=fake,
    )
    with pytest.raises(ServerNotRunningError) as excinfo:
        client.list_models()

    mensagem = str(excinfo.value)
    assert "https://exemplo.invalido/v1" in mensagem
    assert "Não foi possível conectar" in mensagem
    assert "LM Studio" not in mensagem


def test_mensagem_http_cita_base_url() -> None:
    """A mensagem de erro HTTP cita a base_url real (sem 'LM Studio')."""
    fake = FakeSession()
    fake._get_side_effects = [
        FakeResponse(json_data={"data": [{"id": "test-model"}]})
    ]
    fake._post_side_effects = [
        FakeResponse(status_code=500, json_data={}),
        FakeResponse(status_code=500, json_data={}),
        FakeResponse(status_code=500, json_data={}),
    ]
    client = LMStudioClient(
        base_url="https://exemplo.invalido/v1",
        model="test-model",
        max_retries=3,
        session=fake,
    )
    with pytest.raises(LMStudioError) as excinfo:
        client.chat([{"role": "user", "content": "ola"}])

    mensagem = str(excinfo.value)
    assert "Erro HTTP 500" in mensagem
    assert "https://exemplo.invalido/v1" in mensagem
    assert "LM Studio" not in mensagem


# ── Testes MockClient ────────────────────────────────────────────────────────

def test_mock_list_models() -> None:
    """MockClient.list_models retorna ['mock-model']."""
    client = MockClient()
    assert client.list_models() == ["mock-model"]


def test_mock_resolve_model() -> None:
    """MockClient.resolve_model retorna 'mock-model'."""
    client = MockClient()
    assert client.resolve_model() == "mock-model"


def test_mock_is_mock() -> None:
    """MockClient.is_mock == True."""
    client = MockClient()
    assert client.is_mock is True


def test_mock_chat_identity() -> None:
    """MockClient com transform identity: blocos retornam texto original."""
    client = MockClient()
    messages = [
        {"role": "system", "content": "sistema"},
        {"role": "user", "content": "1\nbloco A\n2\nbloco B\n3\nbloco C\n"},
    ]
    result = client.chat(messages)
    assert "1" in result
    assert "2" in result
    assert "3" in result
    assert "bloco A" in result
    assert "bloco B" in result
    assert "bloco C" in result


def test_mock_chat_roundtrip_3_blocks() -> None:
    """MockClient round-trip com 3 blocos contendo marcadores [[n]]/[[/n]]."""
    client = MockClient()
    messages = [
        {"role": "user", "content": "1\ntexto com [[1]] marcador\n2\nmais texto [[2]] aqui\n3\n[[/3]] fim\n"},
    ]
    result = client.chat(messages)
    # O texto deve conter os marcadores originais
    assert "[[1]]" in result
    assert "[[2]]" in result
    assert "[[/3]]" in result
    # O texto original deve estar preservado
    assert "texto com " in result
    assert " marcador" in result
    assert "mais texto " in result
    assert " aqui" in result
    assert " fim" in result


def test_mock_chat_transform_uppercase() -> None:
    """Transform que maiúscula a 1ª letra: marcadores intactos, texto mudado."""
    def uppercase_first(text: str) -> str:
        """Maiúscula a primeira letra de cada palavra."""
        return " ".join(
            w[0].upper() + w[1:] if w else w
            for w in text.split(" ")
        )

    client = MockClient(transform=uppercase_first)
    messages = [
        {"role": "user", "content": "1\nhello world\n2\ntest message\n"},
    ]
    result = client.chat(messages)
    # Marcadores devem estar intactos
    assert "hello" not in result or "Hello" in result  # pode ter ambos
    assert "Hello" in result
    assert "World" in result
    assert "Test" in result
    assert "Message" in result
    # O texto original em minúsculo não deve estar (a menos que como parte de outra palavra)
    assert "hello world" not in result


def test_mock_chat_no_user_message() -> None:
    """Sem mensagem user, retorna string vazia."""
    client = MockClient()
    messages = [
        {"role": "system", "content": "sistema"},
    ]
    result = client.chat(messages)
    assert result == ""


def test_mock_chat_multiple_user_messages() -> None:
    """A última mensagem user é usada."""
    client = MockClient()
    messages = [
        {"role": "user", "content": "1\nantes\n"},
        {"role": "assistant", "content": "resposta"},
        {"role": "user", "content": "2\ndepois\n"},
    ]
    result = client.chat(messages)
    assert "2" in result
    assert "depois" in result
    assert "antes" not in result


def test_lmstudio_client_is_not_mock() -> None:
    """LMStudioClient.is_mock == False."""
    client = LMStudioClient()
    assert client.is_mock is False


# ── Testes MockClient: protocolo [i] ────────────────────────────────────────

def test_mock_chat_formato_com_colchetes() -> None:
    """Entrada no formato '[1]' devolve os mesmos índices e textos."""
    client = MockClient()
    messages = [{"role": "user", "content": "[1]\ntexto\n[2]\ntexto2"}]
    result = client.chat(messages)
    assert result == "[1]\ntexto\n[2]\ntexto2"


def test_mock_chat_formato_com_colchetes_parse_response() -> None:
    """A resposta do mock é parseável e devolve N pares (i, texto)."""
    client = MockClient()
    messages = [{"role": "user", "content": "[1]\ntexto\n[2]\ntexto2"}]
    parsed = parse_response(client.chat(messages))
    assert parsed == [(1, "texto"), (2, "texto2")]


def test_mock_chat_indices_nao_consecutivos() -> None:
    """Lote começando no bloco 7 mantém os índices 7 e 8."""
    client = MockClient()
    messages = [{"role": "user", "content": "[7]\nalpha\n[8]\nbeta"}]
    result = client.chat(messages)
    assert result == "[7]\nalpha\n[8]\nbeta"
    assert parse_response(result) == [(7, "alpha"), (8, "beta")]


def test_mock_chat_ecoa_texto_do_bloco_como_veio() -> None:
    """O mock devolve o texto do bloco exatamente como veio no prompt."""
    client = MockClient()
    messages = [{"role": "user", "content": "[1]\nOlá mundo"}]
    result = client.chat(messages)
    assert result == "[1]\nOlá mundo"


def test_mock_chat_nao_inclui_lixo_antes_do_primeiro_separador() -> None:
    """Texto antes do primeiro separador é descartado."""
    client = MockClient()
    messages = [{"role": "user", "content": "contexto solto\n[1]\ntexto"}]
    result = client.chat(messages)
    assert result == "[1]\ntexto"
    assert "contexto solto" not in result


def test_mock_chat_formato_cru_ainda_funciona() -> None:
    """Retrocompatibilidade: separador cru '1' (sem colchetes)."""
    client = MockClient()
    messages = [{"role": "user", "content": "1\ntexto\n2\ntexto2"}]
    result = client.chat(messages)
    assert result == "[1]\ntexto\n[2]\ntexto2"


def test_mock_chat_transform_preserva_marcadores() -> None:
    """O transform não corrompe os marcadores [[n]]/[[/n]]."""
    client = MockClient(transform=lambda t: t.upper())
    messages = [{"role": "user", "content": "[1]\ntexto [[1]] com [[/1]] marcador"}]
    result = client.chat(messages)
    assert "[[1]]" in result
    assert "[[/1]]" in result
    assert "TEXTO" in result


# ── Testes ChatProvider (Protocol) ──────────────────────────────────────────

def test_lmstudio_client_satisfaz_chat_provider() -> None:
    """LMStudioClient satisfaz o protocolo ChatProvider em runtime."""
    client = LMStudioClient()
    assert isinstance(client, ChatProvider)


def test_mock_client_satisfaz_chat_provider() -> None:
    """MockClient satisfaz o protocolo ChatProvider em runtime."""
    client = MockClient()
    assert isinstance(client, ChatProvider)


def test_chat_provider_exige_os_tres_metodos() -> None:
    """Um objeto sem os três métodos NÃO satisfaz o protocolo."""

    class Incompleto:
        def list_models(self) -> list[str]:
            return []

        def resolve_model(self) -> str:
            return "x"

    assert not isinstance(Incompleto(), ChatProvider)


def test_chat_provider_aceita_provedor_qualquer_openai_compativel() -> None:
    """Qualquer classe com os três métodos satisfaz o protocolo."""

    class ProvedorGenerico:
        def list_models(self) -> list[str]:
            return ["modelo-x"]

        def resolve_model(self) -> str:
            return "modelo-x"

        def chat(
            self,
            messages: list[dict[str, str]],
            temperature: float = 0.25,
            max_tokens: int | None = None,
        ) -> str:
            return "ok"

    provedor = ProvedorGenerico()
    assert isinstance(provedor, ChatProvider)
    assert provedor.list_models() == ["modelo-x"]
    assert provedor.resolve_model() == "modelo-x"
    assert provedor.chat([{"role": "user", "content": "oi"}]) == "ok"


def test_chat_provider_e_um_protocol_runtime_checkable() -> None:
    """O Protocol é runtime_checkable (permite isinstance)."""
    assert getattr(ChatProvider, "_is_runtime_protocol", False) is True
