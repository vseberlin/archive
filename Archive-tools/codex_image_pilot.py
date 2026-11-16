#!/usr/bin/env python3
"""Bounded, sequential image proposals using existing Codex authentication.

Only cached work-area previews are read. No source or catalogue writes.
One fresh low-reasoning process per image; failures are harvested, never retried.
"""
import argparse
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import jsonschema

WORK = Path('/mnt/volume/iss-archive-work/visual-similarity')
PREFIX = 'openai-batch10'
CODEX = '/home/vladimir/.npm-global/bin/codex'
CHILD = None
BATCH_SIZES = {'openai-batch10': 10, 'openai-remaining101': 101}


def path(suffix):
    return WORK / f'{PREFIX}-{suffix}'


def read(p):
    return json.loads(p.read_text())


def write(p, value):
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(p)


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def remaining_candidates(candidates, completed):
    all_ids = [c['id'] for c in candidates]
    done_ids = [r['candidate_id'] for r in completed]
    assert len(all_ids) == len(set(all_ids)) == 111, 'Expected 111 distinct candidates'
    assert len(done_ids) == len(set(done_ids)) == 10, 'Expected ten distinct completed candidates'
    assert all(r['status'] == 'success' for r in completed), 'Prior batch has failures'
    assert set(done_ids) <= set(all_ids), 'Prior candidate not in current set'
    return [c for c in candidates if c['id'] not in set(done_ids)]


def validate_plan(plan):
    count = BATCH_SIZES[PREFIX]
    assert len(plan['images']) == count, f'Exactly {count} images required'
    assert len({i['id'] for i in plan['images']}) == count, 'Duplicate candidate'
    assert plan['reasoning_effort'] == 'low'
    if PREFIX == 'openai-remaining101':
        prior = read(WORK / 'openai-batch10-harvest.json')
        assert digest(WORK / 'openai-batch10-harvest.json') == plan['prior_harvest_sha256']
        expected = remaining_candidates(read(WORK / 'manifest.json')['candidates'], prior)
        assert {c['id'] for c in expected} == {i['id'] for i in plan['images']}
    for item in plan['images']:
        preview = Path(item['preview_path']).resolve()
        assert preview.is_relative_to(WORK / 'previews'), 'Not a cached preview'
        assert digest(preview) == item['preview_sha256'], 'Preview changed'
    for name, expected in plan['input_hashes'].items():
        assert digest(path(name)) == expected, f'Input changed: {name}'
    jsonschema.Draft202012Validator.check_schema(read(path('schema.json')))


def command(item):
    cmd = [CODEX, 'exec', '--ignore-user-config', '--ephemeral',
           '--skip-git-repo-check', '--sandbox', 'read-only', '-C', str(WORK),
           '--model', 'gpt-6-astra', '--json', '--image', item['preview_path'],
           '--output-schema', str(path('schema.json')),
           '--output-last-message', str(path(f"{item['number']:02d}-result.json"))]
    for setting in ['model_reasoning_effort="low"', 'web_search="disabled"',
                    'features.shell_tool=false', 'project_doc_max_bytes=0',
                    'skills.bundled.enabled=false', 'skills.include_instructions=false',
                    'skills.max_context_tokens=1',
                    'model_instructions_file=' + json.dumps(str(path('instructions.txt')))]:
        cmd += ['-c', setting]
    return cmd + ['-']


def harvest(events, result, schema, returncode):
    """Retain all reported usage even if the result fails validation."""
    usage_events = [e['usage'] for e in events if isinstance(e.get('usage'), dict)]
    usage = {k: sum(u.get(k, 0) for u in usage_events)
             for k in {k for u in usage_events for k in u}}
    if usage_events:
        usage['total_tokens'] = usage.get('input_tokens', 0) + usage.get('output_tokens', 0)
    errors = []
    if returncode != 0:
        errors.append(f'CLI exit: {returncode}')
    if not usage_events or not any(e.get('type') == 'turn.completed' for e in events):
        errors.append('No completed turn with usage')
    allowed = {'agent_message', 'reasoning'}
    unexpected = sorted({e['item'].get('type', 'unknown') for e in events
                         if 'item' in e and e['item'].get('type') not in allowed})
    if unexpected:
        errors.append('Unexpected tool/activity items: ' + ', '.join(unexpected))
    if any(e.get('type') in {'turn.failed', 'error'} for e in events):
        errors.append('CLI reported error or failed turn')
    try:
        jsonschema.validate(result, schema)
    except jsonschema.ValidationError as exc:
        errors.append('Schema: ' + exc.message[:300])
    return usage if usage_events else None, errors


def stop_child():
    global CHILD
    if CHILD is not None and CHILD.poll() is None:
        os.killpg(CHILD.pid, signal.SIGTERM)
        try:
            CHILD.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(CHILD.pid, signal.SIGKILL)
            CHILD.wait()


def interrupted(signum, frame):
    raise KeyboardInterrupt(f'Signal {signum}')


def publish(state, receipts):
    state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    state['finished_images'] = len(receipts)
    state['successful_images'] = sum(r['status'] == 'success' for r in receipts)
    state['failed_images'] = sum(r['status'] != 'success' for r in receipts)
    state['reported_usage_totals'] = {
        k: sum((r.get('usage') or {}).get(k, 0) for r in receipts)
        for k in ['input_tokens', 'cached_input_tokens', 'output_tokens',
                  'reasoning_output_tokens', 'total_tokens']}
    state['images_without_usage'] = sum(r.get('usage') is None for r in receipts)
    state['usage_scope'] = 'Full Codex calls; reasoning included in output; not API billing'
    write(path('progress.json'), state)
    write(path('harvest.json'), receipts)
    summary(path('summary.csv'), receipts)
    if PREFIX == 'openai-remaining101':
        prior = read(WORK / 'openai-batch10-harvest.json')
        combined = [dict(r, batch='openai-batch10') for r in prior]
        combined += [dict(r, batch=PREFIX) for r in receipts]
        assert len({r['candidate_id'] for r in combined}) == len(combined)
        write(WORK / 'openai-set111-harvest.json', combined)
        summary(WORK / 'openai-set111-summary.csv', combined)


def summary(destination, receipts):
    fields = ['batch', 'number', 'candidate_id', 'filename', 'status', 'title', 'date', 'places',
              'input_tokens', 'output_tokens', 'total_tokens', 'elapsed_seconds']
    tmp = destination.with_suffix('.csv.tmp')
    with tmp.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in receipts:
            result = (r.get('result') or {}) if r['status'] == 'success' else {}
            writer.writerow({
                'batch': r.get('batch', PREFIX), 'number': r['number'],
                'candidate_id': r['candidate_id'], 'filename': r['filename'], 'status': r['status'],
                'title': result.get('title', {}).get('value'),
                'date': result.get('date', {}).get('display'),
                'places': '; '.join(v['value'] for v in result.get('places', []) if v['value']),
                **{k: (r.get('usage') or {}).get(k) for k in fields[-4:-1]},
                'elapsed_seconds': r['elapsed_seconds']})
    tmp.replace(destination)


def worker():
    global CHILD
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    receipts = []
    state = {'status': 'starting', 'pid': os.getpid(), 'total_images': BATCH_SIZES[PREFIX],
             'metadata_adopted': False, 'current_image': None}
    with path('worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path('progress.json').exists():
            raise RuntimeError('Existing run; refusing restart or duplicate calls')
        try:
            plan = read(path('plan.json'))
            validate_plan(plan)
            schema = read(path('schema.json'))
            prompt = path('prompt.txt').read_text()
            state['status'] = 'running'
            publish(state, receipts)
            for item in plan['images']:
                number = item['number']
                state['current_image'] = number
                publish(state, receipts)
                start = time.monotonic()
                result = None
                errors = []
                was_interrupted = False
                cmd = command(item)
                with path(f'{number:02d}-events.jsonl').open('x') as out, \
                        path(f'{number:02d}-stderr.log').open('x') as err:
                    CHILD = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                             text=True, start_new_session=True)
                    try:
                        CHILD.communicate(prompt, timeout=240)
                    except subprocess.TimeoutExpired:
                        stop_child()
                        errors.append('240-second timeout; no automatic retry')
                    except KeyboardInterrupt:
                        stop_child()
                        errors.append('Interrupted; no automatic retry')
                        was_interrupted = True
                events = []
                for line in path(f'{number:02d}-events.jsonl').read_text().splitlines():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        errors.append('Malformed event line')
                try:
                    result = read(path(f'{number:02d}-result.json'))
                except (FileNotFoundError, json.JSONDecodeError):
                    errors.append('Missing or invalid result JSON')
                usage, validation_errors = harvest(events, result, schema, CHILD.returncode)
                errors += validation_errors
                receipt = {'number': number, 'candidate_id': item['id'],
                           'filename': item['filename'], 'status': 'failed' if errors else 'success',
                           'errors': errors, 'usage': usage, 'result': result,
                           'elapsed_seconds': round(time.monotonic() - start, 2),
                           'command': cmd, 'returncode': CHILD.returncode,
                           'thread_ids': [e['thread_id'] for e in events if e.get('thread_id')],
                           'metadata_adopted': False}
                write(path(f'{number:02d}-receipt.json'), receipt)
                receipts.append(receipt)
                publish(state, receipts)
                print(f"{number}/{state['total_images']} {item['filename']}: {receipt['status']}", flush=True)
                if was_interrupted:
                    state['status'] = 'interrupted'
                    break
                # Two consecutive failures indicate a systemic issue; do not burn the batch.
                if len(receipts) >= 2 and all(r['status'] == 'failed' for r in receipts[-2:]):
                    state['status'] = 'stopped_after_two_failures'
                    break
            else:
                state['status'] = 'completed' if not state['failed_images'] else 'completed_with_errors'
            state['current_image'] = None
        except KeyboardInterrupt as exc:
            state.update(status='interrupted', error=str(exc))
        except Exception as exc:
            state.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        finally:
            stop_child()
            publish(state, receipts)
            write(path('completion.json'), state)
    return 0 if state['status'] == 'completed' else 1


def main():
    global PREFIX
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'start', 'worker', 'status'])
    parser.add_argument('--batch', choices=list(BATCH_SIZES), default='openai-batch10')
    args = parser.parse_args()
    PREFIX = args.batch
    if args.action == 'worker':
        return worker()
    if args.action == 'status':
        print(json.dumps(read(path('progress.json')), indent=2))
        return 0
    validate_plan(read(path('plan.json')))
    if args.action == 'check':
        print(f'Validated {BATCH_SIZES[PREFIX]} distinct previews, input hashes and schema; no model call.')
        return 0
    # Exclusive launch receipt prevents duplicate launches even before worker acquires lock.
    with path('launch.json').open('x') as launch, path('worker.log').open('x') as log:
        proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                                 'worker', '--batch', PREFIX],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                cwd=WORK, start_new_session=True)
        receipt = {'status': 'launched_not_completed', 'pid': proc.pid,
                   'script': str(Path(__file__).resolve()), 'script_sha256': digest(Path(__file__))}
        json.dump(receipt, launch, indent=2)
        print(json.dumps(receipt))
    return 0


if __name__ == '__main__':
    sys.exit(main())
