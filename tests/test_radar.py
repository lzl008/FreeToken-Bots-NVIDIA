#!/usr/bin/env python3
"""Tests for FreeToken-Bots (NVIDIA). Standard library only, no network.

Run:  python3 -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import radar  # noqa: E402
import refresh_catalog as rc  # noqa: E402


CATALOG = {
    'generated_at': '2026-10-10T00:00:00+00:00',
    'source': 'https://build.nvidia.com/models',
    'models': {
        'z-ai/glm-5-3': {'free_endpoint': True, 'kind': 'chat', 'parameters_b': 753.0,
                         'tool_use': True, 'tool_use_source': 'documented', 'description': 'x'},
        'nvidia/nemotron-3-super-120b-a12b': {'free_endpoint': True, 'kind': 'chat',
                                              'parameters_b': 120.0, 'description': 'x'},
        'meta/muse-glimmer-30b': {'free_endpoint': True, 'kind': 'chat',
                                  'parameters_b': 30.0, 'description': 'x'},
        'mystery/new-model': {'free_endpoint': True, 'kind': 'chat',
                              'parameters_b': None, 'description': 'x'},
        'nvidia/nemotron-3-embed-1b': {'free_endpoint': True, 'kind': 'embedding',
                                       'parameters_b': 1.0, 'description': 'x'},
        'nvidia/paywalled-huge': {'free_endpoint': False, 'kind': 'chat',
                                  'parameters_b': 400.0, 'description': 'x'},
        'nvidia/retired-free': {'free_endpoint': True, 'kind': 'chat',
                                'parameters_b': 200.0, 'description': 'x'},
    },
    'overrides': {},
}


class ReportTests(unittest.TestCase):
    def build(self, served, min_params=100.0):
        return radar.build_report(CATALOG, set(served), min_params)

    def test_free_needs_both_catalog_flag_and_live_serving(self):
        """Served but not marked free is excluded; marked free but not served is inactive."""
        report = self.build(['nvidia/paywalled-huge'])
        self.assertEqual(report['free_ids'], [])
        self.assertEqual(report['eligible'], [])
        inactive = [e['id'] for e in report['inactive_free_endpoints']]
        self.assertIn('nvidia/retired-free', inactive)
        self.assertNotIn('nvidia/paywalled-huge', inactive)

    def test_alias_matching_keeps_punctuation_variant(self):
        """Catalog 'z-ai/glm-5-3' must match the served id 'z-ai/glm-5.3'."""
        report = self.build(['z-ai/glm-5.3'])
        self.assertEqual(report['free_ids'], ['z-ai/glm-5.3'])
        self.assertEqual(report['catalog_aliases'], {'z-ai/glm-5.3': 'z-ai/glm-5-3'})
        self.assertEqual([e['id'] for e in report['eligible']], ['z-ai/glm-5.3'])

    def test_alias_prevents_bogus_inactive_entry(self):
        report = self.build(['z-ai/glm-5.3'])
        self.assertNotIn('z-ai/glm-5-3', [e['id'] for e in report['inactive_free_endpoints']])

    def test_threshold_splits_pools(self):
        report = self.build(['z-ai/glm-5-3', 'meta/muse-glimmer-30b'], min_params=100.0)
        self.assertEqual([e['id'] for e in report['eligible']], ['z-ai/glm-5-3'])
        below = [e for e in report['other_free_endpoints']
                 if e['kind'] == 'chat-below-threshold']
        self.assertEqual([e['id'] for e in below], ['meta/muse-glimmer-30b'])

    def test_unknown_size_is_not_promoted(self):
        report = self.build(['mystery/new-model'])
        self.assertEqual(report['eligible'], [])
        self.assertEqual([e['id'] for e in report['needs_parameter_verification']],
                         ['mystery/new-model'])

    def test_non_chat_free_endpoint_is_separated(self):
        report = self.build(['nvidia/nemotron-3-embed-1b'])
        self.assertEqual(report['eligible'], [])
        self.assertEqual(report['other_free_endpoints'][0]['kind'], 'embedding')

    def test_eligible_sorted_largest_first(self):
        report = self.build(['z-ai/glm-5-3', 'nvidia/nemotron-3-super-120b-a12b'])
        self.assertEqual([e['parameters_b'] for e in report['eligible']], [753.0, 120.0])

    def test_probe_results_surface_in_report(self):
        probes = {'z-ai/glm-5.3': {'chat_ok': True, 'tools_ok': False}}
        report = radar.build_report(CATALOG, {'z-ai/glm-5.3'}, probes=probes)
        entry = report['eligible'][0]
        self.assertTrue(entry['chat_probe_ok'])
        self.assertFalse(entry['tools_probe_ok'])


class ResolveTests(unittest.TestCase):
    def test_exact_verified_id_passes(self):
        self.assertEqual(radar.resolve_free_model('z-ai/glm-5.3', {'z-ai/glm-5.3'}),
                         'z-ai/glm-5.3')

    def test_unlisted_id_is_refused(self):
        with self.assertRaises(radar.NotFreeError):
            radar.resolve_free_model('meta/llama2-70b', {'z-ai/glm-5.3'})

    def test_unlisted_id_shaped_like_a_free_one_is_still_refused(self):
        with self.assertRaises(radar.NotFreeError):
            radar.resolve_free_model('z-ai/glm-5-3', {'z-ai/glm-5.3'})

    def test_copied_whitespace_and_comma_are_tolerated(self):
        self.assertEqual(radar.resolve_free_model('  z-ai/glm-5.3,  ', {'z-ai/glm-5.3'}),
                         'z-ai/glm-5.3')

    def test_empty_id_is_refused(self):
        with self.assertRaises(radar.NotFreeError):
            radar.resolve_free_model('', {'z-ai/glm-5.3'})


class CatalogLoadTests(unittest.TestCase):
    def test_overrides_win_over_generated_entry(self):
        catalog = radar.load_catalog(ROOT / 'data' / 'nvidia_catalog.json')
        entry = catalog['models']['nvidia/nemotron-3-ultra-550b-a55b']
        self.assertEqual(entry['parameters_b'], 550.0)
        self.assertTrue(entry['tool_use'])

    def test_shipped_catalog_is_usable(self):
        catalog = radar.load_catalog(ROOT / 'data' / 'nvidia_catalog.json')
        self.assertGreater(len(catalog['models']), 50)
        free = [m for m, e in catalog['models'].items() if e.get('free_endpoint')]
        self.assertGreater(len(free), 10)

    def test_missing_catalog_is_not_fatal(self):
        catalog = radar.load_catalog(ROOT / 'data' / 'does-not-exist.json')
        self.assertEqual(catalog['models'], {})


class ProposalTests(unittest.TestCase):
    def test_proposal_never_claims_to_have_written(self):
        report = radar.build_report(CATALOG, {'z-ai/glm-5.3', 'nvidia/nemotron-3-super-120b-a12b'})
        proposal = radar.pi_proposal(report)
        self.assertEqual(proposal['sync_status'],
                         'proposal_only_requires_host_schema_validation')
        self.assertEqual([m['id'] for m in proposal['provider']['models']],
                         ['z-ai/glm-5.3', 'nvidia/nemotron-3-super-120b-a12b'])
        self.assertEqual(proposal['provider']['api_key_env'], 'NVIDIA_API_KEY')

    def test_proposal_lists_only_the_eligible_pool(self):
        report = radar.build_report(CATALOG, {'z-ai/glm-5.3', 'meta/muse-glimmer-30b'})
        proposal = radar.pi_proposal(report)
        self.assertEqual([m['id'] for m in proposal['provider']['models']], ['z-ai/glm-5.3'])


class CatalogBuilderTests(unittest.TestCase):
    def test_parameters_takes_the_total_not_the_active_count(self):
        self.assertEqual(rc.parameters_b('x', '552B MoE, 8B active params'), 552.0)
        self.assertEqual(rc.parameters_b('x', '320B-total / 18B-active MoE'), 320.0)
        self.assertEqual(rc.parameters_b('x', '753B-parameter text MoE'), 753.0)
        self.assertEqual(rc.parameters_b('x', '~2.8T hybrid KDA+MLA MoE'), 2800.0)
        self.assertEqual(rc.parameters_b('x', 'built on Gemma 4 31B'), 31.0)

    def test_parameters_absent_when_the_card_states_none(self):
        self.assertIsNone(rc.parameters_b('x', 'Translation model in 37 languages'))
        self.assertIsNone(rc.parameters_b('x', 'MoE with 1M context and tool calling'))
        self.assertIsNone(rc.parameters_b('x', ''))

    def test_classify_uses_the_id_not_the_description(self):
        # "accepting text and images" must NOT demote a chat model
        self.assertEqual(rc.classify('meta/muse-glimmer-30b', 'accepting text and images'), 'chat')
        self.assertEqual(rc.classify('meta/llama-3.2-11b-vision-instruct', 'text and images'), 'chat')

    def test_classify_excludes_non_chat_families(self):
        for model_id, kind in (
            ('nvidia/nemotron-3-embed-1b', 'embedding'),
            ('nvidia/riva-translate-4b-instruct-v2', 'translation'),
            ('meta/llama-guard-4-12b', 'safety'),
            ('nvidia/ising-calibration-1.5-31b', 'vision'),
            ('nvidia/body-pose', 'physical'),
            ('nvidia/synthetic-video-detector', 'image'),
            ('nvidia/kumo-tabular', 'tabular'),
            ('nvidia/parakeet-tdt-0_6b', 'speech'),
        ):
            self.assertEqual(rc.classify(model_id, ''), kind, model_id)

    def test_classify_marks_glm_dot_variant_as_chat(self):
        self.assertEqual(rc.classify('z-ai/glm-5.3', ''), 'chat')
        self.assertEqual(rc.classify('z-ai/glm-5.3-flash', ''), 'chat')

    def test_classify_leaves_unknown_ids_unclassified(self):
        self.assertEqual(rc.classify('nvidia/nvidia-cuopt', ''), 'unclassified')

    def test_parse_page_reads_one_entry_per_card(self):
        html = (
            '<li class="flex"><a href="/acme">Acme</a>'
            '<span class="nv-badge">Free Endpoint</span>'
            '<a href="/acme/widget-9b">widget-9b</a>'
            '<span class="nv-text nv-text--label-regular-md">A 9B chat MoE</span></li>'
            '<li class="flex"><a href="/acme">Acme</a>'
            '<span class="nv-badge">Downloadable</span>'
            '<a href="/acme/vision-3b">vision-3b</a>'
            '<span class="nv-text nv-text--label-regular-md">An image model</span></li>'
        )
        parsed = rc.parse_page(html)
        self.assertEqual(sorted(parsed), ['acme/vision-3b', 'acme/widget-9b'])
        self.assertTrue(parsed['acme/widget-9b']['free_endpoint'])
        self.assertFalse(parsed['acme/vision-3b']['free_endpoint'])
        self.assertEqual(parsed['acme/widget-9b']['description'], 'A 9B chat MoE')


class TransportGuardTests(unittest.TestCase):
    """The scrape must fail loudly rather than write a truncated catalog."""

    def test_interstitial_page_is_detected_by_fetch(self):
        class Fake:
            def read(self_inner):
                return b'<!DOCTYPE html><html><head><title></title></head><body></body></html>'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        original = rc.urllib.request.urlopen
        rc.urllib.request.urlopen = lambda *a, **k: Fake()
        try:
            with self.assertRaises(rc.ScrapeError):
                rc.fetch(1)
        finally:
            rc.urllib.request.urlopen = original

    def test_real_card_page_passes_the_check(self):
        class Fake:
            def read(self_inner):
                return b'<li class="flex"><div class="nv-card-root"></div></li>'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        original = rc.urllib.request.urlopen
        rc.urllib.request.urlopen = lambda *a, **k: Fake()
        try:
            self.assertIn('nv-card-root', rc.fetch(1))
        finally:
            rc.urllib.request.urlopen = original

    def test_shrink_guard_blocks_a_partial_catalog(self):
        self.assertTrue(rc.implausible_shrink(1, 97))
        self.assertFalse(rc.implausible_shrink(97, 97))
        self.assertFalse(rc.implausible_shrink(90, 97))
        self.assertFalse(rc.implausible_shrink(1, 97, force=True))

    def test_shrink_guard_allows_a_first_run(self):
        self.assertFalse(rc.implausible_shrink(0, 0))
        self.assertFalse(rc.implausible_shrink(97, 0))


class ShippedCatalogTests(unittest.TestCase):
    def test_catalog_file_matches_its_own_checksum_shape(self):
        data = json.loads((ROOT / 'data' / 'nvidia_catalog.json').read_text())
        for key in ('generated_at', 'source', 'models', 'overrides'):
            self.assertIn(key, data)
        entry = data['models']['z-ai/glm-5-3']
        for key in ('publisher', 'free_endpoint', 'kind', 'parameters_b', 'description'):
            self.assertIn(key, entry)


if __name__ == '__main__':
    unittest.main()
