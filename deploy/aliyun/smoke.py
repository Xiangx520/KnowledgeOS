"""Exercise a deployed instance with private administrator credentials."""
import json
import sys
import time
from pathlib import Path
import httpx

credentials = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
base = credentials['url']
report = {}
kb_id = conversation_id = None
with httpx.Client(base_url=base, headers={'Origin': base, 'X-RAG-Request': '1'}, timeout=180, follow_redirects=True, trust_env=False) as client:
    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response
    report['frontend'] = request('GET', '/').status_code
    report['health'] = request('GET', '/api/health').json()
    report['readiness'] = request('GET', '/api/ready').json()
    result = request('POST', '/api/auth/login', json={k: credentials[k] for k in ('email','password')}).json()
    report['admin_login'] = result['user']['is_platform_admin']
    assert report['admin_login'], 'Account lacks administrator permissions'
    report['model_listing'] = request('GET', '/api/ready/models').json()
    try:
        kb_id = request('POST', '/api/knowledge-bases', json={'name':'Deployment smoke test'}).json()['id']
        text = '# 知衡部署验收\n\n知衡知识助手的部署验收编号是 ZH-20261020。验收服务的入口是 https://knowledge.jlubot.asia 。本段文字用于检查文档上传、后台索引与引用问答。'
        document = request('POST', '/api/documents/upload', data={'knowledge_base_id':kb_id},
            files={'file':('deployment-smoke.md',text.encode('utf-8'),'text/markdown')}).json()
        document_id = document['document']['id']
        for _ in range(60):
            items = request('GET', '/api/documents',params={'knowledge_base_id':kb_id}).json()
            item = next(row for row in items if row['id']==document_id)
            if item['status'] in ('ready','failed'): break
            time.sleep(2)
        report['ingestion'] = {k:item.get(k) for k in ('status','chunk_count','error')}
        assert item['status']=='ready', report['ingestion']
        answer = request('POST', '/api/chat', json={'question':'知衡知识助手的部署验收编号是什么？',
            'knowledge_base_ids':[kb_id]}).json()
        conversation_id = answer.get('conversation_id')
        report['chat']={'answer':answer.get('answer'), 'citations':len(answer.get('citations',[])),
            'provider':answer.get('provider')}
        assert 'ZH-20261020' in answer.get('answer',''), 'Answer did not retrieve the test document'
        assert answer.get('citations'), 'Answer has no citations'
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    finally:
        if conversation_id: request('DELETE','/api/conversations/'+conversation_id)
        if kb_id: request('DELETE','/api/knowledge-bases/'+kb_id)
        client.post('/api/auth/logout')
Path(sys.argv[1]).with_name('aliyun-smoke-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
