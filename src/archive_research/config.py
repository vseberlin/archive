"""Explicit application configuration, shared by installed CLI and Django."""
from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

from .common import plain_file, require, work_root


@dataclass(frozen=True)
class Configuration:
    project_file: Path
    machine_file: Path
    project: dict
    machine: dict
    runtime_root: Path

    @classmethod
    def load(cls, project=None, machine=None):
        project = project or os.environ.get('ARCHIVE_RESEARCH_PROJECT')
        machine = machine or os.environ.get('ARCHIVE_RESEARCH_MACHINE')
        require(project and machine, 'Select explicit project and machine TOML files')
        project_file, machine_file = Path(project).absolute(), Path(machine).absolute()
        plain_file(machine_file)
        require(machine_file.stat().st_mode & 0o077 == 0, 'Machine configuration must be private')
        p = tomllib.loads(plain_file(project_file).read_text())
        m = tomllib.loads(machine_file.read_text())
        require(p.get('mode') in ('synthetic', 'local_review'), 'Explicit synthetic or local review mode required')
        root = work_root(m['runtime_root'])
        require(project_file.parent == root and project_file.resolve() == root / 'project.toml'
                and machine_file.resolve() == root / 'machine.toml', 'Configuration outside runtime')
        require(root.stat().st_mode & 0o077 == 0, 'Runtime directory must be private')
        for name in ('records', 'sample', 'cache', 'exports'):
            require((root / name).is_dir() and not (root / name).is_symlink(), 'Missing or redirected pilot directory')
        require(len(m['secret_key']) >= 50, 'A private random secret is required')
        return cls(project_file, machine_file, p, m, root)

    def activate(self):
        os.environ['ARCHIVE_RESEARCH_PROJECT'] = str(self.project_file)
        os.environ['ARCHIVE_RESEARCH_MACHINE'] = str(self.machine_file)


def extraction_roots(explicit=None, project=None, machine=None):
    """Explicit --roots wins; otherwise use the selected project's existing binding."""
    from .sources import Roots
    if explicit:
        return Roots.from_toml(Path(explicit))
    config = Configuration.load(project, machine)
    require(config.project.get('extraction_roots'), 'Project has no extraction_roots binding')
    return Roots.from_toml(Path(config.project['extraction_roots']))
