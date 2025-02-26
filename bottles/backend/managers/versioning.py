# versioning.py
#
# Copyright 2022 brombinmirko <send@mirko.pm>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, in version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import os
from bottles.backend.utils import yaml
import uuid
import shutil
from glob import glob
from typing import NewType
from datetime import datetime
from gettext import gettext as _
from gi.repository import GLib
from fvs.repo import FVSRepo
from fvs.exceptions import FVSNothingToCommit, FVSEmptyCommitMessage, FVSStateNotFound, FVSNothingToRestore, FVSStateZeroNotDeletable

try:
    from bottles.frontend.operation import OperationManager
except (RuntimeError, GLib.GError):
    from bottles.frontend.cli.operation_cli import OperationManager

from bottles.backend.utils.file import FileUtils
from bottles.backend.models.result import Result
from bottles.backend.utils.manager import ManagerUtils
from bottles.backend.logger import Logger

logging = Logger()


# noinspection PyTypeChecker
class VersioningManager:
    def __init__(self, window, manager):
        self.window = window
        self.manager = manager
        self.__operation_manager = OperationManager(self.window)
    
    @staticmethod
    def __get_patterns(config: dict):
        patterns = [
            "*dosdevices*",
            "*cache*"
        ]
        if config["Parameters"]["versioning_exclusion_patterns"]:
            patterns += config["Versioning_Exclusion_Patterns"]
        return patterns
    
    @staticmethod
    def is_initialized(config: dict):
        try:
            repo = FVSRepo(
                repo_path=ManagerUtils.get_bottle_path(config),
                use_compression=config["Parameters"]["versioning_compression"],
                no_init=True
            )
        except FileNotFoundError:
            return False
        return not repo.has_no_states
    
    @staticmethod
    def re_initialize(config: dict):
        fvs_path = os.path.join(ManagerUtils.get_bottle_path(config), ".fvs")
        if os.path.exists(fvs_path):
            shutil.rmtree(fvs_path)
    
    def update_system(self, config: dict):
        states_path = os.path.join(ManagerUtils.get_bottle_path(config), "states")
        if os.path.exists(states_path):
            shutil.rmtree(states_path)
        return self.manager.update_config(config, "Versioning", False)

    def create_state(self, config: dict, message: str = "No message"):
        task_id = str(uuid.uuid4())
        patterns = self.__get_patterns(config)
        repo = FVSRepo(
            repo_path=ManagerUtils.get_bottle_path(config),
            use_compression=config["Parameters"]["versioning_compression"]
        )
        GLib.idle_add(
            self.__operation_manager.new_task,
            task_id,
            _("Committing state …"),
            False
        )
        try:
            repo.commit(message, ignore=patterns)
        except FVSNothingToCommit:
            GLib.idle_add(self.__operation_manager.remove_task, task_id)
            return Result(
                status=False,
                message=_("Nothing to commit")
            )

        GLib.idle_add(self.__operation_manager.remove_task, task_id)
        return Result(
            status=True,
            message=_("New state [{0}] created successfully!").format(repo.active_state_id),
            data={
                "state_id": repo.active_state_id,
                "states": repo.states
            }
        )
    

    def list_states(self, config: dict) -> Result:
        """
        This function take all the states from the states.yml file
        of the given bottle and return them as a dict.
        """
        if not config.get("Versioning"):
            try:
                repo = FVSRepo(
                    repo_path=ManagerUtils.get_bottle_path(config),
                    use_compression=config["Parameters"]["versioning_compression"]
                )
            except FVSStateNotFound:
                logging.warning("The FVS repository may be corrupted, trying to re-initialize it")
                self.re_initialize(config)
                repo = FVSRepo(
                    repo_path=ManagerUtils.get_bottle_path(config),
                    use_compression=config["Parameters"]["versioning_compression"]
                )
            return Result(
                status=True,
                message=_("States list retrieved successfully!"),
                data={
                    "state_id": repo.active_state_id,
                    "states": repo.states
                }
            )

        bottle_path = ManagerUtils.get_bottle_path(config)
        states = {}

        try:
            states_file = open('%s/states/states.yml' % bottle_path)
            states_file_yaml = yaml.load(states_file)
            states_file.close()
            states = states_file_yaml.get("States")
            logging.info(f"Found [{len(states)}] states for bottle: [{config['Name']}]")
        except (FileNotFoundError, yaml.YAMLError):
            logging.info(f"No states found for bottle: [{config['Name']}]")

        return states

    def set_state(self, config: dict, state_id: int, after=False) -> Result:
        if not config.get("Versioning"):
            task_id = str(uuid.uuid4())
            patterns = self.__get_patterns(config)
            repo = FVSRepo(
                repo_path=ManagerUtils.get_bottle_path(config),
                use_compression=config["Parameters"]["versioning_compression"]
            )
            res = Result(
                status=True,
                message=_("State {0} restored successfully!").format(state_id)
            )
            GLib.idle_add(
                self.__operation_manager.new_task,
                task_id,
                _("Restoring state {} …".format(state_id)),
                False
            )
            try:
                repo.restore_state(state_id, ignore=patterns)
            except FVSStateNotFound:
                logging.error(f"State {state_id} not found.")
                res = Result(
                    status=False,
                    message=_("State not found")
                )
            except (FVSNothingToRestore, FVSStateZeroNotDeletable):
                logging.error(f"State {state_id} is the active state.")
                res = Result(
                    status=False,
                    message=_("State {} is already the active state").format(state_id)
                )
            GLib.idle_add(self.__operation_manager.remove_task, task_id)
            return res

        bottle_path = ManagerUtils.get_bottle_path(config)
        logging.info(f"Restoring to state: [{state_id}]")

        # get bottle and state indexes
        bottle_index = self.get_index(config)
        state_index = self.get_state_files(config, state_id)

        search_sources = list(range(int(state_id) + 1))
        search_sources.reverse()

        # check for removed and changed files
        remove_files = []
        edit_files = []
        for file in bottle_index.get("Files"):
            if file["file"] not in [file["file"] for file in state_index.get("Files")]:
                remove_files.append(file)
            elif file["checksum"] not in [file["checksum"] for file in state_index.get("Files")]:
                edit_files.append(file)
        logging.info(f"[{len(remove_files)}] files to remove.")
        logging.info(f"[{len(edit_files)}] files to replace.")

        # check for new files
        add_files = []
        for file in state_index.get("Files"):
            if file["file"] not in [file["file"] for file in bottle_index.get("Files")]:
                add_files.append(file)
        logging.info(f"[{len(add_files)}] files to add.")

        # perform file updates
        for file in remove_files:
            os.remove("%s/drive_c/%s" % (bottle_path, file["file"]))

        for file in add_files:
            for i in search_sources:
                source = "%s/states/%s/drive_c/%s" % (bottle_path, str(state_id), file["file"])
                target = "%s/drive_c/%s" % (bottle_path, file["file"])
                shutil.copy2(source, target)

        for file in edit_files:
            for i in search_sources:
                source = "%s/states/%s/drive_c/%s" % (
                    bottle_path, str(i), file["file"])
                if os.path.isfile(source):
                    checksum = FileUtils().get_checksum(source)
                    if file["checksum"] == checksum:
                        break
                target = "%s/drive_c/%s" % (bottle_path, file["file"])
                shutil.copy2(source, target)

        # update State in bottle config
        self.manager.update_config(config, "State", state_id)

        # update states
        GLib.idle_add(
            self.window.page_details.view_versioning.update,
            False, config
        )

        # update bottles
        self.manager.update_bottles()

        # execute caller function after all
        if after:
            GLib.idle_add(after)

        return True

    @staticmethod
    def get_state_files(config: dict, state_id: int, plain: bool = False) -> dict:
        """
        Return the files.yml content of the state. Use the plain argument
        to return the content as plain text.
        """
        try:
            file = open('%s/states/%s/files.yml' % (ManagerUtils.get_bottle_path(config), state_id))
            files = file.read() if plain else yaml.load(file.read())
            file.close()
            return files
        except (OSError, IOError, yaml.YAMLError):
            logging.error(f"Could not read the state files file.")
            return {}

    @staticmethod
    def get_index(config: dict):
        """List all files in a bottle and return as dict."""
        bottle_path = ManagerUtils.get_bottle_path(config)
        cur_index = {
            "Update_Date": str(datetime.now()),
            "Files": []
        }
        for file in glob("%s/drive_c/**" % bottle_path, recursive=True):
            if not os.path.isfile(file):
                continue

            if os.path.islink(os.path.dirname(file)):
                continue

            if file[len(bottle_path) + 9:].split("/")[0] in ["users"]:
                continue

            cur_index["Files"].append({
                "file": file[len(bottle_path) + 9:],
                "checksum": FileUtils().get_checksum(file)
            })
        return cur_index
