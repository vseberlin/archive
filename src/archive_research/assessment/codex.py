"""Subscription-authenticated Codex transport for an already approved assessment.

Retains native CLI events and output; never represents this as a Responses API call.
"""
import base64
import hashlib
import json
import os
import signal
import subprocess
import time

from ..contracts import encoded, require
from ..storage import target, write, write_json

DISABLED_HOST_NOTICE = ('Code Mode is unavailable because code-mode host is disabled. '
                        'Code mode will fail closed; enable `features.code_mode_host` '
                        'and install `codex-code-mode-host`.')


def completed_result(events, result):
    require(sum(e.get('type') == 'turn.completed' for e in events) == 1,
            'Expected one completed Codex turn')
    require(not any(e.get('type') in ('error', 'turn.failed') for e in events), 'Codex turn failed')
    started = False
    for event in events:
        if event.get('type') == 'turn.started':
            started = True
        if event.get('type', '').startswith('item.'):
            # This pre-turn CLI notice confirms the intentionally disabled tool host.
            # Other errors and every tool item still fail closed.
            item = event.get('item', {})
            if not started and item.get('type') == 'error' and item.get('message') == DISABLED_HOST_NOTICE:
                continue
            require(event.get('item', {}).get('type') in ('reasoning', 'agent_message'),
                    'Unexpected tool use in assessment')
    messages = [e['item']['text'] for e in events if e.get('type') == 'item.completed'
                and e.get('item', {}).get('type') == 'agent_message']
    require(messages and json.loads(messages[-1]) == result, 'CLI final output does not match retained events')
    return next(e.get('usage', {}) for e in events if e.get('type') == 'turn.completed')


def retained_response(root, prefix, *, elapsed_seconds=None):
    """Recover local CLI output without making a second external request."""
    invocation = json.loads(target(root, prefix + '-invocation.json').read_text())
    command = invocation['command']
    events = [json.loads(line) for line in target(root, prefix + '-events.jsonl').read_text().splitlines() if line.strip()]
    result = json.loads(target(root, prefix + '-result.json').read_text())
    return {'format': 'archive-research-codex-response-1', 'provider': 'codex-chatgpt',
            'status': 'completed' if any(e.get('type') == 'turn.completed' for e in events) else 'incomplete',
            'model': command[command.index('--model') + 1], 'model_basis': 'explicit CLI argument',
            'id': next((e.get('thread_id') for e in events if e.get('type') == 'thread.started'), None),
            'usage': next((e.get('usage', {}) for e in events if e.get('type') == 'turn.completed'), None),
            'assessment': result, 'events': events, 'request_sha256': invocation['request_sha256'],
            'invocation': invocation, 'elapsed_seconds': elapsed_seconds,
            'cli_notices': [e['item'].get('message') for e in events if e.get('item', {}).get('type') == 'error']}


def submit_once(request, transport, *, authorized_request_sha256, executable, root, prefix, timeout=240):
    require(hashlib.sha256(encoded(request).encode()).hexdigest() ==
            transport['request_sha256'] == authorized_request_sha256, 'Authorize the exact prepared evidence')
    require(request['model'] == transport['model'], 'Transport model changed')
    login = subprocess.run([str(executable), 'login', 'status'], capture_output=True, text=True, timeout=15)
    require(login.returncode == 0 and 'Logged in using ChatGPT' in login.stdout + login.stderr,
            'Codex must already be signed in with ChatGPT')
    version = subprocess.run([str(executable), '--version'], capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    content = request['input'][0]['content']
    images = [p for p in content if p['type'] == 'input_image']
    require(len(images) == len(transport['attachments']), 'Attachment count changed')
    names = {k: prefix + '-' + k + suffix for k, suffix in
             [('prompt', '.txt'), ('instructions', '.txt'), ('schema', '.json'),
              ('result', '.json'), ('events', '.jsonl'), ('stderr', '.log'), ('invocation', '.json')]}
    image_names = [prefix + f'-image-{a["evidence"]}.jpg' for a in transport['attachments']]
    for name in [*names.values(), *image_names]:
        require(not target(root, name).exists(), 'Codex run artifacts already exist; inspect before another attempt')
    for part, attachment, name in zip(images, transport['attachments'], image_names):
        require(part['image_url'].startswith('data:image/jpeg;base64,'), 'Expected prepared JPEG')
        data = base64.b64decode(part['image_url'].split(',', 1)[1], validate=True)
        require(hashlib.sha256(data).hexdigest() == attachment['transport_sha256'], 'Prepared image changed')
        write(root, name, data)
    prompt = '\n\n'.join(p['text'] for p in content if p['type'] == 'input_text')
    prompt += '\n\nAttachments are in the listed evidence order: ' + encoded(transport['attached_evidence'])
    prompt += (f'\nReturn only the schema object, compactly, aiming below {request["max_output_tokens"]} output tokens. '
               'Use only these supplied images and text. Do not use tools, search, read files, or delegate. '
               'At least one cited observation is required. Preserve uncertainty. Answer in English.')
    write(root, names['prompt'], prompt.encode())
    write(root, names['instructions'], (request['instructions'] + '\nNo tools or delegation. Only assess supplied evidence.').encode())
    write_json(root, names['schema'], request['text']['format']['schema'])
    command = [str(executable), 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
               '--sandbox', 'read-only', '-C', str(root), '--model', request['model'], '--json',
               '--output-schema', str(target(root, names['schema'])), '-o', str(target(root, names['result']))]
    settings = ['model_reasoning_effort="low"', 'web_search="disabled"', 'project_doc_max_bytes=0',
                'skills.bundled.enabled=false', 'skills.include_instructions=false', 'skills.max_context_tokens=1',
                'model_instructions_file=' + json.dumps(str(target(root, names['instructions'])))]
    settings += ['features.' + feature + '=false' for feature in
                 ('shell_tool', 'apps', 'plugins', 'remote_plugin', 'browser_use', 'computer_use',
                  'image_generation', 'multi_agent', 'multi_agent_v2', 'memories', 'hooks',
                  'view_image', 'code_mode', 'code_mode_host', 'goals')]
    for setting in settings:
        command += ['-c', setting]
    for name in image_names:
        command += ['--image', str(target(root, name))]
    command += ['-']
    invocation = {'provider': 'codex-chatgpt', 'cli_version': version, 'command': command,
                  'request_sha256': transport['request_sha256'], 'packet_sha256': transport['packet_sha256'],
                  'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                  'attachments': transport['attachments'], 'external_submission_authorized': True,
                  'authorization': 'User approved selected material and ChatGPT subscription transport',
                  'limitations': ['Codex uses account controls; Responses API store=false is not a CLI setting.',
                                 'CLI output token target is advisory, not the API hard output cap.',
                                 'CLI may resize images internally; the exact prepared JPEGs are supplied.'],
                  'status': 'submission started; outcome unknown until events retained'}
    write_json(root, names['invocation'], invocation)
    environment = dict(os.environ)
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'):
        environment.pop(key, None)
    start = time.monotonic()
    with target(root, names['events']).open('x') as out, target(root, names['stderr']).open('x') as err:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                   text=True, env=environment, start_new_session=True)
        try:
            process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise RuntimeError('Codex assessment timed out; retained events; no automatic retry') from None
    require(process.returncode == 0, 'Codex failed; inspect retained stderr; no automatic retry')
    # Return the raw envelope first so callers retain it even if validation fails.
    return retained_response(root, prefix, elapsed_seconds=round(time.monotonic() - start, 2))
