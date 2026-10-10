#!/usr/bin/env python3
"""FreeToken-Bots (NVIDIA): conservative NVIDIA free-endpoint discovery and Pi sync.

The job is narrow and fail-closed: find the NVIDIA API-catalog models that are
served on a **free endpoint**, keep only the ones that are agent-capable chat
LLMs, verify them, and hand a *proposal* to whatever agent host (Muse, Pi,
Grokbot, Dot) is going to call them.

Three facts are kept strictly separate, because merging them is how you end up
paying for tokens you thought were free:

1. **catalog** (``data/nvidia_catalog.json``) — what build.nvidia.com publishes
   about a model. Refreshed by ``scripts/refresh_catalog.py``.
2. **live** — what ``GET {base}/v1/models`` says the inference API serves right
   now. Cheap, key-free, changes without notice.
3. **probe** — what actually happened when a real request was sent. An ordinary
   completion is *not* proof of tool-calling; that needs its own call.

Standard library only.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / 'data' / 'nvidia_catalog.json'
BASE_URL = os.getenv('NVIDIA_BASE_URL', 'https://integrate.api.nvidia.com/v1')
HOME = Path(os.getenv('PI_HOME', str(Path.home() / '.pi' / 'agent')))
CACHE = Path(os.getenv('FREETOKEN_CACHE', str(HOME / 'freetoken-bots.json')))
PI_MODELS = HOME / 'models.json'
DEFAULT_MIN_PARAMS = 100.0

# A model id is never trusted to mean what it says; the probe decides. These
# are the tool definitions used for the tool-calling half of `probe`.
TOOL_PROBE = [{
    'type': 'function',
    'function': {
        'name': 'get_weather',
        'description': 'Get the current weather for a city.',
        'parameters': {
            'type': 'object',
            'properties': {'city': {'type': 'string', 'description': 'City name'}},
            'required': ['city'],
        },
    },
}]


class NotFreeError(RuntimeError):
    """Raised when a model id cannot be proven free. Never probe on uncertainty."""


def request(url, key=None, payload=None, timeout=25):
    headers = {'User-Agent': 'FreeToken-Bots-NVIDIA/1.0', 'Accept': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(
        url, headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_catalog(path=None):
    path = Path(path or CATALOG_PATH)
    if not path.exists():
        return {'models': {}, 'overrides': {}}
    data = json.loads(path.read_text())
    overrides = data.get('overrides') or {}
    merged = {}
    for model_id, entry in (data.get('models') or {}).items():
        merged[model_id] = dict(entry)
    for model_id, entry in overrides.items():
        merged.setdefault(model_id, {'free_endpoint': False, 'publisher': model_id.split('/')[0],
                                     'description': '', 'kind': 'unclassified'})
        merged[model_id].update(entry)
    return {'models': merged, 'overrides': overrides,
            'generated_at': data.get('generated_at'), 'source': data.get('source')}


def live_ids(key=None):
    """Model ids the inference API serves right now. No key needed on NVIDIA."""
    raw = request(BASE_URL + '/models', key)
    return {entry['id'] for entry in raw.get('data', []) or []}


def norm_name(model_id):
    """Publisher-scoped, punctuation-insensitive key for matching ids.

    NVIDIA publishes ``z-ai/glm-5-3`` and ``llama-3_1-nemotron-...`` on
    build.nvidia.com but serves ``z-ai/glm-5.3`` and ``llama-3.1-nemotron-...``
    from the inference API. Matching the literal strings silently drops those
    models — including a 753B flagship — so both sides are normalised to
    letters+digits before comparison.
    """
    return re.sub(r'[^a-z0-9]', '', model_id.split('/')[-1].lower())


def catalog_index(models):
    index = {}
    for model_id in models:
        index.setdefault(norm_name(model_id), model_id)
    return index


def lookup(models, index, model_id):
    """(entry, catalog_id) for a served id, following the alias index."""
    if model_id in models:
        return models[model_id], model_id
    alias = index.get(norm_name(model_id))
    if alias is not None:
        return models[alias], alias
    return None, None


def build_report(catalog, served, min_params=DEFAULT_MIN_PARAMS, checked_at=None, probes=None):
    """Pure: catalog + live id set -> the report written to the cache.

    A model is *free* only when the published catalog marks it as a free
    endpoint **and** the inference API still serves it. Free eligibility is
    dynamic; neither half alone authorises a call.
    """
    models = catalog.get('models') or {}
    index = catalog_index(models)
    probes = probes or {}
    eligible, needs_size, free_ids, inactive, not_chat, aliases = [], [], [], [], [], {}

    for model_id in sorted(served):
        entry, catalog_id = lookup(models, index, model_id)
        if not entry or not entry.get('free_endpoint'):
            continue
        if catalog_id != model_id:
            aliases[model_id] = catalog_id
        free_ids.append(model_id)
        kind = entry.get('kind') or 'unclassified'
        params = entry.get('parameters_b')
        probe = probes.get(model_id) or {}
        record = {
            'id': model_id,
            'catalog_id': catalog_id,
            'kind': kind,
            'parameters_b': params,
            'description': entry.get('description', ''),
            'tool_use': entry.get('tool_use'),
            'tool_use_source': entry.get('tool_use_source'),
            'chat_probe_ok': probe.get('chat_ok'),
            'tools_probe_ok': probe.get('tools_ok'),
        }
        if kind != 'chat':
            not_chat.append({'id': model_id, 'kind': kind})
        elif params is None:
            needs_size.append(record)
        elif params >= min_params:
            eligible.append(record)
        else:
            not_chat.append({'id': model_id, 'kind': 'chat-below-threshold',
                             'parameters_b': params, 'min_params_b': min_params})

    matched_catalog_ids = {lookup(models, index, s)[1] for s in served}
    for model_id in sorted(models):
        entry = models[model_id]
        if entry.get('free_endpoint') and model_id not in matched_catalog_ids:
            inactive.append({'id': model_id, 'reason': 'free in catalog, not served by the API'})

    return {
        'provider': 'nvidia',
        'endpoint': BASE_URL,
        'checked_at': checked_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        'catalog_generated_at': catalog.get('generated_at'),
        'catalog_source': catalog.get('source'),
        'min_params_b': min_params,
        'served_model_count': len(served),
        'free_endpoint_count': len(free_ids),
        'eligible': sorted(eligible, key=lambda r: -(r.get('parameters_b') or 0)),
        'needs_parameter_verification': needs_size,
        'other_free_endpoints': not_chat,
        'inactive_free_endpoints': inactive,
        'catalog_aliases': aliases,
        'free_ids': sorted(free_ids),
    }


def discover(key=None, min_params=DEFAULT_MIN_PARAMS):
    catalog = load_catalog()
    served = live_ids(key)
    report = build_report(catalog, served, min_params, probes=read_probes())
    report['catalog_models_total'] = len(catalog.get('models') or {})
    report['served_ids'] = sorted(served)
    return report


def read_probes():
    if not CACHE.exists():
        return {}
    try:
        return (json.loads(CACHE.read_text()).get('probes') or {})
    except (ValueError, OSError):
        return {}


def load_report(refresh_if_stale=True):
    """Latest scan. Re-scans when missing, unreadable, or older than 24h."""
    report = None
    if CACHE.exists():
        try:
            report = json.loads(CACHE.read_text())
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(report['checked_at'])
            if not refresh_if_stale or age > dt.timedelta(hours=24) or 'free_ids' not in report:
                report = None if refresh_if_stale else report
        except (ValueError, KeyError, OSError):
            report = None
    if report is None:
        report = discover()
        atomic_json(CACHE, report)
    return report


def resolve_free_model(requested, free):
    """Fail-closed resolution of a user-supplied id to a verified-free exact id.

    NVIDIA has no ``:free`` suffix to fall back on, so anything not in the
    latest scan is refused outright. A pretty-printed, remembered, or
    hand-typed id is not proof of free.
    """
    requested = (requested or '').strip().strip(',').strip()
    if requested in free:
        return requested
    raise NotFreeError(
        '"%s" is not a verified-free NVIDIA model id from the latest scan. '
        'Probe refused: calling it could consume paid quota. '
        'Run `scan` and use an exact id from `eligible` or `free_ids`.' % requested)


def probe(model_id, key, timeout=45):
    """A real chat call plus a *separate* real tool-calling call.

    An ordinary completion probe is not proof of tool support, so the tool
    result is reported on its own. HTTP 401/402/403/429 are surfaced verbatim:
    a rate limit is temporary, exhausted credit is not the same thing.
    """
    out = {'model': model_id, 'chat_ok': False, 'tools_ok': False,
           'chat_error': None, 'tools_error': None, 'http_status': None}
    try:
        chat = request(BASE_URL + '/chat/completions', key, {
            'model': model_id,
            'messages': [{'role': 'user', 'content': 'Reply with the single word PONG.'}],
            'max_tokens': 32,
        }, timeout=timeout)
        out['chat_ok'] = bool(chat.get('choices'))
        if chat.get('error'):
            out['chat_error'] = str(chat['error'])[:300]
    except urllib.error.HTTPError as exc:
        out['http_status'] = exc.code
        out['chat_error'] = 'HTTP %s: %s' % (exc.code, exc.reason)
        return out
    except (OSError, ValueError) as exc:
        out['chat_error'] = str(exc)[:300]
        return out

    try:
        tool = request(BASE_URL + '/chat/completions', key, {
            'model': model_id,
            'messages': [{'role': 'user',
                          'content': 'What is the weather in Paris right now? Use the tool.'}],
            'tools': TOOL_PROBE,
            'tool_choice': 'auto',
            'max_tokens': 128,
        }, timeout=timeout)
        choices = tool.get('choices') or []
        message = (choices[0].get('message') if choices else {}) or {}
        out['tools_ok'] = bool(message.get('tool_calls'))
        if not out['tools_ok'] and tool.get('error'):
            out['tools_error'] = str(tool['error'])[:300]
    except urllib.error.HTTPError as exc:
        out['http_status'] = exc.code
        out['tools_error'] = 'HTTP %s: %s' % (exc.code, exc.reason)
    except (OSError, ValueError) as exc:
        out['tools_error'] = str(exc)[:300]
    return out


def pi_proposal(report):
    """A proposal only. Pi's model schema varies across releases, so nothing is
    written to ``models.json`` — a wrong write breaks the user's working setup,
    and an unwritten proposal costs one manual step."""
    models = []
    for entry in report.get('eligible', []):
        models.append({
            'id': entry['id'],
            'provider': 'nvidia',
            'base_url': report.get('endpoint', BASE_URL),
            'api_key_env': 'NVIDIA_API_KEY',
            'context_length': entry.get('context_length'),
            'max_output_tokens': None,
            'tools': entry.get('tools_probe_ok') or entry.get('tool_use') or None,
        })
    return {
        'sync_status': 'proposal_only_requires_host_schema_validation',
        'pi_models_path': str(PI_MODELS),
        'provider': {
            'name': 'nvidia-free',
            'type': 'openai-compatible',
            'base_url': report.get('endpoint', BASE_URL),
            'api_key_env': 'NVIDIA_API_KEY',
            'models': models,
        },
        'warnings': [
            'Verify the installed Pi/host schema before applying — do not paste blind.',
            'Keep NVIDIA_API_KEY out of git and out of shell history.',
            'An unprobed model may not support tool calling; run `probe` first.',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=['scan', 'status', 'probe', 'sync', 'catalog'])
    parser.add_argument('--model', help='Exact NVIDIA model id for probe')
    parser.add_argument('--min-params', type=float, default=DEFAULT_MIN_PARAMS,
                        help='Flagship threshold in billions (default %g)' % DEFAULT_MIN_PARAMS)
    parser.add_argument('--timeout', type=float, default=45.0)
    parser.add_argument('--no-cache', action='store_true',
                        help='do not write the report to the cache')
    args = parser.parse_args(argv)

    if args.command == 'catalog':
        catalog = load_catalog()
        print(json.dumps({
            'catalog_path': str(CATALOG_PATH),
            'generated_at': catalog.get('generated_at'),
            'models_total': len(catalog.get('models') or {}),
            'free_endpoints': sorted(m for m, e in (catalog.get('models') or {}).items()
                                     if e.get('free_endpoint')),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.command == 'status':
        print(CACHE.read_text() if CACHE.exists()
              else json.dumps({'message': 'no report yet — run: radar.py scan'}, indent=2))
        return 0

    if args.command == 'probe':
        key = os.getenv('NVIDIA_API_KEY')
        if not args.model or not key:
            parser.error('probe requires --model and NVIDIA_API_KEY')
        report = load_report()
        try:
            model = resolve_free_model(args.model, set(report.get('free_ids') or []))
        except NotFreeError as exc:
            print('error: %s' % exc, file=sys.stderr)
            return 2
        result = probe(model, key, timeout=args.timeout)
        probes = read_probes()
        probes[model] = {
            'chat_ok': result['chat_ok'],
            'tools_ok': result['tools_ok'],
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        # Rebuild from scratch so the new probe result lands in `eligible`.
        served = set(report.get('served_ids') or []) or live_ids(key)
        fresh = build_report(load_catalog(), served,
                             report.get('min_params_b', DEFAULT_MIN_PARAMS), probes=probes)
        fresh['served_ids'] = sorted(served)
        fresh['probes'] = probes
        if not args.no_cache:
            atomic_json(CACHE, fresh)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result['chat_ok'] else 1

    report = discover(key=os.getenv('NVIDIA_API_KEY'), min_params=args.min_params)
    report['probes'] = read_probes()
    if args.command == 'scan':
        if not args.no_cache:
            atomic_json(CACHE, report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    report['pi_proposal'] = pi_proposal(report)
    if not args.no_cache:
        atomic_json(CACHE, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
