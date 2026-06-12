from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self):
        from django.db.backends.signals import connection_created

        def _tune_sqlite(sender, connection, **kwargs):
            if connection.vendor != "sqlite":
                return
            with connection.cursor() as c:
                c.execute("PRAGMA journal_mode=WAL;")      # readers never block writers
                c.execute("PRAGMA synchronous=NORMAL;")    # safe + faster than FULL
                c.execute("PRAGMA cache_size=-65536;")     # 64 MB page cache in RAM
                c.execute("PRAGMA temp_store=MEMORY;")     # temp tables in RAM
                c.execute("PRAGMA mmap_size=268435456;")   # 256 MB memory-mapped IO

        connection_created.connect(_tune_sqlite)
