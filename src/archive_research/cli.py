"""Installed entry point; processing is delegated to application operations."""
import argparse
import json
from pathlib import Path
import sys


def legacy_extract(argv):
    from .pipeline import extract
    p = argparse.ArgumentParser(description='Compatibility for the original extraction command')
    p.add_argument('--roots', type=Path, required=True)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--work', default='artifacts')
    p.add_argument('--expected-plan')
    sub = p.add_subparsers(dest='command', required=True)
    one = sub.add_parser('extract'); one.add_argument('--index', type=int, required=True)
    run = sub.add_parser('run'); run.add_argument('--limit', type=int, default=0); run.add_argument('--summary', default='run-summary.json')
    a = p.parse_args(argv)
    result = extract(a.roots, a.plan, work=a.work, index=getattr(a, 'index', None),
                     limit=getattr(a, 'limit', 0), summary=getattr(a, 'summary', 'run-summary.json'),
                     expected_plan=a.expected_plan)
    print(json.dumps(result))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('--roots', '--plan', '--work'):
        return legacy_extract(argv)
    if argv and argv[0] == 'reference':
        from .reference import main as reference
        return reference(argv[1:])
    if argv and argv[0] == 'reference-diagnostics':
        from .reference_diagnostics import main as diagnostics
        return diagnostics(argv[1:])
    if argv and argv[0] == 'assess':
        from .assessment.cli import main as assess
        return assess(argv[1:])
    if argv and argv[0] == 'web':
        from .web.manage import main as web
        return web(argv[1:])
    p = argparse.ArgumentParser(prog='archive-research', description='Local archive extraction, cross-references and evidence assessment')
    sub = p.add_subparsers(dest='command', required=True)
    def processing(name, help):
        command = sub.add_parser(name, help=help)
        command.add_argument('--roots', type=Path, help='Explicit extraction roots TOML')
        command.add_argument('--project', type=Path, help='Existing project TOML; supplies extraction_roots when --roots is omitted')
        command.add_argument('--machine', type=Path, help='Existing private machine TOML')
        return command
    e = processing('extract', 'Run selected extraction tasks with resumable receipts')
    e.add_argument('--plan', type=Path, required=True); e.add_argument('--work', default='artifacts')
    e.add_argument('--summary', default='run-summary.json'); e.add_argument('--limit', type=int, default=0)
    e.add_argument('--index', type=int, help='One task position'); e.add_argument('--expected-plan', help=argparse.SUPPRESS)
    i = processing('index', 'Build an index over selected existing receipts')
    i.add_argument('--selection', type=Path, required=True); i.add_argument('--output', required=True)
    for name in ('search', 'retrieve'):
        s = processing(name, 'Search a bound index' if name == 'search' else 'Compatibility: build an index and search it')
        s.add_argument('--selection', type=Path, required=True); s.add_argument('--output', required=True)
        s.add_argument('--index' if name == 'search' else '--index-output', required=True)
        s.add_argument('--sift', action='store_true')
    packet = processing('packet', 'Assemble selected source-bound evidence')
    packet.add_argument('--selection', type=Path, required=True); packet.add_argument('--output', required=True)
    packet.add_argument('--work', default='artifacts'); packet.add_argument('--prompt-output')
    r = processing('run', 'Run extraction, index, search and packet preparation together; never submit externally')
    r.add_argument('--plan', type=Path, help='Omit to reuse existing extraction receipts')
    r.add_argument('--retrieval-selection', type=Path, required=True)
    r.add_argument('--packet-selection', type=Path, required=True)
    r.add_argument('--output-prefix', required=True); r.add_argument('--sift', action='store_true')
    r.add_argument('--model', help='Optionally prepare a local request; never send it')
    sub.add_parser('reference', help='Index and evaluate catalogue previews with a bound local model')
    sub.add_parser('reference-diagnostics', help='Baselines, strata, holdout sweep and curator review for a reference index; no model or images')
    sub.add_parser('assess', help='Prepare, validate or explicitly submit an assessment')
    sub.add_parser('web', help='Configure, check, test or serve the local Django application')
    a = p.parse_args(argv)
    from .config import Configuration
    from . import pipeline
    if a.roots is None:
        config = Configuration.load(a.project, a.machine)
        a.roots = Path(config.project['extraction_roots'])
    if a.command == 'extract':
        result = pipeline.extract(a.roots, a.plan, work=a.work, summary=a.summary,
                                  limit=a.limit, index=a.index, expected_plan=a.expected_plan)
    elif a.command == 'index':
        result = pipeline.index(a.roots, pipeline.read_json(a.selection), a.output)
        result = {'indexed': len(result['entries']), 'unavailable': len(result['unavailable']), 'output': a.output}
    elif a.command in ('search', 'retrieve'):
        selection = pipeline.read_json(a.selection)
        if a.command == 'retrieve':
            from .contracts import require
            require(a.index_output != a.output, 'Output filenames must differ')
            pipeline.index(a.roots, selection, a.index_output)
        result = pipeline.search(a.roots, selection, a.index if a.command == 'search' else a.index_output, a.output, sift=a.sift)
        result = {'visual_queries': len(result['visual']), 'text_queries': len(result['text']), 'output': a.output}
    elif a.command == 'packet':
        result = pipeline.packet(a.roots, pipeline.read_json(a.selection), a.output, prompt_output=a.prompt_output, work=a.work)
        result = {'evidence': len(result['evidence']), 'available': sum(e['available'] for e in result['evidence']),
                  'output': a.output, 'external_submission_authorized': False}
    else:
        result = pipeline.run(a.roots, pipeline.read_json(a.retrieval_selection), pipeline.read_json(a.packet_selection),
                              a.output_prefix, plan_file=a.plan, sift=a.sift, model=a.model)
    print(json.dumps(result))
