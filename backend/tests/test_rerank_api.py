import httpx
import pytest
from app.config import Settings
from app.services import retrieval


@pytest.fixture
def api_settings(monkeypatch):
    settings = Settings(_env_file=None, reranker_provider="dashscope", reranker_model="qwen3-rerank",
        reranker_api_url="https://example.com/rerank", llm_api_key="test-key")
    monkeypatch.setattr(retrieval, "get_settings", lambda: settings)
    monkeypatch.setattr(retrieval, "reranker", lambda _: pytest.fail("API mode loaded a local model"))
    return settings


def test_api_scores_follow_original_indexes(api_settings, monkeypatch):
    api_settings.reranker_api_key = "separate-key"

    def post(url, **kwargs):
        assert url == api_settings.reranker_api_url
        assert kwargs["headers"]["Authorization"] == "Bearer separate-key"
        assert kwargs["json"]["input"] == {"query": "topic", "documents": ["first", "second"]}
        assert kwargs["json"]["parameters"]["top_n"] == 2
        return httpx.Response(200, json={"output": {"results": [
            {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .1}]}})

    monkeypatch.setattr(httpx, "post", post)
    ranked = retrieval.rerank_candidates("topic", [{"text": "first"}, {"text": "second"}], 1)
    assert ranked == [{"text": "second", "rerank_score": .9}]


@pytest.mark.parametrize("results", [[], [{"index": 9, "relevance_score": .8}],
    [{"index": 0, "relevance_score": .8}, {"index": 0, "relevance_score": .9}],
    [{"index": 0, "relevance_score": "bad"}]])
def test_api_rejects_invalid_results(api_settings, monkeypatch, results):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json={"output": {"results": results}}))
    with pytest.raises(ValueError, match="精排 API"):
        retrieval.rerank_scores("topic", ["first", "second"])


def test_api_auth_error_does_not_expose_response(api_settings, monkeypatch):
    def post(*args, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        return httpx.Response(401, text="sensitive input")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(ValueError, match="HTTP 401") as error:
        retrieval.rerank_scores("topic", ["first"])
    assert "sensitive" not in str(error.value)


def test_disabled_reranking_does_not_call_api(api_settings, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: pytest.fail("disabled reranking called API"))
    assert retrieval.rerank_candidates("topic", [{"text": "first"}], 1, use_model=False)
