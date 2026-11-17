"""Installed command and shared pipeline behavior, using invented sources only."""
import ast
from dataclasses import asdict
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_research_evidence as fixtures
from archive_research import pipeline


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.config = f.base / 'roots.toml'
        self.config.write_text(f'[roots.source]\npath={json.dumps(str(f.input))}\naccess="read"\n'
                               f'[roots.artifacts]\npath={json.dumps(str(f.output))}\naccess="work"\n')
        self.selection = {'keys': [f.key], 'visual_queries': [], 'text_queries': ['invented person'], 'limit': 5}

    def test_local_workflow_produces_bound_outputs_without_submission(self):
        f = self.fixture
        result = pipeline.run(self.config, self.selection, f.selection, 'trial', model='synthetic-model')
        self.assertFalse(result['external_submission'])
        self.assertEqual(result['indexed'], 1)
        packet = pipeline.read_json(f.output / result['outputs']['packet'])
        self.assertFalse(packet['metadata_adopted'])
        search = pipeline.read_json(f.output / result['outputs']['search'])
        self.assertEqual(search['text'][0]['hits'][0]['target'], f.key)
        self.assertFalse(pipeline.read_json(f.output / result['outputs']['transport'])['external_submission_authorized'])
        with self.assertRaisesRegex(ValueError, 'output exists'):
            pipeline.run(self.config, self.selection, f.selection, 'trial')

    def test_changed_index_is_rejected_without_overwriting_output(self):
        f = self.fixture
        pipeline.index(self.config, self.selection, 'index.json')
        (f.output / (f.key + '.txt')).write_text('Changed source artifact')
        with self.assertRaisesRegex(ValueError, 'changed'):
            pipeline.search(self.config, self.selection, 'index.json', 'search.json')
        self.assertFalse((f.output / 'search.json').exists())

    def test_installed_worker_extracts_from_an_unrelated_working_directory(self):
        from archive_research import Source
        from archive_research.preparation import inspect_page
        from archive_research.web.fixtures import pdf_bytes
        f = self.fixture
        pdf = f.input / 'page.pdf'
        pdf.write_bytes(pdf_bytes('Installed command', 1, '1', 'Synthetic text'))
        stat = pdf.stat()
        source = Source('synthetic', 'installed-command', 'original', 'source', pdf.name, stat.st_size, stat.st_mtime_ns)
        page = inspect_page(f.roots, source, 1, 'pdf', 1)
        plan = f.base / 'plan.json'
        plan.write_text(json.dumps({'format': 'archive-research-plan-1', 'metadata_adopted': False,
                                    'tasks': [{'id': 'one-page', 'page': asdict(page), 'extractor': 'pdf-text'}]}))
        env = dict(os.environ); env.pop('PYTHONPATH', None)
        process = subprocess.run([sys.executable, '-I', '-B', '-m', 'archive_research', 'extract',
                                  '--roots', str(self.config), '--plan', str(plan)],
                                 cwd=f.base, env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['attempts'][0]['state'], 'complete')
        self.assertIn('Installed', (f.output / (result['attempts'][0]['key'] + '.bbox.xhtml')).read_text())

    def test_old_imports_share_the_canonical_module_objects(self):
        for old, new in [('ocr', 'extractors.ocr'), ('layout', 'extractors.layout'),
                         ('assessment_io', 'assessment.io'), ('assessment_codex', 'assessment.codex')]:
            self.assertIs(importlib.import_module('archive_research.' + old),
                          importlib.import_module('archive_research.' + new))

    def test_active_package_has_no_imports_from_legacy_tools(self):
        import archive_research
        for path in Path(archive_research.__file__).parent.rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and not node.level:
                    self.assertNotIn((node.module or '').split('.')[0],
                                     {'archive_common', 'research_routes', 'research_web', 'sift_matching', 'matching'}, str(path))


if __name__ == '__main__':
    unittest.main()
