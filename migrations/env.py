import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from isakisi.db import Store
from isakisi.settings import Settings

if context.is_offline_mode():
    context.configure(url="sqlite://", literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    if os.name != "nt":
        os.umask(0o077)
    store = Store(Settings())
    engine = create_engine(URL.create("sqlite", database=str(store.path)))
    with engine.connect() as conn:
        context.configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()
