#!/usr/bin/env python3
"""Prepare one bounded local evidence packet; never submit it to an external API."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from archive_research.evidence import build_packet
from archive_research.sources import Roots
from archive_research.storage import lock, write_json
from research_routes import research_profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roots', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--work', default='artifacts')
    parser.add_argument('--output', required=True, help='Flat filename in the existing work root')
    parser.add_argument('--prompt-output', help='Optional flat filename for a local multimodal assessment draft')
    args = parser.parse_args()
    roots = Roots.from_toml(args.roots)
    selection = json.loads(args.selection.read_text())
    output = roots.root(args.work, 'work')
    if args.prompt_output:
        from archive_research.contracts import require
        from archive_research.storage import target
        require(args.prompt_output != args.output, 'Packet and prompt filenames must differ')
        target(output, args.prompt_output)
    with lock(output):
        packet = build_packet(roots, args.work, selection, research_profiles(selection['profiles']))
        if args.prompt_output:
            from archive_research.assessment import prepare_assessment
            draft = prepare_assessment(roots, args.work, packet, research_profiles(selection['profiles']))
            write_json(output, args.prompt_output, draft)
        path = write_json(output, args.output, packet)
    print(json.dumps({'path': str(path), 'evidence': len(packet['evidence']),
                      'available': sum(e['available'] for e in packet['evidence']),
                      'external_submission_authorized': False}))


if __name__ == '__main__':
    main()
