from __future__ import absolute_import
from __future__ import unicode_literals

from typing import Dict
from typing import Optional

from clikit.api.io.io import IO

from poetry.core.factory import Factory as BaseFactory
from poetry.core.utils.toml_file import TomlFile

from .config.config import Config
from .config.file_config_source import FileConfigSource
from .io.null_io import NullIO
from .locations import CONFIG_DIR
from .packages.locker import Locker
from .poetry import Poetry
from .repositories.pypi_repository import PyPiRepository
from .utils._compat import Path


class Factory(BaseFactory):
    """
    Factory class to create various elements needed by Poetry.
    """

    def create_poetry(
        self, cwd=None, io=None
    ):  # type: (Optional[Path], Optional[IO]) -> Poetry
        if io is None:
            io = NullIO()

        base_poetry = super(Factory, self).create_poetry(cwd)

        locker = Locker(
            base_poetry.file.parent / "poetry.lock", base_poetry.local_config
        )

        # Loading global configuration
        config = self.create_config(io)

        # Loading local configuration
        local_config_file = TomlFile(base_poetry.file.parent / "poetry.toml")
        if local_config_file.exists():
            if io.is_debug():
                io.write_line(
                    "Loading configuration file {}".format(local_config_file.path)
                )

            config.merge(local_config_file.read())

        poetry = Poetry(
            base_poetry.file.path,
            base_poetry.local_config,
            base_poetry.package,
            locker,
            config,
        )

        # Configuring sources
        for source in poetry.local_config.get("source", []):
            repository = self.create_legacy_repository(source, config)
            is_default = source.get("default", False)
            is_secondary = source.get("secondary", False)
            if io.is_debug():
                message = "Adding repository {} ({})".format(
                    repository.name, repository.url
                )
                if is_default:
                    message += " and setting it as the default one"
                elif is_secondary:
                    message += " and setting it as secondary"

                io.write_line(message)

            poetry.pool.add_repository(repository, is_default, secondary=is_secondary)

        # Always put PyPI last to prefer private repositories
        # but only if we have no other default source
        if not poetry.pool.has_default():
            poetry.pool.add_repository(PyPiRepository(), True)
        else:
            if io.is_debug():
                io.write_line("Deactivating the PyPI repository")

        return poetry

    @classmethod
    def create_config(cls, io=None):  # type: (Optional[IO]) -> Config
        if io is None:
            io = NullIO()

        config = Config()
        # Load global config
        config_file = TomlFile(Path(CONFIG_DIR) / "config.toml")
        if config_file.exists():
            if io.is_debug():
                io.write_line(
                    "<debug>Loading configuration file {}</debug>".format(
                        config_file.path
                    )
                )

            config.merge(config_file.read())

        config.set_config_source(FileConfigSource(config_file))

        # Load global auth config
        auth_config_file = TomlFile(Path(CONFIG_DIR) / "auth.toml")
        if auth_config_file.exists():
            if io.is_debug():
                io.write_line(
                    "<debug>Loading configuration file {}</debug>".format(
                        auth_config_file.path
                    )
                )

            config.merge(auth_config_file.read())

        config.set_auth_config_source(FileConfigSource(auth_config_file))

        return config

    def create_legacy_repository(
        self, source, auth_config
    ):  # type: (Dict[str, str], Config) -> LegacyRepository
        from .repositories.auth import Auth
        from .repositories.legacy_repository import LegacyRepository
        from .utils.helpers import get_client_cert, get_cert
        from .utils.password_manager import PasswordManager

        if "url" in source:
            # PyPI-like repository
            if "name" not in source:
                raise RuntimeError("Missing [name] in source.")
        else:
            raise RuntimeError("Unsupported source specified")

        password_manager = PasswordManager(auth_config)
        name = source["name"]
        url = source["url"]
        disable_ssl = source.get("disable-ssl", False)
        credentials = password_manager.get_http_auth(name)
        if credentials:
            auth = Auth(url, credentials["username"], credentials["password"])
        else:
            auth = None

        return LegacyRepository(
            name,
            url,
            disable_ssl=disable_ssl,
            auth=auth,
            cert=get_cert(auth_config, name),
            client_cert=get_client_cert(auth_config, name),
        )
