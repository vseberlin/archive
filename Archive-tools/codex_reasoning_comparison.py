#!/usr/bin/env python3
"""Three xhigh calls against frozen batch10 low baselines; no batch mutations."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import codex_image_pilot as pilot

ROOT = pilot.WORK
PREFIX = 'openai-compare3'


def output(suffix):
    return ROOT / f'{PREFIX}-{suffix}'


def main():
    # An existing marker prevents a rerun even if a previous process was interrupted.
    with output('started.json').open('x') as f:
        json.dump({'pid': os.getpid(), 'started_at': time.time()}, f)
    plan = pilot.read(ROOT / 'openai-batch10-plan.json')
    pilot.validate_plan(plan)
    baseline = pilot.read(ROOT / 'openai-batch10-harvest.json')
    selected = [x for x in plan['images'] if x['number'] in (1, 2, 7)]
    assert len(selected) == 3
    prompt = (ROOT / 'openai-batch10-prompt.txt').read_text()
    schema = pilot.read(ROOT / 'openai-batch10-schema.json')
    pairs = []
    state = {'status': 'running', 'pid': os.getpid(), 'finished': 0, 'total': 3}
    pilot.write(output('plan.json'), {
        'images': selected, 'input_hashes': plan['input_hashes'],
        'change': 'Only model_reasoning_effort low -> xhigh; same CLI command otherwise except output destination.',
        'baseline': 'Previously completed batch10 low calls, not supplied to xhigh.',
        'limitations': 'One sample per setting; not a statistical test. Simultaneous ongoing batch may affect latency.',
        'timeout_seconds': 900, 'automatic_retries': 0})
    try:
        for item in selected:
            n = item['number']
            state['current_image'] = item['filename']
            pilot.write(output('progress.json'), state)
            low = next(x for x in baseline if x['candidate_id'] == item['id'])
            assert low['status'] == 'success'
            cmd = list(low['command'])
            assert cmd.count('model_reasoning_effort="low"') == 1
            cmd[cmd.index('model_reasoning_effort="low"')] = 'model_reasoning_effort="xhigh"'
            cmd[cmd.index('--output-last-message') + 1] = str(output(f'{n:02d}-xhigh-result.json'))
            started = time.monotonic()
            errors = []
            with output(f'{n:02d}-xhigh-events.jsonl').open('x') as out, \
                    output(f'{n:02d}-xhigh-stderr.log').open('x') as err:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                        text=True, start_new_session=True)
                try:
                    proc.communicate(prompt, timeout=900)
                except BaseException:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    raise
            events = [json.loads(line) for line in output(f'{n:02d}-xhigh-events.jsonl').read_text().splitlines()]
            try:
                result = pilot.read(output(f'{n:02d}-xhigh-result.json'))
            except (FileNotFoundError, json.JSONDecodeError):
                result = None
            usage, errors = pilot.harvest(events, result, schema, proc.returncode)
            high = {'status': 'failed' if errors else 'success', 'errors': errors,
                    'usage': usage, 'elapsed_seconds': round(time.monotonic() - started, 2),
                    'command': cmd, 'result': result, 'returncode': proc.returncode}
            pair = {'candidate_id': item['id'], 'filename': item['filename'], 'low': low, 'xhigh': high}
            pairs.append(pair)
            pilot.write(output(f'{n:02d}-pair.json'), pair)
            pilot.write(output('harvest.json'), pairs)
            state['finished'] = len(pairs)
            pilot.write(output('progress.json'), state)
            print(json.dumps({'filename': item['filename'], 'status': high['status'],
                              'usage': usage, 'elapsed_seconds': high['elapsed_seconds']}), flush=True)
        state['status'] = 'completed' if all(p['xhigh']['status'] == 'success' for p in pairs) else 'completed_with_errors'
    except BaseException as exc:
        state.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        state['current_image'] = None
        pilot.write(output('progress.json'), state)
        pilot.write(output('completion.json'), state)


if __name__ == '__main__':
    main()
