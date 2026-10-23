"""Create the initial administrator once; never print the generated password."""
import json
import os
import secrets
from pathlib import Path
from sqlalchemy import select
from app.config import get_settings
from app.db import Database
from app.models import User
from app.security import hash_password

settings = get_settings()
email = 'admin@knowledge.jlubot.asia'
database = Database(settings)
with database.session() as db:
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        password = secrets.token_urlsafe(24)
        db.add(User(email=email, name='Administrator', password_hash=hash_password(password)))
        db.commit()
        path = settings.data_dir / 'initial-admin.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump({'url': settings.public_base_url, 'email': email, 'password': password}, output, indent=2)
        print('Initial administrator created; credentials saved privately.')
    else:
        print('Administrator already exists; password unchanged.')
database.engine.dispose()
