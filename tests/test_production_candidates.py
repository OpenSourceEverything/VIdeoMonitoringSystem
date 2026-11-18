"""Candidate input, evidence, cost and generated-report safety boundaries."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
import io
import json
import re
from pathlib import Path
import tempfile
import unittest

from video_monitoring_system.candidate_catalog import (
    CATALOG_FILES, CatalogError, DEFAULT_CATALOG, HISTORY_FILE, MAX_REPORT_LINES, SOURCE_FILES, VENDOR_SEARCHES,
    derived_status, load_catalog, main, render_catalog, validate_catalog, vendor_search_url,
)


ROOT = Path(__file__).resolve().parents[1]


def refresh_decision(candidate):
    candidate['blockers'] = [
        {'gate': name, 'message': gate['reason']}
        for name, gate in candidate['gates'].items() if gate['status'] != 'PASS'
    ]
    candidate['status'] = derived_status(candidate)


class ProductionCandidatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.evidence = load_catalog(ROOT / DEFAULT_CATALOG)
        cls.requirements = (ROOT / 'docs/requirements/requirements.md').read_bytes()

    def setUp(self):
        self.data = deepcopy(self.catalog)
        self.sources = deepcopy(self.evidence)
        self.sources['research_archive'] = None  # Separate preservation test covers the large appendix.
        self.camera = self.data['candidates'][0]
        self.claims = self.sources['claims'][self.camera['id']]

    def validate(self):
        validate_catalog(self.data, self.sources, self.requirements)

    def write_split_fixture(self, root):
        hardware = root / 'research/candidates'
        requirements = root / 'requirements'
        hardware.mkdir(parents=True)
        requirements.mkdir()
        (requirements / 'requirements.md').write_bytes(self.requirements)
        for role, filename in CATALOG_FILES.items():
            records = [c for c in self.data['candidates'] if c['role'] == role]
            (hardware / filename).write_text(json.dumps({**self.data, 'candidates': records}))
        for role, filename in SOURCE_FILES.items():
            ids = {c['id'] for c in self.data['candidates'] if c['role'] == role}
            document = {
                'schema_version': 1, 'role': role, 'description': 'Synthetic source fixture',
                'claims': {key: value for key, value in self.sources['claims'].items() if key in ids},
                'research': self.sources['research'][role],
            }
            (hardware / filename).write_text(json.dumps(document))
        (hardware.parent / HISTORY_FILE).write_text(json.dumps(self.evidence['research_archive']))
        return hardware

    def setUpCompleteCamera(self):
        """Synthetic measured evidence for exercising the successful qualification path."""
        measured = {
            'url': 'https://example.com/retained-measurement-report',
            'observed_on': '2026-11-08', 'basis': 'measurement', 'support': 'SUPPORTED',
            'locator': 'Synthetic test result, not a real hardware observation',
            'excerpt': 'Fixture demonstrates the named gate; only for unit testing.',
        }
        self.claims['qualified_fixture'] = {'value': 'fixture', 'sources': [measured], 'note': 'Synthetic'}
        for component in self.camera['cost']['components'][1:]:
            amount = '4.00' if component['id'] == 'required_power_mounts_and_cables' else '0.00'
            ref = 'cost_' + component['id']
            component.update(amount_usd=amount, evidence=[ref])
            self.claims[ref] = {'value': amount, 'sources': [deepcopy(measured)], 'note': 'Synthetic'}
        self.camera['cost'].update(known_subtotal_usd='48.99', total_cost_usd='48.99')
        for gate in self.camera['gates'].values():
            gate.update(status='PASS', evidence=['qualified_fixture'], reason='Measured synthetic fixture')
        self.camera['local_operation'] = 'PASS'
        refresh_decision(self.camera)

    def test_real_catalog_and_deterministic_report_validate(self):
        self.validate()
        for role, filename in CATALOG_FILES.items():
            output = render_catalog(self.catalog, role)
            self.assertEqual(output, (ROOT / DEFAULT_CATALOG / Path(filename).with_suffix('.md')).read_text())
            self.assertLessEqual(len(output.splitlines()), MAX_REPORT_LINES)
            self.assertNotRegex(output, r'(?m)^\s*\|')
            self.assertNotRegex(output, r'\[[^\]\n]+\]\([^\)\n]+\)')
            self.assertEqual(output, render_catalog(self.catalog, role))
            self.assertIn(SOURCE_FILES[role], output)
            for line in output.splitlines():
                if 'https://' not in line:
                    self.assertLessEqual(len(line), 96, line)
            for candidate in self.catalog['candidates']:
                if candidate['role'] != role or candidate['status'] == 'REJECTED':
                    self.assertNotIn(candidate['buying_url'], output)
                else:
                    self.assertIn(candidate['buying_url'], output)

    def test_exact_configurations_and_active_role_limit(self):
        clone = deepcopy(self.camera)
        clone['id'] = 'another-offer'
        self.data['candidates'].append(clone)
        self.sources['claims'][clone['id']] = deepcopy(self.claims)
        with self.assertRaisesRegex(CatalogError, 'maximum three'):
            self.validate()
        self.data['candidates'] = [self.camera, clone]
        self.sources['claims'] = {c['id']: self.sources['claims'][c['id']] for c in self.data['candidates']}
        with self.assertRaisesRegex(CatalogError, 'duplicate model/configuration'):
            self.validate()

    def test_schema_rejects_missing_fields_and_invalid_types(self):
        del self.camera['role']
        with self.assertRaisesRegex(CatalogError, 'fields must be'):
            self.validate()
        self.data = deepcopy(self.catalog)
        self.data['schema_version'] = True
        with self.assertRaisesRegex(CatalogError, 'unsupported schema'):
            self.validate()

    def test_every_published_value_requires_matching_reviewed_source(self):
        del self.camera['evidence']['price_usd']
        with self.assertRaisesRegex(CatalogError, 'missing evidence'):
            self.validate()
        self.camera['evidence']['price_usd'] = ['price_usd']
        self.claims['price_usd']['value'] = '0.99'
        with self.assertRaisesRegex(CatalogError, 'evidence value mismatch'):
            self.validate()

    def test_contradictory_or_unreviewed_support_cannot_be_reported_as_fact(self):
        source = self.claims['specifications']['sources'][0]
        source['support'] = 'CONTRADICTED'
        with self.assertRaisesRegex(CatalogError, 'unsupported claim'):
            self.validate()
        source['support'] = 'UNREVIEWED'
        with self.assertRaisesRegex(CatalogError, 'source support unreviewed'):
            self.validate()

    def test_source_requires_direct_https_and_observation_details(self):
        source = self.claims['price_usd']['sources'][0]
        source['url'] = 'http://example.com/offer'
        with self.assertRaisesRegex(CatalogError, 'HTTPS'):
            self.validate()
        source['url'] = 'https://example.com/offer'
        source['excerpt'] = ''
        with self.assertRaisesRegex(CatalogError, 'expected text'):
            self.validate()

    def test_vendor_coverage_and_search_links_are_explicit_for_every_candidate(self):
        for candidate in self.data['candidates']:
            sourcing = candidate['sourcing']
            self.assertTrue(sourcing['full_product_name'])
            self.assertTrue(sourcing['search_terms'])
            self.assertTrue({'manufacturer', *VENDOR_SEARCHES} <= set(sourcing['vendors']))
            for vendor in VENDOR_SEARCHES:
                record = sourcing['vendors'][vendor]
                self.assertEqual(record['search_url'], vendor_search_url(vendor, sourcing['search_terms']))
        del self.camera['sourcing']['vendors']['digikey']
        with self.assertRaisesRegex(CatalogError, 'explicit manufacturer and marketplace'):
            self.validate()

    def test_vendor_search_navigation_cannot_be_presented_as_a_product(self):
        record = self.camera['sourcing']['vendors']['amazon']
        record.update(result='EXACT', product_url=record['search_url'])
        with self.assertRaisesRegex(CatalogError, 'search link is not a product'):
            self.validate()
        record['product_url'] = 'https://www.ebay.com/itm/123456789'
        with self.assertRaisesRegex(CatalogError, 'wrong vendor host'):
            self.validate()

    def test_vendor_missing_offer_is_not_an_unchecked_or_free_offer(self):
        record = self.camera['sourcing']['vendors']['amazon']
        self.assertEqual(record['result'], 'NONE_FOUND')
        record['price_usd'] = '0.00'
        with self.assertRaisesRegex(CatalogError, 'absent vendor offer'):
            self.validate()
        record['price_usd'] = None
        record['result'] = 'NOT_CHECKED'
        with self.assertRaisesRegex(CatalogError, 'unchecked vendor cannot claim a dated check'):
            self.validate()
        record.update(checked_on=None, evidence=[])
        self.validate()
        self.assertIn('Amazon: NOT CHECKED', render_catalog(self.data))

    def test_vendor_observation_requires_matching_dated_evidence(self):
        record = self.camera['sourcing']['vendors']['ebay']
        record['seller'] = 'Unsupported alternate seller'
        with self.assertRaisesRegex(CatalogError, 'matching evidence'):
            self.validate()
        record['checked_on'] = '2099-01-01'
        with self.assertRaisesRegex(CatalogError, 'vendor date exceeds'):
            self.validate()

    def test_other_or_unselected_configurations_cannot_supply_exact_price(self):
        recorder = next(c for c in self.data['candidates'] if c['role'] == 'recorder')
        record = recorder['sourcing']['vendors']['amazon']
        self.assertEqual(record['result'], 'DIFFERENT_CONFIGURATION')
        record['price_usd'] = '1.00'
        ref = record['evidence'][0]
        self.sources['claims'][recorder['id']][ref]['value']['price_usd'] = '1.00'
        with self.assertRaisesRegex(CatalogError, 'non-exact vendor configuration'):
            self.validate()
        record['price_usd'] = None
        self.sources['claims'][recorder['id']][ref]['value']['price_usd'] = None
        self.validate()
        output = render_catalog(self.data)
        self.assertIn('different configuration; not an exact match', output)
        self.assertIn('NONE FOUND in recorded search', output)
        normalized = ' '.join(output.split())
        for candidate in self.data['candidates']:
            if candidate['status'] != 'REJECTED':
                self.assertIn(candidate['sourcing']['full_product_name'], normalized)

    def test_report_keeps_each_candidates_facts_and_links_in_one_bullet_tree(self):
        for role in CATALOG_FILES:
            with self.subTest(role=role):
                output = render_catalog(self.data, role)
                trees = re.split(r'(?m)(?=^- )', output)[1:]
                candidates = sorted((c for c in self.data['candidates']
                                     if c['role'] == role and c['status'] != 'REJECTED'),
                                    key=lambda c: output.index(c['sourcing']['full_product_name']))
                self.assertEqual(len(trees), len(candidates))
                self.assertNotIn('## ', output)
                self.assertNotIn('- Status:', output)
                self.assertNotRegex(output, r'(?m)^\s*- .*: (?:PARTIAL|QUALIFIED|REJECTED)\.')
                required_sections = ('Bottom line', 'Approximate cost', 'Drawings and independent manufacture',
                                     'Technical fit', 'Purchase options', 'Search', 'Evidence date')
                for candidate, tree in zip(candidates, trees):
                    self.assertTrue(tree.startswith('- ' + candidate['sourcing']['full_product_name']))
                    for section in required_sections:
                        self.assertIn('  - ' + section, tree)
                    self.assertIn('Meets all our requirements: UNKNOWN', tree)
                    actual_urls = re.findall(r'https://[^\s]+', tree)
                    expected_urls = {candidate['buying_url']}
                    fabrication = candidate['report_details']['fabrication']
                    expected_urls.update(fabrication['download_urls'])
                    expected_urls.update(fabrication['evidence_urls'])
                    if fabrication['license_url']:
                        expected_urls.add(fabrication['license_url'])
                    expected_urls.update(candidate['report_details']['technical']['documentation_urls'])
                    for vendor in candidate['sourcing']['vendors'].values():
                        for key in ('product_url', 'search_url'):
                            if vendor[key] is not None:
                                expected_urls.add(vendor[key])
                    self.assertEqual(set(actual_urls), expected_urls)
                    self.assertEqual(len(actual_urls), len(expected_urls), 'Do not repeat URLs')
                    _, purchase = tree.split('  - Purchase options', 1)
                    purchase, searches = purchase.split('  - Search', 1)
                    for vendor in candidate['sourcing']['vendors'].values():
                        if vendor['search_url']:
                            self.assertNotIn(vendor['search_url'], purchase)
                            self.assertIn(vendor['search_url'], searches)
                        if vendor['result'] == 'DIFFERENT_CONFIGURATION':
                            self.assertIn('different configuration; not an exact match', purchase)
                            self.assertIn(vendor['product_url'], purchase)
                for candidate in self.data['candidates']:
                    if candidate['role'] != role or candidate['status'] == 'REJECTED':
                        self.assertNotIn(candidate['sourcing']['full_product_name'], output)

    def test_report_details_need_evidence_and_cover_every_vendor(self):
        details = self.camera['report_details']
        details['included'].append('Unsupported accessory')
        with self.assertRaisesRegex(CatalogError, 'report details require matching evidence'):
            self.validate()
        details['included'].pop()
        del details['offer_terms']['amazon']
        with self.assertRaisesRegex(CatalogError, 'offer terms must cover each vendor'):
            self.validate()

    def test_fabrication_answers_cannot_hide_missing_files_rights_or_cost_quantity(self):
        recorder = next(c for c in self.data['candidates'] if c['role'] == 'recorder')
        details = recorder['report_details']
        original = deepcopy(details['fabrication'])
        cases = (
            ({'complete_files': 'YES'}, 'no unverified required files'),
            ({'scope': 'carrier_only', 'complete_files': 'YES', 'missing_or_unverified': []},
             'whole device'),
            ({'scope': 'complete_camera', 'complete_files': 'YES', 'missing_or_unverified': []},
             'whole device'),
            ({'commercial_reuse': 'YES'}, 'named license and URL'),
            ({'feasible': 'YES'}, 'complete files, commercial rights and production evidence'),
            ({'unit_cost_usd': '100.00'}, 'stated quantity'),
            ({'scope': 'unrecognized'}, 'invalid fabrication scope'),
        )
        for edits, message in cases:
            with self.subTest(edits=edits):
                details['fabrication'] = {**deepcopy(original), **edits}
                with self.assertRaisesRegex(CatalogError, message):
                    self.validate()
        details['fabrication'] = original
        self.validate()
        output = render_catalog(self.data, 'recorder')
        self.assertIn('Complete production files available: UNKNOWN', output)
        self.assertIn('Independent fabrication/assembly feasible: UNKNOWN', output)
        self.assertIn('Manufacturing unit cost: UNKNOWN; quantity UNKNOWN', output)
        self.assertNotIn('USD 0.00', output)

    def test_supported_fabrication_package_updates_answer_without_passing_other_gates(self):
        recorder = next(c for c in self.data['candidates'] if c['role'] == 'recorder')
        details = recorder['report_details']
        details['fabrication'].update(
            complete_files='YES', scope='complete_compute_board', missing_or_unverified=[],
            commercial_reuse='YES', license_name='Synthetic hardware license',
            license_url='https://example.com/manufacturing-license', feasible='YES',
            evidence_urls=['https://example.com/manufacturer-package-review'],
            unit_cost_usd='140.00', quantity_at_cost=100, engineering_tooling_usd='2000.00',
        )
        claims = self.sources['claims'][recorder['id']]
        claims['report_details']['value'] = {
            key: deepcopy(value) for key, value in details.items() if key != 'evidence'
        }
        for source in claims['report_details']['sources']:
            source['support'] = 'SUPPORTED'  # Synthetic review, not a real manufacturer qualification.
        with self.assertRaisesRegex(CatalogError, 'fabrication answer contradicts mandatory gate'):
            self.validate()
        recorder['gates']['independent_board_fabrication'].update(
            status='PASS', evidence=['report_details'], reason='Reviewed synthetic production package',
        )
        refresh_decision(recorder)
        self.validate()
        self.assertEqual(recorder['status'], 'PARTIAL')
        output = render_catalog(self.data, 'recorder')
        self.assertIn('Independent fabrication/assembly feasible: YES', output)
        self.assertIn('Manufacturing unit cost: USD 140.00; quantity 100', output)
        self.assertIn('https://example.com/manufacturing-license', output)
        self.assertIn('Meets all our requirements: UNKNOWN', output)
        details['fabrication']['scope'] = 'module_and_carrier'
        claims['report_details']['value']['fabrication']['scope'] = 'module_and_carrier'
        self.validate()
        self.assertIn('File scope under review: compute module plus carrier',
                      render_catalog(self.data, 'recorder'))

    def test_camera_files_can_be_complete_without_becoming_a_camera_requirement(self):
        details = self.camera['report_details']
        details['fabrication'].update(complete_files='YES', scope='complete_camera',
                                      missing_or_unverified=[],
                                      download_urls=['https://example.com/camera-production-files'])
        self.claims['report_details']['value'] = {
            key: deepcopy(value) for key, value in details.items() if key != 'evidence'
        }
        for source in self.claims['report_details']['sources']:
            source['support'] = 'SUPPORTED'  # Synthetic complete camera package.
        self.validate()
        output = render_catalog(self.data, 'camera')
        self.assertIn('Complete production files available: YES', output)
        self.assertIn('Commercial manufacture/modification permitted: UNKNOWN', output)
        self.assertIn('Independent fabrication/assembly feasible: UNKNOWN', output)
        self.assertIn('Camera fabrication is not mandatory', output)

    def test_money_is_exact_and_subtotal_must_add_up(self):
        self.camera['price_usd'] = 44.99
        with self.assertRaisesRegex(CatalogError, 'decimal string'):
            self.validate()
        self.camera['price_usd'] = '44.99'
        self.camera['cost']['known_subtotal_usd'] = '44.98'
        with self.assertRaisesRegex(CatalogError, 'costs do not add up'):
            self.validate()

    def test_missing_cost_is_not_zero_or_a_complete_total(self):
        self.camera['cost']['total_cost_usd'] = '44.99'
        with self.assertRaisesRegex(CatalogError, 'UNKNOWN while any cost is missing'):
            self.validate()
        self.camera['cost']['total_cost_usd'] = None
        self.camera['cost']['components'][1]['amount_usd'] = '0.00'
        with self.assertRaisesRegex(CatalogError, 'including zero, requires evidence'):
            self.validate()

    def test_unpriced_documentary_lead_stays_unknown_not_free_or_qualified(self):
        self.camera.update(price_usd=None, price_date=None)
        self.camera['evidence'].update(price_usd=[], price_date=[])
        for component in self.camera['cost']['components']:
            component.update(amount_usd=None, evidence=[])
        self.camera['cost'].update(known_subtotal_usd='0.00', total_cost_usd=None)
        refresh_decision(self.camera)
        self.validate()
        self.assertEqual(self.camera['status'], 'PARTIAL')
        output = render_catalog(self.data)
        record = next(line for line in output.splitlines() if 'Board/assembled configuration:' in line)
        self.assertIn('configuration: UNKNOWN', record)
        self.assertIn('- Complete delivered hardware: UNKNOWN.', output)
        self.assertNotIn('$0.00', record)
        self.camera['price_date'] = self.data['updated']
        with self.assertRaisesRegex(CatalogError, 'unknown price must have null price_date'):
            self.validate()

    def test_every_required_cost_component_and_gate_remains_present(self):
        self.camera['cost']['components'].pop()
        with self.assertRaisesRegex(CatalogError, 'missing or duplicate cost component'):
            self.validate()
        self.camera['cost'] = deepcopy(self.catalog['candidates'][0]['cost'])
        del self.camera['gates']['generic_local_talk']
        with self.assertRaisesRegex(CatalogError, 'mandatory gate coverage'):
            self.validate()

    def test_qualification_and_rejection_follow_gates_not_labels(self):
        self.camera['status'] = 'QUALIFIED'
        with self.assertRaisesRegex(CatalogError, 'status contradicts'):
            self.validate()
        refresh_decision(self.camera)
        self.camera['gates']['generic_local_talk']['status'] = 'FAIL'
        refresh_decision(self.camera)
        with self.assertRaisesRegex(CatalogError, 'FAIL requires evidence'):
            self.validate()
        self.camera['gates']['generic_local_talk']['evidence'] = ['specifications']
        self.validate()
        self.assertEqual(self.camera['status'], 'REJECTED')

    def test_published_specs_cannot_pass_a_physical_gate(self):
        gate = self.camera['gates']['generic_local_talk']
        gate.update(status='PASS', evidence=['specifications'])
        refresh_decision(self.camera)
        with self.assertRaisesRegex(CatalogError, 'not measured performance'):
            self.validate()

    def test_decode_and_inference_are_independent_gates(self):
        recorder = next(c for c in self.data['candidates'] if c['role'] == 'recorder')
        recorder['acceleration']['inference'] = 'PASS'
        with self.assertRaisesRegex(CatalogError, 'separate gates'):
            self.validate()

    def test_complete_measured_fixture_can_qualify_and_ranks_first(self):
        self.setUpCompleteCamera()
        self.validate()
        self.assertEqual(self.camera['status'], 'QUALIFIED')
        output = render_catalog(self.data)
        first_record = next(line for line in output.splitlines() if line.startswith('- '))
        self.assertIn(self.camera['exact_model'], first_record)
        self.assertIn('- Meets all our requirements: YES.', output)

    def test_rejected_records_are_retained_but_not_offered_as_candidates(self):
        output = render_catalog(self.data)
        for candidate in self.data['candidates']:
            if candidate['status'] == 'REJECTED':
                self.assertNotIn(candidate['buying_url'], output)
                self.assertIn(candidate['id'], self.sources['claims'])

    def test_role_without_nonrejected_records_has_no_fallback_recommendation(self):
        self.data['candidates'] = [c for c in self.data['candidates']
                                   if c['role'] != 'recorder' or c['status'] == 'REJECTED']
        output = render_catalog(self.data)
        self.assertIn('No eligible candidate verified', output)
        for candidate in self.data['candidates']:
            if candidate['role'] == 'recorder':
                self.assertNotIn(candidate['buying_url'], output)

    def test_sort_uses_complete_cost_not_incomplete_item_price(self):
        from video_monitoring_system.candidate_catalog import _sort_key
        complete = deepcopy(self.camera)
        complete['cost']['total_cost_usd'] = '49.99'
        incomplete = deepcopy(self.camera)
        incomplete['price_usd'] = '1.00'
        self.assertLess(_sort_key(complete), _sort_key(incomplete))
        cheaper = deepcopy(complete)
        cheaper['cost']['total_cost_usd'] = '48.99'
        self.assertLess(_sort_key(cheaper), _sort_key(complete))

    def test_recorder_prices_do_not_impose_a_ceiling_or_selling_price_ratio(self):
        recorder = next(c for c in self.data['candidates'] if c['role'] == 'recorder')
        claims = self.sources['claims'][recorder['id']]
        for price, selling in [('126.00', None), ('999.99', '100.00'), ('9999.00', '19999.00')]:
            with self.subTest(price=price, selling=selling):
                recorder['price_usd'] = price
                claims['price_usd']['value'] = price
                recorder['cost']['components'][0]['amount_usd'] = price
                recorder['cost'].update(intended_selling_price_usd=selling, known_subtotal_usd=price)
                refresh_decision(recorder)
                self.validate()
                self.assertEqual(recorder['status'], 'PARTIAL')
                self.assertNotIn('production_cost', recorder['gates'])

    def test_camera_production_cost_limit_is_unchanged(self):
        self.setUpCompleteCamera()
        self.camera['cost']['intended_selling_price_usd'] = '90.00'
        with self.assertRaisesRegex(CatalogError, '50% ceiling'):
            self.validate()
        self.camera['gates']['production_cost'].update(status='FAIL', evidence=['price_usd'])
        refresh_decision(self.camera)
        self.validate()
        self.assertEqual(self.camera['status'], 'REJECTED')

    def test_recorder_requires_a_separate_complete_board_fabrication_gate(self):
        recorder = next(c for c in self.data['candidates'] if c['role'] == 'recorder')
        del recorder['gates']['independent_board_fabrication']
        with self.assertRaisesRegex(CatalogError, 'mandatory gate coverage'):
            self.validate()
        recorder['gates']['independent_board_fabrication'] = {
            'status': 'PASS', 'evidence': [], 'reason': 'Unsupported claim of production files',
        }
        refresh_decision(recorder)
        with self.assertRaisesRegex(CatalogError, 'PASS requires evidence'):
            self.validate()

    def test_recorder_ranking_uses_priced_subtotal_and_unknown_is_not_free(self):
        from video_monitoring_system.candidate_catalog import _sort_key
        recorder = next(c for c in self.data['candidates'] if c['role'] == 'recorder')
        cheaper = deepcopy(recorder)
        cheaper['price_usd'] = '160.00'
        cheaper['cost']['known_subtotal_usd'] = '160.00'
        cheaper['status'] = 'UNKNOWN'
        complete = deepcopy(recorder)
        complete['price_usd'] = '150.00'
        complete['cost']['total_cost_usd'] = '210.00'
        complete['status'] = 'QUALIFIED'
        self.assertLess(_sort_key(cheaper), _sort_key(complete))
        unknown = deepcopy(recorder)
        unknown['price_usd'] = None
        unknown['cost'].update(known_subtotal_usd='0.00', total_cost_usd=None)
        self.assertLess(_sort_key(complete), _sort_key(unknown))
        report = render_catalog(self.data)
        self.assertIn('no controller price gate', report)
        self.assertIn('Main unresolved item: Complete licensed compute-board', report)

    def test_recorded_sources_do_not_pass_gates_without_review(self):
        self.camera['gates']['exact_configuration'].update(status='PASS', evidence=['specifications'])
        self.claims['specifications']['sources'][0]['support'] = 'RECORDED'
        refresh_decision(self.camera)
        with self.assertRaisesRegex(CatalogError, 'PASS requires reviewed support'):
            self.validate()

    def test_changed_requirements_require_explicit_requalification(self):
        with self.assertRaisesRegex(CatalogError, 'requirements changed'):
            validate_catalog(self.data, self.sources, self.requirements + b'\nNew gate\n')

    def test_all_prior_inspected_evidence_is_losslessly_preserved(self):
        archive = self.evidence['research_archive']
        reconstructed = archive['prefix_markdown'] + ''.join(
            offer['markdown'] for offer in archive['offers']) + archive['suffix_markdown']
        self.assertEqual(sha256(reconstructed.encode()).hexdigest(), archive['source_sha256'])
        self.assertEqual(len(archive['offers']), archive['offer_count'])
        ids = [offer['id'] for offer in archive['offers']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(archive['lookup_gaps'])
        self.assertFalse(set(ids).intersection(gap['id'] for gap in archive['lookup_gaps']))
        self.sources['research_archive'] = deepcopy(archive)
        self.sources['research_archive']['offers'][0]['markdown'] += 'corrupted'
        with self.assertRaisesRegex(CatalogError, 'preservation hash mismatch'):
            self.validate()

    def test_generate_lint_stale_failure_and_duplicate_key_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_split_fixture(Path(directory))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['render', '--catalog', str(path)]), 0)
                self.assertEqual(main(['lint', '--catalog', str(path)]), 0)
                (path / 'camera.md').write_text('unreviewed manual change')
                self.assertEqual(main(['lint', '--catalog', str(path)]), 2)
                (path / 'camera.json').write_text('{"schema_version":1,"schema_version":2}')
                self.assertEqual(main(['lint', '--catalog', str(path)]), 2)

    def test_split_sources_reject_wrong_roles_missing_claims_and_duplicate_keys(self):
        for defect, message in (
            ('wrong_role', 'source role mismatch'),
            ('missing_claim', 'orphan, missing or wrong-role'),
            ('wrong_role_claim', 'orphan, missing or wrong-role'),
            ('duplicate_key', 'duplicate JSON key'),
        ):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                path = self.write_split_fixture(Path(directory))
                source_path = path / SOURCE_FILES['camera']
                source = json.loads(source_path.read_text())
                if defect == 'wrong_role':
                    source['role'] = 'recorder'
                elif defect == 'missing_claim':
                    source['claims'].pop(self.camera['id'])
                elif defect == 'wrong_role_claim':
                    recorder = next(c for c in self.data['candidates'] if c['role'] == 'recorder')
                    source['claims'][recorder['id']] = self.sources['claims'][recorder['id']]
                if defect == 'duplicate_key':
                    source_path.write_text('{"role":"camera","role":"recorder"}')
                else:
                    source_path.write_text(json.dumps(source))
                with self.assertRaisesRegex(CatalogError, message):
                    load_catalog(path)

    def test_loader_requires_each_split_source_and_preserved_history(self):
        for filename in (*CATALOG_FILES.values(), *SOURCE_FILES.values(), '../' + HISTORY_FILE):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                path = self.write_split_fixture(Path(directory))
                (path / filename).unlink()
                with self.assertRaises(FileNotFoundError):
                    load_catalog(path)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_split_fixture(Path(directory))
            archive_path = path.parent / HISTORY_FILE
            archive = json.loads(archive_path.read_text())
            archive['offers'][0]['markdown'] += 'corrupted'
            archive_path.write_text(json.dumps(archive))
            with self.assertRaisesRegex(CatalogError, 'preservation hash mismatch'):
                load_catalog(path)

    def test_role_catalogs_load_independently_and_reject_cross_role_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_split_fixture(Path(directory))
            camera_path = path / CATALOG_FILES['camera']
            (path / CATALOG_FILES['recorder']).unlink()
            camera_catalog, camera_sources = load_catalog(camera_path)
            self.assertTrue(all(c['role'] == 'camera' for c in camera_catalog['candidates']))
            self.assertEqual(set(camera_sources['claims']), {c['id'] for c in camera_catalog['candidates']})
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['render', '--catalog', str(camera_path)]), 0)
                self.assertTrue((path / 'camera.md').exists())
                self.assertFalse((path / 'controller.md').exists())
            document = json.loads(camera_path.read_text())
            document['candidates'].append(next(c for c in self.data['candidates'] if c['role'] == 'recorder'))
            camera_path.write_text(json.dumps(document))
            with self.assertRaisesRegex(CatalogError, 'wrong-role candidate'):
                load_catalog(camera_path)

    def test_lint_checks_both_role_reports_for_stale_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_split_fixture(Path(directory))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['render', '--catalog', str(path)]), 0)
                (path / 'controller.md').write_text('stale controller report')
                self.assertEqual(main(['lint', '--catalog', str(path)]), 2)
                self.assertEqual(main(['lint', '--catalog', str(path / 'camera.json')]), 0)


if __name__ == '__main__':
    unittest.main()
