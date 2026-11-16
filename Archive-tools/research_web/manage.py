#!/usr/bin/env python3
"""Run only with explicit project and machine configuration."""
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'research_web.settings')
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'runserver':
        if sys.argv[2:] != ['127.0.0.1:8785', '--noreload']:
            raise SystemExit('Pilot launch requires runserver 127.0.0.1:8785 --noreload')
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
