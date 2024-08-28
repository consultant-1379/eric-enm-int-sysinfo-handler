"""
Helm common functions module, based on Local shell
"""
import enum
import glob
import os
import time
import shutil
import tarfile
import tempfile
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
import ruamel.yaml
import yaml

from utilities.cmd_common import execute_command
from utilities import logutil
from utilities.netrc_common import NetRCCredsGetter
from helmpython.helm_chart import HelmChart
from helmpython.helm_chart import Credentials

LOGGER = logutil.get_logger(__name__)

SUPPORTED_HELM_VERSIONS = enum.Enum('SUPPORTED_HELM_VERSIONS', 'V3')
TIMEOUT = 240


###############################################################################
def get_helmver():
    return SUPPORTED_HELM_VERSIONS.V3


###############################################################################
def _get_data_from_chart(chart_path):
    chart_yaml_path = os.path.join(chart_path, 'Chart.yaml')
    if not os.path.exists(chart_yaml_path):
        raise HelmCommonException("Chart.yaml does not exists: "
                                  f"{chart_yaml_path}")

    with open(chart_yaml_path, "r") as chart_file:
        chart_data = chart_file.read()
    return ruamel.yaml.round_trip_load(chart_data)


###############################################################################
def _add_quote_app_version(workspace, chart_name, chart_package,
                           app_version):
    LOGGER.debug("Found 'e' in 'app-version' value, add quote")
    untar_tmp = os.path.join(workspace, "untar_tmp")

    # Clean untar directory and create new folder
    _cleanup_old_folder(untar_tmp)
    os.mkdir(untar_tmp)

    with tarfile.open(chart_package, 'r') as tar:
        untar_files_top = os.path.commonprefix(tar.getnames())
        if os.path.normpath(untar_files_top) != chart_name:
            raise HelmCommonException("Not one folder in the tar file: "
                                      f"{os.path.normpath(untar_files_top)}")

        tar.extractall(path=untar_tmp)

        data = _get_data_from_chart(os.path.join(untar_tmp, chart_name))
        data['appVersion'] = DoubleQuotedScalarString(str(app_version))

        with open(os.path.join(untar_tmp, chart_name, 'Chart.yaml'),
                  "w") as new_chart_file:
            ruamel.yaml.round_trip_dump(data, new_chart_file,
                                        explicit_start=True)
    os.remove(chart_package)

    with tarfile.open(chart_package, "w:gz") as tar:
        for root, _, files in os.walk(untar_tmp):
            root_ = os.path.relpath(root, start=untar_tmp)
            for file_helm_file in files:
                rel_path = os.path.join(root_, file_helm_file)
                tar.add(os.path.join(root, file_helm_file),
                        arcname=rel_path)
    return chart_package


###############################################################################
def _cleanup_old_folder(clean_dir):
    if os.path.exists(clean_dir):
        LOGGER.warning("Removing existing %s", clean_dir)
        try:
            shutil.rmtree(clean_dir)
        except Exception as helm_except:
            raise HelmCommonException(
                f"Failed to delete {clean_dir}"
                f"exception is: {str(helm_except)}") from helm_except


###############################################################################
def _resolve_package_name(helm_chart: HelmChart, package_name: 'str | None'):
    """
    This function takes a helm chart and a package name
    and returns the name for the helm package .tgz.

    Params
    ---------------------------------------------------------------
    helm_chart: HelmChart
        A HelmChart object.

    package_name: str | None
        A specified name for the package. Can be None.

    Returns
    ----------------------------------------------------------------
    str
        package_name if it was not None, otherwise the
        name found in Chart.yaml.
    """
    # This function exists to please pylint
    if package_name is None:
        chart_name = helm_chart.name
    else:
        chart_name = package_name
    return chart_name


###############################################################################
def _get_workspace_destination(workspace, destination):
    if not workspace:
        workspace = os.getcwd()
    else:
        workspace = os.path.abspath(workspace)
    if not os.path.exists(workspace):
        os.makedirs(workspace)
    if not destination:
        destination = os.getcwd()
    else:
        destination = os.path.abspath(destination)
    if not os.path.exists(destination):
        os.makedirs(destination)
    return workspace, destination


###############################################################################
def _print_helmversion_used(self):
    if self.version == SUPPORTED_HELM_VERSIONS.V3:
        LOGGER.info("Helm V3 used")


###############################################################################
def _add_double_quotes(input_string):
    """
    Check if string is already quoted,
    if not then add double quotes to string.
    :arg input_string string to be quoted
    :returns quoted string
    """
    output_string = input_string
    if len(input_string) > 1:
        if not (input_string.startswith(('"', "'")) and
                input_string.endswith(('"', "'"))):
            output_string = f'"{input_string}"'
    return output_string


###############################################################################
class Helm:
    """
    Helm class, call Local shell helm bin
    """

    def __init__(self, workdir=None,
                 version=SUPPORTED_HELM_VERSIONS.V3):
        """
        :arg stable the stable helm repository url
        :arg workdir work directory
        """

        if version not in SUPPORTED_HELM_VERSIONS:
            raise Exception("Unsupported Helm version")

        self.version = version
        self.helm_cmd = None
        self.file_repos = []
        if os.environ.get("HELM_HOME") is not None:
            self.home = os.environ["HELM_HOME"]
        elif workdir is not None:
            self.home = os.path.join(workdir, ".helm")
            os.environ["HELM_HOME"] = os.path.realpath(self.home)
        else:
            self.home = os.path.join(os.environ.get("HOME"), ".helm")
        if not os.path.isdir(self.home):
            os.makedirs(self.home)

        if self.version == SUPPORTED_HELM_VERSIONS.V3:
            self.__init_v3()

        cmd = f"{self.helm_cmd} version --client"
        response = execute_command(cmd, verbose=True, timeout=TIMEOUT)
        if response.returncode > 0:
            raise Exception("Failed to construct Helm client wrapper")

        LOGGER.debug("Path = %s", os.environ['PATH'])
        LOGGER.info("Helm client wrapper successfully instantiated:")
        LOGGER.info(response.stdout)
        LOGGER.warning(response.stderr)

    def __init_v3(self):
        # As of helm 3.1.2, helm's quality is abismal, full of crazy bugs,
        # like not even the command line flags working as expected:
        #
        # Open Issue on --repository-cache not working:
        # https://github.com/helm/helm/issues/7141
        #
        # Open Issue on --registry-config not working:
        # https://github.com/helm/helm/issues/7351
        #
        # Etc. etc. nothing really works here, and some of these issues
        # are months old as I am writing this, so as of now our only
        # way forward is doing all these workarounds with env vars.
        # So basically the solution is to set HOME and have Helm 3
        # Store the repos, the config and the cache there:

        self.helm_cmd = "/usr/local/bin/helm"
        self.v3_settings = {
            k: os.path.join(os.environ.get("HOME"), v)
            for k, v in {
                'repository-config': 'repositories.yaml',
                'registry-config': 'registry.json',
                'repository-cache': 'repository'
            }.items()
        }

        self.v3_settings_str = ''.join(
            f' --{k}={v}' for k, v in self.v3_settings.items())

    def repo_update(self):
        """
        Run helm repo update
        """
        cmd = f"{self.helm_cmd} repo update"
        if execute_command(cmd,
                           verbose=True,
                           retries=2,
                           timeout=TIMEOUT).returncode > 0:
            raise HelmCommonException("Helm repo add failed")

    def repo_add(self, url, name=None, username=None, password=None):
        """
        Add helm repository
        :arg url repo url
        :arg name repo name. When name is not set, generate a name
        :arg username ARM username
        :arg password ARM api token
        """
        url = url.strip("/")
        repos = HelmRepositories(self.version)
        if name is None:
            name = repos.get_name(url)
            if name is None:
                name = repos.generate_name(url)
            else:
                LOGGER.info("Helm repo url %s already exists with name %s",
                            url, name)
                return name
        authstr = ''
        maskstr = None
        if username and password:
            authstr = (
                (' --username {username} --password {password}').format(
                    username=username, password=password))
            maskstr = password
            authstr = f' --pass-credentials{authstr}'

        cmd = (f"{self.helm_cmd} repo add {name} {url}{authstr}")
        _print_helmversion_used(self)
        if execute_command(cmd,
                           verbose=True,
                           mask=maskstr,
                           timeout=TIMEOUT).returncode > 0:
            raise HelmCommonException("Helm repo add failed")
        LOGGER.info("Successfully added %s with name %s", url, name)
        return name

    def replace_in_released_chart(self, replace, chart_filename):
        """
        Return the tmp path of released hedlm tgz package that has
        replaced values.
        """
        tmp_path = os.path.realpath(chart_filename)
        directory = os.path.dirname(chart_filename)
        with tempfile.TemporaryDirectory() as path:
            with tarfile.open(tmp_path, 'r') as tar:
                tar.extractall(path)
                chart_name, chart_version = self.get_chart_name_version(
                    chart_filename)
                data = os.path.join(path, chart_name)
                Helm._replace_in_chart(replace, data)
                pack_name = f"{chart_name}-{chart_version}.tgz"
            with tarfile.open(os.path.join(directory,
                                           pack_name), "w:gz") as tar:
                tar.add(data, arcname=os.path.basename(data))
        return pack_name

    def get_repo_name(self, repository):
        """
        Return the helm repo name stored in local cache
        :arg repository the helm chart repository url
        """
        repository = repository.strip("/")
        repos = HelmRepositories(self.version)
        return repos.get_name(repository)

    def get_chart_name_version(self, chart_archive):
        """
        Deduct a "chart_name" and "chart_version" for using it as part of
        a release name.
        Takes the chart archive and does a 'helm inspect chart' on it
        takes the value part of the name (after the space) from the line
        starting with 'name: ' and 'version: '

        helm inspect result example:
            name: eric-data-search-engine
            version: 2.8.0-2

            ---
            ......

        """

        # Below code, get the first line match the "name: "
        # And .split(' ')[1], here index "1" means the second work
        # of the match line string, here is the <name>
        chart_name = list(
            filter(
                lambda x: x.startswith('name: '),
                execute_command(
                    f"{self.helm_cmd} inspect chart " +
                    chart_archive,
                    timeout=TIMEOUT
                ).stdout.split('\n')))[0].split(' ')[1]

        chart_version = list(
            filter(
                lambda x: x.startswith('version: '),
                execute_command(
                    f"{self.helm_cmd} inspect chart {chart_archive}",
                    timeout=TIMEOUT
                ).stdout.split('\n')))[0].split(' ')[1]

        if (not chart_name) or (not chart_version):
            raise HelmCommonException("Failed to get chart name and chart "
                                      "version from .tgz file: "
                                      f"{chart_archive}")
        return chart_name, chart_version

    def search(self, keyword, version=None, params=None, retry=0):
        """
        Run helm search command with parameters
        :arg keyword e.g. repo path
        :arg version the helm chart version (can be partial)
        :arg params Example "released/eric-demo-common-a-int --version 1.2.2"
        :arg retry workaround for helm repo racing issue
        """

        try:
            retry_local = int(retry) + 1
        except ValueError as err:
            raise HelmCommonException(
                "search terminates. Parameter retry "
                f"[{retry}] is not a integer") from err

        LOGGER.debug("keyword in search = %s", keyword)
        LOGGER.debug("version in search = %s", version)
        LOGGER.debug("params in search = %s", params)

        if self.version == SUPPORTED_HELM_VERSIONS.V3:
            cmd = f"{self.helm_cmd} search repo --regexp '{keyword}\\v'"
        cmd += " --output yaml"

        if params:
            cmd = f"{cmd} {params}"
        # workaround for helm returning random versions on versioned
        # searches. We fetch all versions for x.y.z and process them
        # in here
        if version:
            cmd += " --versions"

        while retry_local > 0:
            self.repo_update()

            _r = execute_command(cmd, verbose=True, timeout=TIMEOUT)
            if _r.returncode != 0:
                raise HelmCommonException("Helm repo search failed")

            chart_versions = yaml.safe_load(_r.stdout)
            if isinstance(chart_versions, list):
                for chart_version in chart_versions:
                    # Helm 3 uses all lowercase keys
                    if (version == chart_version.get("version") or
                            version == chart_version.get("Version")):
                        return chart_version

            retry_local -= 1
            time.sleep(3)
        return None

    def package(self,
                helm_chart_folder,
                new_version,
                helm_package_name=None,
                app_version=None,
                destination=None,
                replace=None,
                workspace=None,
                repo_cred_path=None,
                helm_user=None,
                helm_token=None,
                retries=2,
                skip_dep_update=False):
        # pylint: disable=too-many-arguments,too-many-locals
        """
        Run helm package command
        :arg helm_chart_folder
        :arg new_version
        :arg app_version
        :arg replace replace given parameters in values.yaml
        :arg destination folder (default is current path)
        :arg workspace folder (default is current path)
        :arg repo_cred_path repositories yaml (default is repositories.yaml)
        :arg helm_user helm user, lower prio then repo_cred_path
        :arg helm_token helm password, lower prio then repo_cred_path
        :arg retries number of retries when executing the package command
        :arg skip_dep_update skip dependency update during packaging
        """
        if not os.path.exists(helm_chart_folder):
            raise AttributeError("Helm chart folder does not exists"
                                 f"{helm_chart_folder}")
        # converts to abspath
        workspace, destination = _get_workspace_destination(workspace,
                                                            destination)

        # repo add check credential, single repo credential
        # or use helm_user and helm_token globally, or no user/pass
        self._repo_add_credential(helm_chart_folder, repo_cred_path,
                                  helm_user, helm_token)

        chart = HelmChart.load_chart(helm_chart_folder)
        # Get chart name
        chart_name = _resolve_package_name(
            chart, helm_package_name
        )

        # Chart package path
        chart_package = os.path.join(destination,
                                     f"{chart_name}-{new_version}.tgz")

        if os.path.exists(chart_package):
            os.remove(chart_package)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_chart_folder = os.path.join(tmp_dir, chart_name)
            LOGGER.info("TMP folder [%s]", os.path.realpath(tmp_chart_folder))

            # Copy helm chart to temporary location
            try:
                shutil.copytree(helm_chart_folder, tmp_chart_folder)
                self._copy_chart_to_folder(helm_chart_folder, tmp_chart_folder)
            except Exception as helm_except:
                raise HelmCommonException(
                    f"Fail to copy from {helm_chart_folder}"
                    f" to {tmp_chart_folder}"
                    f" Exception info: {str(helm_except)}") from helm_except

            if not os.path.exists(tmp_chart_folder):
                raise HelmCommonException(
                    f"Failed to copy {helm_chart_folder} to"
                    f"{tmp_chart_folder}")
            if replace:
                Helm._replace_in_chart(replace, tmp_chart_folder)

            # Run helm package
            if self.version == SUPPORTED_HELM_VERSIONS.V3:
                cmd = (f"{self.helm_cmd} package "
                       f"--version {_add_double_quotes(new_version)} "
                       f"--destination {destination}")

            if not skip_dep_update:
                cmd += " --dependency-update"

            if app_version is not None:
                cmd += f" --app-version {_add_double_quotes(app_version)}"
            cmd += f" {tmp_chart_folder}"
            _print_helmversion_used(self)
            response = execute_command(cmd,
                                       workspace,
                                       True,
                                       retries=retries,
                                       timeout=None)

            if response.returncode > 0:
                LOGGER.error("helm package command failed!")
                LOGGER.error(" ======= stderr ======= ")
                LOGGER.error(response.stderr)
                LOGGER.error(" ======= stdout ======= ")
                LOGGER.error(response.stdout)
                return None

            # Return result
            if os.path.exists(chart_package):
                LOGGER.info("Successfully created package %s",
                            chart_package)
                app_version_latest = app_version
                chart_data = _get_data_from_chart(helm_chart_folder)
                if (not app_version) and ('appVersion' in chart_data):
                    app_version_latest = chart_data['appVersion']

                if app_version_latest:
                    _add_quote_app_version(workspace, chart.name,
                                           chart_package, app_version_latest)
                    LOGGER.info("Successfully modify 'app-version'")
                return chart_package

        LOGGER.info("Helm package failed to create %s", chart_package)
        return None

    def _copy_chart_to_folder(self, helm_chart_folder, tmp_chart_folder):
        for repo in self.file_repos:
            repo_dir = os.path.join(helm_chart_folder, repo)
            tmp_repo_folder = os.path.join(tmp_chart_folder, repo)
            shutil.copytree(repo_dir, tmp_repo_folder)

    def _repo_add_credential(self,
                             helm_chart_folder,
                             repo_cred_path,
                             helm_user,
                             helm_token):
        # Get dependencies
        chart = HelmChart.load_chart(helm_chart_folder)
        netrc_path = os.environ.get("NETRC", '')
        try:
            netrc_creds = NetRCCredsGetter(netrc_path)
        except FileNotFoundError:
            netrc_creds = None
        repo_cred = None
        for url in chart.repositories:
            if repo_cred_path and os.path.exists(repo_cred_path):
                repo_cred = Credentials(repo_cred_path)
                repo_cred.register_repos(self)
                if should_url_be_copied(url):
                    self.file_repos.append(url[7:])
            elif should_url_be_copied(url):
                self.file_repos.append(url[7:])
            elif helm_user and helm_token:
                self.repo_add(url, username=helm_user, password=helm_token)
            elif (not helm_user and not helm_token and
                  netrc_path and
                  (netrc_creds.is_hostname_exists(url.split('/')[2])
                   or netrc_creds.is_default_exists())):
                hostname = url.split('/')[2]
                username, _, password = netrc_creds.get_credentials(hostname)
                self.repo_add(url, username=username, password=password)
            elif url.startswith('https'):
                self.repo_add(url)

    @staticmethod
    def _replace_in_chart(replace, chart_folder):
        for from_to_str in replace:
            fromto = from_to_str.split('=', maxsplit=1)
            if (not fromto) or (len(fromto) != 2):
                raise HelmCommonException("replace string does not "
                                          "follow: <from>=<to> format: "
                                          f"{from_to_str}")
            from_value, to_value = fromto[:2]
            file_in = f"{chart_folder}/values.yaml"
            from_value_split = from_value.split(':')
            if len(from_value_split) == 2:
                from_value = from_value_split[1]
                file_in = f"{chart_folder}/{from_value_split[0]}"
            if not os.path.isfile(file_in):
                raise HelmCommonException("File not exist: {file}"
                                          .format(file=file_in))
            file_out = "tmp_value.yaml"
            with open(file_in, "rt") as fin, open(file_out, "wt") as fout:
                for line in fin:
                    fout.write(line.replace(from_value, to_value))
            os.remove(file_in)
            shutil.move(file_out, file_in)

    def fetch(self,
              chart_name,
              version,
              repo,
              workspace=None,
              helm_user=None,
              helm_token=None,
              retries=0):
        # pylint: disable=too-many-arguments
        """
        Run helm fetch command
        :arg chart_name
        :arg version chart version
        :arg repo from which repo to fetch
        :arg workspace folder (default is current path)
        :arg helm_user helm user, lower prio then repo_cred_path
        :arg helm_token helm password, lower prio then repo_cred_path
        """
        if workspace is None:
            workspace = os.getcwd()
        else:
            workspace = os.path.abspath(workspace)
        if not os.path.exists(workspace):
            os.makedirs(workspace)

        authstr = ''
        maskstr = None
        if helm_user and helm_token:
            self.repo_add(repo, username=helm_user, password=helm_token)
            time.sleep(2)
            authstr = f" --username {helm_user} --password {helm_token}"
            maskstr = helm_token
            authstr = f' --pass-credentials{authstr}'

        # Run helm fetch
        cmd = (f"{self.helm_cmd} fetch --repo {repo} {chart_name} --version "
               f"{version} {authstr} --destination {workspace}")

        _print_helmversion_used(self)
        response = execute_command(cmd,
                                   verbose=True,
                                   mask=maskstr,
                                   timeout=TIMEOUT,
                                   retries=retries)

        if response.returncode > 0:
            LOGGER.error("helm fetch command failed!")
            LOGGER.error(" ======= stderr ======= ")
            LOGGER.error(response.stderr)
            LOGGER.error(" ======= stdout ======= ")
            LOGGER.error(response.stdout)
            return None

        results = []
        for extension in ["tgz", "tar.gz"]:
            result = glob.glob(
                f"{workspace}/{chart_name}-{version}*.{extension}")
            if result:
                results.extend(result)
        if len(results) != 1:
            raise HelmCommonException("Failed to obtain the archive name")

        LOGGER.info("Archive successfully fetched: %s", results[0])
        return results[0]

    def fetch_untar(self,
                    chart_name,
                    version,
                    repo,
                    workspace=None,
                    helm_user=None,
                    helm_token=None,
                    retries=0):
        # pylint: disable=too-many-arguments
        """
        Run helm package command
        :arg chart_name
        :arg version chart version
        :arg repo from which repo to fetch
        :arg workspace folder (default is current path)
        :arg helm_user helm user, lower prio then repo_cred_path
        :arg helm_token helm password, lower prio then repo_cred_path
        """
        if workspace is None:
            workspace = os.getcwd()
        else:
            workspace = os.path.abspath(workspace)
        if not os.path.exists(workspace):
            os.makedirs(workspace)

        authstr = ''
        maskstr = None
        if helm_user and helm_token:
            self.repo_add(repo, username=helm_user, password=helm_token)
            time.sleep(2)
            authstr = f" --username {helm_user} --password {helm_token}"
            maskstr = helm_token
            authstr = f' --pass-credentials{authstr}'

        # Run helm package
        cmd = (f"{self.helm_cmd} fetch --repo {repo} {chart_name} --version "
               f"{version} {authstr} --untar --untardir {workspace}")

        _print_helmversion_used(self)
        response = execute_command(cmd,
                                   verbose=True,
                                   mask=maskstr,
                                   timeout=TIMEOUT,
                                   retries=retries)

        if response.returncode > 0:
            LOGGER.error("helm fetch command failed!")
            LOGGER.error(" ======= stderr ======= ")
            LOGGER.error(response.stderr)
            LOGGER.error(" ======= stdout ======= ")
            LOGGER.error(response.stdout)
            return None

        LOGGER.info("Successfully fetch %s with %s", chart_name, version)
        return chart_name


###############################################################################
class HelmRepositories:
    """
    Helm repositories class
    """

    def __init__(self, version=SUPPORTED_HELM_VERSIONS.V3):
        """
        initiate
        """

        if version not in SUPPORTED_HELM_VERSIONS:
            raise Exception("Unsupported Helm version")

        self.version = version
        self.yaml_file = None

        # In case of Helm 3 there is no init and
        # And the repositories.yaml doesn't seem to exist
        # if no repo is added so we can't populate the cache
        # immediately
        if self.version == SUPPORTED_HELM_VERSIONS.V3:
            self.yaml_file = None
            self.repositories = None

    def populate_in_memory_repositories_cache(self):
        '''
        docstring
        '''
        if self.version == SUPPORTED_HELM_VERSIONS.V3:
            yaml_file = f"{os.environ['HOME']}/repository/repositories.yaml"
        if not os.path.exists(yaml_file):
            self.yaml_file = None
            self.repositories = {}
            return
        with open(yaml_file, 'r') as stream:
            doc = yaml.safe_load(stream)
        self.yaml_file = yaml_file
        self.repositories = doc

    def contains_url(self, url):
        """
        Return true if helm repo list contains the repository url
        :arg url helm chart repository url
        """
        url = url.strip("/")
        for repo in self.repositories["repositories"]:
            if repo["url"] is url:
                return True
        return False

    def contains_name(self, name):
        """
        Return true if helm repo list contains the name
        :arg name helm chart repository name
        """
        if self.version == SUPPORTED_HELM_VERSIONS.V3:
            self.populate_in_memory_repositories_cache()
        if self.repositories:
            for repo in self.repositories["repositories"]:
                if repo["name"] == name:
                    return True
        return False

    def get_name(self, url):
        """
        Return the helm repo name stored in local cache
        :arg url the helm chart repository url
        """
        if self.version == SUPPORTED_HELM_VERSIONS.V3:
            self.populate_in_memory_repositories_cache()

        if self.repositories:
            url = url.strip("/")
            for repo in self.repositories["repositories"]:
                if repo["url"] == url:
                    return repo["name"]
        return None

    def generate_name(self, repository):
        """
        Generate a repo name from the url given
        :arg repository helm chart repository url
        """
        LOGGER.debug("Generate repository name for %s", repository)
        tokens = repository.strip("/").split("/")
        name = tokens[len(tokens) - 1]
        if self.contains_name(name):
            for i in range(1, 11):
                new_name = '%s-%d' % (name, i)
                if not self.contains_name(new_name):
                    name = new_name
                    break
            if not name:
                raise Exception("Coult not generate repo name for "
                                "%s.The name %s is used too often"
                                "" % (repository, name))
        return name


###############################################################################
class HelmCommonException(Exception):
    """
    Signal a fatal exception while executing helm actions.
    """

    def __init__(self, msg):
        Exception.__init__(self, msg)


def should_url_be_copied(url):
    """Return true if a url is located outside the current directory"""
    if url.startswith("file"):
        part_url = url[7:]
        if part_url.startswith('/') or part_url.startswith('..'):
            return True
    return False
