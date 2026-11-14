#!/usr/bin/env python3
"""Review prepared archive candidates on localhost; save decisions, never sources."""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

from archive_common import (ReviewError, atomic_write, checked_member, file_lock,
                            plain_file, preview, read_bytes, require, source_path, work_root)
from catalogues import DISCOVERY_SCOPE, ersatz_jpeg, genuine_tiff, md_identity
from consolidation import active_comparison, assigned_md_id
from research_routes import ROUTES, routing_for, validate_override

HERE = Path(__file__).resolve().parent
FORMAT = 'archive-tools-workbench-1'
INPUT = 'workbench-input.json'
DECISIONS = 'decisions.jsonl'
CSV = 'dispositions.csv'
LOCK = '.workbench.lock'
MODEL_PROPOSALS = 'model-proposals.json'
METADATA_FIELDS = {'title', 'description', 'date', 'places', 'creator', 'subjects', 'inscriptions', 'notes'}
RECEIPTS = ('accession-receipt.json', 'accession-completion.json', 'publication.json')
LEGACY_EDIT_FIELDS = {'selected_candidate_id', 'redundant_candidate_ids', 'different_pairs', 'note', 'metadata_reviewed'}
EDIT_FIELDS = LEGACY_EDIT_FIELDS | {'same_pairs', 'destination_unit'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')


def valid_id(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 300


def proposal_fields(proposal):
    """Editable suggestions from current evidence; never mutate saved decisions."""
    def text(claim):
        if isinstance(claim, str): return claim
        if not claim: return ''
        value = claim.get('display') or claim.get('value') or claim.get('text') or ''
        tentative = claim.get('basis') in ('hypothesis', 'visual_estimate') or claim.get('uncertain_parts')
        return value + (' (tentative)' if tentative and value else '')
    result = proposal['result']
    fields = {k: text(result.get(k)) for k in ('title', 'description', 'date', 'creator')}
    fields.update({k: '; '.join(filter(None, map(text, result.get(k, []))))
                   for k in ('places', 'subjects', 'inscriptions')})
    fields['notes'] = ''
    check = proposal.get('reference_check', {})
    research = check.get('result') if check.get('status') == 'compared' else None
    if research:
        # A specific supported identity may supply a title. Tentative identities
        # remain hypotheses instead of silently replacing a descriptive title.
        identity = research.get('depicted_object', {})
        if identity.get('status') == 'supported' and identity.get('value'):
            fields['title'] = identity['value']
        observations = [o['description'].strip() for o in research.get('observations', [])
                        if o.get('reference_id') == 'INPUT' and o.get('description', '').strip()]
        parts = ([identity['value']+'.'] if identity.get('status') == 'supported' and identity.get('value') else [])
        parts += observations
        if parts: fields['description'] = '\n\n'.join(dict.fromkeys(parts))[:12000]
        for key, target in [('location', 'places'), ('photograph_date', 'date')]:
            claim = research.get(key, {})
            if claim.get('value') and claim.get('status') in ('supported', 'tentative'):
                fields[target] = claim['value'] + (' (tentative)' if claim['status'] == 'tentative' else '')
            elif target == 'date' and claim.get('status') in ('unresolved', 'contradicted'):
                fields[target] = ''
        fields['notes'] = '\n'.join(filter(None, [research.get('photograph_date', {}).get('explanation', '')]
                                        + research.get('limitations', [])))[:12000]
    # Existing reviewed editorial corrections take precedence over model prose.
    fields.update({k: v for k, v in proposal.get('consolidated_proposal', {}).items() if k in METADATA_FIELDS})
    return fields


class Workbench:
    def __init__(self, root, actor):
        self.root = work_root(root)
        self.actor = actor.strip()
        require(0 < len(self.actor) <= 100, 'Supply a reviewer name, at most 100 characters')
        self.lock = threading.RLock()
        self.load()
        self.read_decisions()

    def load(self):
        work_root(self.root)
        raw = read_bytes(self.root / INPUT)
        if getattr(self, '_loaded', False) and raw == self.input_bytes:
            self.frozen = self.config['read_only'] or any(p.exists() or p.is_symlink() for p in self.receipts)
            return
        self._loaded = False
        self.input_bytes = raw
        self.config = json.loads(self.input_bytes)
        require(isinstance(self.config, dict) and self.config.get('format') == FORMAT, 'Unsupported input format')
        self.fixed_archive = self.config.get('archive_masters_fixed', False)
        require(type(self.fixed_archive) is bool, 'Invalid archive Master policy')
        require(self.config.get('review_scope', '') in ('', DISCOVERY_SCOPE), 'Invalid review scope')
        self.discovery = self.config.get('review_scope') == DISCOVERY_SCOPE
        require(not self.discovery or self.fixed_archive, 'Discovery keeps archive Masters fixed')
        require(type(self.config.get('read_only')) is bool, 'Input must explicitly declare read_only')
        roots = self.config.get('read_roots')
        require(isinstance(roots, list), 'Input must declare read_roots')
        self.roots = [self.root]
        for value in roots:
            require(isinstance(value, str) and Path(value).is_absolute(), 'Read roots must be absolute')
            path = Path(value)
            require(path == path.resolve(), 'Read roots must be canonical paths without symlinks')
            require(not self.root.is_relative_to(path), 'A source read root contains the writable work area')
            self.roots.append(path)
        objects = self.config.get('objects')
        require(isinstance(objects, list), 'Input must contain a prepared object list')
        self.objects = {}
        self.candidates = {}
        for obj in objects:
            require(isinstance(obj, dict), 'Object must be a record')
            oid = obj.get('id')
            require(valid_id(oid) and oid not in self.objects, 'Missing or duplicate object ID')
            require(isinstance(obj.get('metadata', {}), dict), 'Metadata must be an evidence dictionary')
            candidates = obj.get('candidates')
            require(isinstance(candidates, list), 'Object must contain a candidate list')
            self.objects[oid] = obj
            by_id = self.candidates[oid] = {}
            for candidate in candidates:
                require(isinstance(candidate, dict), 'Candidate must be a record')
                cid = candidate.get('id')
                require(valid_id(cid) and cid not in by_id, 'Missing or duplicate candidate ID')
                require('reference_object_id' not in candidate or valid_id(candidate['reference_object_id']),
                        'Invalid existing archive object reference')
                require(type(candidate.get('eligible')) is bool and type(candidate.get('complete')) is bool,
                        'Candidate needs explicit eligible and complete flags')
                require(isinstance(candidate.get('metadata', {}), dict), 'Candidate metadata must be a dictionary')
                members = candidate.get('members')
                require(isinstance(members, list) and members, 'Candidate needs ordered members')
                seen = set()
                for member in members:
                    require(isinstance(member, dict), 'Member must be a record')
                    path = source_path(member.get('path'), self.roots)
                    require(path not in seen, 'Duplicate member in a representation')
                    seen.add(path)
                    require(path not in {self.root / name for name in (INPUT, DECISIONS, CSV, LOCK, *RECEIPTS)},
                            'Source overlaps a workbench output or control file')
                    require(type(member.get('size_bytes')) is int and member['size_bytes'] >= 0 and
                            type(member.get('mtime_ns')) is int, 'Member needs size_bytes and mtime_ns')
                    require(type(member.get('pages')) is int and 1 <= member['pages'] <= 100000,
                            'Member needs a positive page count')
                    digest = member.get('sha256', '')
                    require(isinstance(digest, str) and (not digest or len(digest) == 64 and
                            all(c in '0123456789abcdef' for c in digest)), 'Invalid stored SHA-256')
                by_id[cid] = candidate
            for match in obj.get('matches', []):
                require(isinstance(match, dict) and match.get('a') in by_id and match.get('b') in by_id
                        and match['a'] != match['b'], 'Match needs two distinct candidate IDs')
        receipts = self.config.get('completion_receipts')
        require(isinstance(receipts, list), 'Declare completion_receipts explicitly, even when empty')
        self.receipts = [source_path(p, self.roots) for p in receipts]
        self.receipts += [self.root / name for name in RECEIPTS]
        self.frozen = self.config['read_only'] or any(p.exists() or p.is_symlink() for p in self.receipts)
        initial = self.config.get('initial_decisions', {})
        require(isinstance(initial, dict) and set(initial) <= set(self.objects), 'Unknown initial decision object')
        require(all(isinstance(row, dict) and set(row) <= EDIT_FIELDS | {'actor', 'decided_at'}
                    for row in initial.values()), 'Invalid prepared initial decision')
        destinations = self.config.get('destination_units', [])
        require(isinstance(destinations, list) and all(isinstance(d, dict) and isinstance(d.get('path'), str)
                and isinstance(d.get('label'), str) for d in destinations), 'Invalid destination units')
        self.destinations = {d['path'] for d in destinations}
        self._loaded = True

    def blank(self, oid):
        row = {'object_id': oid, 'selected_candidate_id': '', 'redundant_candidate_ids': [],
                'different_pairs': [], 'same_pairs': [], 'destination_unit': '', 'note': '', 'actor': '', 'decided_at': '',
                'metadata_reviewed': False, 'previous_master_ids': [], 'deletion_authorized': False,
                'input_object': self.objects[oid]}
        row.update(deepcopy(self.config.get('initial_decisions', {}).get(oid, {})))
        return row

    @staticmethod
    def same_component(row, selected):
        connected = {selected} if selected else set()
        edges = list(row.get('same_pairs', []))
        edges += [[row['selected_candidate_id'], cid] for cid in row['redundant_candidate_ids']]
        while True:
            more = connected | {cid for edge in edges if connected.intersection(edge) for cid in edge}
            if more == connected:
                return connected
            connected = more

    def scope_context(self, oid, row):
        obj, candidates = self.objects[oid], self.candidates[oid]
        sources = {cid for cid, c in candidates.items() if not c.get('reference_object_id')}
        references = set(candidates) - sources
        selected = row['selected_candidate_id']
        connected = self.same_component(row, selected)
        assigned = assigned_md_id(obj.get('previous_proposals', []))
        rejected = {tuple(sorted(p)) for p in row['different_pairs']}
        active, exceptions = [], []
        for cid in sorted(references):
            c = candidates[cid]
            leads = [m for m in obj.get('matches', []) if cid in (m['a'], m['b']) and
                     (m['b'] if m['a'] == cid else m['a']) in sources and
                     tuple(sorted((m['a'], m['b']))) not in rejected]
            proven = any(cid in self.same_component(row, source) for source in sources)
            recovery = ersatz_jpeg(c) and any(genuine_tiff(candidates[source]) and
                tuple(sorted((source, cid))) not in rejected for source in sources)
            if proven or selected == cid or active_comparison(leads) or recovery and assigned == c['reference_object_id']:
                active.append(cid)
                if recovery:
                    exceptions.append(cid)
        known = {candidates[c]['reference_object_id'] for c in references & connected
                 if md_identity(candidates[c]['reference_object_id']) and sources & connected}
        known_md = (next(iter(known)) if len(known) == 1 else '') or assigned
        covered = bool(sources) and selected in references and sources <= connected
        assigned_group = set()
        for p in obj.get('previous_proposals', []):
            if assigned and assigned_md_id([p]) == assigned:
                for source in sources:
                    if candidates[source]['members'][0]['path'] == p.get('selected_path'):
                        assigned_group.update(self.same_component(row, source))
        assigned_covers = bool(sources) and sources <= assigned_group
        separate_sources = any(a in sources and b in sources for a, b in row['different_pairs'])
        closed = self.discovery and (assigned_covers or covered) and not exceptions and not separate_sources
        return {'closed': closed, 'md_object_id': known_md, 'active_reference_ids': active,
                'tiff_reference_ids': exceptions,
                'tiff_recovery_pending': self.discovery and bool(exceptions) and not row.get('decided_at'),
                'kind': 'existing_md_object' if closed and known_md else 'existing_archive_object' if closed else
                        'tiff_for_jpeg_review' if exceptions and self.discovery else
                        'newness_review' if active else 'proposed_new_object'}

    def selection_context(self, oid, row):
        selected = row['selected_candidate_id']
        scope = self.scope_context(oid, row) if self.discovery else None
        if scope and scope['closed'] and scope['md_object_id'] and (
                not selected or not self.candidates[oid][selected].get('reference_object_id')):
            identity = scope['md_object_id']
            return {'action': 'existing_md_object', 'archive_object_id': identity,
                    'destination_unit': identity.rsplit('#', 1)[0]}
        if not selected:
            return {'action': 'unresolved', 'archive_object_id': '', 'destination_unit': row.get('destination_unit', '')}
        obj, candidates = self.objects[oid], self.candidates[oid]
        chosen = candidates[selected]
        if chosen.get('reference_object_id'):
            return {'action': 'retain_archive_master', 'archive_object_id': chosen['reference_object_id'],
                    'destination_unit': chosen.get('archive_unit', row.get('destination_unit', ''))}
        references = [candidates[c] for c in self.same_component(row, selected) if candidates[c].get('reference_object_id')]
        destination = row.get('destination_unit', '')
        if destination:
            references = [c for c in references if c.get('archive_unit') == destination]
        if len(references) == 1:
            recovery = self.discovery and genuine_tiff(chosen) and ersatz_jpeg(references[0])
            return {'action': 'proposed_master_upgrade' if recovery or not self.fixed_archive else 'retain_archive_master', 'archive_object_id': references[0]['reference_object_id'],
                    'destination_unit': references[0].get('archive_unit', destination)}
        if references or any(candidates[c].get('reference_object_id') for c in self.same_component(row, selected)):
            return {'action': 'archive_identity_review', 'archive_object_id': '', 'destination_unit': destination}
        if self.fixed_archive and any(c.get('reference_object_id') and
                (not self.discovery or c['id'] in scope['active_reference_ids']) and not any(
                set(pair) == {selected, c['id']} for pair in row['different_pairs']) for c in candidates.values()):
            return {'action': 'archive_identity_review', 'archive_object_id': '', 'destination_unit': destination}
        proposals = [p for p in obj.get('previous_proposals', []) if p.get('assigned_object_id')
                     and p.get('selected_path') == chosen['members'][0]['path']]
        if proposals and self.fixed_archive:
            return {'action': 'archive_identity_review', 'archive_object_id': '', 'destination_unit': destination}
        if len(proposals) == 1 and not self.fixed_archive:
            return {'action': 'proposed_master_reassignment', 'archive_object_id': proposals[0]['assigned_object_id'],
                    'destination_unit': proposals[0]['destination_unit']}
        return {'action': 'proposed_new_object', 'archive_object_id': '', 'destination_unit': destination}

    def retain_archive(self, oid, row):
        """Apply the fixed-Master policy to confirmed identities only, in place."""
        if not self.fixed_archive:
            return
        selected = row['selected_candidate_id']
        candidates = self.candidates[oid]
        connected = self.same_component(row, selected)
        references = sorted(cid for cid in connected if candidates[cid].get('reference_object_id'))
        if selected and not candidates[selected].get('reference_object_id') and references:
            require(len(references) == 1, 'Several archive identities confirmed; choose the retained archive Master')
            # Preserve implicit copy links before changing the centre of the group.
            known = {tuple(sorted(pair)) for pair in row['same_pairs']}
            for cid in row['redundant_candidate_ids']:
                pair = tuple(sorted((selected, cid)))
                if pair not in known:
                    row['same_pairs'].append(list(pair)); known.add(pair)
            if self.discovery and genuine_tiff(candidates[selected]) and ersatz_jpeg(candidates[references[0]]):
                row['redundant_candidate_ids'] = sorted(cid for cid in self.same_component(row, selected)
                    if cid != selected and not candidates[cid].get('reference_object_id'))
                return
            if selected not in row['previous_master_ids']:
                row['previous_master_ids'].append(selected)
            row['selected_candidate_id'] = references[0]
            row['destination_unit'] = candidates[references[0]].get('archive_unit', '')
        selected = row['selected_candidate_id']
        if selected and candidates[selected].get('reference_object_id'):
            row['redundant_candidate_ids'] = sorted(cid for cid in self.same_component(row, selected)
                if cid != selected and not candidates[cid].get('reference_object_id'))

    def archive_resolved(self, oid, row):
        if not self.fixed_archive:
            return False
        if self.discovery and self.scope_context(oid, row)['tiff_recovery_pending']:
            return False
        selected = row['selected_candidate_id']
        if not selected or not self.candidates[oid][selected].get('reference_object_id'):
            return False
        sources = {cid for cid, c in self.candidates[oid].items() if not c.get('reference_object_id')}
        return bool(sources) and sources <= self.same_component(row, selected)

    def validate(self, rows):
        claims, master_members = {}, {}
        for oid, row in rows.items():
            by_id = self.candidates[oid]
            require(isinstance(row, dict) and row.get('object_id') == oid, 'Invalid decision object')
            require(row.get('deletion_authorized') is False, 'Decisions cannot authorize deletion')
            if 'metadata_proposal' in row:
                meta = row['metadata_proposal']
                require(isinstance(meta, dict) and meta.get('metadata_adopted') is False, 'Edited metadata remains a proposal')
                require(isinstance(meta.get('fields'), dict) and set(meta['fields']) == METADATA_FIELDS and
                        all(isinstance(v, str) and len(v) <= 12000 for v in meta['fields'].values()), 'Invalid saved metadata fields')
                require(meta.get('candidate_id') in by_id and any(m['path'] == meta.get('source_path') and
                        all(m[k] == meta.get('source_stamp', {}).get(k) for k in ('size_bytes', 'mtime_ns'))
                        for m in by_id[meta['candidate_id']]['members']), 'Saved metadata source changed')
                validate_override(meta.get('research_routing'))

            require(type(row.get('metadata_reviewed', False)) is bool, 'Invalid metadata review flag')
            require(isinstance(row.get('destination_unit', ''), str) and
                    (not row.get('destination_unit') or row['destination_unit'] in self.destinations), 'Unknown destination unit')
            require(row.get('input_object') == self.objects[oid],
                    'Prepared evidence changed since saving; explicitly reconcile it before continuing')
            require(isinstance(row.get('note'), str) and len(row['note']) <= 2000, 'Note is limited to 2000 characters')
            require(isinstance(row.get('actor'), str) and isinstance(row.get('decided_at'), str), 'Invalid reviewer evidence')
            previous = row.get('previous_master_ids')
            require(isinstance(previous, list) and all(isinstance(c, str) and c in by_id for c in previous)
                    and len(previous) == len(set(previous)), 'Invalid previous Master provenance')
            selected = row.get('selected_candidate_id')
            require(isinstance(selected, str) and (selected == '' or selected in by_id), 'Unknown Master candidate')
            redundant = row.get('redundant_candidate_ids')
            require(isinstance(redundant, list) and all(isinstance(c, str) and c in by_id for c in redundant),
                    'Unknown redundant candidate')
            require(len(set(redundant)) == len(redundant) and selected not in redundant,
                    'Master and redundant candidates must be distinct')
            require(not redundant or bool(selected), 'Redundancy requires a retained Master')
            pairs = row.get('different_pairs')
            require(isinstance(pairs, list), 'Different-photo decisions must be pairs')
            seen = set()
            same = self.same_component(row, selected) if selected else set()
            positives = row.get('same_pairs', [])
            require(isinstance(positives, list), 'Same-photo decisions must be pairs')
            seen_positive = set()
            for pair in positives:
                require(isinstance(pair, list) and len(pair) == 2 and all(isinstance(c, str) and c in by_id for c in pair)
                        and pair[0] != pair[1], 'Same-photo decision needs two distinct candidates')
                key = tuple(sorted(pair))
                require(key not in seen_positive, 'Duplicate same-photo pair')
                seen_positive.add(key)
            for pair in pairs:
                require(isinstance(pair, list) and len(pair) == 2 and
                        all(isinstance(c, str) and c in by_id for c in pair) and pair[0] != pair[1],
                        'Different-photo decision needs two distinct candidates')
                key = tuple(sorted(pair))
                require(key not in seen, 'Duplicate different-photo pair')
                seen.add(key)
                require(not set(pair) <= same, 'Candidates cannot be both redundant copies and different photos')
                require(pair[1] not in self.same_component(row, pair[0]), 'Conflicting same-photo and different-photo decisions')
            if selected:
                candidate = by_id[selected]
                require(candidate['eligible'] and candidate['complete'], 'Master must be eligible and complete')
                context = self.selection_context(oid, row)
                owner = context['archive_object_id'] or candidate.get('reference_object_id', oid)
                if self.discovery and context['action'] == 'existing_md_object':
                    owner = oid  # Historical source selection does not claim the md Master.
                if self.discovery and context['action'] == 'proposed_master_upgrade':
                    owner = 'proposal:' + owner  # Exactly one proposal; the archive Master stays fixed.
                signature = tuple((m['path'], m.get('sha256', '')) for m in candidate['members'])
                require(owner not in master_members or master_members[owner] == signature,
                        'An archive object cannot have different retained Masters across review cases')
                master_members[owner] = signature
                for member in candidate['members']:
                    identities = ['path:' + member['path']]
                    if member.get('sha256'):
                        identities.append('sha256:' + member['sha256'])
                    for identity in identities:
                        require(identity not in claims or claims[identity] == owner,
                                'This source identity is already selected for another object')
                        claims[identity] = owner

    def read_decisions(self):
        raw = read_bytes(self.root / DECISIONS, optional=True)
        cache_key = (self.input_bytes, raw, (self.root / DECISIONS).exists())
        if getattr(self, '_decision_cache_key', None) == cache_key:
            return self._decision_cache
        rows = {oid: self.blank(oid) for oid in self.objects}
        if (self.root / DECISIONS).exists():
            saved = [json.loads(line) for line in raw.splitlines() if line.strip()]
            ids = [row.get('object_id') if isinstance(row, dict) else None for row in saved]
            require(len(ids) == len(rows) and all(isinstance(i, str) for i in ids)
                    and set(ids) == set(rows), 'Saved decisions must cover each prepared object exactly once')
            rows = dict(zip(ids, saved))
            for row in rows.values():
                row.setdefault('metadata_reviewed', False)
                row.setdefault('same_pairs', [])
                row.setdefault('destination_unit', '')
        self.validate(rows)
        revision = hashlib.sha256(self.input_bytes + b'\0' + raw).hexdigest()
        self._decision_cache_key = cache_key
        self._decision_cache = rows, revision
        return rows, revision

    def problems(self, candidate):
        problems = []
        for member in candidate['members']:
            try:
                checked_member(member, self.roots)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                problems.append(str(error))
        return list(dict.fromkeys(problems))

    def disposition(self, row, cid, reference=False):
        if self.discovery and not reference:
            scope = self.scope_context(row['object_id'], row)
            if scope['closed'] and self.selection_context(row['object_id'], row)['action'] == 'existing_md_object':
                return 'existing_md_source'
            if scope['tiff_recovery_pending'] and genuine_tiff(self.candidates[row['object_id']][cid]):
                return 'tiff_for_jpeg_review'
        if row['selected_candidate_id'] == cid:
            return 'selected_master'
        if reference and self.fixed_archive:
            return 'reference'
        if row['selected_candidate_id'] and cid in Workbench.same_component(row, row['selected_candidate_id']):
            return 'redundant'
        if row['selected_candidate_id'] and any(set(pair) == {cid, row['selected_candidate_id']}
                                               for pair in row['different_pairs']):
            return 'different_photo'
        return 'reference' if reference else 'unresolved'

    def projection(self, rows):
        out = io.StringIO(newline='')
        fields = ['object_id', 'candidate_id', 'member_order', 'source_path', 'size_bytes', 'mtime_ns',
                  'sha256', 'disposition', 'retained_master_id', 'different_from_ids', 'note',
                  'actor', 'decided_at', 'deletion_authorized', 'source_roots', 'archive_unit',
                  'archive_object_id', 'retained_archive_object_id', 'destination_unit', 'proposed_action', 'same_photo_ids', 'metadata_reviewed']
        if any('metadata_proposal' in row for row in rows.values()):
            fields.append('metadata_proposal')
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for oid, obj in self.objects.items():
            row = rows[oid]
            context = self.selection_context(oid, row)
            for candidate in obj['candidates']:
                cid = candidate['id']
                state = self.disposition(row, cid, bool(candidate.get('reference_object_id')))
                others = [b if a == cid else a for a, b in row['different_pairs'] if cid in (a, b)]
                for order, member in enumerate(candidate['members'], 1):
                    writer.writerow({**({'metadata_proposal': json.dumps(row.get('metadata_proposal'), ensure_ascii=False, sort_keys=True)} if 'metadata_proposal' in fields else {}), 'object_id': oid, 'candidate_id': cid, 'member_order': order,
                                     'source_path': member['path'], 'size_bytes': member['size_bytes'],
                                     'mtime_ns': member['mtime_ns'], 'sha256': member.get('sha256', ''),
                                     'disposition': state, 'retained_master_id': row['selected_candidate_id']
                                     if state in ('selected_master', 'redundant') else '',
                                     'different_from_ids': json.dumps(sorted(others)), 'note': row['note'],
                                     'actor': row['actor'], 'decided_at': row['decided_at'], 'deletion_authorized': 'false',
                                     'source_roots': json.dumps(candidate.get('source_roots', [])),
                                     'archive_unit': candidate.get('archive_unit', ''),
                                     'archive_object_id': candidate.get('reference_object_id', context['archive_object_id']),
                                     'retained_archive_object_id': context['archive_object_id'], 'destination_unit': context['destination_unit'],
                                     'proposed_action': context['action'], 'metadata_reviewed': row.get('metadata_reviewed', False),
                                     'same_photo_ids': json.dumps(sorted(self.same_component(row, cid) - {cid}))})
        return out.getvalue().encode('utf-8')

    def model_proposals(self):
        """Optional research sidecar; never a metadata or decision authority."""
        try:
            raw = read_bytes(self.root / MODEL_PROPOSALS, optional=True)
            key = (raw, self.input_bytes)
            if getattr(self, '_proposal_key', None) == key:
                return self._proposal_values
            records, warning = {}, ''
            if raw:
                data = json.loads(raw)
                require(data.get('format') == 'archive-model-proposals-1', 'Unsupported model proposals')
                require(data.get('metadata_adopted') is False, 'Model proposals must remain unadopted')
                skipped = 0
                for record in data['records']:
                    oid, cid = record['object_id'], record['candidate_id']
                    require(oid not in records, 'Duplicate model proposal')
                    candidate = self.candidates.get(oid, {}).get(cid, {})
                    bound = any(m['path'] == record['source_path'] and
                                all(m[k] == record['source_stamp'][k] for k in ('size_bytes', 'mtime_ns'))
                                for m in candidate.get('members', []))
                    if not bound:
                        skipped += 1
                        continue
                    require(isinstance(record['result'], dict), 'Invalid model result')
                    record['suggested_fields'] = proposal_fields(record)
                    records[oid] = record
                if skipped:
                    warning = f'{skipped} AI proposals omitted: their source bindings no longer match the prepared evidence.'
            self._proposal_key, self._proposal_values = key, (records, warning)
            return self._proposal_values
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {}, f'AI proposals unavailable: {error}'

    def snapshot(self, oid=None):
        with self.lock:
            self.load()
            rows, revision = self.read_decisions()
            proposals, proposal_warning = self.model_proposals()
            if oid is not None:
                require(oid in self.objects, 'Unknown object')
                obj = deepcopy(self.objects[oid])
                for candidate in obj['candidates']:
                    candidate['problems'] = self.problems(candidate)
                proposal = deepcopy(proposals.get(oid))
                if proposal:
                    proposal['source_problems'] = next(c['problems'] for c in obj['candidates']
                                                       if c['id'] == proposal['candidate_id'])
                scope = self.scope_context(oid, rows[oid])
                for candidate in obj['candidates']:
                    candidate['active_comparison'] = not self.discovery or not candidate.get('reference_object_id') or candidate['id'] in scope['active_reference_ids']
                    candidate['tiff_recovery_source'] = self.discovery and bool(scope['tiff_reference_ids']) and not candidate.get('reference_object_id') and genuine_tiff(candidate)
                available = {c['id'] for c in obj['candidates'] if not c['problems']}
                # Reuse negative decisions only while both compared inputs remain current.
                rejected = {tuple(sorted(p)) for p in rows[oid]['different_pairs'] if set(p) <= available}
                obj['proposed_matches'] = [m for m in obj.get('matches', [])
                                           if tuple(sorted((m['a'], m['b']))) not in rejected]
                return {'object': obj, 'decision': rows[oid], 'revision': revision, 'read_only': self.frozen or scope['closed'],
                        'research_routing': routing_for(proposal, rows[oid]) if proposal else None,
                        'research_route_options': [{'id':k,'label':v['label']} for k,v in ROUTES.items()],
                        'model_proposal': proposal, 'model_proposal_warning': proposal_warning,
                        'metadata_revision': self.metadata_revision(revision),
                        'archive_masters_fixed': self.fixed_archive,
                        'review_scope': self.config.get('review_scope', ''), 'scope': scope,
                        'archive_resolved': self.archive_resolved(oid, rows[oid]),
                        'selection_context': self.selection_context(oid, rows[oid])}
            records = []
            for key, obj in self.objects.items():
                row = rows[key]
                pending = sum(self.disposition(row, c['id'], bool(c.get('reference_object_id'))) == 'unresolved' for c in obj['candidates'])
                context = self.selection_context(key, row)
                resolved = self.archive_resolved(key, row)
                scope = self.scope_context(key, row)
                records.append({'id': key, 'title': obj.get('title', key), 'note': row['note'],
                                'research_route': routing_for(proposals[key], row)['primary'] if key in proposals else None,
                                'has_model_proposal': key in proposals,
                                'model_proposal_title': row.get('metadata_proposal', {}).get('fields', {}).get('title', proposals.get(key, {}).get('suggested_fields', {}).get('title')) or '',
                                'metadata_proposal_saved': bool(row.get('metadata_proposal')),
                                'selected': row['selected_candidate_id'], 'unresolved_count': pending,
                                'review_reason': obj.get('review_reason', ''),
                                'review_pending': scope['tiff_recovery_pending'] or not scope['closed'] and not resolved and bool(obj.get('review_required')) and not row.get('decided_at'),
                                'scope_closed': scope['closed'], 'review_kind': scope['kind'],
                                'tiff_recovery_pending': scope['tiff_recovery_pending'],
                                'archive_resolved': resolved,
                                'destination_pending': bool(self.destinations) and context['action'] == 'proposed_new_object' and not context['destination_unit'],
                                'identity_pending': context['action'] == 'archive_identity_review',
                                'separate_source_pending': any(self.disposition(row, cid) == 'different_photo'
                                    for cid in obj.get('source_candidate_ids', [])),
                                'metadata_pending': not scope['closed'] and not (self.discovery and scope['md_object_id']) and not resolved and bool(obj.get('metadata_review_required')) and not row.get('metadata_reviewed', False),
                                'metadata_known': bool(obj.get('metadata')) or any(c.get('metadata') for c in obj['candidates']),
                                'candidate_count': len(obj['candidates'])})
            return {'title': self.config.get('title', self.root.name), 'objects': records, 'revision': revision,
                    'research_route_options': [{'id':k,'label':v['label']} for k,v in ROUTES.items()],
                    'model_proposal_count': len(proposals), 'model_proposal_warning': proposal_warning,
                    'read_only': self.frozen, 'actor': self.actor,
                    'archive_masters_fixed': self.fixed_archive,
                    'review_scope': self.config.get('review_scope', ''),
                    'destination_units': self.config.get('destination_units', []),
                    'projection_agrees': read_bytes(self.root / CSV, optional=True) == self.projection(rows)}

    def metadata_revision(self, revision):
        return hashlib.sha256(revision.encode() + read_bytes(self.root / MODEL_PROPOSALS, optional=True)).hexdigest()

    def save_metadata(self, payload):
        require(isinstance(payload, dict) and {'object_id','revision','fields'} <= set(payload)
                and set(payload) <= {'object_id','revision','fields','research_routing'}, 'Invalid metadata request')
        fields = payload['fields']
        require(isinstance(fields, dict) and set(fields) == METADATA_FIELDS, 'Invalid metadata fields')
        require(all(isinstance(v, str) and len(v) <= 12000 for v in fields.values()), 'Metadata fields must be text, at most 12000 characters')
        with self.lock, file_lock(self.root / LOCK):
            self.load()
            rows, revision = self.read_decisions()
            oid = payload['object_id']
            require(isinstance(oid, str) and oid in rows, 'Unknown object')
            require(not self.frozen and not self.scope_context(oid, rows[oid])['closed'], 'Completed/read-only object: editing is disabled')
            require(payload['revision'] == self.metadata_revision(revision), 'Evidence or decisions changed; reload before saving')
            proposal = self.model_proposals()[0].get(oid)
            require(proposal is not None, 'No source-bound proposal available')
            for member in self.candidates[oid][proposal['candidate_id']]['members']:
                checked_member(member, self.roots)
            rows = deepcopy(rows)
            routing = validate_override(payload.get('research_routing', rows[oid].get('metadata_proposal',{}).get('research_routing')))
            rows[oid]['metadata_proposal'] = {'fields': fields, 'actor': self.actor,
                'saved_at': datetime.now(timezone.utc).isoformat(), 'metadata_adopted': False,
                'candidate_id': proposal['candidate_id'], 'source_path': proposal['source_path'],
                'source_stamp': deepcopy(proposal['source_stamp'])}
            if routing is not None: rows[oid]['metadata_proposal']['research_routing'] = routing
            self.load()
            require(not self.frozen and payload['revision'] == self.metadata_revision(self.read_decisions()[1]),
                    'Evidence or decisions changed; reload before saving')
            self.validate(rows)
            atomic_write(self.root / DECISIONS, b''.join(encoded(rows[key]) + b'\n' for key in self.objects))
            warning = ''
            try:
                atomic_write(self.root / CSV, self.projection(rows))
            except (OSError, ValueError) as error:
                warning = f'Metadata saved. CSV needs rebuilding: {error}'
            result = self.snapshot(oid)
            result['warning'] = warning
            return result

    def save(self, payload):
        require(isinstance(payload, dict) and LEGACY_EDIT_FIELDS | {'object_id', 'revision'} <= set(payload)
                and set(payload) <= EDIT_FIELDS | {'object_id', 'revision'}, 'Invalid decision fields')
        work_root(self.root)
        with self.lock, file_lock(self.root / LOCK):
            self.load()
            require(not self.frozen, 'Completed/read-only unit: editing is disabled')
            plain_file(self.root / CSV)
            rows, revision = self.read_decisions()
            rows = dict(rows)
            require(payload['revision'] == revision, 'Evidence or decisions changed; reload before saving')
            oid = payload['object_id']
            require(isinstance(oid, str) and oid in rows, 'Unknown object')
            require(not self.scope_context(oid, rows[oid])['closed'], 'Existing archive object is outside new-object discovery')
            previous_row = rows[oid]
            row = self.blank(oid)
            if 'metadata_proposal' in previous_row:
                row['metadata_proposal'] = deepcopy(previous_row['metadata_proposal'])
            row.update({key: deepcopy(payload.get(key, previous_row.get(key, row[key]))) for key in EDIT_FIELDS})
            row['previous_master_ids'] = list(previous_row['previous_master_ids'])
            prior = previous_row['selected_candidate_id']
            # Reject malformed user input before expanding a confirmed group;
            # normalization must never hide invalid or contradictory choices.
            rows[oid] = row
            self.validate({oid: row})
            if prior and prior != row['selected_candidate_id'] and row['same_pairs'] == previous_row.get('same_pairs', []):
                # Older clients clear redundant_candidate_ids on Master changes.
                # Carry their confirmed-copy edges forward unless the reviewer
                # changed the same-photo decisions in this save.
                row['same_pairs'] = deepcopy(row['same_pairs'])
                known_pairs = {tuple(sorted(pair)) for pair in row['same_pairs']}
                for cid in previous_row['redundant_candidate_ids']:
                    pair = tuple(sorted((prior, cid)))
                    if pair not in known_pairs:
                        row['same_pairs'].append(list(pair))
                        known_pairs.add(pair)
            selected = row['selected_candidate_id']
            if selected:
                row['redundant_candidate_ids'] = sorted(self.same_component(row, selected) - {selected})
            else:
                row['redundant_candidate_ids'] = []
            self.retain_archive(oid, row)
            if prior and prior != row['selected_candidate_id'] and prior not in row['previous_master_ids']:
                row['previous_master_ids'].append(prior)
            row.update(actor=self.actor, decided_at=datetime.now(timezone.utc).isoformat())
            rows[oid] = row
            self.validate(rows)
            involved = set(row['redundant_candidate_ids'])
            if row['selected_candidate_id']:
                involved.add(row['selected_candidate_id'])
            involved.update(c for pair in row['different_pairs'] for c in pair)
            involved.update(c for pair in row['same_pairs'] for c in pair)
            for cid in involved:
                for member in self.candidates[oid][cid]['members']:
                    checked_member(member, self.roots)
            # Recheck preparation and completion after source inspection.
            self.load()
            require(not self.frozen, 'Completed/read-only unit: editing is disabled')
            require(self.read_decisions()[1] == revision, 'Evidence or decisions changed; reload before saving')
            self.validate(rows)
            atomic_write(self.root / DECISIONS, b''.join(encoded(rows[key]) + b'\n' for key in self.objects))
            warning = ''
            try:
                atomic_write(self.root / CSV, self.projection(rows))
            except (OSError, ValueError) as error:
                warning = f'Decisions saved. CSV needs rebuilding: {error}'
            result = self.snapshot(oid)
            result['warning'] = warning
            return result

    def rebuild_csv(self):
        work_root(self.root)
        with self.lock, file_lock(self.root / LOCK):
            self.load()
            require(not self.frozen, 'Completed/read-only unit: editing is disabled')
            rows, _ = self.read_decisions()
            atomic_write(self.root / CSV, self.projection(rows))

    def display_recipe(self, member, page):
        """Reuse checked research views only for the exact source bytes and page."""
        for record in self.model_proposals()[0].values():
            recipe = record.get('preview_recipe', {})
            if (record['source_path'] == member['path']
                    and all(record['source_stamp'][k] == member[k] for k in ('size_bytes', 'mtime_ns'))
                    and recipe.get('visual_checked') is True
                    and recipe.get('page') == page - 1
                    and recipe.get('polarity') in ('unchanged', 'inverted research view')
                    and recipe.get('rotation', 0) in (0, 90, -90, 180, 270)):
                return recipe
        return None

    def media(self, oid, cid, member_index, page, edge, text=False):
        with self.lock:
            self.load()
            require(oid in self.candidates and cid in self.candidates[oid], 'Unknown candidate')
            members = self.candidates[oid][cid]['members']
            require(0 <= member_index < len(members), 'Invalid member number')
            member = members[member_index]
            require(1 <= page <= member['pages'] and edge in (1200, 3200), 'Invalid page or preview size')
            path = checked_member(member, self.roots)
            result = preview(path, page, edge, text)
            recipe = None if text else self.display_recipe(member, page)
            if recipe and (recipe['polarity'] == 'inverted research view' or recipe.get('rotation', 0)):
                from PIL import Image, ImageOps
                with Image.open(io.BytesIO(result[0])) as image:
                    image = image.convert('RGB')
                    if recipe['polarity'] == 'inverted research view':
                        image = ImageOps.invert(image)
                    image = image.rotate(recipe.get('rotation', 0), expand=True)
                    output = io.BytesIO()
                    image.save(output, format='JPEG', quality=90)
                    result = output.getvalue(), 'image/jpeg'
            checked_member(member, self.roots)
            return result


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        require(address[0] == '127.0.0.1', 'Workbench binds only to localhost')
        super().__init__(address, Handler)
        self.app = app
        self.token = secrets.token_urlsafe(32)
        self.hosts = {f'127.0.0.1:{self.server_port}', f'localhost:{self.server_port}'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, body, mime='application/json; charset=utf-8', status=200):
        if not isinstance(body, bytes):
            body = encoded(body)
        self.send_response(status)
        for key, value in {'Content-Type': mime, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
                           'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                           'Content-Security-Policy': "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get('Host') not in self.server.hosts:
            return self.send({'error': 'Local workbench host required'}, status=403)
        route = urlsplit(self.path)
        query = parse_qs(route.query)
        get = lambda name, default='': query.get(name, [default])[0]
        try:
            if route.path in ('/', '/workbench.js', '/workbench.css'):
                name, mime = ('workbench.html', 'text/html') if route.path == '/' else (
                    route.path[1:], 'text/javascript' if route.path.endswith('.js') else 'text/css')
                self.send((HERE / name).read_bytes(), mime + '; charset=utf-8')
            elif route.path == '/api/overview':
                result = self.server.app.snapshot()
                self.send({**result, 'token': self.server.token})
            elif route.path == '/api/object':
                self.send(self.server.app.snapshot(get('id')))
            elif route.path in ('/media', '/text'):
                body, mime = self.server.app.media(get('object'), get('candidate'), int(get('member', '0')),
                                                 int(get('page', '1')), int(get('edge', '1200')), route.path == '/text')
                self.send(body, mime)
            else:
                self.send({'error': 'Unknown route'}, status=404)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
            self.send({'error': str(error)}, status=400)

    def do_POST(self):
        host = self.headers.get('Host')
        if (host not in self.server.hosts or self.headers.get('Origin') != f'http://{host}' or
                not secrets.compare_digest(self.headers.get('X-Review-Token', ''), self.server.token)):
            return self.send({'error': 'Use the local workbench page to save'}, status=403)
        if self.path not in ('/api/decision', '/api/metadata'):
            return self.send({'error': 'Unknown route'}, status=404)
        try:
            require(self.headers.get('Content-Type') == 'application/json', 'Expected JSON')
            size = int(self.headers.get('Content-Length', '0'))
            require(0 < size <= 1000000 and not self.headers.get('Transfer-Encoding'), 'Invalid request size')
            payload = json.loads(self.rfile.read(size))
            self.send(self.server.app.save_metadata(payload) if self.path == '/api/metadata' else self.server.app.save(payload))
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
            self.send({'error': str(error)}, status=409)


def create_demo(value):
    """Only synthetic files, in a deliberately requested new/empty directory."""
    root = work_root(value, must_exist=False)
    require(not root.exists() or root.is_dir() and not any(root.iterdir()), 'Demo requires a new or empty directory')
    root.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageDraw
    full = Image.new('RGB', (1400, 1000), '#d8d0b6')
    draw = ImageDraw.Draw(full)
    draw.rectangle((100, 300, 1300, 900), fill='#657677')
    for x in range(160, 1200, 160):
        draw.rectangle((x, 380, x + 90, 650), fill='#c2d3cd')
    draw.polygon([(100, 300), (350, 160), (600, 300), (850, 160), (1100, 300)], fill='#425658')
    draw.text((70, 50), 'SYNTHETIC FACTORY / full source', fill='#223637', font_size=36)
    full.save(root / 'factory.tiff')
    full.crop((100, 170, 1300, 900)).resize((600, 365)).save(root / 'factory-crop.jpg')
    other = Image.new('RGB', (1400, 1000), '#d8d0b6')
    draw = ImageDraw.Draw(other)
    draw.ellipse((200, 160, 1000, 860), fill='#657677')
    draw.text((70, 50), 'SYNTHETIC OTHER PHOTO / false match', fill='#223637', font_size=36)
    other.save(root / 'other.jpg')
    full.save(root / 'pages.tiff', save_all=True, append_images=[other])
    other.save(root / 'document.pdf', save_all=True, append_images=[full])

    def candidate(cid, name, label, pages=1):
        p = root / name
        stat = p.stat()
        return {'id': cid, 'label': label, 'eligible': True, 'complete': True,
                'members': [{'path': str(p), 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'pages': pages}]}

    data = {'format': FORMAT, 'title': 'Archive tools · synthetic demonstration', 'read_only': False,
            'read_roots': [], 'completion_receipts': [], 'objects': [
                {'id': 'demo-photo', 'title': 'Factory photograph', 'metadata': {},
                 'candidates': [candidate('full', 'factory.tiff', 'Full-resolution TIFF'),
                                candidate('crop', 'factory-crop.jpg', 'Resized crop'),
                                candidate('other', 'other.jpg', 'Different photograph')],
                 'matches': [{'a': 'full', 'b': 'other', 'algorithm': 'pHash', 'distance': 2,
                              'profile': 'synthetic-example-not-a-measurement',
                              'note': 'Illustrative low-distance false positive; no real hash was computed.'}]},
                {'id': 'demo-pages', 'title': 'Two-page TIFF', 'metadata': {},
                 'candidates': [candidate('pages', 'pages.tiff', 'Ordered TIFF pages', pages=2)]},
                {'id': 'demo-pdf', 'title': 'Two-page PDF', 'metadata': {},
                 'candidates': [candidate('pdf', 'document.pdf', 'Complete PDF', pages=2)]}]}
    atomic_write(root / INPUT, json.dumps(data, ensure_ascii=False, indent=2).encode())
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--actor', default='local reviewer')
    parser.add_argument('--port', type=int, default=8782)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true', help='Validate evidence and decisions without writing')
    actions.add_argument('--rebuild-csv', action='store_true', help='Rebuild derived CSV from readable decisions')
    actions.add_argument('--demo', action='store_true', help='Create synthetic inputs in a new/empty work root and serve')
    args = parser.parse_args()
    try:
        if args.demo:
            create_demo(args.work_root)
        app = Workbench(args.work_root, args.actor)
        if args.rebuild_csv:
            app.rebuild_csv()
        if args.check or args.rebuild_csv:
            problems = {oid: {cid: issues for cid, c in candidates.items() if (issues := app.problems(c))}
                        for oid, candidates in app.candidates.items()}
            problems = {oid: values for oid, values in problems.items() if values}
            print(json.dumps({'status': 'unavailable_or_stale_sources' if problems else 'ok',
                              'source_problems': problems, **app.snapshot()}, ensure_ascii=False, indent=2))
            return 1 if problems else 0
        server = Server(('127.0.0.1', args.port), app)
        print(f'Archive workbench: http://127.0.0.1:{server.server_port}/ (Ctrl-C to stop)', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(2, f'workbench: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
