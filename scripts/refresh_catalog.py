#!/usr/bin/env python3
"""Refresh the bundled NVIDIA free-endpoint model catalog.

Scrapes the server-rendered model cards on https://build.nvidia.com/models and
writes ``data/nvidia_catalog.json``. Standard library only — no key required
(the catalog pages are public).

Building the catalog is deliberately separated from ``radar.py``:

* the **catalog** records what NVIDIA *publishes about a model* (is it served on
  a free endpoint, is it a chat LLM, how big is it) and changes slowly;
* the **scan** in ``radar.py`` records what the *inference API* currently
  serves, and changes whenever NVIDIA adds or retires an endpoint.

A model is only usable when both agree. Refreshing one never silently authorises
the other.

Hand-maintained facts live in the ``overrides`` block of the catalog and are
preserved across refreshes — edit them there, not in the generated ``models``
block, which is overwritten.
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / 'data' / 'nvidia_catalog.json'
SOURCE = 'https://build.nvidia.com/models'
USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')

# Classification runs on the *model id only*, with punctuation stripped and
# matched by substring (see classify()). Descriptions are never used for the
# exclusion test: a chat card that merely says "accepting text and images"
# would otherwise be demoted to an image model.
#
# Two ordering rules matter:
#   * non-chat families are tested FIRST, so `nemotron-3-embed-1b` stays an
#     embedding despite "nemotron" being a chat family;
#   * `vision` deliberately holds only truly vision-only families (neva, vila,
#     paligemma, ...). A vision-*instruct* model is still a chat LLM.
NON_CHAT = {
    'embedding': ('embed', 'embedqa', 'embedcode', 'rerank', 'retriever',
                  'nemoretriever', 'arctic', 'bge', 'nvembed'),
    'speech': ('asr', 'parakeet', 'whisper', 'canary', 'conformer', 'tts', 'magpie',
               'voicechat', 'studiovoice', 'audioshield'),
    'translation': ('translate', 'nmt', 'megatron'),
    'safety': ('guard', 'safety', 'jailbreak', 'nemoguard'),
    'image': ('flux', 'diffusion', 'vsr', 'relighting', 'bnr', 'trellis', 'nvclip',
              'eyecontact', 'lipsync', 'imageedit', 'sdxl', 'detector', 'image'),
    'vision': ('neva', 'vila', 'deplot', 'kosmos', 'fuyu', 'paddleocr', 'ocr',
               'paligemma', 'nemotronparse', 'ising', 'calibration', 'graphicelements',
               'pageelements', 'tablestructure'),
    'biology': ('evo', 'protein', 'rfdiffusion', 'diffdock', 'boltz', 'openfold',
                'msa', 'genmol', 'molmim', 'fourcastnet', 'genomics'),
    'physical': ('cosmos', 'sparsedrive', 'bevformer', 'bodypose', 'speaker',
                 'streampetr', 'petr', 'autonomous'),
    'tabular': ('kumo',),
}
# Substrings that mark a model id as a conversational / instruction-tuned LLM.
CHAT_FAMILIES = ('llama', 'gemma', 'mistral', 'mixtral', 'nemotron', 'glm', 'qwen',
                 'kimi', 'deepseek', 'gpt', 'oss', 'muse', 'jamba', 'dbrx', 'yi',
                 'sealion', 'zamba', 'phi', 'granite', 'palmyra', 'laguna', 'chatqa',
                 'minitron', 'starcoder', 'codegemma', 'codellama', 'codestral', 'nemo')
# "552B MoE", "~2.8T hybrid", "753B-parameter", "320B-total / 18B-active"
PARAM = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(T|B)(?![a-zA-Z])')


def stripped(model_id):
    return re.sub(r'[^a-z0-9]', '', model_id.lower())


def fetch(page):
    url = SOURCE + '?' + urllib.parse.urlencode(
        {'api-key': 'true', 'pageSize': 96, 'page': page})
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    # NOTE: do NOT add Accept / Accept-Language. build.nvidia.com answers any
    # request that adds those headers with a 202 anti-bot interstitial page
    # (2.4 KB, no model cards) instead of the catalog.
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode('utf-8', 'ignore')


def parse_page(raw):
    """Extract one entry per model card from the server-rendered HTML."""
    found = {}
    for card in raw.split('<li class="flex">')[1:]:
        link = re.search(r'href="/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)"', card)
        if not link:
            continue
        model_id = link.group(1)
        if model_id.split('/')[0] in ('explore', 'models', 'blueprints', 'skills', 'playbooks'):
            continue
        badges = re.findall(r'nv-badge[^>]*>([A-Za-z ]+)</span>', card)
        publisher = re.search(r'href="/([A-Za-z0-9_.\-]+)"[^>]*>([^<]+)</a>', card)
        desc = re.search(r'nv-text--label-regular-md[^>]*>([^<]*)</span>', card)
        found[model_id] = {
            'publisher': html.unescape(publisher.group(2)).strip() if publisher else model_id.split('/')[0],
            'free_endpoint': 'Free Endpoint' in badges,
            'downloadable': 'Downloadable' in badges,
            'description': html.unescape(desc.group(1)).strip() if desc else '',
        }
    return found


def parameters_b(model_id, description):
    """Total parameters in billions, or None when the card does not state them.

    Only the *first* size stated on the card counts (cards lead with total size,
    e.g. "552B MoE, 8B active params"). Nothing is inferred from the model ID or
    family name — an unstated size stays unknown on purpose.
    """
    match = PARAM.search(description or '')
    if not match:
        return None
    value = float(match.group(1))
    return value * 1000 if match.group(2) == 'T' else value


def classify(model_id, description):
    """Coarse capability class. ``chat`` is the only class probe-eligible.

    Decided from the model id alone. Anything the rules do not recognise stays
    ``unclassified`` and is never probed — a wrong "chat" costs quota on every
    run, a wrong "unclassified" costs one hand-edit in the catalog's
    ``overrides`` block.
    """
    blob = stripped(model_id)
    for kind, needles in NON_CHAT.items():
        if any(needle in blob for needle in needles):
            return kind
    if any(family in blob for family in CHAT_FAMILIES):
        return 'chat'
    return 'unclassified'


def scrape():
    models, page = {}, 1
    while page <= 12:
        page_models = parse_page(fetch(page))
        new = {k: v for k, v in page_models.items() if k not in models}
        sys.stderr.write('page %d: %d cards, %d new\n' % (page, len(page_models), len(new)))
        if not new:
            break
        models.update(new)
        page += 1
    for model_id, entry in models.items():
        entry['kind'] = classify(model_id, entry['description'])
        entry['parameters_b'] = parameters_b(model_id, entry['description'])
    return models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='report drift against the committed catalog, write nothing')
    args = parser.parse_args()

    models = scrape()
    old = {}
    if CATALOG.exists():
        try:
            old = json.loads(CATALOG.read_text()).get('models', {})
        except (ValueError, OSError):
            old = {}

    added = sorted(set(models) - set(old))
    removed = sorted(set(old) - set(models))
    free_now = {k for k, v in models.items() if v['free_endpoint']}
    free_before = {k for k, v in old.items() if v.get('free_endpoint')}
    gained_free = sorted(free_now - free_before)
    lost_free = sorted(free_before - free_now)

    print('models: %d (was %d)' % (len(models), len(old)))
    for label, items in (('added', added), ('removed', removed),
                         ('gained free endpoint', gained_free), ('LOST free endpoint', lost_free)):
        print('%s: %d' % (label, len(items)))
        for item in items[:40]:
            print('   ', item)

    if args.check:
        return 0

    overrides = {}
    if CATALOG.exists():
        try:
            overrides = json.loads(CATALOG.read_text()).get('overrides', {})
        except (ValueError, OSError):
            overrides = {}
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps({
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'source': SOURCE,
        'note': ('Generated by scripts/refresh_catalog.py. Edit only the "overrides" '
                 'block by hand; "models" is overwritten on every refresh.'),
        'models': dict(sorted(models.items())),
        'overrides': overrides,
    }, indent=1, ensure_ascii=False) + '\n')
    print('wrote', CATALOG)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
