from jirafs.plugin import CommandPlugin
from jirafs.utils import run_command_method_with_kwargs


class Command(CommandPlugin):
    """ Fetch and merge remote changes """
    RUN_FOR_SUBTASKS = True
    TRY_SUBFOLDERS = True
    MIN_VERSION = '1.15'
    MAX_VERSION = '1.99.99'

    def main(self, folder, **kwargs):
        fetch_result = run_command_method_with_kwargs('fetch', folder=folder)
        merge_result = run_command_method_with_kwargs('merge', folder=folder)

        return fetch_result, merge_result
