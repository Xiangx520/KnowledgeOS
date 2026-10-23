"""Verify registration and ordinary-user permissions, removing only the test user."""
import json
import secrets
import uuid
import httpx
from sqlalchemy import select
from app.config import get_settings
from app.db import Database
from app.models import User

settings = get_settings()
assert settings.allow_registration, 'Registration is disabled'
email = 'registration-check-' + uuid.uuid4().hex + '@example.com'
password = secrets.token_urlsafe(24)
user_id = None
report = {}
database = Database(settings)
try:
    with httpx.Client(base_url=settings.public_base_url, timeout=30, trust_env=False,
            headers={'Origin':settings.public_base_url,'X-RAG-Request':'1'}) as client:
        result = client.post('/api/auth/register',json={'email':email,'name':'Registration check','password':password})
        assert result.status_code == 201, ('register', result.status_code)
        user = result.json()['user']
        user_id = user['id']
        assert user['is_platform_admin'] is False
        assert client.cookies.get('rag_session')
        me = client.get('/api/auth/me')
        assert me.status_code == 200 and me.json()['email'] == email
        report['registration_and_automatic_login'] = 'passed'
        duplicate = client.post('/api/auth/register',json={'email':email,'name':'Registration check','password':password})
        assert duplicate.status_code == 409
        report['duplicate_email_rejected'] = 'passed'
        assert client.get('/api/platform/quotas').status_code == 403
        report['ordinary_user_cannot_access_admin_api'] = 'passed'
        assert client.post('/api/auth/logout').status_code == 204
        assert client.get('/api/auth/me').status_code == 401
        login = client.post('/api/auth/login',json={'email':email,'password':password})
        assert login.status_code == 200 and login.json()['user']['is_platform_admin'] is False
        report['logout_and_login'] = 'passed'
        assert client.get('/api/knowledge-bases').json() == []
        report['new_account_has_independent_workspace'] = 'passed'
        assert client.post('/api/auth/logout').status_code == 204
finally:
    with database.session() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None:
            assert user_id is None or user.id == user_id
            db.delete(user)
            db.commit()
    database.engine.dispose()
print(json.dumps(report,indent=2))
