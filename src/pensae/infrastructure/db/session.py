from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        dsn,
        echo=echo,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=120000 -c lock_timeout=5000",
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
