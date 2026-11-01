"""Quality isolation, source-preserving context, and indexing contracts."""
import json
import time
from types import SimpleNamespace

import pytest
from app.config import get_settings
from app.models import Chunk, ChunkTerm, Document, DocumentRevision, IngestionJob
from app.services.context_enrichment import enrich_chunks, source_window, verified_context
from app.services.document_structure import ParsedPage
from app.services.quality import NeedsReview, prepare_pages, validate_chunks
from sqlalchemy import select
from test_api import create_kb, register, upload, wait_job


def wait_terminal(client, headers, job_id):
    for _ in range(200):
        result = client.get('/api/jobs/' + job_id, headers=headers).json()
        if result['status'] in {'completed', 'failed', 'needs_review'}:
            return result
        time.sleep(.05)
    pytest.fail('Job did not reach terminal state')


def test_quality_inspects_before_cleaning_and_allows_blank_cover():
    pages, report = prepare_pages([ParsedPage(' \n', 1), ParsedPage('住宿不超过500元。\r\n  条件不变。', 2)])
    assert report['status'] == 'passed'
    assert report['pages'][0]['issues'][0]['code'] == 'empty_page'
    assert '\r' not in pages[1].text and '  条件' in pages[1].text
    for value in ('\ufffd' * 100 + '金额500', '\x00' * 100 + '条款', '   '):
        with pytest.raises(NeedsReview):
            prepare_pages([ParsedPage(value)])


def test_chunk_gate_rejects_corrupt_output():
    with pytest.raises(ValueError, match='超长'):
        validate_chunks([{'text': 'x' * 101, 'ordinal': 0}], 100)
    with pytest.raises(ValueError, match='连续'):
        validate_chunks([{'text': 'x', 'ordinal': 2}], 100)


def test_pdf_bad_text_layer_is_replaced_by_ocr_or_reviewed(monkeypatch, tmp_path):
    import pypdfium2
    from app.services import parsing

    closed = []
    def resource(**kwargs):
        return SimpleNamespace(close=lambda: closed.append(True), **kwargs)
    image = resource(save=lambda *args, **kwargs: None)
    bitmap = resource(to_pil=lambda: image)
    textpage = resource(get_text_range=lambda: '\ufffd' * 100)
    page = resource(get_textpage=lambda: textpage, get_objects=lambda **kwargs: [], render=lambda **kwargs: bitmap)

    class PDF:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return page

        def close(self):
            closed.append(True)

    monkeypatch.setattr(pypdfium2, 'PdfDocument', lambda path: PDF())
    monkeypatch.setattr(parsing, '_image_text', lambda data: '住宿上限不超过500元。')
    pages = parsing.parse_document(tmp_path / 'scan.pdf', 'scan.pdf')
    assert pages[0].text == '住宿上限不超过500元。' and len(closed) == 5

    def unavailable(data):
        raise ValueError('OCR unavailable')

    monkeypatch.setattr(parsing, '_image_text', unavailable)
    with pytest.raises(NeedsReview) as error:
        parsing.parse_document(tmp_path / 'scan.pdf', 'scan.pdf')
    assert error.value.report['pages'][0]['page'] == 1
    assert len(closed) == 10


def test_context_window_and_exact_extract_validation():
    chunks = [{'text': '住宿上限不超过500元。' * 10, 'page': i + 1} for i in range(10)]
    window = source_window(chunks, 5, 1024)
    assert any(s['id'] == 5 for s in window)
    assert sum(len(json.dumps(s, ensure_ascii=False).encode()) + 2 for s in window) <= 1024
    context, locators = verified_context({'excerpts': [{'source_id': 5, 'text': '住宿上限不超过500元。'}]}, window)
    assert context == '住宿上限不超过500元。' and locators[0]['page'] == 6
    for item in ({'source_id': 99, 'text': '500'}, {'source_id': 5, 'text': '上限1000元'},
                 {'source_id': True, 'text': '500'}):
        assert verified_context({'excerpts': [item]}, window) == ('', [])


def test_enrichment_is_bounded_and_failure_preserves_source(monkeypatch):
    from app.services import answering
    settings = get_settings().model_copy(update={'context_enrichment_enabled': True, 'context_enrichment_max_calls': 1})
    calls = []

    class Model:
        def invoke(self, messages):
            calls.append(messages)
            return SimpleNamespace(content='{"excerpts":[{"source_id":0,"text":"差旅制度"}]}')

    monkeypatch.setattr(answering, 'llm_ready', lambda: True)
    monkeypatch.setattr(answering, 'configured_llm', lambda **kwargs: Model())
    chunks = [{'text': '差旅制度：住宿不超过500元。', 'page': 1, 'ordinal': 0},
              {'text': '需要发票。', 'page': 2, 'ordinal': 1}]
    stats = enrich_chunks(chunks, settings)
    assert stats['calls'] == 1 and len(calls) == 1 and stats['enhanced'] == 1
    assert chunks[0]['text'] == '差旅制度：住宿不超过500元。'
    assert chunks[0]['context_summary'] == '差旅制度'
    assert chunks[0]['source_metadata']['context_sources'][0]['ordinal'] == 0

    class Broken:
        def invoke(self, messages):
            raise TimeoutError()

    monkeypatch.setattr(answering, 'configured_llm', lambda **kwargs: Broken())
    stats = enrich_chunks(chunks, settings)
    assert stats['fallback'] == 1 and chunks[0]['context_summary'] == ''


def test_review_stops_before_index_and_can_be_replaced(client, monkeypatch):
    from app.services import retrieval
    owner = register(client)
    kb = create_kb(client, owner)
    calls = []
    original = retrieval.index_chunks
    monkeypatch.setattr(retrieval, 'index_chunks', lambda *args, **kwargs: calls.append(args))
    uploaded = upload(client, owner, kb, ('\ufffd' * 100 + '住宿500元').encode())
    result = wait_terminal(client, owner, uploaded['job_id'])
    assert result['status'] == 'needs_review' and not calls
    assert result['quality_report']['pages'][0]['issues'][0]['code'] == 'corrupt_text'
    document_id = uploaded['document']['id']
    versions = client.get(f'/api/documents/{document_id}/versions', headers=owner).json()
    assert versions[0]['status'] == 'needs_review' and not versions[0]['active']
    outsider = register(client, 'outsider@example.com')
    assert client.get('/api/jobs/' + uploaded['job_id'], headers=outsider).status_code == 404
    # No automatic retry loop; explicit retry still passes through the same gate.
    retried = client.post(f'/api/documents/{document_id}/retry', headers=owner)
    assert retried.status_code == 200
    assert wait_terminal(client, owner, retried.json()['job_id'])['status'] == 'needs_review'
    monkeypatch.setattr(retrieval, 'index_chunks', original)
    replaced = client.post('/api/documents/upload', headers=owner,
        data={'knowledge_base_id': kb, 'document_id': document_id},
        files={'file': ('clear.txt', '住宿报销上限500元。'.encode(), 'text/plain')})
    assert replaced.status_code == 201, replaced.text
    assert wait_job(client, owner, replaced.json()['job_id'])['status'] == 'completed'


def test_bad_update_keeps_active_version_and_retry_uses_rejected_file(client):
    owner = register(client)
    kb = create_kb(client, owner)
    uploaded = upload(client, owner, kb)
    wait_job(client, owner, uploaded['job_id'])
    document_id = uploaded['document']['id']
    with client.app.state.database.session() as db:
        active = db.get(Document, document_id).active_revision_id
    replaced = client.post('/api/documents/upload', headers=owner,
        data={'knowledge_base_id': kb, 'document_id': document_id},
        files={'file': ('bad.txt', ('\ufffd' * 100).encode(), 'text/plain')}).json()
    assert wait_terminal(client, owner, replaced['job_id'])['status'] == 'needs_review'
    with client.app.state.database.session() as db:
        doc = db.get(Document, document_id)
        assert doc.active_revision_id == active and doc.status == 'ready'
        assert db.scalars(select(Chunk).where(Chunk.revision_id == active)).all()
    retried = client.post(f'/api/documents/{document_id}/retry', headers=owner).json()
    assert wait_terminal(client, owner, retried['job_id'])['status'] == 'needs_review'
    with client.app.state.database.session() as db:
        job = db.get(IngestionJob, retried['job_id'])
        assert db.get(DocumentRevision, job.revision_id).name == 'bad.txt'


def test_context_is_indexed_but_chunk_api_keeps_original_evidence(client, monkeypatch):
    from app.services import answering
    from app.services.embeddings import tokenize
    settings = client.app.state.settings
    settings.context_enrichment_enabled = True
    monkeypatch.setattr(answering, 'llm_ready', lambda: True)

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(content='{"excerpts":[{"source_id":0,"text":"差旅制度"}]}')

    monkeypatch.setattr(answering, 'configured_llm', lambda **kwargs: Model())
    owner = register(client)
    kb = create_kb(client, owner)
    uploaded = upload(client, owner, kb, '差旅制度。住宿上限500元。'.encode())
    result = wait_job(client, owner, uploaded['job_id'])
    assert result['status'] == 'completed', result
    chunks = client.get('/api/documents/' + uploaded['document']['id'] + '/chunks', headers=owner).json()
    assert chunks[0]['text'] == '差旅制度。住宿上限500元。'
    assert chunks[0]['context_summary'] == '差旅制度'
    with client.app.state.database.session() as db:
        c = db.get(Chunk, chunks[0]['id'])
        assert c.token_count == len([t for t in tokenize('差旅制度\n\n' + c.text) if len(t) <= 128])
        assert db.scalars(select(ChunkTerm).where(ChunkTerm.chunk_id == c.id)).all()
