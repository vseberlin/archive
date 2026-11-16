#!/usr/bin/env python3
"""Complete native comparisons after the full rotation-aware Gradient pass."""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from compare_remaining_images import W, BINARY, app, dump, stamp

snapshot = json.loads((W / 'remaining-similarity-inputs.json').read_text())
inputs = [r for r in snapshot['unresolved'] if r.get('features')] + snapshot['explicitly_retained'] + snapshot['references'] + snapshot['additional_index_candidates']
assert len(inputs) == 973
app.mount_guard()
gradient_pid = Path('/proc/135741/stat')
while gradient_pid.exists() and gradient_pid.read_text().split()[2] != 'Z':
    time.sleep(2)
gradient_output = W/'remaining-czkawka-gradient.json'
gradient = json.loads(gradient_output.read_text())
print(f'Gradient completed: {len(gradient)} groups', flush=True)
runs = [dict(algorithm='Gradient', hash_size=16, max_difference=20,
             geometric_invariance='mirror-flip-rotate90', output=str(gradient_output), groups=len(gradient))]
env = os.environ | {'CZKAWKA_CACHE_PATH':str(W), 'CZKAWKA_CONFIG_PATH':str(W)}
for alg in ('Mean','Blockhash'):
    output=W/f'remaining-czkawka-{alg.lower()}.json'
    assert not output.exists(), 'Reuse completed output instead of rerunning.'
    command=[str(BINARY),'image','-x','jpg,jpeg,tif,tiff,png','-m','1','-g',alg,'-c','16',
             '-s','20','--geometric-invariance','off','-p',str(output),'-N','-W','-T','4','-D','NONE']
    for item in inputs:command+=['-d',item['path']]
    print(f'Starting {alg} normal-orientation confirmation',flush=True)
    with (W/f'remaining-czkawka-{alg.lower()}.log').open('x') as log:
        result=subprocess.run(command,env=env,stdout=log,stderr=subprocess.STDOUT)
    assert result.returncode==0,(alg,result.returncode)
    groups=json.loads(output.read_text())
    runs.append(dict(algorithm=alg,hash_size=16,max_difference=20,geometric_invariance='off',output=str(output),groups=len(groups)))
    print(f'{alg} completed: {len(groups)} groups',flush=True)
for p,old in snapshot['input_stamps'].items():assert stamp(p)==old,p
for item in inputs:assert stamp(item['path'])==[item['size_bytes'],item['mtime_ns']],item['path']
app.mount_guard()
dump('remaining-czkawka-runs.json',dict(completed_at=datetime.now(timezone.utc).isoformat(),binary=str(BINARY),
     input_count=len(inputs),runs=runs,unchanged_inputs=True,unchanged_controls=True,
     decisions_changed=False,deletion_authorized=False))
