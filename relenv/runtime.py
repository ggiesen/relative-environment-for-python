# Copyright 2022-2023 VMware, Inc.
# SPDX-License-Identifier: Apache-2
"""
This code is run when initializing the python interperter in a Relenv environment.

- Point Relenv's Openssl to the system installed Openssl certificate path
- Make sure pip creates scripts with a shebang that points to the correct
  python using a relative path.
- On linux, provide pip with the proper location of the Relenv toolchain
  gcc. This ensures when using pip any c dependencies are compiled against the
  proper glibc version.
"""
import contextlib
import functools
import importlib
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

from .common import MODULE_DIR, format_shebang, get_triplet, work_dirs


def get_major_version():
    """
    Current python major version.
    """
    return "{}.{}".format(*sys.version_info)


@contextlib.contextmanager
def pushd(new_dir):
    """
    Changedir context.
    """
    old_dir = os.getcwd()
    os.chdir(new_dir)
    try:
        yield
    finally:
        os.chdir(old_dir)


def debug(string):
    """
    Prints the provided message if RELENV_DEBUG is truthy in the environment.

    :param string: The message to print
    :type string: str
    """
    if os.environ.get("RELENV_DEBUG"):
        print(string)


def relenv_root():
    """
    Return the relenv module root.
    """
    # XXX Look for rootdir / ".relenv"
    if sys.platform == "win32":
        # /Lib/site-packages/relenv/
        return MODULE_DIR.parent.parent.parent
    # /lib/pythonX.X/site-packages/relenv/
    return MODULE_DIR.parent.parent.parent.parent


def _build_shebang(*args, **kwargs):
    """
    Build a shebang to point to the proper location.

    :return: The shebang
    :rtype: bytes
    """
    debug("Relenv - _build_shebang")
    if sys.platform == "win32":
        if os.environ.get("RELENV_PIP_DIR"):
            return "#!<launcher_dir>\\Scripts\\python.exe".encode()
        return "#!<launcher_dir>\\python.exe".encode()
    if os.environ.get("RELENV_PIP_DIR"):
        return format_shebang("/bin/python3").encode()
    return format_shebang("/python3").encode()


def get_config_var_wrapper(func):
    """
    Return a wrapper to resolve paths relative to the relenv root.
    """

    def wrapped(name):
        if name == "BINDIR":
            orig = func(name)
            if os.environ.get("RELENV_PIP_DIR"):
                val = relenv_root()
            else:
                val = relenv_root() / "Scripts"
            debug(f"get_config_var call {name} old: {orig} new: {val}")
            return val
        else:
            val = func(name)
            debug(f"get_config_var call {name} {val}")
            return val

    return wrapped


def get_paths_wrapper(func, default_scheme):
    """
    Return a wrapper to resolve paths relative to the relenv root.
    """

    def wrapped(scheme=default_scheme, vars=None, expand=True):
        paths = func(scheme=scheme, vars=vars, expand=expand)
        if "RELENV_PIP_DIR" in os.environ:
            paths["scripts"] = str(relenv_root())
            sys.exec_prefix = paths["scripts"]
        return paths

    return wrapped


def finalize_options_wrapper(func):
    """
    Wrapper around build_ext.finalize_options.

    Used to add the relenv environment's include path.
    """

    def wrapper(self, *args, **kwargs):
        func(self, *args, **kwargs)
        self.include_dirs.append(f"{relenv_root()}/include")

    return wrapper


def install_wheel_wrapper(func):
    """
    Wrap pip's wheel install function.

    This method determines any newly installed files and checks their RPATHs.
    """

    @functools.wraps(func)
    def wrapper(
        name,
        wheel_path,
        scheme,
        req_description,
        pycompile,
        warn_script_location,
        direct_url,
        requested,
    ):
        from zipfile import ZipFile

        from pip._internal.utils.wheel import parse_wheel

        from relenv import relocate

        with ZipFile(wheel_path) as zf:
            info_dir, metadata = parse_wheel(zf, name)
        func(
            name,
            wheel_path,
            scheme,
            req_description,
            pycompile,
            warn_script_location,
            direct_url,
            requested,
        )
        plat = pathlib.Path(scheme.platlib)
        rootdir = relenv_root()
        with open(plat / info_dir / "RECORD") as fp:
            for line in fp.readlines():
                file = plat / line.split(",", 1)[0]
                if not file.exists():
                    debug(f"Relenv - File not found {file}")
                    continue
                if relocate.is_elf(file):
                    debug(f"Relenv - Found elf {file}")
                    relocate.handle_elf(plat / file, rootdir / "lib", True, rootdir)

    return wrapper


def install_legacy_wrapper(func):
    """
    Wrap pip's legacy install function.

    This method determines any newly installed files and checks their RPATHs.
    """
    # XXX It might be better to handle legacy installs by overriding things in
    # setuptools, would we get more bang for our buck or increase complexity?

    @functools.wraps(func)
    def wrapper(
        install_options,
        global_options,
        root,
        home,
        prefix,
        use_user_site,
        pycompile,
        scheme,
        setup_py_path,
        isolated,
        req_name,
        build_env,
        unpacked_source_directory,
        req_description,
    ):
        from relenv import relocate

        pkginfo = pathlib.Path(setup_py_path).parent / "PKG-INFO"
        with open(pkginfo) as fp:
            pkg_info = fp.read()
        version = None
        name = None
        for line in pkg_info.splitlines():
            if line.startswith("Version:"):
                version = line.split("Version: ")[1].strip()
                if name:
                    break
            if line.startswith("Name:"):
                name = line.split("Name: ")[1].strip()
                if version:
                    break
        func(
            install_options,
            global_options,
            root,
            home,
            prefix,
            use_user_site,
            pycompile,
            scheme,
            setup_py_path,
            isolated,
            req_name,
            build_env,
            unpacked_source_directory,
            req_description,
        )
        egginfo = None
        if prefix:
            sitepack = (
                pathlib.Path(prefix)
                / "lib"
                / f"python{get_major_version()}"
                / "site-packages"
            )
            for path in sorted(sitepack.glob("*.egg-info")):
                if path.name.startswith(f"{name}-{version}"):
                    egginfo = path
                    break
        for path in sorted(pathlib.Path(scheme.purelib).glob("*.egg-info")):
            if path.name.startswith(f"{name}-{version}"):
                egginfo = path
                break
        if egginfo is None:
            debug(f"Relenv was not able to find egg info for: {req_description}")
            return
        plat = pathlib.Path(scheme.platlib)
        rootdir = relenv_root()
        with pushd(egginfo):
            with open("installed-files.txt") as fp:
                for line in fp.readlines():
                    file = pathlib.Path(line.strip()).resolve()
                    if not file.exists():
                        debug(f"Relenv - File not found {file}")
                        continue
                    if relocate.is_elf(file):
                        debug(f"Relenv - Found elf {file}")
                        relocate.handle_elf(plat / file, rootdir / "lib", True, rootdir)

    return wrapper


class Wrapper:
    """
    Wrap methods of an imported module.
    """

    def __init__(self, module, wrapper, matcher="equals", _loading=False):
        self.module = module
        self.wrapper = wrapper
        self.matcher = matcher
        self.loading = _loading

    def matches(self, module):
        """
        Check if wrapper metches module being imported.
        """
        if self.matcher == "startswith":
            return module.startswith(self.module)
        return self.module == module

    def __call__(self, module_name):
        """
        Preform the wrapper operation.
        """
        return self.wrapper(module_name)


class RelenvImporter:
    """
    Handle runtime wrapping of module methods.
    """

    def __init__(self, wrappers=None, _loads=None):
        if wrappers is None:
            wrappers = []
        self.wrappers = wrappers
        if _loads is None:
            _loads = {}
        self._loads = _loads

    def find_module(self, module_name, package_path=None):
        """
        Find modules being imported.
        """
        for wrapper in self.wrappers:
            if wrapper.matches(module_name) and not wrapper.loading:
                debug(f"RelenvImporter - match {module_name}")
                wrapper.loading = True
                return self

    def load_module(self, name):
        """
        Load an imported module.
        """
        for wrapper in self.wrappers:
            if wrapper.matches(name):
                debug(f"RelenvImporter - load_module {name}")
                mod = wrapper(name)
                wrapper.loading = False
                break
        sys.modules[name] = mod
        return mod

    def create_module(self, spec):
        """
        Create the module via a spec.
        """
        return self.load_module(spec.name)

    def exec_module(self, module):
        """
        Exec module noop.
        """
        return None


def wrap_sysconfig(name):
    """
    Sysconfig wrapper.
    """
    mod = importlib.import_module("sysconfig")
    mod.get_config_var = get_config_var_wrapper(mod.get_config_var)
    mod._PIP_USE_SYSCONFIG = True
    try:
        # Python >= 3.10
        scheme = mod.get_default_scheme()
    except AttributeError:
        # Python < 3.10
        scheme = mod._get_default_scheme()
    mod.get_paths = get_paths_wrapper(mod.get_paths, scheme)
    return mod


def wrap_pip_distlib_scripts(name):
    """
    pip.distlib.scripts wrapper.
    """
    mod = importlib.import_module(name)
    mod.ScriptMaker._build_shebang = _build_shebang
    return mod


def wrap_distutils_command(name):
    """
    distutils.command wrapper.
    """
    mod = importlib.import_module(name)
    mod.build_ext.finalize_options = finalize_options_wrapper(
        mod.build_ext.finalize_options
    )
    return mod


def wrap_pip_install_wheel(name):
    """
    pip._internal.operations.install.wheel wrapper.
    """
    mod = importlib.import_module(name)
    mod.install_wheel = install_wheel_wrapper(mod.install_wheel)
    return mod


def wrap_pip_install_legacy(name):
    """
    pip._internal.operations.install.legacy wrapper.
    """
    mod = importlib.import_module(name)
    mod.install = install_legacy_wrapper(mod.install)
    return mod


def set_env_if_not_set(name, value):
    """
    Set an environment variable if not already set.

    If the environment variable is already set and not equal to value, warn the
    user.
    """
    if name in os.environ and os.environ[name] != value:
        print(
            f"Warning: {name} environment not set to relenv's root!\n"
            f"expected: {value}\ncurrent: {os.environ[name]}"
        )
    else:
        debug(f"Relenv set {name}")
        os.environ[name] = value


def wrap_pip_build_wheel(name):
    """
    pip._internal.operations.build wrapper.
    """
    mod = importlib.import_module(name)

    def wrap(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            dirs = work_dirs()
            toolchain = dirs.toolchain / get_triplet()
            if not toolchain.exists():
                debug("Unable to set CARGO_HOME no toolchain exists")
            else:
                relenvroot = str(sys.RELENV)
                rustflags = (
                    f"-C link-arg=-Wl,-rpath,{relenvroot}/lib "
                    f"-C link-arg=-L{relenvroot}/lib "
                    f"-C link-arg=-L{toolchain}/sysroot/lib"
                )
                cargo_home = str(toolchain / "cargo")
                set_env_if_not_set("CARGO_HOME", cargo_home)
                set_env_if_not_set("OPENSSL_DIR", relenvroot)
                set_env_if_not_set("RUSTFLAGS", rustflags)
            return func(*args, **kwargs)

        return wrapper

    mod.build_wheel_pep517 = wrap(mod.build_wheel_pep517)
    return mod


importer = RelenvImporter(
    wrappers=[
        Wrapper("sysconfig", wrap_sysconfig, "startswith"),
        Wrapper("pip._vendor.distlib.scripts", wrap_pip_distlib_scripts),
        Wrapper("distutils.command.build_ext", wrap_distutils_command),
        Wrapper("pip._internal.operations.install.wheel", wrap_pip_install_wheel),
        Wrapper("pip._internal.operations.install.legacy", wrap_pip_install_legacy),
        Wrapper("pip._internal.operations.build.wheel", wrap_pip_build_wheel),
    ],
)


def install_cargo_config():
    """
    Setup cargo config.
    """
    if sys.platform != "linux":
        return
    triplet = get_triplet()
    dirs = work_dirs()
    toolchain = dirs.toolchain / triplet
    if not toolchain.exists():
        debug("Unable to set CARGO_HOME no toolchain exists")
        return
    cargo_home = toolchain / "cargo"
    if not cargo_home.exists():
        cargo_home.mkdir()
    cargo_config = cargo_home / "config.toml"
    if not cargo_config.exists():
        if triplet == "x86_64-linux-gnu":
            cargo_triplet = "x86_64-unknown-linux-gnu"
        else:
            cargo_triplet = "aarch64-unknown-linux-gnu"
        gcc = toolchain / "bin" / f"{triplet}-gcc"
        with open(cargo_config, "w") as fp:
            fp.write(
                textwrap.dedent(
                    """\
            [target.{}]
            linker = "{}"
            """
                ).format(cargo_triplet, gcc)
            )


def setup_openssl():
    """
    Configure openssl certificate locations.
    """
    # Use system openssl dirs
    # XXX Should we also setup SSL_CERT_FILE, OPENSSL_CONF &
    # OPENSSL_CONF_INCLUDE?
    if "SSL_CERT_DIR" not in os.environ and sys.platform != "win32":
        openssl_bin = shutil.which("openssl")
        if not openssl_bin:
            debug("Could not find the 'openssl' binary in the path")
        else:
            proc = subprocess.run(
                [openssl_bin, "version", "-d"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                shell=False,
                check=False,
            )
            if proc.returncode != 0:
                msg = "Unable to get the certificates directory from openssl"
                if proc.stderr:
                    msg += f": {proc.stderr}"
                debug(msg)
            else:
                _, directory = proc.stdout.split(":")
                path = pathlib.Path(directory.strip().strip('"'))
                if not os.environ.get("SSL_CERT_DIR"):
                    os.environ["SSL_CERT_DIR"] = str(path / "certs")
                cert_file = path / "cert.pem"
                if cert_file.exists() and not os.environ.get("SSL_CERT_FILE"):
                    os.environ["SSL_CERT_FILE"] = str(cert_file)


def setup_crossroot():
    """
    Setup cross root if needed.
    """
    cross = os.environ.get("RELENV_CROSS", "")
    if cross:
        crossroot = pathlib.Path(cross).resolve()
        sys.prefix = str(crossroot)
        sys.exec_prefix = str(crossroot)
        # XXX What about dist-packages
        pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        sys.path = [
            str(crossroot / "lib" / pyver),
            str(crossroot / "lib" / pyver / "lib-dynload"),
            str(crossroot / "lib" / pyver / "site-packages"),
        ] + [_ for _ in sys.path if "site-packages" not in _]


def bootstrap():
    """
    Bootstrap the relenv environment.
    """
    setup_crossroot()
    setup_openssl()
    install_cargo_config()
    sys.meta_path = [importer] + sys.meta_path
    sys.RELENV = relenv_root()
