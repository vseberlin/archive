"""Run a finite local OCR comparison with a process-tree memory/time budget.

Input is an explicit task list, never an archive scan. Outputs are technical logs.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import psutil


def run(plan, output):
    history=[];used=0
    for task in plan['tasks']:
        if used >= plan['total_seconds']: break
        limit=min(task.get('timeout',600),plan['total_seconds']-used)
        cap=min(plan['memory_bytes'],int(psutil.virtual_memory().available*.85))
        log=output/(task['id']+'.log')
        start=time.monotonic();peak=0;status='complete'
        env={**os.environ,'OMP_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2',
             'EYNOLLAH_ONNX_EP':'CPU','CUDA_VISIBLE_DEVICES':'',**task.get('env',{})}
        with log.open('wb') as handle:
            process=subprocess.Popen(task['argv'],stdout=handle,stderr=subprocess.STDOUT,
                                     env=env,start_new_session=True)
            root=psutil.Process(process.pid)
            while process.poll() is None:
                try:
                    memory=sum(p.memory_info().rss for p in [root,*root.children(recursive=True)] if p.is_running())
                    peak=max(peak,memory)
                except (psutil.NoSuchProcess,psutil.AccessDenied): pass
                if peak>cap or time.monotonic()-start>limit:
                    status='memory_limit' if peak>cap else 'timeout'
                    os.killpg(process.pid,signal.SIGTERM)
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL);process.wait()
                    break
                time.sleep(.5)
        elapsed=time.monotonic()-start;used+=elapsed
        if process.returncode and status=='complete':status='failed'
        row={'id':task['id'],'status':status,'returncode':process.returncode,'seconds':elapsed,
             'peak_rss_bytes':peak,'memory_limit_bytes':cap,'argv':task['argv'],'log':str(log)}
        history.append(row)
        (output/plan['summary']).write_text(json.dumps({'tasks':history,'seconds':used,
                    'metadata_adopted':False,'planned_tasks':len(plan['tasks'])},indent=2)+'\n')
        print(json.dumps({k:row[k] for k in ['id','status','seconds','peak_rss_bytes']}),flush=True)
        if status in ['memory_limit','timeout'] and plan.get('stop_on_limit',True):break
    return history


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan');p.add_argument('output')
    a=p.parse_args();run(json.loads(Path(a.plan).read_text()),Path(a.output))
