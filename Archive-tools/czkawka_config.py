"""Validated Czkawka job settings; no arbitrary command or deletion flags."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from archive_common import plain_file, require

ALGORITHMS = ('Mean', 'Gradient', 'Blockhash', 'VertGradient', 'DoubleGradient', 'Median')
FILTERS = ('Nearest', 'Triangle', 'CatmullRom', 'Gaussian', 'Lanczos3')
GEOMETRY = ('off', 'mirror-flip', 'mirror-flip-rotate90')
DEFAULT_PASS = {'algorithm': 'Gradient', 'hash_size': 16, 'resize_filter': 'Lanczos3',
                'max_difference': 12, 'geometric_invariance': 'mirror-flip-rotate90'}
DEFAULT_PROFILE = {'format': 'archive-tools-czkawka-1', 'exact_hash': 'off', 'scope': 'residual',
                   'proxy_edge': 256, 'preview_mode': 'catalogued',
                   'passes': [DEFAULT_PASS, {**DEFAULT_PASS, 'algorithm': 'Blockhash'}]}


def pass_id(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:12]


def validate_profile(profile):
    require(isinstance(profile, dict) and set(profile) <= set(DEFAULT_PROFILE), 'Unknown Czkawka profile fields')
    profile = {**deepcopy(DEFAULT_PROFILE), **profile}
    require(profile['format'] == DEFAULT_PROFILE['format'], 'Unsupported Czkawka profile format')
    require(profile['exact_hash'] in ('off', 'BLAKE3'), 'Exact hash must be off or BLAKE3; SHA-256 catalogue evidence remains separate')
    require(profile['scope'] in ('residual', 'unconfirmed'), 'Czkawka scope must be residual or unconfirmed')
    require(type(profile['proxy_edge']) is int and 64 <= profile['proxy_edge'] <= 4096, 'Proxy edge must be 64–4096 pixels')
    require(profile['preview_mode'] in ('catalogued', 'primary'), 'Preview mode must be catalogued or primary')
    require(isinstance(profile['passes'], list) and 0 <= len(profile['passes']) <= 12, 'Supply at most 12 visual passes')
    passes = []
    for settings in profile['passes']:
        require(isinstance(settings, dict) and set(settings) <= set(DEFAULT_PASS), 'Unknown visual-pass fields')
        settings = {**DEFAULT_PASS, **settings}
        require(settings['algorithm'] in ALGORITHMS, 'Unsupported perceptual algorithm; BLAKE3 belongs in exact_hash')
        require(type(settings['hash_size']) is int and settings['hash_size'] in (8, 16, 32, 64), 'Hash size must be 8, 16, 32 or 64')
        require(settings['resize_filter'] in FILTERS, 'Unsupported image resize filter')
        require(type(settings['max_difference']) is int and 0 <= settings['max_difference'] <= 40, 'Maximum difference must be 0–40')
        require(settings['geometric_invariance'] in GEOMETRY, 'Unsupported geometric invariance')
        require(settings not in passes, 'Duplicate visual pass')
        passes.append(settings)
    profile['passes'] = passes
    return profile


def load_profile(args):
    profile = {}
    if getattr(args, 'czkawka_profile', None):
        profile = json.loads(plain_file(Path(args.czkawka_profile)).read_text())
        require(isinstance(profile, dict), 'Czkawka profile must be an object')
    if getattr(args, 'czkawka_pass', None):
        passes = []
        for value in args.czkawka_pass:
            fields = value.split('/')
            require(3 <= len(fields) <= 5, 'Use ALGORITHM/SIZE/FILTER[/MAX_DIFFERENCE[/GEOMETRY]]')
            settings = {'algorithm': fields[0], 'hash_size': int(fields[1]), 'resize_filter': fields[2]}
            if len(fields) >= 4:
                settings['max_difference'] = int(fields[3])
            if len(fields) == 5:
                settings['geometric_invariance'] = fields[4]
            passes.append(settings)
        profile['passes'] = passes
    for option, key in (('czkawka_scope', 'scope'), ('czkawka_exact_hash', 'exact_hash'),
                        ('czkawka_proxy_edge', 'proxy_edge'), ('czkawka_preview_mode', 'preview_mode')):
        if getattr(args, option, None) is not None:
            profile[key] = getattr(args, option)
    return validate_profile(profile)
