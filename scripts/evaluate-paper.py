#!/usr/bin/env python3
"""Evaluate a private entry-snapshot export locally. Never opens a wallet or changes the application.

Laya is optional and installed in a separate research environment, never requirements.lock.
Use a reviewed, pinned local model directory; no model downloads are started by this script.
"""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wormhole import paper_research as research


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--provider', choices=('rules', 'laya', 'recorded'), default='rules')
    p.add_argument('--model-path', type=Path)
    args = p.parse_args()
    data = json.loads(args.input.read_text())
    rows = data['rows']
    model_info = None
    model = None
    if args.provider == 'laya':
        if not args.model_path or not args.model_path.is_dir():
            p.error('Laya requires a reviewed local --model-path')
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        from laya import Agent
        model_info = {'package_version': version('laya'), 'files': {}}
        for name in ('model.safetensors', 'rl_agent_config.json', 'encoder/config.json', 'tokenizer/tokenizer.json'):
            with (args.model_path / name).open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest()
            model_info['files'][name] = digest
        model = Agent(str(args.model_path.resolve()), device='cpu')
    predictions = {}
    for row in rows:
        started = time.monotonic()
        if args.provider == 'recorded':
            result = dict(row.get('entry_shadow') or {'decision': 'abstain', 'missing': ['recorded decision']})
        elif model is None:
            result = research.risk_filter(row)
        else:
            text = research.state(row)
            # Reserve the runtime's entire question budget. Reject rather than truncate entry evidence.
            if len(model.tok(text, add_special_tokens=False)['input_ids']) + 196 > model.cfg.get('max_len', 512):
                result = {'decision': 'abstain', 'error': 'input exceeds model context budget'}
            else:
                result = research.laya_decision(model.predict(text, research.QUESTION))
        result['elapsed_s'] = round(time.monotonic()-started, 4)
        predictions[str(row['id'])] = result
    training, holdout, purged = research.chronological_split(rows)
    output = {'provider': args.provider, 'input_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest(),
              'created_at': time.time(), 'model': model_info,
              'question_sha256': hashlib.sha256(json.dumps(research.QUESTION, sort_keys=True).encode()).hexdigest(),
              'predictions': predictions, 'report': research.report(rows, predictions),
              'chronological_diagnostic': {'training_n': len(training), 'holdout_n': len(holdout), 'purged_n': purged,
                                           'report': research.report(holdout, predictions)}}
    # Private research artifacts must not be placed in the publication tree.
    root = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(root):
        p.error('write research output outside the repository')
    with args.output.open('x') as f:
        json.dump(output, f, indent=2, allow_nan=False)
    print(json.dumps(output['report'], indent=2))


if __name__ == '__main__':
    main()
