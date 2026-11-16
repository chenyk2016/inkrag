"""验证实际BGE分词工件、TEI预算和原生切分边界，HTTP替身仅隔离推理调用。"""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from inkparse import FileSource, ParseOptions, Parser, ParseRequest
from inkrag_platform import embedding as module
from inkrag_platform.embedding import (
    MODEL_ID,
    MODEL_REVISION,
    TOKENIZER_PATH,
    TOKENIZER_SHA256,
    RemoteEmbedding,
    load_tokenizer,
)
from inkrag_platform.models import DomainError
from pydantic import JsonValue, TypeAdapter

from inkrag.models import ProcessingConfig
from inkrag.search import result_nodes


def test_bundled_tokenizer_preserves_long_multilingual_document(tmp_path: Path) -> None:
    """精确WordPiece切分中文、emoji和英文；所有正文进入片段，且为特殊token留足空间。"""
    assert hashlib.sha256(TOKENIZER_PATH.read_bytes()).hexdigest() == TOKENIZER_SHA256
    tokenizer = load_tokenizer(MODEL_REVISION)

    def encode(text: str) -> list[int]:
        """提供原生token ID，切分计数不包含CLS和SEP。"""
        return tokenizer.encode(text, add_special_tokens=False).ids

    sentence = (
        "企业文档包含中文架构说明🙂🚀。 English enterprise architecture and operating procedure.\n"
    )
    text = sentence * 80 + "最后的完整性标记TAIL-END。"
    source = tmp_path / "long.txt"
    source.write_text(text)
    parsed = Parser().parse(
        ParseRequest(source=FileSource(path=str(source)), options=ParseOptions(format="text"))
    )
    nodes = result_nodes(
        "tenant",
        "kb",
        "document",
        parsed,
        ProcessingConfig(chunk_size=8192, chunk_overlap=32),
        tokenizer=encode,
        max_tokens=510,
        tokenizer_revision=TOKENIZER_SHA256,
    )
    assert len(nodes) > 1
    assert all(
        len(tokenizer.encode(node.get_content(), add_special_tokens=True).ids) <= 512
        for node in nodes
    )
    assert any("TAIL-END" in node.get_content() for node in nodes)
    assert all("🙂🚀" in node.get_content() for node in nodes[:-1])
    assert sum(node.get_content().count("English enterprise") for node in nodes) >= 80


def test_remote_embedding_counts_prefix_and_splits_real_service_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询前缀计入512上限；批请求按真实条数与padding token预算分批且不截断。"""
    batches: list[list[str]] = []
    original_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        """返回明确的上游契约，记录正式适配器发送的输入及truncate参数。"""
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "model_id": MODEL_ID,
                    "model_sha": MODEL_REVISION,
                    "model_dtype": "float32",
                    "model_type": {"embedding": {"pooling": "cls"}},
                    "max_input_length": 512,
                    "max_client_batch_size": 2,
                    "max_batch_tokens": 512,
                },
            )
        packet = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
        assert packet["truncate"] is False
        texts = TypeAdapter(list[str]).validate_python(packet["inputs"], strict=True)
        batches.append(texts)
        return httpx.Response(200, json=[[0.1, 0.2, 0.3, 0.4] for _ in texts])

    def client(*, base_url: str, timeout: httpx.Timeout, trust_env: bool) -> httpx.Client:
        """只替换HTTP传输，保持正式Client的请求与响应行为。"""
        return original_client(
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr(module.httpx, "Client", client)
    model = RemoteEmbedding("https://tei.example", MODEL_REVISION)
    try:
        assert (
            model.max_text_tokens == 510 and model.metadata["tokenizer_sha256"] == TOKENIZER_SHA256
        )
        before = len(batches)
        with pytest.raises(DomainError) as caught:
            model.get_query_embedding("中" * 510)
        assert caught.value.code == "embedding_input_too_long" and len(batches) == before
        assert len(model.get_text_embedding_batch(["中" * 300] * 5)) == 5
        assert [len(batch) for batch in batches[before:]] == [1] * 5
        before = len(batches)
        assert len(model.get_text_embedding_batch(["短句"] * 5)) == 5
        assert [len(batch) for batch in batches[before:]] == [2, 2, 1]
        assert json.loads(json.dumps(model.metadata))["max_input_tokens"] == 512
    finally:
        model.close()


@pytest.mark.parametrize(
    "payload", [{"vectors": "wrong-schema"}, [], [[0.1, 0.2]], [[0.0, 0.0, 0.0, 0.0]]]
)
def test_invalid_embedding_response_is_dependency_error(
    monkeypatch: pytest.MonkeyPatch, payload: JsonValue
) -> None:
    """上游结构、数量、维度或全零错误统一依赖类别，不误判用户输入无效。"""
    original_client, calls = httpx.Client, 0

    def handle(request: httpx.Request) -> httpx.Response:
        """只让启动探针成功，随后提供有意损坏的上游响应。"""
        nonlocal calls
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "model_id": MODEL_ID,
                    "model_sha": MODEL_REVISION,
                    "model_dtype": "float32",
                    "model_type": {"embedding": {"pooling": "cls"}},
                    "max_input_length": 512,
                    "max_client_batch_size": 32,
                    "max_batch_tokens": 8192,
                },
            )
        calls += 1
        return httpx.Response(200, json=[[0.1, 0.2, 0.3, 0.4]] if calls == 1 else payload)

    def client(*, base_url: str, timeout: httpx.Timeout, trust_env: bool) -> httpx.Client:
        """保留正式HTTP客户端，只替换上游传输。"""
        return original_client(
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr(module.httpx, "Client", client)
    model = RemoteEmbedding("https://tei.example", MODEL_REVISION)
    try:
        with pytest.raises(DomainError) as caught:
            model.get_text_embedding("有效正文")
        assert caught.value.code == "invalid_embedding_response"
    finally:
        model.close()
