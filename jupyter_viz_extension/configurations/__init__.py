import os
from abc import ABC, abstractmethod
from io import FileIO
from jupyter_server.serverapp import ServerApp
from pathlib import Path
from secrets import token_urlsafe, token_hex
from socket import socket
from subprocess import Popen
from tempfile import mkstemp
from yaml import safe_load

from ..types import *
from ..proxy import make_trame_proxy_handler


class Configuration(ABC):
    """
    The Configuration class allows users to customize the behaviour of the JuViz Backend. By creating a custom
    Configuration class, you can change how ParaView is launched, how trame instances are launched and how to load the
    config file for a JuViz app. See the methods here or the predefined configuration for reference.
    The Configuration is determined at runtime using the "JUVIZ_CONFIGURATION" environment variable.
    """
    def __init__(self, logger):
        self._logger = logger

    @property
    def log(self):
        """ Logger property """
        return self._logger

    @staticmethod
    def get_open_port() -> int:
        """ Query an open socket on the machine """

        # We let socket determine an available open port
        with socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    def discover_apps(self) -> list[TrameApp]:
        """
        Search the system for trame apps that can be launched with JuViz.
        The default behavious will rely on the `JUPYTER_PATH`environment variable, where trame apps will be found in
        `trame/<app_name> directories`.

        @return: A list wich all the found trame apps
        """
        # We use JUPYTER_PATH to discover Apps
        paths = os.getenv("JUPYTER_PATH")
        paths = paths.split(os.pathsep) if paths else []
        self.log.info(f"Searching for trame apps in {paths!r}")

        apps = []
        for path in paths:
            path = Path(path) / "trame"
            if not path.is_dir() or not path.exists():
                continue

            for name in path.iterdir():
                app = self.parse_app_config(name)
                apps.append(app)

        return apps

    def parse_app_config(self, path) -> TrameApp:
        """
        Parse the app.yml file of a trame app. A default config file has the following values:

        - name: The display name of the app to be shown in the UI
        - command: The shell command that will be executed to lauch an instance for this trame app
            This command must append the $JUVIZ_ARGS environment variable to the python script, which provides
            some information for trame. See L{Configuration.generate_trame_env} for the generation of the variable.
        - working_directore: Optional, location here I{command} will be executed.

        @param path: The path to the app folder, i.e., `share/jupyter/trame/my-app/`
        @return: The parsed app information for the app
        """
        # Open Config
        config_file = path / "app.yml"
        if not config_file.exists():
            config_file = path / "app.yaml"

            if not config_file.exists():
                raise AttributeError(f"trame app at {path.resolve()!r} does not have a app.yml file")

        self.log.info(f"Found trame app config at {config_file.resolve()!r}")

        config = safe_load(config_file.read_text())
        return TrameApp(
            name=path.name,
            display_name=config["name"],
            path=str(config_file.resolve()),
            command=config["command"],
            working_directory=config.get("working_directory", None),
        )

    def generate_trame_parameters(self, app: TrameApp) -> dict:
        """
        Some of the parameters for a launched trame app is generated on the server by this configuration. The parameters
        generated by this function are directly passed to the constructor of L{TrameAppInstance}.

        By default, this function generated a UUID, the port to run on, a logger where the outputs of the app are logged
        to, and a tempfile where the authentication key is stored.

        @param app: A reference to the trame app that should be launched
        @return: A dict with the generated parameters
        """
        # UUID
        uuid = token_hex(8)

        # Port
        port = self.get_open_port()

        # Log-file
        _, log_dir = mkstemp(suffix=".log", text=True)
        logger = FileIO(log_dir, "w")

        # authKey
        auth_key = token_urlsafe(32)

        # Write authKey to tempfile
        tempfile, auth_key_file = mkstemp(text=True)
        with open(tempfile, "w") as file:
            file.write(auth_key)

        self.log.info(f"{uuid=}, {port=}, {log_dir=}, {auth_key_file=}")
        return {
            "uuid": uuid,
            "port": port,
            "log_dir": log_dir,
            "logger": logger,
            "auth_key": auth_key,
            "auth_key_file": auth_key_file,
        }

    def generate_trame_env(self, instance: TrameAppInstance) -> dict:
        """
        Generated the environment used by the trame instance. This environment contains the $JUVIZ_ARGS variable, that
        passes information, e.g., the port, to trame.

        @param instance: A reference to the trame instance that will be launched
        @return: The generated environment
        """
        env = os.environ.copy()
        env["JUVIZ_ARGS"] = (f"--port={instance.port} "
                             f"--data={instance.data_directory} "
                             f"--authKeyFile={instance.auth_key_file} "
                             f"--server")
        return env

    def route_trame(self, instance: TrameAppInstance, server_app: ServerApp) -> str:
        """
        After trame has been lauched, it must be routed to the user and made accessible by the browser. This
        implementation relies on L{jupyter_server_proxy.NamedLocalProxyHandler}, that will be registered to
        `/trame/<UUID>/` on the server.

        @param instance: The launched trame instance
        @param server_app: A reference to the server of this JupyterLab
        @return: The base_url of the trame instance that will be opened when the user click on this instance in the lab
        """
        base_url, rule = make_trame_proxy_handler(instance, server_app.base_url)
        server_app.web_app.add_handlers('.*', [rule])
        return base_url

    async def launch_trame(self, app: TrameApp, server_app, **options) -> TrameAppInstance:
        """
        Launch a new instance of the given trame app.

        @param app: The trame app that should be launched
        @param server_app: A reference to the server of JupyterLab, might be required to route trame
        @param options: The options for this instance that were entered by the user in the launch dialog. Currently, the
            name of the instance and the data directory
        @return: The launched trame instance
        """
        parameters = self.generate_trame_parameters(app)
        self.log.info(f"Starting {app.name}")

        instance = TrameAppInstance(
            **options,
            **parameters,
            process_handle=None,
            base_url="",
        )

        # env and handler
        env = self.generate_trame_env(instance)
        instance.base_url = self.route_trame(instance, server_app)

        # Create Process
        process = Popen(
            app.command, env=env, cwd=app.working_directory,
            shell=True, stdout=instance.logger, stderr=instance.logger, text=True
        )
        instance.process_handle = process

        return instance

    @abstractmethod
    async def get_running_servers(self) -> list[ParaViewServer]:
        """
        Get the list of currently running ParaView Servers that were launched by JuViz. This information can, e.g., be
        fetched from the job scheduler.

        @return: A list of all ParaView Servers
        """
        pass

    @abstractmethod
    async def launch_paraview(self, options: dict) -> tuple[int, str]:
        """
        Lauch a new ParaView Server. This server should be lauched such that L{Configuration.get_running_servers} is
        able to retrieve the information persistently, even after JupyterLab has been restarted.

        @param options: The options for this instance that were entered by the user in the ParavIEW launch dialog.
            Currently, this includes name, account, partition, nodes and timeLimit.
        @return: The status of the launch command. This include the return code and an error message. The message will
            be displayed in the UI, as an error when the return code is != 0
        """
        pass

    @abstractmethod
    async def get_user_data(self) -> UserData:
        """
        Query information about the user. This includes:
        - The name of the user
        - The available accounts and partitions available when launching a ParaView Server
        - The path to the home directory, served as the default data directory for trame apps

        @return: The retrieved information about the user
        """
        pass
