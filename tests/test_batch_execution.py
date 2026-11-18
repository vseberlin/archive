"""Bounded subprocess progress, timeout and selective resume on invented PDFs."""
from dataclasses import asdict
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_positioned_extraction as fixtures
from archive_research import pipeline
from archive_research.preparation import inspect_page
from archive_research.web.fixtures import pdf_bytes


class BatchExecutionTests(unittest.TestCase):
    setUp = fixtures.PositionedTests.setUp
    bind = fixtures.PositionedTests.bind
    pdf = fixtures.PositionedTests.pdf

    def plan(self):
        first = self.pdf()
        second_file = self.input / 'second.pdf'
        second_file.write_bytes(pdf_bytes('SECOND PAGE', 1, '2', 'Different text'))
        second = inspect_page(self.roots, self.bind(second_file), 2, 'pdf', 1)
        config = self.base / 'roots.toml'
        config.write_text(f'[roots.source]\npath={json.dumps(str(self.input))}\naccess="read"\n'
                          f'[roots.artifacts]\npath={json.dumps(str(self.output))}\naccess="work"\n')
        plan = self.base / 'plan.json'
        plan.write_text(json.dumps({'format': 'archive-research-plan-1', 'metadata_adopted': False,
                                    'tasks': [{'id': str(i), 'page': asdict(page), 'extractor': 'pdf-text',
                                               'settings': {'dpi': 100}} for i, page in enumerate((first, second))]}))
        return config, plan

    def test_bounded_chunk_progress_and_resume(self):
        config, plan = self.plan()
        first = pipeline.extract(config, plan, chunk_size=2, worker_timeout=30)
        self.assertEqual([a['state'] for a in first['attempts']], ['complete', 'complete'], first)
        second = pipeline.extract(config, plan, chunk_size=2, worker_timeout=30)
        self.assertTrue(all(a['reused'] for a in second['attempts']), second)
        progress = list(self.output.glob('worker-*.json'))
        self.assertEqual(len(progress), 1)
        self.assertEqual(len(json.loads(progress[0].read_text())['attempts']), 2)

    def test_timeout_keeps_finished_prefix_and_retry_uses_its_receipt(self):
        config, plan = self.plan()
        first = pipeline.extract(config, plan, limit=1, worker_timeout=30)['attempts'][0]
        real_popen = subprocess.Popen
        def hung_worker(command, **kwargs):
            # The first item is already complete. Simulate a native hang on item
            # two after the worker durably reported that finished prefix.
            progress = self.output / command[command.index('--progress')+1]
            digest = command[command.index('--expected-plan')+1]
            body = json.dumps({'plan_sha256': digest, 'worker_start': 0, 'attempts': [first]})
            script = 'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(sys.argv[2]); time.sleep(60)'
            return real_popen([sys.executable, '-B', '-c', script, str(progress), body], **kwargs)
        with patch('archive_research.pipeline.subprocess.Popen', side_effect=hung_worker):
            interrupted = pipeline.extract(config, plan, chunk_size=2, worker_timeout=1)
        self.assertEqual([a['state'] for a in interrupted['attempts']], ['complete', 'worker_timeout'])
        resumed = pipeline.extract(config, plan, chunk_size=2, worker_timeout=30)
        self.assertTrue(resumed['attempts'][0]['reused'])
        self.assertEqual(resumed['attempts'][1]['state'], 'complete')

    def test_invalid_worker_bounds_fail_before_launch(self):
        config, plan = self.plan()
        for size, timeout in ((0, 30), (17, 30), (1, 0)):
            with self.subTest(size=size, timeout=timeout), self.assertRaisesRegex(ValueError, 'worker limits'):
                pipeline.extract(config, plan, chunk_size=size, worker_timeout=timeout)


if __name__ == '__main__':
    unittest.main()
