import io

import six

from jirafs import constants, exceptions, utils
from jirafs.plugin import CommandPlugin
from jirafs.utils import run_command_method_with_kwargs


class Command(CommandPlugin):
    """ Push locally-committed changes to JIRA """
    TRY_SUBFOLDERS = True
    MIN_VERSION = '1.15'
    MAX_VERSION = '1.99.99'

    def get_valid_issue_link_types(self, folder):
        if not hasattr(self, '_valid_issue_link_types'):
            data = {}
            for item in folder.jira.issue_link_types():
                data[item.outward.lower()] = ('outward', item)
                data[item.inward.lower()] = ('inward', item)

            self._valid_issue_link_types = data
        return self._valid_issue_link_types

    def validate_issue(self, folder):
        status = folder.status()

        # Validate issue statuses
        valid_types = self.get_valid_issue_link_types(folder)
        links = status['ready']['links'].get('issue', {})
        for target, data in links.items():
            if data[1] is None:
                continue
            if data[1].get('status') not in valid_types:
                raise exceptions.IssueValidationError(
                    u"{status} is not a valid issue link type for {target}; "
                    u"options include the following: {options}".format(
                        status=data[1].get('status'),
                        target=target,
                        options=', '.join(valid_types.keys())
                    )
                )

    def main(self, folder, **kwargs):
        self.validate_issue(folder)
        with utils.stash_local_changes(folder):
            status = folder.status()

            if not folder.is_up_to_date():
                raise exceptions.LocalCopyOutOfDate()

            file_meta = folder.get_remote_file_metadata(shadow=False)

            for filename in status['ready']['files']:
                upload = six.BytesIO(
                    folder.get_local_file_at_revision(
                        filename,
                        'HEAD',
                        binary=True
                    )
                )
                filename, upload = folder.execute_plugin_method_series(
                    'alter_file_upload',
                    args=((filename, upload, ), ),
                    single_response=True,
                )
                folder.log(
                    'Uploading file "%s"',
                    (filename, ),
                )
                # Delete the existing issue if there is one
                for attachment in folder.issue.fields.attachment:
                    if attachment.filename == filename:
                        attachment.delete()
                upload.seek(0)
                attachment = folder.jira.add_attachment(
                    folder.ticket_number,
                    upload,
                    filename=filename,
                )
                file_meta[filename] = attachment.created

            folder.set_remote_file_metadata(file_meta, shadow=False)

            comment = folder.get_new_comment(clear=True, ready=True)
            if comment:
                folder.log(
                    u'Adding comment "{comment}"'.format(
                        comment=self.truncate_field_value(comment)
                    )
                )
                folder.jira.add_comment(folder.ticket_number, comment)

            collected_updates = {}
            for field, diff_values in status['ready']['fields'].items():
                collected_updates[field] = diff_values[1]

            if collected_updates:
                folder.log(
                    'Updating fields "%s"',
                    (collected_updates, )
                )
                folder.issue.update(**collected_updates)

            links = status['ready']['links']
            statuses = self.get_valid_issue_link_types(folder)
            for target, data in links.get('issue', {}).items():
                orig = data[0]
                new = data[1]
                other = folder.jira.issue(target)
                if orig is None:
                    # New links
                    status_data = statuses[new['status']]
                    args = [
                        status_data[1].name,
                    ]
                    if status_data[0] == 'inward':
                        args.extend([
                            other,
                            folder.issue
                        ])
                    elif status_data[0] == 'outward':
                        args.extend([
                            folder.issue,
                            other,
                        ])
                    folder.jira.create_issue_link(*args)
                elif new is None:
                    # Deleted links
                    for existing_link in folder.issue.fields.issuelinks:
                        if (
                            hasattr(existing_link, 'inwardIssue') and
                            existing_link.inwardIssue.key == target
                        ):
                            existing_link.delete()
                        if (
                            hasattr(existing_link, 'outwardIssue') and
                            existing_link.outwardIssue.key == target
                        ):
                            existing_link.delete()
                else:
                    # Changed links
                    for existing_link in folder.issue.fields.issuelinks:
                        if (
                            hasattr(existing_link, 'inwardIssue') and
                            existing_link.inwardIssue.key == target
                        ):
                            existing_link.type = statuses[new['status']][1]
                            existing_link.update()
                        if (
                            hasattr(existing_link, 'outwardIssue') and
                            existing_link.outwardIssue.key == target
                        ):
                            existing_link.type = statuses[new['status']][1]
                            existing_link.update()

            links = status['ready']['links']
            remote_links = folder.jira.remote_links(folder.issue)
            # Workaround for bug in python-jira:
            folder.jira._applicationlinks = []
            for target, data in links.get('remote', {}).items():
                orig = data[0]
                new = data[1]
                if orig is None:
                    # New links
                    link_object = {
                        'url': target,
                        'title': new['description'],
                    }
                    folder.jira.add_remote_link(folder.issue, link_object)
                elif new is None:
                    # Deleted links
                    for existing_link in remote_links:
                        if existing_link.object.url == target:
                            existing_link.delete()
                else:
                    # Changed links
                    for existing_link in remote_links:
                        if existing_link.object.url == target:
                            existing_link.update({
                                'url': target,
                                'title': new['description']
                            })

            # Commit local copy
            folder.run_git_command('reset', '--soft', failure_ok=True)
            folder.run_git_command(
                'add',
                folder.get_path('.jirafs/remote_files.json'),
                failure_ok=True
            )
            folder.run_git_command(
                'add',
                folder.get_path(constants.TICKET_NEW_COMMENT),
                failure_ok=True
            )
            folder.run_git_command(
                'commit', '-m', 'Pushed local changes', failure_ok=True
            )

            # Apply macros and record the changes so we can reverse
            # them a little later.
            macro_patch_filename = folder.get_path(
                '.jirafs/macros_applied.patch'
            )
            folder.run_git_command(
                'apply',
                macro_patch_filename,
                failure_ok=True
            )
            fields = folder.get_fields()
            for field, value in collected_updates.items():
                fields[field] = value
            fields.write()
            result = folder.run_git_command('diff')
            if result.strip():
                with io.open(macro_patch_filename, 'w', encoding='utf-8') as o:
                    o.write(result)
                    o.write(u'\n\n')
                folder.run_git_command(
                    'add',
                    macro_patch_filename,
                    failure_ok=True
                )
                folder.run_git_command(
                    'commit', '-m', 'Updating macro patch', failure_ok=True
                )
                # Re-set the applied changes we just made above
                folder.run_git_command(
                    'checkout', '--', '.'
                )
            else:
                with io.open(macro_patch_filename, 'w', encoding='utf-8') as o:
                    o.write(u'\n\n')

            # Commit changes to remote copy, too, so we record remote
            # file metadata.
            folder.run_git_command('fetch', shadow=True)
            folder.run_git_command('merge', 'origin/master', shadow=True)
            folder.run_git_command('add', '-A', shadow=True)
            folder.run_git_command(
                'commit', '-m', 'Pulled remote changes',
                failure_ok=True, shadow=True
            )
            folder.run_git_command('push', 'origin', 'jira', shadow=True)
            pull_result = run_command_method_with_kwargs('pull', folder=folder)
            return pull_result[1]
