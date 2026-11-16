"""Import reusable decisions and relationships without promoting similarity to identity."""
from collections import defaultdict
import json
from pathlib import Path

from archive_common import plain_file, require
from catalogues import digest, md_identity
from matching import stamp


def jsonl(path):
    plain_file(Path(path))
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def failed_historical_lead(lead):
    evidence = lead.get('evidence', {})
    return lead.get('engine') == 'prior_similarity' and any(
        evidence.get(key) is False for key in ('passes_orientation_screen', 'passes_secondary_checks'))


def active_comparison(leads):
    """Admission to the working comparison list; never an identity decision."""
    if any(m.get('engine') == 'confirmed' or m.get('engine') in ('pixel_coverage', 'photo_identity')
           and m.get('status') == 'verified' for m in leads):
        return True
    sift = [m for m in leads if m.get('engine') == 'sift']
    if any(m.get('status') == 'supported' for m in sift):
        return True
    if sift and all(m.get('status') == 'not_supported' for m in sift):
        return False
    return any(not failed_historical_lead(m) and m.get('engine') not in
               ('sift', 'pixel_coverage', 'photo_identity') and
               m.get('basis') != 'previous_pixel_candidate' for m in leads)


def assigned_md_id(proposals):
    """An explicit source-to-md metadata binding is not a same-image assertion."""
    identities = set()
    for proposal in proposals:
        record = proposal.get('record', {})
        if record.get('metadata_assignment_confirmed') is not True:
            continue
        identity = proposal.get('assigned_object_id', '')
        if not md_identity(identity) and record.get('action') == 'assign_source':
            # An explicitly assigned historical md identity may have no current
            # selected Master (for example, an alias with conflicting imagery).
            # Preserve that identity; master_key is only the old comparison.
            key = record.get('object_record_key', '')
            metadata_key = record.get('metadata_key', key)
            unit = proposal.get('destination_unit', '')
            if unit and md_identity(key) and key == metadata_key and '#' not in key:
                identity = unit + '#' + key
        if md_identity(identity):
            identities.add(identity)
    return next(iter(identities)) if len(identities) == 1 else ''


class Evidence:
    def __init__(self, sources, targets):
        self.sources = {s['path']: s for s in sources}
        self.targets = {t['path']: t for t in targets}
        self.targets_by_id = {t['id']: t for t in targets}
        self.same = []                  # explicit source/source same-photo relationships
        self.confirmed = defaultdict(list)
        self.rejected = set()           # (source path, target id)
        self.different_sources = set()
        self.leads = defaultdict(list)
        self.context = defaultdict(list)
        self.proposals = {}
        self.issues = defaultdict(list)
        self.input_stamps = {}
        self.stats = defaultdict(int)
        self.reviewed_metadata = {}

    def read(self, path):
        self.input_stamps[str(path)] = stamp(path)
        return jsonl(path)

    def current(self, member):
        item = self.sources.get(member.get('path')) or self.targets.get(member.get('path'))
        if item and all(member.get(k) == item['stamp'][k] for k in ('size_bytes', 'mtime_ns')):
            if member.get('sha256') and item.get('sha256') and digest(member['sha256']) != item['sha256']:
                return None
            return item
        return None

    def relation(self, left, right, kind, basis):
        a, b = self.current(left), self.current(right)
        if not a or not b:
            self.stats['stale_or_out_of_scope_relationships'] += 1
            return
        if a['path'] in self.targets and b['path'] in self.sources:
            a, b = b, a
            if kind == 'covered_by':
                kind = 'same_photo'  # The source was retained; the archive copy was the redundant side.
        if a['path'] not in self.sources or a['path'] == b['path']:
            return
        if b['path'] in self.sources:
            pair = tuple(sorted((a['path'], b['path'])))
            if kind == 'different_photo':
                self.different_sources.add(pair)
            else:
                self.same.append((a['path'], b['path'], basis))
        elif kind == 'different_photo':
            self.rejected.add((a['path'], b['id']))
        else:
            self.confirmed[a['path']].append({'target_id': b['id'], 'engine': 'confirmed',
                                              'adequacy_confirmed': kind == 'covered_by', **basis})
        self.stats['current_relationships'] += 1

    def import_relations(self, path):
        for row in self.read(path):
            require(row.get('format') == 'archive-tools-relation-1' and row.get('confirmed') is True,
                    'Relationships must explicitly identify their confirmed evidence')
            require(row.get('relation') in ('same_photo', 'covered_by', 'different_photo'), 'Unknown relationship')
            require(bool(row.get('basis')), 'A confirmed relationship needs its evidence basis')
            self.relation(row['source'], row['other'], row['relation'],
                          {'basis': row['basis'], 'evidence_file': str(path), 'evidence_record': row})

    def reuse_md_assignments(self, obj):
        members = {c['members'][0]['path']: c['members'][0] for c in obj['candidates']
                   if not c.get('reference_object_id') and len(c['members']) == 1}
        for proposal in obj.get('previous_proposals', []):
            source = proposal.get('selected_path')
            if source in members and self.current(members[source]) and assigned_md_id([proposal]):
                self.proposals.setdefault(source, proposal)

    def import_workbench(self, path):
        for row in self.read(path):
            obj = row.get('input_object')
            require(isinstance(obj, dict), 'Prior decisions must include their prepared input_object')
            self.reuse_md_assignments(obj)
            if not row.get('decided_at'):
                continue  # A preparation recommendation is not a human decision.
            members = {c['id']: c['members'][0] for c in obj['candidates'] if len(c['members']) == 1}
            basis = {'basis': 'saved_human_review', 'evidence_file': str(path),
                     'actor': row.get('actor'), 'decided_at': row.get('decided_at')}
            selected = row.get('selected_candidate_id')
            for cid in row.get('redundant_candidate_ids', []):
                if cid in members and selected in members:
                    self.relation(members[cid], members[selected], 'covered_by', basis)
                    a, b = self.current(members[cid]), self.current(members[selected])
                    if row.get('metadata_reviewed') and a and b and a['path'] in self.sources and b['path'] in self.targets:
                        by_id = {c['id']:c for c in obj['candidates']}
                        self.reviewed_metadata[(a['path'], b['id'])] = (by_id[cid].get('metadata', {}), by_id[selected].get('metadata', {}))
            for a, b in row.get('same_pairs', []):
                if a in members and b in members:
                    self.relation(members[a], members[b], 'same_photo', basis)
            for a, b in row.get('different_pairs', []):
                if a in members and b in members:
                    self.relation(members[a], members[b], 'different_photo', basis)
            if selected in members and members[selected]['path'] in self.sources and self.current(members[selected]):
                source = members[selected]['path']
                self.proposals[source] = {'selected_path': source, 'destination_unit': row.get('destination_unit', ''),
                                          'kind': obj.get('review_reason', 'saved_master_proposal'),
                                          'record': {k:row.get(k) for k in ('object_id','note','actor','decided_at','metadata_reviewed')}, 'basis': basis}
            for member in members.values():
                if member['path'] in self.sources and row.get('decided_at'):
                    self.context[member['path']].append({'file': str(path), 'record': {k:row.get(k) for k in
                        ('object_id','note','actor','decided_at','metadata_reviewed','destination_unit')}})

    def import_preparation(self, root):
        """Reuse candidate evidence only if both compared file versions still match."""
        path = root / 'matches.jsonl'
        summary = root / 'preparation-summary.json'
        if not path.exists() or not summary.exists():
            return
        self.input_stamps[str(summary)] = stamp(summary)
        info = json.loads(summary.read_text())
        old_unit = info.get('against_unit', '')
        old_input = root / 'workbench-input.json'
        old_targets = {}
        if old_input.exists():
            self.input_stamps[str(old_input)] = stamp(old_input)
            for obj in json.loads(old_input.read_text())['objects']:
                self.reuse_md_assignments(obj)
                for c in obj['candidates']:
                    if c.get('reference_object_id') and len(c['members']) == 1:
                        old_targets[c['id'].removeprefix('archive:')] = c['members'][0]
        for row in self.read(path):
            source = self.sources.get(row['source_path'])
            if not source or source['stamp'] != row['source_stamp']:
                continue
            for old in row.get('matches', []):
                member = old_targets.get(old['target_id'])
                target = self.current(member) if member else None
                if target and target['path'] in self.targets:
                    # Explicit confirmations are imported from decisions/relations, never from search results.
                    if old.get('engine') == 'confirmed':
                        continue
                    self.leads[source['path']].append({**old, 'target_id': target['id'], 'reused_from': str(path)})
                    self.stats['reused_search_leads'] += 1

    def import_legacy_avantmedia(self, root):
        """Explicit adapter for the existing TIFF consolidation, including unresolved replacements.

        A legacy assign_source decision changes the proposed metadata/Master binding;
        it does NOT assert that the displaced archive image is the same photograph.
        """
        summary_path = root / 'tiff-scan-summary.json'
        if not summary_path.exists():
            return
        self.input_stamps[str(summary_path)] = stamp(summary_path)
        info = json.loads(summary_path.read_text())
        unit = str(Path(info['unit']))
        masters = {r['record_key']: r for r in self.read(root / 'tiff-scan-masters.jsonl')}
        current_masters = {}
        for key, old in masters.items():
            member = {'path': old['master_path'], **old['master_stamp'], 'sha256': old.get('stored_final_sha256', '')}
            target = self.current(member)
            if target and target['path'] in self.targets and target['unit'] == unit:
                current_masters[key] = target
        for row in self.read(root / 'tiff-scan-results.jsonl'):
            source = self.sources.get(row['source_path'])
            if not source or source['stamp'] != row['source_stamp']:
                self.stats['stale_or_out_of_scope_legacy_sources'] += 1
                continue
            review = row['finding'] in ('possible_new_kurt_object', 'known_object_image_gap',
                                        'prior_alias_image_conflict', 'unreadable_tiff') or row.get('metadata_link_differs_from_image_match')
            self.context[source['path']].append({'file': str(root / 'tiff-scan-results.jsonl'),
                                                'prior_review_required': bool(review),
                                                'finding': row['finding'], 'note': row.get('review_note', '')})
            self.stats['legacy_sources'] += 1
            self.stats['legacy_examination_cases'] += bool(review)
            for field in ('strong_matches', 'orientation_matches', 'residual_pixel_matches'):
                for match in row.get(field, []):
                    target = current_masters.get(match.get('record_key'))
                    if target:
                        lead = {'target_id': target['id'], 'engine': 'prior_similarity',
                                'basis': field, 'evidence': match,
                                'reused_from': str(root / 'tiff-scan-results.jsonl')}
                        if failed_historical_lead(lead):
                            self.context[source['path']].append({'previous_comparison': lead,
                                'note': 'Failed historical screening; retained as evidence, not an active suggestion'})
                            continue
                        self.leads[source['path']].append({'target_id': target['id'], 'engine': 'prior_similarity',
                                                          'basis': field, 'evidence': match,
                                                          'reused_from': str(root / 'tiff-scan-results.jsonl')})
                        self.stats['reused_search_leads'] += 1
        decision_file = root / 'tiff-review-decisions.jsonl'
        if decision_file.exists():
            for row in self.read(decision_file):
                source = self.sources.get(row['source_path'])
                if not source or source['stamp'] != row['source_stamp']:
                    continue
                self.stats['legacy_saved_decisions'] += 1
                self.context[source['path']].append({'file': str(decision_file), 'record': row})
                action = row.get('action')
                target = current_masters.get(row.get('object_record_key') or row.get('master_key'))
                if action == 'confirm_existing' and target:
                    self.confirmed[source['path']].append({'target_id': target['id'], 'engine': 'confirmed',
                        'adequacy_confirmed': True, 'basis': 'legacy_human_confirmed_existing_master',
                        'evidence_file': str(decision_file), 'evidence_record': row})
                elif action in ('new_object', 'assign_source'):
                    self.proposals[source['path']] = {'kind': 'proposed_new_object' if action == 'new_object' else 'master_assignment_review',
                        'selected_path': source['path'], 'destination_unit': unit,
                        'assigned_object_id': target['id'] if action == 'assign_source' and target else '',
                        'record': row, 'basis': {'basis': 'legacy_saved_master_proposal', 'evidence_file': str(decision_file)}}
                    proposal = self.proposals[source['path']]
                    proposal['assigned_object_id'] = assigned_md_id([proposal]) or proposal['assigned_object_id']
                elif action == 'redundant':
                    # Old "reject" decisions can lack a retained Master; preserve them without inventing coverage.
                    self.issues[source['path']].append('Earlier redundant/rejected proposal needs an explicit retained Master')
        research = root / 'new-object-metadata-proposals.jsonl'
        if research.exists():
            for row in self.read(research):
                if row.get('source_path') in self.sources:
                    self.context[row['source_path']].append({'file': str(research), 'record': row})

    def import_review(self, spec):
        spec = Path(spec)
        if spec.is_file():
            self.import_workbench(spec)
        else:
            if (spec / 'decisions.jsonl').exists():
                self.import_workbench(spec / 'decisions.jsonl')
            self.import_preparation(spec)
            self.import_legacy_avantmedia(spec)

    def groups(self):
        parent = {p: p for p in self.sources}
        def find(p):
            while parent[p] != p:
                parent[p] = parent[parent[p]]; p = parent[p]
            return p
        by_target = defaultdict(list)
        for path, links in self.confirmed.items():
            if any((path, x['target_id']) in self.rejected for x in links):
                self.issues[path].append('Conflicting positive and negative archive identity decisions')
                self.confirmed[path] = []
            else:
                for link in links:
                    by_target[link['target_id']].append(path)
        for group in by_target.values():
            for path in group[1:]:
                parent[find(path)] = find(group[0])
        for a, b, _ in self.same:
            parent[find(b)] = find(a)
        conflicts = {find(a) for a, b in self.different_sources if find(a) == find(b)}
        grouped = defaultdict(list)
        for path, source in self.sources.items():
            key = find(path)
            if key in conflicts:
                self.issues[path].append('Conflicting same-photo and different-photo evidence; grouping withheld')
                key = path
            grouped[key].append(source)
        return list(grouped.values())
