import atexit
from pathlib import Path
from typing import Optional

from mr_data.config import settings


class PgEmbedManager:
    """Manages an embedded PostgreSQL server via pgembed.

    The server is started lazily and stopped on process exit.
    Instances are cached by data_dir so multiple PostgresStore objects
    in the same process share the same embedded server.
    """

    _instances: dict[str, "PgEmbedManager"] = {}

    def __new__(cls, data_dir: Optional[str] = None):
        target = Path(data_dir or settings.pgembed_data_dir).resolve()
        key = str(target)
        if key not in cls._instances:
            instance = super().__new__(cls)
            instance._data_dir = target
            instance._server = None
            instance._dsn = None
            cls._instances[key] = instance
        return cls._instances[key]

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def start(self) -> str:
        if self._dsn is not None:
            return self._dsn

        import pgembed

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._server = pgembed.get_server(str(self.data_dir))
        self._dsn = self._server.get_uri()
        atexit.register(self.stop)
        return self._dsn

    def stop(self) -> None:
        if self._server is not None:
            try:
                # PostgresServer uses cleanup() to stop the process.
                self._server.cleanup()
            except Exception:
                pass
            self._server = None
            self._dsn = None

    def get_dsn(self) -> str:
        if self._dsn is None:
            return self.start()
        return self._dsn


def get_pgembed_dsn(data_dir: Optional[str] = None) -> str:
    return PgEmbedManager(data_dir=data_dir).get_dsn()
