"""
    Copyright 2017 Inmanta

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Contact: code@inmanta.com
"""

import glob
import imp
from io import BytesIO
import logging
import os
from os.path import sys
import re
from subprocess import CalledProcessError
import subprocess
from tarfile import TarFile

from pkg_resources import parse_version, parse_requirements
import yaml

from inmanta import env
from inmanta import plugins
from inmanta.ast import Namespace, CompilerException, ModuleNotFoundException, Location, LocatableString
from inmanta.ast.blocks import BasicBlock
from inmanta.ast.statements import DefinitionStatement, BiStatement, Statement
from inmanta.ast.statements.define import DefineImport
from inmanta.parser import plyInmantaParser
from inmanta.util import memoize, get_compiler_version
from typing import Tuple, List, Dict


LOGGER = logging.getLogger(__name__)


class InvalidModuleException(Exception):
    """
        This exception is raised if a module is invalid
    """


class InvalidModuleFileException(Exception):
    """
        This exception is raised if a module file is invalid
    """


class ProjectNotFoundExcpetion(Exception):
    """
        This exception is raised when inmanta is unable to find a valid project
    """


class GitProvider(object):

    def clone(self, src, dest):
        pass

    def fetch(self, repo):
        pass

    def get_all_tags(self, repo):
        pass

    def get_file_for_version(self, repo, tag, file):
        pass

    def checkout_tag(self, repo, tag):
        pass

    def commit(self, repo, message, commit_all, add=[]):
        pass

    def tag(self, repo, tag):
        pass

    def push(self, repo):
        pass


class CLIGitProvider(GitProvider):

    def clone(self, src, dest):
        env = os.environ.copy()
        env["GIT_ASKPASS"] = "true"
        subprocess.check_call(["git", "clone", src, dest], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env)

    def fetch(self, repo):
        env = os.environ.copy()
        env["GIT_ASKPASS"] = "true"
        subprocess.check_call(["git", "fetch", "--tags"], cwd=repo, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env)

    def status(self, repo):
        return subprocess.check_output(["git", "status", "--porcelain"], cwd=repo).decode("utf-8")

    def get_all_tags(self, repo):
        return subprocess.check_output(["git", "tag"], cwd=repo).decode("utf-8").splitlines()

    def checkout_tag(self, repo, tag):
        subprocess.check_call(["git", "checkout", tag], cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def commit(self, repo, message, commit_all, add=[]):
        for file in add:
            subprocess.check_call(["git", "add", file], cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not commit_all:
            subprocess.check_call(["git", "commit", "-m", message], cwd=repo,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.check_call(["git", "commit", "-a", "-m", message], cwd=repo,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tag(self, repo, tag):
        subprocess.check_call(["git", "tag", "-a", "-m", "auto tag by module tool", tag], cwd=repo,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def push(self, repo):
        return subprocess.check_output(["git", "push", "--follow-tags", "--porcelain"],
                                       cwd=repo, stderr=subprocess.DEVNULL).decode("utf-8")

    def get_file_for_version(self, repo, tag, file):
        data = subprocess.check_output(["git", "archive", "--format=tar", tag, file],
                                       cwd=repo, stderr=subprocess.DEVNULL)
        tf = TarFile(fileobj=BytesIO(data))
        tfile = tf.next()
        b = tf.extractfile(tfile)
        return b.read().decode("utf-8")


# try:
#     import pygit2
#     import re
#
#     class LibGitProvider(GitProvider):
#
#         def clone(self, src, dest):
#             pygit2.clone_repository(src, dest)
#
#         def fetch(self, repo):
#             repoh = pygit2.Repository(repo)
#             repoh.remotes["origin"].fetch()
#
#         def status(self, repo):
#             # todo
#             return subprocess.check_output(["git", "status", "--porcelain"], cwd=repo).decode("utf-8")
#
#         def get_all_tags(self, repo):
#             repoh = pygit2.Repository(repo)
#             regex = re.compile('^refs/tags/(.*)')
#             return [m.group(1) for m in [regex.match(t) for t in repoh.listall_references()] if m]
#
#         def checkout_tag(self, repo, tag):
#             repoh = pygit2.Repository(repo)
#             repoh.checkout("refs/tags/" + tag)
#
#         def commit(self, repo, message, commit_all, add=[]):
#             repoh = pygit2.Repository(repo)
#             index = repoh.index
#             index.read()
#
#             for file in add:
#                 index.add(os.path.relpath(file, repo))
#
#             if commit_all:
#                 index.add_all()
#
#             index.write()
#             tree = index.write_tree()
#
#             config = pygit2.Config.get_global_config()
#             try:
#                 email = config["user.email"]
#             except KeyError:
#                 email = "inmanta@example.com"
#                 LOGGER.warn("user.email not set in git config")
#
#             try:
#                 username = config["user.name"]
#             except KeyError:
#                 username = "Inmanta Moduletool"
#                 LOGGER.warn("user.name not set in git config")
#
#             author = pygit2.Signature(username, email)
#
#             return repoh.create_commit("HEAD", author, author, message, tree, [repoh.head.get_object().hex])
#
#         def tag(self, repo, tag):
#             repoh = pygit2.Repository(repo)
#
#             config = pygit2.Config.get_global_config()
#             try:
#                 email = config["user.email"]
#             except KeyError:
#                 email = "inmanta@example.com"
#                 LOGGER.warn("user.email not set in git config")
#
#             try:
#                 username = config["user.name"]
#             except KeyError:
#                 username = "Inmanta Moduletool"
#                 LOGGER.warn("user.name not set in git config")
#
#             author = pygit2.Signature(username, email)
#
#             repoh.create_tag(tag, repoh.head.target, pygit2.GIT_OBJ_COMMIT, author, "auto tag by module tool")
#
#     gitprovider = LibGitProvider()
# except ImportError as e:
gitprovider = CLIGitProvider()


class ModuleRepo(object):

    def clone(self, name: str, dest: str) -> bool:
        raise NotImplementedError("Abstract method")

    def path_for(self, name: str):
        # same class is used for search parh and remote repos, perhaps not optimal
        raise NotImplementedError("Abstract method")


class CompositeModuleRepo(ModuleRepo):

    def __init__(self, children):
        self.children = children

    def clone(self, name: str, dest: str) -> bool:
        for child in self.children:
            if child.clone(name, dest):
                return True
        return False

    def path_for(self, name: str):
        for child in self.children:
            result = child.path_for(name)
            if result is not None:
                return result
        return None


class LocalFileRepo(ModuleRepo):

    def __init__(self, root, parent_root=None):
        if parent_root is None:
            self.root = os.path.abspath(root)
        else:
            self.root = os.path.join(parent_root, root)

    def clone(self, name: str, dest: str) -> bool:
        try:
            gitprovider.clone(os.path.join(self.root, name), os.path.join(dest, name))
            return True
        except Exception:
            LOGGER.debug("could not clone repo", exc_info=True)
            return False

    def path_for(self, name: str):
        path = os.path.join(self.root, name)
        if os.path.exists(path):
            return path
        return None


class RemoteRepo(ModuleRepo):

    def __init__(self, baseurl):
        self.baseurl = baseurl

    def clone(self, name: str, dest: str) -> bool:
        try:
            url = self.baseurl.format(name)
            if url == self.baseurl:
                url = self.baseurl + name

            gitprovider.clone(url, os.path.join(dest, name))
            return True
        except Exception:
            LOGGER.debug("could not clone repo", exc_info=True)
            return False

    def path_for(self, name: str):
        raise NotImplementedError("Should only be called on local repos")


def make_repo(path, root=None):
    if ":" in path:
        return RemoteRepo(path)
    else:
        return LocalFileRepo(path, parent_root=root)


def merge_specs(mainspec, new):
    """Merge two maps str->[T] by concatting their lists."""
    for req in new:
        key = req.project_name
        if key not in mainspec:
            mainspec[key] = [req]
        else:
            mainspec[key] = mainspec[key] + [req]


class ModuleLike(object):
    """
        Commons superclass for projects and modules, which are both versioned by git
    """

    def __init__(self, path):
        """
            @param path: root git directory
        """
        self._path = path
        self._meta = {}

    def get_name(self):
        raise NotImplemented()

    name = property(get_name)

    def _load_file(self, ns, file) -> Tuple[List[Statement], BasicBlock]:
        ns.location = Location(file, 1)
        statements = []
        stmts = plyInmantaParser.parse(ns, file)
        block = BasicBlock(ns)
        for s in stmts:
            if isinstance(s, BiStatement):
                statements.append(s)
                block.add(s)
            elif isinstance(s, DefinitionStatement):
                statements.append(s)
            elif isinstance(s, str) or isinstance(s, LocatableString):
                pass
            else:
                block.add(s)
        return (statements, block)

    def requires(self) -> "List[List[Requirement]]":
        """
            Get the requires for this module
        """
        # filter on import stmt

        if "requires" not in self._meta or self._meta["requires"] is None:
            return []

        reqs = []
        for spec in self._meta["requires"]:
            req = [x for x in parse_requirements(spec)]
            if len(req) > 1:
                print("Module file for %s has bad line in requirements specification %s" % (self._path, spec))
            req = req[0]
            reqs.append(req)
        return reqs

    def get_config(self, name, default):
        if name not in self._meta:
            return default
        else:
            return self._meta[name]


INSTALL_RELEASES = "release"
INSTALL_PRERELEASES = "prerelease"
INSTALL_MASTER = "master"
INSTALL_OPTS = [INSTALL_MASTER, INSTALL_PRERELEASES, INSTALL_RELEASES]


class Project(ModuleLike):
    """
        An inmanta project
    """
    PROJECT_FILE = "project.yml"
    _project = None

    def __init__(self, path, autostd=True, main_file="main.cf"):
        """
            Initialize the project, this includes
             * Loading the project.yaml (into self._meta)
             * Setting paths from project.yaml
             * Loading all modules in the module path (into self.modules)
            It does not include
             * verify if project.yml corresponds to the modules in self.modules

            @param path: The directory where the project is located

        """
        super().__init__(path)
        self.project_path = path
        self.main_file = main_file

        if not os.path.exists(path):
            raise Exception("Unable to find project directory %s" % path)

        project_file = os.path.join(path, Project.PROJECT_FILE)

        if not os.path.exists(project_file):
            raise Exception("Project directory does not contain a project file")

        with open(project_file, "r") as fd:
            self._meta = yaml.load(fd)

        if "modulepath" not in self._meta:
            raise Exception("modulepath is required in the project(.yml) file")

        modulepath = self._meta["modulepath"]
        if not isinstance(modulepath, list):
            modulepath = [modulepath]
        self.modulepath = [os.path.abspath(os.path.join(path, x)) for x in modulepath]
        self.resolver = CompositeModuleRepo([make_repo(x) for x in self.modulepath])

        if "repo" not in self._meta:
            raise Exception("repo is required in the project(.yml) file")

        repo = self._meta["repo"]
        if not isinstance(repo, list):
            repo = [repo]
        self.repolist = [x for x in repo]
        self.externalResolver = CompositeModuleRepo([make_repo(x, root=path) for x in self.repolist])

        self.downloadpath = None
        if "downloadpath" in self._meta:
            self.downloadpath = os.path.abspath(os.path.join(
                path, self._meta["downloadpath"]))
            if self.downloadpath not in self.modulepath:
                LOGGER.warning("Downloadpath is not in module path! Module install will not work as expected")

            if not os.path.exists(self.downloadpath):
                os.mkdir(self.downloadpath)

        self.virtualenv = env.VirtualEnv(os.path.join(path, ".env"))

        self.loaded = False
        self.modules = {}

        self.root_ns = Namespace("__root__")

        self.autostd = autostd
        self._install_mode = INSTALL_RELEASES
        if "install_mode" in self._meta:
            mode = self._meta["install_mode"]
            if mode not in INSTALL_OPTS:
                LOGGER.warning("Invallid value for install_mode, should be one of [%s]" % ','.join(INSTALL_OPTS))
            else:
                self._install_mode = mode

    @classmethod
    def get_project_dir(cls, cur_dir):
        """
            Find the project directory where we are working in. Traverse up until we find Project.PROJECT_FILE or reach /
        """
        project_file = os.path.join(cur_dir, Project.PROJECT_FILE)

        if os.path.exists(project_file):
            return cur_dir

        parent_dir = os.path.abspath(os.path.join(cur_dir, os.pardir))
        if parent_dir == cur_dir:
            raise ProjectNotFoundExcpetion("Unable to find an inmanta project (project.yml expected)")

        return cls.get_project_dir(parent_dir)

    @classmethod
    def get(cls, main_file="main.cf"):
        """
            Get the instance of the project
        """
        if cls._project is None:
            cls._project = Project(cls.get_project_dir(os.curdir), main_file=main_file)

        return cls._project

    @classmethod
    def set(cls, project):
        """
            Get the instance of the project
        """
        cls._project = project
        os.chdir(project._path)
        plugins.PluginMeta.clear()

    def load(self):
        if not self.loaded:
            self.get_complete_ast()
            self.use_virtual_env()
            self.loaded = True
            self.verify()
            try:
                self.load_plugins()
            except CompilerException:
                # do python install
                pyreq = self.collect_python_requirements()
                if len(pyreq) > 0:
                    try:
                        # install reqs, with cache
                        self.virtualenv.install_from_list(pyreq)
                        self.load_plugins()
                    except CompilerException:
                        # cache could be damaged, ignore it
                        self.virtualenv.install_from_list(pyreq, cache=False)
                        self.load_plugins()
                else:
                    self.load_plugins()

    @memoize
    def get_ast(self) -> Tuple[List[Statement], BasicBlock]:
        return self.__load_ast()

    @memoize
    def get_imports(self):
        (statements, _) = self.get_ast()
        imports = [x for x in statements if isinstance(x, DefineImport)]
        if self.autostd:
            imports.insert(0, DefineImport("std", "std"))
        return imports

    @memoize
    def get_complete_ast(self):
        # load ast
        (statements, block) = self.get_ast()
        blocks = [block]
        statements = [x for x in statements]

        # get imports
        imports = [x for x in self.get_imports()]
        for _, nstmt, nb in self.load_module_recursive(imports):
            statements.extend(nstmt)
            blocks.append(nb)

        return (statements, blocks)

    def __load_ast(self):
        main_ns = Namespace("__config__", self.root_ns)
        return self._load_file(main_ns, os.path.join(self.project_path, self.main_file))

    def get_modules(self) -> Dict[str, "Module"]:
        self.load()
        return self.modules

    def get_module(self, full_module_name):
        parts = full_module_name.split("::")
        module_name = parts[0]

        if module_name in self.modules:
            return self.modules[module_name]
        return self.load_module(module_name)

    def load_module_recursive(self, imports: List[DefineImport]) -> List[Tuple[str, List[Statement], BasicBlock]]:
        """
            Load a specific module and all submodules into this project

            For each module, return a triple of name, statements, basicblock
        """
        out = []

        # get imports
        imports = [x for x in self.get_imports()]

        done = set()
        while len(imports) > 0:
            imp = imports.pop()
            ns = imp.name
            if ns in done:
                continue

            parts = ns.split("::")
            module_name = parts[0]

            try:
                # get module
                module = self.get_module(module_name)
                # get NS
                for i in range(1, len(parts) + 1):
                    subs = '::'.join(parts[0:i])
                    if subs in done:
                        continue
                    (nstmt, nb) = module.get_ast(subs)

                    done.add(subs)
                    out.append((subs, nstmt, nb))

                    # get imports and add to list
                    imports.extend(module.get_imports(subs))
            except InvalidModuleException:
                raise ModuleNotFoundException(ns, imp)

        return out

    def load_module(self, module_name) -> "Module":
        try:
            path = self.resolver.path_for(module_name)
            if path is not None:
                module = Module(self, path)
            else:
                reqs = self.collect_requirements()
                if module_name in reqs:
                    module = Module.install(self, module_name, reqs[module_name], install_mode=self._install_mode)
                else:
                    module = Module.install(self, module_name, parse_requirements(module_name), install_mode=self._install_mode)
            self.modules[module_name] = module
            return module
        except Exception:
            raise InvalidModuleException("Could not load module %s" % module_name)

    def load_plugins(self) -> None:
        """
            Load all plug-ins
        """
        if not self.loaded:
            LOGGER.warning("loading plugins on project that has not been loaded completely")
        for module in self.modules.values():
            module.load_plugins()

    def verify(self) -> None:
        # verify module dependencies
        result = True
        result &= self.verify_requires()
        if not result:
            raise CompilerException("Not all module dependencies have been met.")

    def use_virtual_env(self) -> None:
        """
            Use the virtual environment
        """
        self.virtualenv.use_virtual_env()

    def sorted_modules(self) -> list:
        """
            Return a list of all modules, sorted on their name
        """
        names = self.modules.keys()
        names = sorted(names)

        mod_list = []
        for name in names:
            mod_list.append(self.modules[name])

        return mod_list

    def collect_requirements(self):
        """
            Collect the list of all requirements of all modules in the project.
        """
        if not self.loaded:
            LOGGER.warning("collecting reqs on project that has not been loaded completely")

        specs = {}
        merge_specs(specs, self.requires())
        for module in self.modules.values():
            reqs = module.requires()
            merge_specs(specs, reqs)
        return specs

    def collect_imported_requirements(self):
        imports = set([x.name.split("::")[0] for x in self.get_complete_ast()[0] if isinstance(x, DefineImport)])
        imports.add("std")
        specs = self.collect_requirements()

        def get_spec(name):
            if name in specs:
                return specs[name]
            return parse_requirements(name)

        return {name: get_spec(name) for name in imports}

    def verify_requires(self) -> bool:
        """
            Check if all the required modules for this module have been loaded
        """
        LOGGER.info("verifying project")
        imports = set([x.name for x in self.get_complete_ast()[0] if isinstance(x, DefineImport)])
        modules = self.modules

        good = True

        for name, spec in self.collect_requirements().items():
            if name not in imports:
                continue
            module = modules[name]
            version = parse_version(str(module.version))
            for r in spec:
                if version not in r:
                    LOGGER.warning("requirement %s on module %s not fullfilled, now at version %s" % (r, name, version))
                    good = False

        return good

    def collect_python_requirements(self):
        """
            Collect the list of all python requirements off all modules in this project
        """
        pyreq = [x.strip() for x in [mod.get_python_requirements() for mod in self.modules.values()] if x is not None]
        pyreq = '\n'.join(pyreq).split("\n")
        pyreq = [x for x in pyreq if len(x.strip()) > 0]
        return list(set(pyreq))

    def get_name(self):
        return "project.yml"

    name = property(get_name)

    def get_config_file_name(self):
        return os.path.join(self._path, "project.yml")

    def get_root_namespace(self):
        return self.root_ns

    def get_freeze(self, mode="==", recursive=False):
        # collect in scope modules
        if not recursive:
            modules = {m.name: m for m in (self.get_module(imp.name) for imp in self.get_imports())}
        else:
            modules = self.get_modules()

        out = {}
        for name, mod in modules.items():
            version = str(mod.version)
            out[name] = mode + " " + version

        return out


class Module(ModuleLike):
    """
        This class models an inmanta configuration module
    """
    MODEL_DIR = "model"
    requires_fields = ["name", "license", "version"]

    def __init__(self, project: Project, path: str, **kwmeta: dict):
        """
            Create a new configuration module

            :param project: A reference to the project this module belongs to.
            :param path: Where is the module stored
            :param kwmeta: Meta-data
        """
        super().__init__(path)
        self._project = project
        self._meta = kwmeta
        self._plugin_namespaces = []

        if not Module.is_valid_module(self._path):
            raise InvalidModuleException(("Module %s is not a valid inmanta configuration module. Make sure that a " +
                                          "model/_init.cf file exists and a module.yml definition file.") % self._path)

        self.load_module_file()
        self.is_versioned()

    def rewrite_version(self, new_version):
        new_version = str(new_version)  # make sure it is a string!
        with open(self.get_config_file_name(), "r") as fd:
            module_def = fd.read()

        module_info = yaml.safe_load(module_def)
        if "version" not in module_info:
            raise Exception("Not a valid module definition")

        current_version = str(module_info["version"])
        if current_version == new_version:
            LOGGER.debug("Current version is the same as the new version: %s", current_version)

        new_module_def = re.sub("([\s]version\s*:\s*['\"\s]?)[^\"'}\s]+(['\"]?)",
                                "\g<1>" + new_version + "\g<2>", module_def)

        try:
            new_info = yaml.safe_load(new_module_def)
        except Exception:
            raise Exception("Unable to rewrite module definition %s" % self.get_config_file_name())

        if str(new_info["version"]) != new_version:
            raise Exception("Unable to write module definition, should be %s got %s instead." %
                            (new_version, new_info["version"]))

        with open(self.get_config_file_name(), "w+") as fd:
            fd.write(new_module_def)

        self._meta = new_info

    def get_name(self):
        """
            Returns the name of the module (if the meta data is set)
        """
        if "name" in self._meta:
            return self._meta["name"]

        return None

    name = property(get_name)

    def get_version(self) -> str:
        """
            Return the version of this module
        """
        if "version" in self._meta:
            return str(self._meta["version"])

        return None

    version = property(get_version)

    @property
    def compiler_version(self) -> str:
        """
            Get the minimal compiler version required for this module version. Returns none is the compiler version is not
            constrained.
        """
        if "compiler_version" in self._meta:
            return str(self._meta["compiler_version"])
        return None

    @classmethod
    def install(cls, project, modulename, requirements, install=True, install_mode=INSTALL_RELEASES):
        """
           Install a module, return module object
        """
        # verify pressence in module path
        path = project.resolver.path_for(modulename)
        if path is not None:
            # if exists, report
            LOGGER.info("module %s already found at %s", modulename, path)
            gitprovider.fetch(path)
        else:
            # otherwise install
            path = os.path.join(project.downloadpath, modulename)
            result = project.externalResolver.clone(modulename, project.downloadpath)
            if not result:
                raise InvalidModuleException("could not locate module with name: %s" % modulename)

        return cls.update(project, modulename, requirements, path, False, install_mode=install_mode)

    @classmethod
    def update(cls, project, modulename, requirements, path=None, fetch=True, install_mode=INSTALL_RELEASES):
        """
           Update a module, return module object
        """

        if path is None:
            path = project.resolver.path_for(modulename)

        if fetch:
            gitprovider.fetch(path)

        if install_mode == INSTALL_MASTER:
            gitprovider.checkout_tag(path, "master")
        else:
            release_only = (install_mode == INSTALL_RELEASES)
            version = cls.get_suitable_version_for(modulename, requirements, path, release_only=release_only)

            if version is None:
                print("no suitable version found for module %s" % modulename)
            else:
                gitprovider.checkout_tag(path, str(version))

        return Module(project, path)

    @classmethod
    def get_suitable_version_for(cls, modulename, requirements, path, release_only=True):
        versions = gitprovider.get_all_tags(path)

        def try_parse(x):
            try:
                return parse_version(x)
            except Exception:
                return None

        versions = [x for x in [try_parse(v) for v in versions] if x is not None]
        versions = sorted(versions, reverse=True)

        for r in requirements:
            versions = [x for x in r.specifier.filter(versions, not release_only)]

        comp_version = get_compiler_version()
        if comp_version is not None:
            comp_version = parse_version(comp_version)
            # use base version, to make sure dev versions work as expected
            comp_version = parse_version(comp_version.base_version)
            return cls.__best_for_compiler_version(modulename, versions, path, comp_version)
        else:
            return versions[0]

    @classmethod
    def __best_for_compiler_version(cls, modulename, versions, path, comp_version):
        def get_cv_for(best):
            cfg = gitprovider.get_file_for_version(path, str(best), "module.yml")
            cfg = yaml.load(cfg)
            if "compiler_version" not in cfg:
                return None
            v = cfg["compiler_version"]
            if isinstance(v, (int, float)):
                v = str(v)
            return parse_version(v)

        if not versions:
            return None

        best = versions[0]
        atleast = get_cv_for(best)
        if atleast is None or comp_version >= atleast:
            return best

        # binary search
        hi = len(versions)
        lo = 1
        while lo < hi:
            mid = (lo + hi) // 2
            atleast = get_cv_for(versions[mid])
            if atleast is not None and atleast > comp_version:
                lo = mid + 1
            else:
                hi = mid
        if hi == len(versions):
            LOGGER.warning("Could not find version of module %s suitable for this compiler, try a newer compiler" % modulename)
            return None
        return versions[lo]

    def is_versioned(self):
        """
            Check if this module is versioned, and if so the version number in the module file should
            have a tag. If the version has + the current revision can be a child otherwise the current
            version should match the tag
        """
        if not os.path.exists(os.path.join(self._path, ".git")):
            LOGGER.warning("Module %s is not version controlled, we recommend you do this as soon as possible."
                           % self._meta["name"])
            return False
        return True

    @classmethod
    def is_valid_module(cls, module_path):
        """
            Checks if this module is a valid configuration module. A module should contain a
            module.yml file.
        """
        if not os.path.isfile(os.path.join(module_path, "module.yml")):
            return False

        return True

    def load_module_file(self):
        """
            Load the module definition file
        """
        with open(self.get_config_file_name(), "r") as fd:
            mod_def = yaml.load(fd)

            if mod_def is None or len(mod_def) < len(Module.requires_fields):
                raise InvalidModuleFileException("The module file of %s does not have the required fields: %s" %
                                                 (self._path, ", ".join(Module.requires_fields)))

            for name, value in mod_def.items():
                self._meta[name] = value

        for req_field in Module.requires_fields:
            if req_field not in self._meta:
                raise InvalidModuleFileException(
                    "%s is required in module file of module %s" % (req_field, self._path))

        if self._meta["name"] != os.path.basename(self._path):
            LOGGER.warning("The name in the module file (%s) does not match the directory name (%s)"
                           % (self._meta["name"], os.path.basename(self._path)))

    def get_config_file_name(self):
        return os.path.join(self._path, "module.yml")

    def get_module_files(self):
        """
            Returns the path of all model files in this module, relative to the module root
        """
        files = []
        for model_file in glob.glob(os.path.join(self._path, "model", "*.cf")):
            files.append(model_file)

        return files

    @memoize
    def get_ast(self, name) -> Tuple[List[Statement], BasicBlock]:
        if name == self.name:
            file = os.path.join(self._path, Module.MODEL_DIR, "_init.cf")
        else:
            parts = name.split("::")
            parts = parts[1:]
            if os.path.isdir(os.path.join(self._path, Module.MODEL_DIR, *parts)):
                path_elements = [self._path, Module.MODEL_DIR] + parts + ["_init.cf"]
            else:
                path_elements = [self._path, Module.MODEL_DIR] + parts[:-1] + [parts[-1] + ".cf"]
            file = os.path.join(*path_elements)

        ns = self._project.get_root_namespace().get_ns_or_create(name)

        try:
            return self._load_file(ns, file)
        except FileNotFoundError:
            raise InvalidModuleException("could not locate module with name: %s", name)

    def get_freeze(self, submodule, recursive=False, mode=">="):
        imports = [statement.name for statement in self.get_imports(submodule)]

        out = {}

        todo = imports

        for impor in todo:
            if impor not in out:
                mainmod = self._project.get_module(impor)
                version = mainmod.version
                # track submodules for cycle avoidance
                out[impor] = mode + " " + version
                if recursive:
                    todo.extend([statement.name for statement in mainmod.get_imports(impor)])

        # drop submodules
        return {x: v for x, v in out.items() if "::" not in x}

    @memoize
    def get_imports(self, name):
        (statements, _) = self.get_ast(name)
        imports = [x for x in statements if isinstance(x, DefineImport)]
        if self._project.autostd:
            imports.insert(0, DefineImport("std", "std"))
        return imports

    def _get_model_files(self, curdir):
        files = []
        init_cf = os.path.join(curdir, "_init.cf")
        if not os.path.exists(init_cf):
            return files

        for entry in os.listdir(curdir):
            entry = os.path.join(curdir, entry)
            if os.path.isdir(entry):
                files.extend(self._get_model_files(entry))

            elif entry[-3:] == ".cf":
                files.append(entry)

        return files

    def get_all_submodules(self) -> List[str]:
        """
            Get all submodules of this module
        """
        modules = []
        cur_dir = os.path.join(self._path, Module.MODEL_DIR)
        files = self._get_model_files(cur_dir)

        for f in files:
            name = f[len(cur_dir) + 1:-3]
            parts = name.split("/")
            if parts[-1] == "_init":
                parts = parts[:-1]

            parts.insert(0, self.get_name())
            name = "::".join(parts)

            modules.append(name)

        return modules

    def load_plugins(self):
        """
            Load all plug-ins from a configuration module
        """
        plugin_dir = os.path.join(self._path, "plugins")

        if not os.path.exists(plugin_dir):
            return

        if not os.path.exists(os.path.join(plugin_dir, "__init__.py")):
            raise CompilerException(
                "The plugin directory %s should be a valid python package with a __init__.py file" % plugin_dir)

        try:
            mod_name = self._meta["name"]
            imp.load_package("inmanta_plugins." + mod_name, plugin_dir)

            self._plugin_namespaces.append(mod_name)

            for py_file in glob.glob(os.path.join(plugin_dir, "*.py")):
                if not py_file.endswith("__init__.py"):
                    # name of the python module
                    sub_mod = "inmanta_plugins." + mod_name + "." + os.path.basename(py_file).split(".")[0]
                    self._plugin_namespaces.append(sub_mod)

                    # load the python file
                    imp.load_source(sub_mod, py_file)

        except ImportError as e:
            raise CompilerException("Unable to load all plug-ins for module %s" % self._meta["name"]) from e

    def versions(self):
        """
            Provide a list of all versions available in the repository
        """
        versions = gitprovider.get_all_tags(self._path)

        def try_parse(x):
            try:
                return parse_version(x)
            except Exception:
                return None

        versions = [x for x in [try_parse(v) for v in versions] if x is not None]
        versions = sorted(versions, reverse=True)

        return versions

    def status(self):
        """
            Run a git status on this module
        """
        try:
            output = gitprovider.status(self._path)

            files = [x.strip() for x in output.split("\n") if x != ""]

            if len(files) > 0:
                print("Module %s (%s)" % (self._meta["name"], self._path))
                for f in files:
                    print("\t%s" % f)

                print()
            else:
                print("Module %s (%s) has no changes" % (self._meta["name"], self._path))
        except Exception:
            print("Failed to get status of module")
            LOGGER.exception("Failed to get status of module %s")

    def push(self):
        """
            Run a git status on this module
        """
        sys.stdout.write("%s (%s) " % (self.get_name(), self._path))
        sys.stdout.flush()
        try:
            print(gitprovider.push(self._path))
        except CalledProcessError:
            print("Cloud not push module %s" % self.get_name())
        else:
            print("done")
        print()

    def get_python_requirements(self):
        """
            Install python requirements with pip in a virtual environment
        """
        file = os.path.join(self._path, "requirements.txt")
        if os.path.exists(file):
            with open(file, 'r') as fd:
                return fd.read()
        else:
            return None

    @memoize
    def get_python_requirements_as_list(self):
        raw = self.get_python_requirements()
        if raw is None:
            return []
        else:
            return [y for y in [x.strip() for x in raw.split("\n")] if len(y) != 0]

    def execute_command(self, cmd):
        print("executing %s on %s in %s" % (cmd, self.get_name(), self._path))
        print("=" * 10)
        subprocess.call(cmd, shell=True, cwd=self._path)
        print("=" * 10)
