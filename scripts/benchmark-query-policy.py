"""Isolated query-policy smoke benchmark. Never writes to the deployed index."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "backend/tests/fixtures/query_policy.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.cases.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="query-policy-") as temporary:
        for name, value in {"DATA_DIR": temporary, "QDRANT_URL": "", "REDIS_URL": "", "LLM_MODEL": "",
                            "LLM_API_KEY": "", "EMBEDDING_PROVIDER": "hash", "RERANKER_MODEL": "",
                            "RERANKER_PROVIDER": "local", "ENVIRONMENT": "development"}.items():
            os.environ["RAG_" + name] = value
        from app.config import get_settings
        from app.services.query_evaluation import compare_queries
        from app.services.retrieval import client, index_chunks
        get_settings.cache_clear()
        try:
            index_chunks(fixture["chunks"])
            result = compare_queries(fixture["cases"], fixture["chunks"])
            result["fixture_note"] = fixture.get("note", "")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"output": str(args.output.resolve()), "paired": result["paired"]}, ensure_ascii=False))
        finally:
            if client.cache_info().currsize:
                client().close()
                client.cache_clear()
            get_settings.cache_clear()


if __name__ == "__main__":
    main()
