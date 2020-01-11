import os

from subprocess import CalledProcessError

from requests import Session

from poetry.core.packages.file_dependency import FileDependency
from poetry.core.packages.package import Package
from poetry.io.null_io import NullIO
from poetry.locations import CACHE_DIR
from poetry.puzzle.operations.install import Install
from poetry.puzzle.operations.uninstall import Uninstall
from poetry.puzzle.operations.update import Update
from poetry.utils._compat import Path
from poetry.utils.helpers import safe_rmtree

from .chef import Chef
from .chooser import Chooser


class Executor(object):
    def __init__(self, env, io):
        self._env = env
        self._io = io
        self._dry_run = False
        self._enabled = True
        self._verbose = False
        self._chef = Chef(self._env)
        self._chooser = Chooser(self._env)
        self._cache_dir = Path(CACHE_DIR) / "artifacts"

    def disable(self):
        self._enabled = False

        return self

    def dry_run(self, dry_run=True):
        self._dry_run = dry_run

        return self

    def verbose(self, verbose=True):
        self._verbose = verbose

        return self

    def execute(self, operations):
        for operation in operations:
            self.execute_operation(operation)

    def execute_operation(self, operation):
        method = operation.job_type

        return getattr(self, "_execute_{}".format(method))(operation)

    def run(self, *args, **kwargs):  # type: (...) -> str
        return self._env.run("python", "-m", "pip", *args, **kwargs)

    def _execute_install(self, operation):  # type: (Install) -> None
        if operation.skipped:
            if self._verbose and (self._enabled or self._dry_run):
                self._io.write_line(
                    "  - Skipping <c1>{}</c1> (<b>{}</b>) {}".format(
                        operation.package.pretty_name,
                        operation.package.full_pretty_version,
                        operation.skip_reason,
                    )
                )

            return

        if not self._enabled or self._dry_run:
            self._io.write_line(
                "  - Installing <c1>{}</c1> (<b>{}</b>)".format(
                    operation.package.pretty_name, operation.package.full_pretty_version
                )
            )

            return

        self._install(operation.package)

    def _execute_update(self, operation):  # type: (Update) -> None
        source = operation.initial_package
        target = operation.target_package

        if operation.skipped:
            if self._verbose and (self._enabled or self._dry_run):
                self._io.write_line(
                    "  - Skipping <c1>{}</c1> (<b>{}</b>) {}".format(
                        target.pretty_name,
                        target.full_pretty_version,
                        operation.skip_reason,
                    )
                )

            return

        if self._enabled or self._dry_run:
            self._io.write_line(
                "  - Updating <c1>{}</c1> (<b>{}</b> -> <b>{}</b>)".format(
                    target.pretty_name,
                    source.full_pretty_version,
                    target.full_pretty_version,
                )
            )

        if not self._enabled:
            return

        self._update(source, target)

    def _execute_uninstall(self, operation):  # type: (Uninstall) -> None
        if operation.skipped:
            if self._verbose and (self._enabled or self._dry_run):
                self._io.write_line(
                    "  - Not removing <c1>{}</c1> (<b>{}</b>) {}".format(
                        operation.package.pretty_name,
                        operation.package.full_pretty_version,
                        operation.skip_reason,
                    )
                )

            return

        if self._enabled or self._dry_run:
            self._io.write_line(
                "  - Removing <c1>{}</c1> (<b>{}</b>)".format(
                    operation.package.pretty_name, operation.package.full_pretty_version
                )
            )

        if not self._enabled:
            return

        self._remove(operation.package)

    def _install(self, package, update=False):
        if package.source_type == "directory":
            self._install_directory(package)

            return

        if package.source_type == "git":
            self._install_git(package)

            return

        archive = self._download(package)
        message = "  - Installing <c1>{}</c1> (<b>{}</b>)".format(
            package.name, package.version
        )
        if not self._io.output.supports_ansi() or self._io.is_debug():
            self._io.write_line(message)
        else:
            self._io.overwrite(message)
            self._io.write_line("")

        args = ["install", "--no-deps", str(archive)]
        if update:
            args.insert(2, "-U")

        self.run(*args)

    def _update(self, source, target):
        return self._install(target, update=True)

    def _remove(self, package):
        # If we have a VCS package, remove its source directory
        if package.source_type == "git":
            src_dir = self._env.path / "src" / package.name
            if src_dir.exists():
                safe_rmtree(str(src_dir))

        try:
            self.run("uninstall", package.name, "-y")
        except CalledProcessError as e:
            if "not installed" in str(e):
                return

            raise

    def _install_directory(self, package, from_vcs=False):
        from poetry.factory import Factory
        from poetry.masonry.builder import SdistBuilder
        from poetry.utils._compat import decode
        from poetry.utils.env import NullEnv
        from poetry.utils.toml_file import TomlFile

        if not from_vcs:
            message = "  - Installing <c1>{}</c1> (<b>{}</b>)".format(
                package.name, package.full_pretty_version
            )
            self._io.write_line(message)

        if package.root_dir:
            req = os.path.join(package.root_dir, package.source_url)
        else:
            req = os.path.realpath(package.source_url)

        args = ["install", "--no-deps", "-U"]

        pyproject = TomlFile(os.path.join(req, "pyproject.toml"))

        has_poetry = False
        has_build_system = False
        if pyproject.exists():
            pyproject_content = pyproject.read()
            has_poetry = (
                "tool" in pyproject_content and "poetry" in pyproject_content["tool"]
            )
            # Even if there is a build system specified
            # pip as of right now does not support it fully
            # TODO: Check for pip version when proper PEP-517 support lands
            # has_build_system = ("build-system" in pyproject_content)

        setup = os.path.join(req, "setup.py")
        has_setup = os.path.exists(setup)
        if not has_setup and has_poetry and (package.develop or not has_build_system):
            # We actually need to rely on creating a temporary setup.py
            # file since pip, as of this comment, does not support
            # build-system for editable packages
            # We also need it for non-PEP-517 packages
            builder = SdistBuilder(
                Factory().create_poetry(pyproject.parent), NullEnv(), NullIO()
            )

            with open(setup, "w") as f:
                f.write(decode(builder.build_setup()))

        if package.develop:
            args.append("-e")

        args.append(req)
        try:
            return self.run(*args)
        finally:
            if not has_setup and os.path.exists(setup):
                os.remove(setup)

    def _install_git(self, package):
        from poetry.packages import Package
        from poetry.vcs import Git

        def _clone():
            src_dir = self._env.path / "src" / package.name
            if src_dir.exists():
                safe_rmtree(str(src_dir))

            src_dir.parent.mkdir(exist_ok=True)

            git = Git()
            git.clone(package.source_url, src_dir)
            git.checkout(package.source_reference, src_dir)

            # Now we just need to install from the source directory
            pkg = Package(package.name, package.version)
            pkg.source_type = "directory"
            pkg.source_url = str(src_dir)
            pkg.develop = True

            return pkg

        message = "  - Cloning <info>{}</info> (<comment>{}</comment>)".format(
            package.name, package.full_pretty_version
        )
        if not self._io.output.supports_ansi() or self._io.is_debug():
            self._io.write_line(message)
        else:
            self._io.write(message)

        pkg = _clone()

        message = "  - Installing <info>{}</info> (<comment>{}</comment>)".format(
            package.name, package.full_pretty_version
        )
        if not self._io.output.supports_ansi() or self._io.is_debug():
            self._io.write_line(message)
        else:
            self._io.overwrite(message)
            self._io.write_line("")

        self._install_directory(pkg, from_vcs=True)

    def _download(self, package):  # type: (Package) -> Path
        cache_dir = self._cache_dir / package.name
        cache_dir.mkdir(parents=True, exist_ok=True)

        link = self._chooser.choose_for(package)

        archive = cache_dir / link.filename
        if not archive.exists():
            session = Session()
            response = session.get(link.url, stream=True)
            wheel_size = response.headers.get("content-length")
            message = "  - Downloading <c1>{}</c1> (<b>{}</b>)".format(
                package.name, package.version
            )
            if not self._io.output.supports_ansi() or self._io.is_debug():
                self._io.write_line(message)
            else:
                if wheel_size is None:
                    progress = self._io.progress_indicator(
                        fmt="{} <b>{{indicator}}</b>".format(message)
                    )
                else:
                    progress = self._io.progress_bar(max=int(wheel_size))
                    progress.set_format("{} <b>%percent%%</b>".format(message))

                progress.start()

            done = 0
            with archive.open("wb") as f:
                for chunk in response.iter_content(chunk_size=4096):
                    if not chunk:
                        break

                    done += len(chunk)

                    if self._io.output.supports_ansi() or self._io.is_debug():
                        if wheel_size is None:
                            progress.advance()
                        else:
                            progress.set_progress(done)

                    f.write(chunk)

            if not link.is_wheel:
                archive = self._chef.prepare(archive)

        if package.files:
            archive_hash = "sha256:" + FileDependency(package.name, archive).hash()
            if archive_hash not in {f["hash"] for f in package.files}:
                raise RuntimeError(
                    "Invalid hash for {} using archive {}".format(package, archive.name)
                )

        return archive
