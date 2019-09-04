from pcs.common import file_type_codes
from pcs.lib import reports as common_reports
from pcs.lib.env_file import export_ghost_file
from pcs.lib.file.instance import FileInstance
from pcs.lib.errors import LibraryError


class BoothEnv():
    def __init__(self, instance_name, booth_files_data):
        """
        Create a new BoothEnv

        string instance_name -- booth instance name
        dict booth_files_data -- ghost files (config_data, key_data, key_path)
        """
        if (
            "config_data" in booth_files_data
            and
            "key_data" not in booth_files_data
        ):
            raise LibraryError(
                common_reports.live_environment_not_consistent(
                    [file_type_codes.BOOTH_CONFIG],
                    [file_type_codes.BOOTH_KEY],
                )
            )
        if (
            "config_data" not in booth_files_data
            and
            "key_data" in booth_files_data
        ):
            raise LibraryError(
                common_reports.live_environment_not_consistent(
                    [file_type_codes.BOOTH_KEY],
                    [file_type_codes.BOOTH_CONFIG],
                )
            )

        self._instance_name = instance_name
        self._config_file = FileInstance.for_booth_config(
            f"{instance_name}.conf",
            **self._init_file_data(booth_files_data, "config_data")
        )
        self._key_file = FileInstance.for_booth_key(
            f"{instance_name}.key",
            **self._init_file_data(booth_files_data, "key_data")
        )
        if self._key_file.raw_file.is_ghost:
            self._key_path = booth_files_data.get("key_path", "")
        else:
            self._key_path = self._key_file.raw_file.file_type.path

    @staticmethod
    def _init_file_data(booth_files_data, file_key):
        # ghost file not specified
        if not file_key in booth_files_data:
            return dict(
                ghost_file=False,
                ghost_data=None,
            )
        return dict(
            ghost_file=True,
            ghost_data=booth_files_data[file_key],
        )

    @property
    def instance_name(self):
        return self._instance_name

    @property
    def config(self):
        return self._config_file

    @property
    def config_path(self):
        if self._config_file.raw_file.is_ghost:
            raise AssertionError(
                "Reading config path is supported only in live environment"
            )
        return self._config_file.raw_file.file_type.path

    @property
    def key(self):
        return self._key_file

    @property
    def key_path(self):
        return self._key_path

    @property
    def ghost_file_codes(self):
        codes = []
        if self._config_file.raw_file.is_ghost:
            codes.append(self._config_file.raw_file.file_type.file_type_code)
        if self._key_file.raw_file.is_ghost:
            codes.append(self._key_file.raw_file.file_type.file_type_code)
        return codes

    def create_facade(self, site_list, arbitrator_list):
        return self._config_file.toolbox.facade.create(
            site_list,
            arbitrator_list
        )

    def export(self):
        if not self._config_file.raw_file.is_ghost:
            return {}
        return {
            "config_file": export_ghost_file(self._config_file.raw_file),
            "key_file": export_ghost_file(self._key_file.raw_file),
        }
