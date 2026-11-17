"""Installed Django entry point with explicit configuration and a fixed loopback server."""
import argparse
import os
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog='archive-research web', description=__doc__)
    p.add_argument('--project', help='Existing project.toml')
    p.add_argument('--machine', help='Existing private machine.toml')
    p.add_argument('action', choices=['serve', 'check', 'test', 'manage', 'configure',
                                    'init', 'rebuild', 'export', 'storage-check', 'ocr', 'package', 'selection'])
    args, rest = p.parse_known_args(argv)
    if args.action == 'configure':
        from .configure import main as configure
        return configure(rest)
    from archive_research.config import Configuration
    from archive_research.common import require
    Configuration.load(args.project, args.machine).activate()
    os.environ['DJANGO_SETTINGS_MODULE'] = 'archive_research.web.settings'
    if args.action in ('init', 'rebuild', 'export', 'storage-check', 'ocr', 'package'):
        from .bootstrap import main as bootstrap
        return bootstrap([args.action, *rest])
    if args.action == 'selection':
        from .load_selection import main as selection
        return selection(rest)
    command = (['runserver', '127.0.0.1:8785', '--noreload'] if args.action == 'serve'
               else rest if args.action == 'manage' else [args.action, *rest])
    if args.action == 'serve':
        require(not rest, 'Serve uses the fixed manual loopback address and no reloader')
    require(command, 'Select a Django management command')
    if command[0] == 'runserver':
        require(command[1:] == ['127.0.0.1:8785', '--noreload'],
                'Pilot launch requires runserver 127.0.0.1:8785 --noreload')
    if command[0] == 'test' and (len(command) == 1 or command[1].startswith('-')):
        command.insert(1, 'archive_research.web')
    from django.core.management import execute_from_command_line
    execute_from_command_line(['archive-research web', *command])


if __name__ == '__main__':
    main()
