# Copyright 2022-2023 VMware, Inc.
# SPDX-License-Identifier: Apache-2
"""
Build process common methods.
"""
import logging
import os.path
import hashlib
import pathlib
import glob
import shutil
import tarfile
import tempfile
import time
import traceback
import subprocess
import random
import sys
import io
import os
import multiprocessing
import pprint
import re
from html.parser import HTMLParser


from relenv.common import (
    DATA_DIR,
    LINUX,
    MODULE_DIR,
    RelenvException,
    build_arch,
    download_url,
    extract_archive,
    format_shebang,
    get_download_location,
    get_toolchain,
    get_triplet,
    runcmd,
    work_dirs,
    fetch_url,
)
from relenv.relocate import main as relocate_main


CHECK_VERSIONS_SUPPORT = True
try:
    from packaging.version import InvalidVersion, parse
    from looseversion import LooseVersion
except ImportError:
    CHECK_VERSIONS_SUPPORT = False

log = logging.getLogger(__name__)


GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
END = "\033[0m"
MOVEUP = "\033[F"


CICD = "CI" in os.environ
NODOWLOAD = False


SITECUSTOMIZE = '''"""
Relenv site customize
"""
import os
import site
import sys

# Remove any paths not relative to relenv's root directory, or a virtualenv's
# root directory if one was created from the relenv python

# On non virtualenv scenarios, sys.prefix is the same as sys.base_prefix,
# while on virtualenv scenarios, they differ. Both of these prefixes should not
# be removed from sys.path
__valid_path_prefixes = tuple({sys.prefix, sys.base_prefix})
__sys_path = []
for __path in sys.path:
    if __path.startswith(__valid_path_prefixes):
        __sys_path.append(__path)
if 'PYTHONPATH' in os.environ:
    sep = ':'
    if sys.platform == 'win32':
       sep = ';'
    for __path in os.environ['PYTHONPATH'].split(sep):
        __sys_path.append(__path)

# Replace sys.path
sys.path[:] = __sys_path
site.ENABLE_USER_SITE = False

try:
    import relenv.runtime
except ImportError:
    if "RELENV_DEBUG" in os.environ:
        print("Unable to find relenv.runtime for bootstrap.", file=sys.stderr, flush=True)
else:
    relenv.runtime.bootstrap()
'''

SYSCONFIGDATA = """
import pathlib, sys, platform, os

def build_arch():
    machine = platform.machine()
    return machine.lower()

def get_triplet(machine=None, plat=None):
    if not plat:
        plat = sys.platform
    if not machine:
        machine = build_arch()
    if plat == "darwin":
        return f"{machine}-macos"
    elif plat == "win32":
        return f"{machine}-win"
    elif plat == "linux":
        return f"{machine}-linux-gnu"
    else:
        raise RelenvException("Unknown platform {}".format(platform))



pydir = pathlib.Path(__file__).resolve().parent
if sys.platform == "win32":
    DEFAULT_DATA_DIR = pathlib.Path.home() / "AppData" / "Local" / "relenv"
else:
    DEFAULT_DATA_DIR = pathlib.Path.home() / ".local" / "relenv"

if "RELENV_DATA" in os.environ:
    DATA_DIR = pathlib.Path(os.environ["RELENV_DATA"]).resolve()
else:
    DATA_DIR = DEFAULT_DATA_DIR

buildroot = pydir.parent.parent
toolchain = DATA_DIR / "toolchain" / get_triplet()
build_time_vars = {}
for key in _build_time_vars:
    val = _build_time_vars[key]
    orig = val
    if isinstance(val, str):
        val = val.format(
            BUILDROOT=buildroot,
            TOOLCHAIN=toolchain,
        )
    build_time_vars[key] = val
"""


def print_ui(events, processes, fails, flipstat=None):
    """
    Prints the UI during the relenv building process.

    :param events: A dictionary of events that are updated during the build process
    :type events: dict
    :param processes: A dictionary of build processes
    :type processes: dict
    :param fails: A list of processes that have failed
    :type fails: list
    :param flipstat: A dictionary of process statuses, defaults to {}
    :type flipstat: dict, optional
    """
    if flipstat is None:
        flipstat = {}
    if CICD:
        sys.stdout.flush()
        return
    uiline = []
    for name in events:
        if not events[name].is_set():
            status = " {}.".format(YELLOW)
        elif name in processes:
            now = time.time()
            if name not in flipstat:
                flipstat[name] = (0, now)
            if flipstat[name][1] < now:
                flipstat[name] = (1 - flipstat[name][0], now + random.random())
            status = " {}{}".format(GREEN, " " if flipstat[name][0] == 1 else ".")
        elif name in fails:
            status = " {}\u2718".format(RED)
        else:
            status = " {}\u2718".format(GREEN)
        uiline.append(status)
    uiline.append("  " + END)
    sys.stdout.write("\r")
    sys.stdout.write("".join(uiline))
    sys.stdout.flush()


def verify_checksum(file, checksum):
    """
    Verify the checksum of a files.

    :param file: The path to the file to check.
    :type file: str
    :param checksum: The checksum to verify against
    :type checksum: str

    :raises RelenvException: If the checksum verification failed

    :return: True if it succeeded, or False if the checksum was None
    :rtype: bool
    """
    if checksum is None:
        log.error("Can't verify checksum because none was given")
        return False
    with open(file, "rb") as fp:
        if checksum != hashlib.md5(fp.read()).hexdigest():
            raise RelenvException("md5 checksum verification failed")
    return True


def all_dirs(root, recurse=True):
    """
    Get all directories under and including the given root.

    :param root: The root directory to traverse
    :type root: str
    :param recurse: Whether to recursively search for directories, defaults to True
    :type recurse: bool, optional

    :return: A list of directories found
    :rtype: list
    """
    paths = [root]
    for root, dirs, files in os.walk(root):
        for name in dirs:
            paths.append(os.path.join(root, name))
    return paths


def populate_env(dirs, env):
    pass


def build_default(env, dirs, logfp):
    """
    The default build function if none is given during the build process.

    :param env: The environment dictionary
    :type env: dict
    :param dirs: The working directories
    :type dirs: ``relenv.build.common.Dirs``
    :param logfp: A handle for the log file
    :type logfp: file
    """
    cmd = [
        "./configure",
        "--prefix={}".format(dirs.prefix),
    ]
    if env["RELENV_HOST"].find("linux") > -1:
        cmd += [
            "--build={}".format(env["RELENV_BUILD"]),
            "--host={}".format(env["RELENV_HOST"]),
        ]
    runcmd(cmd, env=env, stderr=logfp, stdout=logfp)
    runcmd(["make", "-j8"], env=env, stderr=logfp, stdout=logfp)
    runcmd(["make", "install"], env=env, stderr=logfp, stdout=logfp)


def build_openssl(env, dirs, logfp):
    """
    Build openssl.

    :param env: The environment dictionary
    :type env: dict
    :param dirs: The working directories
    :type dirs: ``relenv.build.common.Dirs``
    :param logfp: A handle for the log file
    :type logfp: file
    """
    temp = tempfile.mkdtemp()
    arch = "aarch64"
    if sys.platform == "darwin":
        plat = "darwin64"
        if env["RELENV_HOST_ARCH"] == "x86_64":
            arch = "x86_64-cc"
    else:
        plat = "linux"
        if env["RELENV_HOST_ARCH"] == "x86_64":
            arch = "x86_64"
        elif env["RELENV_HOST_ARCH"] == "aarch64":
            arch = "aarch64"
    runcmd(
        [
            "./Configure",
            f"{plat}-{arch}",
            "no-idea",
            "shared",
            f"--prefix={dirs.prefix}",
            f"--openssldir={temp}",
            "--libdir=lib",
        ],
        env=env,
        stderr=logfp,
        stdout=logfp,
    )
    runcmd(["make", "-j8"], env=env, stderr=logfp, stdout=logfp)
    runcmd(["make", "install_sw"], env=env, stderr=logfp, stdout=logfp)


def build_sqlite(env, dirs, logfp):
    """
    Build sqlite.

    :param env: The environment dictionary
    :type env: dict
    :param dirs: The working directories
    :type dirs: ``relenv.build.common.Dirs``
    :param logfp: A handle for the log file
    :type logfp: file
    """
    # extra_cflags=('-Os '
    #              '-DSQLITE_ENABLE_FTS5 '
    #              '-DSQLITE_ENABLE_FTS4 '
    #              '-DSQLITE_ENABLE_FTS3_PARENTHESIS '
    #              '-DSQLITE_ENABLE_JSON1 '
    #              '-DSQLITE_ENABLE_RTREE '
    #              '-DSQLITE_TCL=0 '
    #              )
    # configure_pre=[
    #    '--enable-threadsafe',
    #    '--enable-shared=no',
    #    '--enable-static=yes',
    #    '--disable-readline',
    #    '--disable-dependency-tracking',
    # ]
    cmd = [
        "./configure",
        "--with-shared",
        "--without-static",
        "--enable-threadsafe",
        "--disable-readline",
        "--disable-dependency-tracking",
        "--prefix={}".format(dirs.prefix),
        "--enable-add-ons=nptl,ports",
    ]
    if env["RELENV_HOST"].find("linux") > -1:
        cmd += [
            "--build={}".format(env["RELENV_BUILD_ARCH"]),
            "--host={}".format(env["RELENV_HOST"]),
        ]
    runcmd(cmd, env=env, stderr=logfp, stdout=logfp)
    runcmd(["make", "-j8"], env=env, stderr=logfp, stdout=logfp)
    runcmd(["make", "install"], env=env, stderr=logfp, stdout=logfp)


def tarball_version(href):
    if href.endswith("tar.gz"):
        try:
            x = href.split("-", 1)[1][:-7]
            if x != "latest":
                return x
        except IndexError:
            return None


def sqlite_version(href):
    if "releaselog" in href:
        link = href.split("/")[1][:-5]
        return "{:d}{:02d}{:02d}00".format(*[int(_) for _ in link.split("_")])


def github_version(href):
    if "tag/" in href:
        return href.split("/v")[-1]


def krb_version(href):
    if re.match(r"\d\.\d\d/", href):
        return href[:-1]


def python_version(href):
    if re.match(r"(\d+\.)+\d/", href):
        return href[:-1]


def uuid_version(href):
    if "download" in href and "latest" not in href:
        return href[:-16].rsplit("/")[-1].replace("libuuid-", "")


def parse_links(text):
    class HrefParser(HTMLParser):
        hrefs = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                link = dict(attrs).get("href", "")
                if link:
                    self.hrefs.append(link)

    parser = HrefParser()
    parser.feed(text)
    return parser.hrefs


def check_files(location, func, current):
    fp = io.BytesIO()
    fetch_url(location, fp)
    fp.seek(0)
    text = fp.read().decode()
    loose = False
    try:
        current = parse(current)
    except InvalidVersion:
        current = LooseVersion(current)
        loose = True

    versions = []
    for _ in parse_links(text):
        version = func(_)
        if version:
            if loose:
                versions.append(LooseVersion(version))
            else:
                try:
                    versions.append(parse(version))
                except InvalidVersion:
                    pass

    versions.sort()
    compare_versions(current, versions)


def compare_versions(current, versions):
    for version in versions:
        try:
            if version > current:
                print(f"Found new version {version} > {current}")
        except TypeError:
            print(f"Unable to compare versions {version}")


# def check_named_versions(name, current, versions):
#    version, current = check_versions
#    if version == NOOP:
#        return
#    print(f"{name}: {version} > {current}")


class Download:
    """
    A utility that holds information about content to be downloaded.

    :param name: The name of the download
    :type name: str
    :param url: The url of the download
    :type url: str
    :param signature: The signature of the download, defaults to None
    :type signature: str
    :param destination: The path to download the file to
    :type destination: str
    :param version: The version of the content to download
    :type version: str
    :param md5sum: The md5 sum of the download
    :type md5sum: str

    """

    def __init__(
        self,
        name,
        url,
        fallback_url=None,
        signature=None,
        destination="",
        version="",
        md5sum=None,
        checkfunc=None,
        checkurl=None,
    ):
        self.name = name
        self.url_tpl = url
        self.fallback_url_tpl = fallback_url
        self.signature_tpl = signature
        self.destination = destination
        self.version = version
        self.md5sum = md5sum
        self.checkfunc = checkfunc
        self.checkurl = checkurl

    def copy(self):
        return Download(
            self.name,
            self.url_tpl,
            self.fallback_url_tpl,
            self.signature_tpl,
            self.destination,
            self.version,
            self.md5sum,
            self.checkfunc,
            self.checkurl,
        )

    @property
    def url(self):
        return self.url_tpl.format(version=self.version)

    @property
    def fallback_url(self):
        if self.fallback_url_tpl:
            return self.fallback_url_tpl.format(version=self.version)

    @property
    def signature_url(self):
        return self.signature_tpl.format(version=self.version)

    @property
    def filepath(self):
        _, name = self.url.rsplit("/", 1)
        return pathlib.Path(self.destination) / name

    @property
    def formatted_url(self):
        return self.url.format(version=self.version)

    def fetch_file(self):
        """
        Download the file.

        :return: The path to the downloaded content, and whether it was downloaded.
        :rtype: tuple(str, bool)
        """
        try:
            return download_url(self.url, self.destination, CICD), True
        except Exception as exc:
            if self.fallback_url:
                print(f"Download failed ({exc}); trying fallback url")
                return download_url(self.fallback_url, self.destination, CICD), True

    def fetch_signature(self, version):
        """
        Download the file signature.

        :return: The path to the downloaded signature.
        :rtype: str
        """
        return download_url(self.signature_url, self.destination, CICD)

    def exists(self):
        """
        True when the artifact already exists on disk.

        :return: True when the artifact already exists on disk
        :rtype: bool
        """
        return self.filepath.exists()

    def valid_hash(self):
        pass

    @staticmethod
    def validate_signature(archive, signature):
        """
        True when the archive's signature is valid.

        :param archive: The path to the archive to validate
        :type archive: str
        :param signature: The path to the signature to validate against
        :type signature: str

        :return: True if it validated properly, else False
        :rtype: bool
        """
        if signature is None:
            log.error("Can't check signature because none was given")
            return False
        try:
            runcmd(
                ["gpg", "--verify", signature, archive],
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            return True
        except RelenvException as exc:
            log.error("Signature validation failed on %s: %s", archive, exc)
            return False

    @staticmethod
    def validate_md5sum(archive, md5sum):
        """
        True when when the archive matches the md5 hash.

        :param archive: The path to the archive to validate
        :type archive: str
        :param md5sum: The md5 sum to validate against
        :type md5sum: str
        :return: True if the sums matched, else False
        :rtype: bool
        """
        try:
            verify_checksum(archive, md5sum)
            return True
        except RelenvException as exc:
            log.error("md5 validation failed on %s: %s", archive, exc)
            return False

    def __call__(self, force_download=False):
        """
        Downloads the url and validates the signature and md5 sum.

        :return: Whether or not validation succeeded
        :rtype: bool
        """
        os.makedirs(self.filepath.parent, exist_ok=True)
        downloaded = False
        if force_download:
            _, downloaded = self.fetch_file()
        else:
            file_is_valid = False
            dest = get_download_location(self.url, self.destination)
            if self.md5sum and os.path.exists(dest):
                file_is_valid = self.validate_md5sum(dest, self.md5sum)
            if file_is_valid:
                log.debug("%s already downloaded, skipping.", self.url)
            else:
                _, downloaded = self.fetch_file()
        valid = True
        if downloaded:
            if self.signature_tpl is not None:
                sig, _ = self.fetch_signature()
                valid_sig = self.validate_signature(self.filepath, sig)
                valid = valid and valid_sig
            if self.md5sum is not None:
                valid_md5 = self.validate_md5sum(self.filepath, self.md5sum)
                valid = valid and valid_md5
            log.debug("Checksum for %s: %s", self.name, self.md5sum)
        return valid

    def check_version(self):
        if self.checkurl:
            url = self.checkurl
        else:
            url = self.url.rsplit("/", 1)[0]
        check_files(url, self.checkfunc, self.version)


class Dirs:
    """
    A container for directories during build time.

    :param dirs: A collection of working directories
    :type dirs: ``relenv.common.WorkDirs``
    :param name: The name of this collection
    :type name: str
    :param arch: The architecture being worked with
    :type arch: str
    """

    def __init__(self, dirs, name, arch, version):
        # XXX name is the specific to a step where as everything
        # else here is generalized to the entire build
        self.name = name
        self.version = version
        self.arch = arch
        self.root = dirs.root
        self.build = dirs.build
        self.downloads = dirs.download
        self.logs = dirs.logs
        self.sources = dirs.src
        self.tmpbuild = tempfile.mkdtemp(prefix="{}_build".format(name))

    @property
    def toolchain(self):
        if sys.platform == "darwin":
            return get_toolchain(root=self.root)
        elif sys.platform == "win32":
            return get_toolchain(root=self.root)
        else:
            return get_toolchain(self.arch, self.root)

    @property
    def _triplet(self):
        if sys.platform == "darwin":
            return "{}-macos".format(self.arch)
        elif sys.platform == "win32":
            return "{}-win".format(self.arch)
        else:
            return "{}-linux-gnu".format(self.arch)

    @property
    def prefix(self):
        return self.build / f"{self.version}-{self._triplet}"

    def __getstate__(self):
        """
        Return an object used for pickling.

        :return: The picklable state
        """
        return {
            "name": self.name,
            "arch": self.arch,
            "root": self.root,
            "build": self.build,
            "downloads": self.downloads,
            "logs": self.logs,
            "sources": self.sources,
            "tmpbuild": self.tmpbuild,
        }

    def __setstate__(self, state):
        """
        Unwrap the object returned from unpickling.

        :param state: The state to unpickle
        :type state: dict
        """
        self.name = state["name"]
        self.arch = state["arch"]
        self.root = state["root"]
        self.downloads = state["downloads"]
        self.logs = state["logs"]
        self.sources = state["sources"]
        self.build = state["build"]
        self.tmpbuild = state["tmpbuild"]

    def to_dict(self):
        """
        Get a dictionary representation of the directories in this collection.

        :return: A dictionary of all the directories
        :rtype: dict
        """
        return {
            x: getattr(self, x)
            for x in [
                "root",
                "prefix",
                "downloads",
                "logs",
                "sources",
                "build",
                "toolchain",
            ]
        }


class Builds:
    """
    Collection of builds.
    """

    def __init__(self):
        self.builds = {}

    def add(self, platform, *args, **kwargs):
        if "builder" in kwargs:
            build = kwargs.pop("builder")
            if args or kwargs:
                raise RuntimeError(
                    "builder keyword can not be used with other kwargs or args"
                )
        else:
            build = Builder(*args, **kwargs)
        if platform not in self.builds:
            self.builds[platform] = {build.version: build}
        else:
            self.builds[platform][build.version] = build
        return build


builds = Builds()


class Builder:
    """
    Utility that handles the build process.

    :param root: The root of the working directories for this build
    :type root: str
    :param recipies: The instructions for the build steps
    :type recipes: list
    :param build_default: The default build function, defaults to ``build_default``
    :type build_default: types.FunctionType
    :param populate_env: The default function to populate the build environment, defaults to ``populate_env``
    :type populate_env: types.FunctionType
    :param force_download: If True, forces downloading the archives even if they exist, defaults to False
    :type force_download: bool
    :param arch: The architecture being built
    :type arch: str
    """

    def __init__(
        self,
        root=None,
        recipies=None,
        build_default=build_default,
        populate_env=populate_env,
        force_download=False,
        arch="x86_64",
        version="",
    ):
        self.root = root
        self.dirs = work_dirs(root)
        self.build_arch = build_arch()
        self.build_triplet = get_triplet(self.build_arch)
        self.arch = arch
        self.triplet = get_triplet(self.arch)
        self.version = version
        # XXX Refactor WorkDirs, Dirs and Builder so as not to duplicate logic
        self.prefix = self.dirs.build / f"{self.version}-{self.triplet}"
        self.sources = self.dirs.src
        self.downloads = self.dirs.download

        if recipies is None:
            self.recipies = {}
        else:
            self.recipies = recipies

        self.build_default = build_default
        self.populate_env = populate_env
        self.force_download = force_download
        self.toolchains = get_toolchain(root=self.dirs.root)
        self.set_arch(self.arch)

    def copy(self, version, md5sum):
        recipies = {}
        for name in self.recipies:
            _ = self.recipies[name]
            recipies[name] = {
                "build_func": _["build_func"],
                "wait_on": _["wait_on"],
                "download": _["download"].copy() if _["download"] else None,
            }
        build = Builder(
            self.root,
            recipies,
            self.build_default,
            self.populate_env,
            self.force_download,
            self.arch,
            version,
        )
        build.recipies["python"]["download"].version = version
        build.recipies["python"]["download"].md5sum = md5sum
        return build

    def set_arch(self, arch):
        """
        Set the architecture for the build.

        :param arch: The arch to build
        :type arch: str
        """
        self.arch = arch
        self.triplet = get_triplet(self.arch)
        self.prefix = self.dirs.build / f"{self.version}-{self.triplet}"
        if sys.platform in ["darwin", "win32"]:
            self.toolchain = None
        else:
            self.toolchain = get_toolchain(self.arch, self.dirs.root)

    @property
    def _triplet(self):
        if sys.platform == "darwin":
            return "{}-macos".format(self.arch)
        elif sys.platform == "win32":
            return "{}-win".format(self.arch)
        else:
            return "{}-linux-gnu".format(self.arch)

    def add(self, name, build_func=None, wait_on=None, download=None):
        """
        Add a step to the build process.

        :param name: The name of the step
        :type name: str
        :param build_func: The function that builds this step, defaults to None
        :type build_func: types.FunctionType, optional
        :param wait_on: Processes to wait on before running this step, defaults to None
        :type wait_on: list, optional
        :param download: A dictionary of download information, defaults to None
        :type download: dict, optional
        """
        if wait_on is None:
            wait_on = []
        if build_func is None:
            build_func = self.build_default
        if download is not None:
            download = Download(name, destination=self.downloads, **download)
        self.recipies[name] = {
            "build_func": build_func,
            "wait_on": wait_on,
            "download": download,
        }

    def run(self, name, event, build_func, download):
        """
        Run a build step.

        :param name: The name of the step to run
        :type name: str
        :param event: An event to track this process' status and alert waiting steps
        :type event: ``multiprocessing.Event``
        :param build_func: The function to use to build this step
        :type build_func: types.FunctionType
        :param download: The ``Download`` instance for this step
        :type download: ``Download``

        :return: The output of the build function
        """
        while event.is_set() is False:
            time.sleep(0.3)

        if not self.dirs.build.exists():
            os.makedirs(self.dirs.build, exist_ok=True)

        dirs = Dirs(self.dirs, name, self.arch, self.version)
        os.makedirs(dirs.sources, exist_ok=True)
        os.makedirs(dirs.logs, exist_ok=True)
        os.makedirs(dirs.prefix, exist_ok=True)
        logfp = io.open(os.path.join(dirs.logs, "{}.log".format(name)), "w")
        handler = logging.FileHandler(dirs.logs / f"{name}.log")
        log.addHandler(handler)
        log.setLevel(logging.NOTSET)

        # DEBUG: Uncomment to debug
        # logfp = sys.stdout

        cwd = os.getcwd()
        if download:
            extract_archive(dirs.sources, str(download.filepath))
            dirs.source = dirs.sources / download.filepath.name.split(".tar")[0]
            os.chdir(dirs.source)
        else:
            os.chdir(dirs.prefix)

        if sys.platform == "win32":
            env = os.environ.copy()
        else:
            env = {
                "PATH": os.environ["PATH"],
            }
        env["RELENV_DEBUG"] = "1"
        env["RELENV_HOST"] = self.triplet
        env["RELENV_HOST_ARCH"] = self.arch
        env["RELENV_BUILD"] = self.build_triplet
        env["RELENV_BUILD_ARCH"] = self.build_arch
        env["RELENV_PY_VERSION"] = self.recipies["python"]["download"].version
        env["RELENV_PY_MAJOR_VERSION"] = env["RELENV_PY_VERSION"].rsplit(".", 1)[0]
        if "RELENV_DATA" in os.environ:
            env["RELENV_DATA"] = os.environ["RELENV_DATA"]
        if self.build_arch != self.arch:
            native_root = DATA_DIR / "native"
            env["RELENV_NATIVE_PY"] = str(native_root / "bin" / "python3")

        self.populate_env(env, dirs)

        logfp.write("*" * 80 + "\n")
        _ = dirs.to_dict()
        for k in _:
            logfp.write("{} {}\n".format(k, _[k]))
        logfp.write("*" * 80 + "\n")
        for k in env:
            logfp.write("{} {}\n".format(k, env[k]))
        logfp.write("*" * 80 + "\n")
        try:
            return build_func(env, dirs, logfp)
        except Exception:
            logfp.write(traceback.format_exc() + "\n")
            sys.exit(1)
        finally:
            os.chdir(cwd)
            log.removeHandler(handler)
            logfp.close()

    def cleanup(self):
        """
        Clean up the build directories.
        """
        shutil.rmtree(self.prefix)

    def clean(self):
        """
        Completely clean up the remnants of a relenv build.
        """
        # Clean directories
        for _ in [self.prefix, self.sources]:
            try:
                shutil.rmtree(_)
            except PermissionError:
                sys.stderr.write(f"Unable to remove directory: {_}")
            except FileNotFoundError:
                pass
        # Clean files
        archive = f"{self.prefix}.tar.xz"
        for _ in [archive]:
            try:
                os.remove(_)
            except FileNotFoundError:
                pass

    def download_files(self, steps=None, force_download=False):
        """
        Download all of the needed archives.

        :param steps: The steps to download archives for, defaults to None
        :type steps: list, optional
        """
        if steps is None:
            steps = list(self.recipies)

        fails = []
        processes = {}
        events = {}
        sys.stdout.write("Starting downloads \n")
        print_ui(events, processes, fails)
        for name in steps:
            download = self.recipies[name]["download"]
            if download is None:
                continue
            event = multiprocessing.Event()
            event.set()
            events[name] = event
            proc = multiprocessing.Process(
                name=name, target=download, kwargs={"force_download": force_download}
            )
            proc.start()
            processes[name] = proc

        while processes:
            for proc in list(processes.values()):
                proc.join(0.3)
                # DEBUG: Comment to debug
                print_ui(events, processes, fails)
                if proc.exitcode is None:
                    continue
                processes.pop(proc.name)
                if proc.exitcode != 0:
                    fails.append(proc.name)
        print_ui(events, processes, fails)
        sys.stdout.write("\n")
        if fails:
            print_ui(events, processes, fails)
            sys.stderr.write("The following failures were reported\n")
            for fail in fails:
                sys.stderr.write(fail + "\n")
            sys.stderr.flush()
            sys.exit(1)

    def build(self, steps=None, cleanup=True):
        """
        Build!

        :param steps: The steps to run, defaults to None
        :type steps: list, optional
        :param cleanup: Whether to clean up or not, defaults to True
        :type cleanup: bool, optional
        """  # noqa: D400
        fails = []
        events = {}
        waits = {}
        processes = {}

        sys.stdout.write("Starting builds\n")
        # DEBUG: Comment to debug
        print_ui(events, processes, fails)

        for name in steps:
            event = multiprocessing.Event()
            events[name] = event
            kwargs = dict(self.recipies[name])

            # Determine needed dependency recipies.
            wait_on = kwargs.pop("wait_on", [])
            for _ in wait_on[:]:
                if _ not in steps:
                    wait_on.remove(_)

            waits[name] = wait_on
            if not waits[name]:
                event.set()

            proc = multiprocessing.Process(
                name=name, target=self.run, args=(name, event), kwargs=kwargs
            )
            proc.start()
            processes[name] = proc

        # Wait for the processes to finish and check if we should send any
        # dependency events.
        while processes:
            for proc in list(processes.values()):
                proc.join(0.3)
                # DEBUG: Comment to debug
                print_ui(events, processes, fails)
                if proc.exitcode is None:
                    continue
                processes.pop(proc.name)
                if proc.exitcode != 0:
                    fails.append(proc.name)
                    is_failure = True
                else:
                    is_failure = False
                for name in waits:
                    if proc.name in waits[name]:
                        if is_failure:
                            if name in processes:
                                processes[name].terminate()
                                time.sleep(0.1)
                        waits[name].remove(proc.name)
                    if not waits[name] and not events[name].is_set():
                        events[name].set()

        if fails:
            sys.stderr.write("The following failures were reported\n")
            for fail in fails:
                try:
                    with io.open(self.dirs.logs / f"{fail}.log") as fp:
                        fp.seek(0, 2)
                        end = fp.tell()
                        ind = end - 4096
                        if ind > 0:
                            fp.seek(ind)
                        else:
                            fp.seek(0)
                        sys.stderr.write("=" * 20 + f" {fail} " + "=" * 20 + "\n")
                        sys.stderr.write(fp.read() + "\n\n")
                except FileNotFoundError:
                    pass
            sys.stderr.flush()
            if cleanup:
                self.cleanup()
            sys.exit(1)
        time.sleep(0.1)
        # DEBUG: Comment to debug
        print_ui(events, processes, fails)
        sys.stdout.write("\n")
        sys.stdout.flush()
        if cleanup:
            self.cleanup()

    def check_prereqs(self):
        """
        Check pre-requsists for build.

        This method verifies all requrements for a successful build are satisfied.

        :return: Returns a list of string describing failed checks
        :rtype: list
        """
        fail = []
        if self.toolchain and not self.toolchain.exists():
            fail.append(
                f"Toolchain for {self.arch} does not exist. Please use relenv toolchain to obtain a toolchain."
            )
        return fail

    def __call__(
        self, steps=None, arch=None, clean=True, cleanup=True, force_download=False
    ):
        """
        Set the architecture, define the steps, clean if needed, download what is needed, and build.

        :param steps: The steps to run, defaults to None
        :type steps: list, optional
        :param arch: The architecture to build, defaults to None
        :type arch: str, optional
        :param clean: If true, cleans the directories first, defaults to True
        :type clean: bool, optional
        :param cleanup: Cleans up after build if true, defaults to True
        :type cleanup: bool, optional
        :param force_download: Whether or not to download the content if it already exists, defaults to True
        :type force_download: bool, optional
        """
        if arch:
            self.set_arch(arch)

        if steps is None:
            steps = self.recipies

        failures = self.check_prereqs()
        if failures:
            for _ in failures:
                sys.stderr.write(f"{_}\n")
            sys.stderr.flush()
            sys.exit(1)

        if clean:
            self.clean()

        if self.build_arch != self.arch:
            native_root = DATA_DIR / "native"
            if not native_root.exists():
                from relenv.create import create

                create("native", DATA_DIR, version=self.version)

        # Start a process for each build passing it an event used to notify each
        # process if it's dependencies have finished.
        self.download_files(steps, force_download=force_download)
        self.build(steps, cleanup)

    def check_versions(self):
        for step in list(self.recipies):
            download = self.recipies[step]["download"]
            if not download:
                continue
            print(f"Checking {step}")
            download.check_version()


def patch_shebang(path, old, new):
    """
    Replace a file's shebang.

    :param path: The path of the file to patch
    :type path: str
    :param old: The old shebang, will only patch when this is found
    :type old: str
    :param name: The new shebang to be written
    :type name: str
    """
    with open(path, "rb") as fp:
        try:
            data = fp.read(len(old.encode())).decode()
        except UnicodeError:
            return False
        except Exception as exc:
            log.warning("Unhandled exception: %r", exc)
            return False
        if data != old:
            log.warning("Shebang doesn't match: %s %r != %r", path, old, data)
            return False
        data = fp.read().decode()
    with open(path, "w") as fp:
        fp.write(new)
        fp.write(data)
    with open(path, "r") as fp:
        data = fp.read()
    log.info("Patched shebang of %s => %r", path, data)
    return True


def patch_shebangs(path, old, new):
    """
    Traverse directory and patch shebangs.

    :param path: The of the directory to traverse
    :type path: str
    :param old: The old shebang, will only patch when this is found
    :type old: str
    :param name: The new shebang to be written
    :type name: str
    """
    for root, _dirs, files in os.walk(str(path)):
        for file in files:
            patch_shebang(os.path.join(root, file), old, new)


def install_sysdata(mod, destfile, buildroot, toolchain):
    """
    Create a Relenv Python environment's sysconfigdata.

    Helper method used by the `finalize` build method to create a Relenv
    Python environment's sysconfigdata.

    :param mod: The module to operate on
    :type mod: ``types.ModuleType``
    :param destfile: Path to the file to write the data to
    :type destfile: str
    :param buildroot: Path to the root of the build
    :type buildroot: str
    :param toolchain: Path to the root of the toolchain
    :type toolchain: str
    """
    data = {}
    fbuildroot = lambda _: _.replace(str(buildroot), "{BUILDROOT}")  # noqa: E731
    ftoolchain = lambda _: _.replace(str(toolchain), "{TOOLCHAIN}")  # noqa: E731
    # XXX: keymap is not used, remove it?
    # keymap = {
    #    "BINDIR": (fbuildroot,),
    #    "BINLIBDEST": (fbuildroot,),
    #    "CFLAGS": (fbuildroot, ftoolchain),
    #    "CPPLAGS": (fbuildroot, ftoolchain),
    #    "CXXFLAGS": (fbuildroot, ftoolchain),
    #    "datarootdir": (fbuildroot,),
    #    "exec_prefix": (fbuildroot,),
    #    "LDFLAGS": (fbuildroot, ftoolchain),
    #    "LDSHARED": (fbuildroot, ftoolchain),
    #    "LIBDEST": (fbuildroot,),
    #    "prefix": (fbuildroot,),
    #    "SCRIPTDIR": (fbuildroot,),
    # }
    for key in sorted(mod.build_time_vars):
        val = mod.build_time_vars[key]
        if isinstance(val, str):
            for _ in (fbuildroot, ftoolchain):
                val = _(val)
                log.info("SYSCONFIG [%s] %s => %s", key, mod.build_time_vars[key], val)
        data[key] = val

    with open(destfile, "w", encoding="utf8") as f:
        f.write(
            "# system configuration generated and used by" " the relenv at runtime\n"
        )
        f.write("_build_time_vars = ")
        pprint.pprint(data, stream=f)
        f.write(SYSCONFIGDATA)


def find_sysconfigdata(pymodules):
    """
    Find sysconfigdata directory for python installation.

    :param pymodules: Path to python modules (e.g. lib/python3.10)
    :type pymodules: str

    :return: The name of the sysconig data module
    :rtype: str
    """
    for root, dirs, files in os.walk(pymodules):
        for file in files:
            if file.find("sysconfigdata") > -1 and file.endswith(".py"):
                return file[:-3]


def install_runtime(sitepackages):
    """
    Install a base relenv runtime.
    """
    sitecustomize = sitepackages / "sitecustomize.py"
    with io.open(str(sitecustomize), "w") as fp:
        fp.write(SITECUSTOMIZE)

    # Lay down relenv.runtime, we'll pip install the rest later
    relenv = sitepackages / "relenv"
    os.makedirs(relenv, exist_ok=True)

    for name in ["runtime.py", "relocate.py", "common.py"]:
        src = MODULE_DIR / name
        dest = relenv / name
        with io.open(src, "r") as rfp:
            with io.open(dest, "w") as wfp:
                wfp.write(rfp.read())

    init = relenv / "__init__.py"
    init.touch()


def finalize(env, dirs, logfp):
    """
    Run after we've fully built python.

    This method enhances the newly created python with Relenv's runtime hacks.

    :param env: The environment dictionary
    :type env: dict
    :param dirs: The working directories
    :type dirs: ``relenv.build.common.Dirs``
    :param logfp: A handle for the log file
    :type logfp: file
    """
    # Run relok8 to make sure the rpaths are relocatable.
    relocate_main(dirs.prefix)
    # Install relenv-sysconfigdata module
    libdir = pathlib.Path(dirs.prefix) / "lib"

    def find_pythonlib(libdir):
        for root, dirs, files in os.walk(libdir):
            for _ in dirs:
                if _.startswith("python"):
                    return _

    pymodules = libdir / find_pythonlib(libdir)

    cwd = os.getcwd()
    modname = find_sysconfigdata(pymodules)
    path = sys.path
    sys.path = [str(pymodules)]
    try:
        mod = __import__(str(modname))
    finally:
        os.chdir(cwd)
        sys.path = path

    dest = pymodules / f"{modname}.py"
    install_sysdata(mod, dest, dirs.prefix, dirs.toolchain)

    # Lay down site customize
    bindir = pathlib.Path(dirs.prefix) / "bin"
    sitepackages = pymodules / "site-packages"
    install_runtime(sitepackages)
    # Install pip
    python = dirs.prefix / "bin" / "python3"
    if env["RELENV_HOST_ARCH"] != env["RELENV_BUILD_ARCH"]:
        env["RELENV_CROSS"] = dirs.prefix
        python = env["RELENV_NATIVE_PY"]
    runcmd(
        [str(python), "-m", "ensurepip"],
        env=env,
        stderr=logfp,
        stdout=logfp,
    )

    # Fix the shebangs in the scripts python layed down. Order matters.
    shebangs = [
        "#!{}".format(bindir / f"python{env['RELENV_PY_MAJOR_VERSION']}"),
        "#!{}".format(
            bindir / f"python{env['RELENV_PY_MAJOR_VERSION'].split('.', 1)[0]}"
        ),
    ]
    newshebang = format_shebang("/python3")
    for shebang in shebangs:
        log.info("Patch shebang %r with  %r", shebang, newshebang)
        patch_shebangs(
            str(pathlib.Path(dirs.prefix) / "bin"),
            shebang,
            newshebang,
        )

    if sys.platform == "linux":
        pyconf = f"config-{env['RELENV_PY_MAJOR_VERSION']}-{env['RELENV_HOST']}"
        patch_shebang(
            str(pymodules / pyconf / "python-config.py"),
            "#!{}".format(str(bindir / f"python{env['RELENV_PY_MAJOR_VERSION']}")),
            format_shebang("../../../bin/python3"),
        )

    patch_shebang(
        str(pymodules / "cgi.py"),
        "#! /usr/local/bin/python",
        format_shebang("../../bin/python3"),
    )

    def runpip(pkg, upgrade=False):
        target = None
        python = dirs.prefix / "bin" / "python3"
        if sys.platform == LINUX:
            if env["RELENV_HOST_ARCH"] != env["RELENV_BUILD_ARCH"]:
                target = pymodules / "site-packages"
                python = env["RELENV_NATIVE_PY"]
        cmd = [
            str(python),
            "-m",
            "pip",
            "install",
            str(pkg),
        ]
        if upgrade:
            cmd.append("--upgrade")
        if target:
            cmd.append("--target={}".format(target))
        runcmd(cmd, env=env, stderr=logfp, stdout=logfp)

    runpip("wheel")
    # This needs to handle running from the root of the git repo and also from
    # an installed Relenv
    if (MODULE_DIR.parent / ".git").exists():
        runpip(MODULE_DIR.parent, upgrade=True)
    else:
        runpip("relenv", upgrade=True)
    globs = [
        "/bin/python*",
        "/bin/pip*",
        "/bin/relenv",
        "/lib/python*/ensurepip/*",
        "/lib/python*/site-packages/*",
        "/include/*",
        "*.so",
        "/lib/*.so.*",
        "*.a",
        "*.py",
        # Mac specific, factor this out
        "*.dylib",
    ]
    archive = f"{ dirs.prefix }.tar.xz"
    log.info("Archive is %s", archive)
    with tarfile.open(archive, mode="w:xz") as fp:
        create_archive(fp, dirs.prefix, globs, logfp)


def create_archive(tarfp, toarchive, globs, logfp=None):
    """
    Create an archive.

    :param tarfp: A pointer to the archive to be created
    :type tarfp: file
    :param toarchive: The path to the directory to archive
    :type toarchive: str
    :param globs: A list of filtering patterns to match against files to be added
    :type globs: list
    :param logfp: A pointer to the log file
    :type logfp: file
    """
    log.info("Current directory %s", os.getcwd())
    log.info("Creating archive %s", tarfp.name)
    for root, _dirs, files in os.walk(toarchive):
        relroot = pathlib.Path(root).relative_to(toarchive)
        for f in files:
            relpath = relroot / f
            matches = False
            for g in globs:
                if glob.fnmatch.fnmatch("/" / relpath, g):
                    matches = True
                    break
            if matches:
                log.info("Adding %s", relpath)
                tarfp.add(relpath, relpath, recursive=False)
            else:
                log.info("Skipping %s", relpath)
