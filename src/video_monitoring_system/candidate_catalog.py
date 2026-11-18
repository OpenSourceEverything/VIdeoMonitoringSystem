"""Offline candidate validation and deterministic, compact Markdown generation.

The schema is enforced here without another dependency or schema sidecar.
Source support is a recorded review, not something URL reachability can prove.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
from textwrap import wrap
from typing import Any
from urllib.parse import quote_plus, urlsplit


DEFAULT_CATALOG = Path("docs/research/candidates")
CATALOG_FILES = {"camera": "camera.json", "recorder": "controller.json"}
SOURCE_FILES = {"camera": "camera-sources.json", "recorder": "controller-sources.json"}
HISTORY_FILE = "historical-sourcing.json"
VENDOR_SEARCHES = {
    "amazon": ("Amazon", "https://www.amazon.com/s?k="),
    "alibaba": ("Alibaba", "https://www.alibaba.com/trade/search?SearchText="),
    "ebay": ("eBay", "https://www.ebay.com/sch/i.html?_nkw="),
    "digikey": ("DigiKey", "https://www.digikey.com/en/products?keywords="),
    "mouser": ("Mouser", "https://www.mouser.com/c/?q="),
}
VENDOR_HOSTS = {
    "amazon": "amazon.com", "alibaba": "alibaba.com", "ebay": "ebay.com",
    "digikey": "digikey.com", "mouser": "mouser.com",
}
VENDOR_RESULTS = {"EXACT", "SELECT_OPTIONS", "DIFFERENT_CONFIGURATION", "UNVERIFIED",
                  "NONE_FOUND", "NOT_CHECKED", "BLOCKED"}
MAX_REPORT_LINES = 700
STATUSES = ("QUALIFIED", "PARTIAL", "UNKNOWN", "REJECTED")
STATES = {"PASS", "FAIL", "UNKNOWN"}
COMMON_GATES = (
    "exact_configuration", "offline_lifecycle", "identity_and_commissioning",
    "signed_updates_and_recovery", "supply_warranty_and_licensing",
    "complete_business_cost", "system_acceptance",
)
ROLE_GATES = {
    "camera": (
        "production_cost",
        "native_video_and_dual_streams", "generic_local_talk", "outdoor_image_quality",
        "power_and_network", "enrollment_reset_and_replacement",
    ),
    "recorder": (
        "independent_board_fabrication",
        "installed_ram_boot_and_enclosure", "maintained_linux_video_decode",
        "maintained_linux_inference", "wired_home_lan", "recording_storage_and_restore",
        "eight_camera_capacity_and_thermals", "owner_ui_and_diagnostics",
    ),
}
MEASURED_GATES = {
    "exact_configuration", "installed_ram_boot_and_enclosure",
    "offline_lifecycle", "identity_and_commissioning", "signed_updates_and_recovery",
    "system_acceptance", "native_video_and_dual_streams", "generic_local_talk",
    "outdoor_image_quality", "power_and_network", "enrollment_reset_and_replacement",
    "maintained_linux_video_decode", "maintained_linux_inference", "wired_home_lan",
    "recording_storage_and_restore", "eight_camera_capacity_and_thermals",
    "owner_ui_and_diagnostics",
}
ROLE_CEILINGS = {"camera": Decimal("100")}
COST_COMPONENTS = {
    "camera": {"item", "required_power_mounts_and_cables", "inbound_freight", "duty"},
    "recorder": {"item", "required_completion_parts", "inbound_freight", "duty"},
}


class CatalogError(ValueError):
    """A schema, evidence, cost, qualification or generation invariant failed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogError(message)


def _fields(value: Any, names: set[str], where: str) -> None:
    _require(isinstance(value, dict), f"{where}: expected object")
    _require(set(value) == names, f"{where}: fields must be {', '.join(sorted(names))}")


def _text(value: Any, where: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), f"{where}: expected text")
    _require(not any(c in value for c in "\r\n\x00"), f"{where}: expected single-line text")


def _day(value: Any, where: str) -> None:
    _text(value, where)
    try:
        _require(date.fromisoformat(value).isoformat() == value, f"{where}: invalid date")
    except ValueError as error:
        raise CatalogError(f"{where}: invalid ISO date") from error


def _https(value: Any, where: str) -> None:
    _text(value, where)
    parsed = urlsplit(value)
    _require(parsed.scheme == "https" and bool(parsed.hostname)
             and not parsed.username and not parsed.password,
             f"{where}: expected HTTPS URL without credentials")


def _money(value: Any, where: str, *, nullable: bool = False) -> Decimal | None:
    if nullable and value is None:
        return None
    _require(isinstance(value, str) and bool(re.fullmatch(r"\d+\.\d{2}", value)),
             f"{where}: expected nonnegative USD decimal string, e.g. 44.99")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise CatalogError(f"{where}: invalid money") from error
    _require(result.is_finite(), f"{where}: nonfinite money")
    return result


def required_gates(role: str) -> set[str]:
    return set(COMMON_GATES + ROLE_GATES[role])


def vendor_search_url(vendor: str, terms: str) -> str:
    """Construct a navigation link, never evidence of a matching purchasable offer."""
    return VENDOR_SEARCHES[vendor][1] + quote_plus(terms)


def _validate_sourcing(candidate: dict[str, Any], claims: dict[str, Any], updated: str) -> None:
    sourcing = candidate["sourcing"]
    _fields(sourcing, {"full_product_name", "manufacturer", "manufacturer_part_number",
                      "search_terms", "vendors"}, "sourcing")
    for field in ("full_product_name", "manufacturer", "search_terms"):
        _text(sourcing[field], f"sourcing.{field}")
    if sourcing["manufacturer_part_number"] is not None:
        _text(sourcing["manufacturer_part_number"], "manufacturer part number")
    vendors = sourcing["vendors"]
    _require(isinstance(vendors, dict) and {"manufacturer", *VENDOR_SEARCHES} <= set(vendors),
             "sourcing: explicit manufacturer and marketplace/distributor coverage required")
    identity = {key: value for key, value in sourcing.items() if key != "vendors"}
    _require("sourcing_identity" in claims and claims["sourcing_identity"]["value"] == identity,
             "sourcing identity requires matching reviewed evidence")
    _require(any(s["support"] in {"SUPPORTED", "RECORDED"}
                 for s in claims["sourcing_identity"]["sources"]),
             "sourcing identity source support unreviewed")
    for vendor, record in vendors.items():
        _text(vendor, "vendor id")
        _fields(record, {"search_url", "product_url", "seller", "seller_sku", "result",
                         "price_usd", "stock", "checked_on", "evidence", "note"}, f"vendor {vendor}")
        result = record["result"]
        _require(result in VENDOR_RESULTS, "invalid vendor result")
        _text(record["note"], "vendor note")
        if vendor in VENDOR_SEARCHES:
            _require(record["search_url"] == vendor_search_url(vendor, sourcing["search_terms"]),
                     "vendor search URL must match the candidate search terms")
        elif record["search_url"] is not None:
            _https(record["search_url"], "vendor search URL")
        url = record["product_url"]
        if url is not None:
            _https(url, "vendor product URL")
            parsed = urlsplit(url)
            if vendor in VENDOR_HOSTS:
                host = VENDOR_HOSTS[vendor]
                _require(parsed.hostname == host or parsed.hostname.endswith('.' + host),
                         "vendor product URL has wrong vendor host")
            _require(url != record["search_url"] and not (
                parsed.hostname.endswith('amazon.com') and parsed.path.rstrip('/') == '/s'
                or parsed.hostname.endswith('alibaba.com') and parsed.path.startswith('/trade/search')
                or parsed.hostname.endswith('ebay.com') and parsed.path.startswith('/sch/')
                or parsed.hostname.endswith('digikey.com') and parsed.path.rstrip('/') == '/en/products'
                or parsed.hostname.endswith('mouser.com') and parsed.path.rstrip('/') == '/c'),
                "a vendor search link is not a product listing")
        price = _money(record["price_usd"], "vendor price", nullable=True)
        _require(record["stock"] in {"IN_STOCK", "OUT_OF_STOCK", "UNKNOWN"}, "invalid vendor stock")
        for field in ("seller", "seller_sku"):
            if record[field] is not None:
                _text(record[field], f"vendor {field}")
        if result in {"NONE_FOUND", "NOT_CHECKED"}:
            _require(url is None and price is None and record["stock"] == "UNKNOWN"
                     and record["seller"] is None and record["seller_sku"] is None,
                     "absent vendor offer cannot have a product, seller, price or stock claim")
        elif result != "BLOCKED":
            _require(url is not None, "found vendor result requires a product URL")
        refs = record["evidence"]
        _require(isinstance(refs, list) and len(refs) == len(set(refs)), "invalid vendor evidence")
        if result == "NOT_CHECKED":
            _require(record["checked_on"] is None and not refs, "unchecked vendor cannot claim a dated check")
        else:
            _day(record["checked_on"], "vendor.checked_on")
            _require(record["checked_on"] <= updated, "vendor date exceeds catalog date")
            value = {key: item for key, item in record.items() if key != "evidence"}
            _require(bool(refs), "vendor observation requires evidence")
            for ref in refs:
                _require(ref in claims and claims[ref]["value"] == value,
                         "vendor observation requires matching evidence")
                _require(any(s["support"] in {"SUPPORTED", "RECORDED"} for s in claims[ref]["sources"]),
                         "vendor observation source support unreviewed")
        if result != "EXACT":
            # Prices from a different or unselected configuration must not drive this candidate's ranking.
            _require(price is None, "non-exact vendor configuration must keep candidate price UNKNOWN")


def derived_status(candidate: dict[str, Any]) -> str:
    states = [gate["status"] for gate in candidate["gates"].values()]
    if "FAIL" in states:
        return "REJECTED"
    if all(state == "PASS" for state in states):
        return "QUALIFIED"
    if candidate["specifications"] is not None or candidate["price_usd"] is not None:
        return "PARTIAL"
    return "UNKNOWN"


def _validate_report_details(candidate: dict[str, Any], claims: dict[str, Any], updated: str) -> None:
    """Keep human report facts evidence-backed and fabrication answers distinct."""
    details = candidate['report_details']
    _fields(details, {'included', 'still_needed', 'technical', 'fabrication', 'offer_terms',
                      'checked_on', 'evidence'}, 'report_details')
    _day(details['checked_on'], 'report_details.checked_on')
    _require(details['checked_on'] <= updated, 'report date exceeds catalog date')

    def texts(value: Any, where: str, *, urls: bool = False) -> None:
        _require(isinstance(value, list), f'{where}: expected list')
        for item in value:
            (_https if urls else _text)(item, where)
        _require(len(value) == len(set(value)), f'{where}: duplicate items')

    for key in ('included', 'still_needed'):
        texts(details[key], key)
    technical = details['technical']
    _fields(technical, {'processor', 'ram', 'boot_storage', 'video_decode', 'inference',
                       'network', 'recording_connection', 'linux_support',
                       'proprietary_dependencies', 'documentation_urls'}, 'technical summary')
    for key, value in technical.items():
        if key == 'documentation_urls':
            texts(value, key, urls=True)
        elif value is not None:
            _text(value, key)
    fabrication = details['fabrication']
    _fields(fabrication, {'complete_files', 'scope', 'available_files', 'missing_or_unverified',
                         'download_urls', 'commercial_reuse', 'license_name', 'license_url',
                         'feasible', 'evidence_note', 'evidence_urls', 'unit_cost_usd',
                         'quantity_at_cost', 'engineering_tooling_usd'}, 'fabrication')
    for key in ('complete_files', 'commercial_reuse', 'feasible'):
        _require(fabrication[key] in {'YES', 'NO', 'UNKNOWN'}, f'fabrication.{key}: invalid answer')
    for key in ('scope', 'evidence_note'):
        _text(fabrication[key], f'fabrication.{key}')
    _require(fabrication['scope'] in {'complete_compute_board', 'module_and_carrier',
                                     'carrier_only', 'complete_camera'},
             'invalid fabrication scope')
    for key in ('available_files', 'missing_or_unverified', 'download_urls', 'evidence_urls'):
        texts(fabrication[key], key, urls=key.endswith('_urls'))
    if fabrication['license_name'] is not None:
        _text(fabrication['license_name'], 'hardware license')
    if fabrication['license_url'] is not None:
        _https(fabrication['license_url'], 'hardware license URL')
    if fabrication['complete_files'] == 'YES':
        _require(bool(fabrication['download_urls']) and not fabrication['missing_or_unverified'],
                 'complete fabrication package needs downloads and no unverified required files')
        complete_scopes = ({'complete_compute_board', 'module_and_carrier'}
                           if candidate['role'] == 'recorder' else {'complete_camera'})
        _require(fabrication['scope'] in complete_scopes,
                 'complete files must cover the whole device, not a carrier or another role')
    if fabrication['commercial_reuse'] == 'YES':
        _require(fabrication['license_name'] is not None and fabrication['license_url'] is not None,
                 'commercial fabrication rights require a named license and URL')
    if fabrication['feasible'] == 'YES':
        _require(fabrication['complete_files'] == fabrication['commercial_reuse'] == 'YES'
                 and bool(fabrication['evidence_urls']),
                 'fabrication feasibility needs complete files, commercial rights and production evidence')
    if candidate['role'] == 'recorder':
        expected = {'PASS': 'YES', 'FAIL': 'NO', 'UNKNOWN': 'UNKNOWN'}[
            candidate['gates']['independent_board_fabrication']['status']]
        _require(fabrication['feasible'] == expected, 'fabrication answer contradicts mandatory gate')
    unit = _money(fabrication['unit_cost_usd'], 'manufacturing unit cost', nullable=True)
    _money(fabrication['engineering_tooling_usd'], 'engineering/tooling', nullable=True)
    quantity = fabrication['quantity_at_cost']
    _require(quantity is None or type(quantity) is int and quantity > 0, 'invalid manufacturing quantity')
    _require(unit is None or quantity is not None, 'manufacturing unit cost needs a stated quantity')
    _require(isinstance(details['offer_terms'], dict)
             and set(details['offer_terms']) == set(candidate['sourcing']['vendors']),
             'offer terms must cover each vendor')
    for terms in details['offer_terms'].values():
        _fields(terms, {'quantity_moq', 'shipping_origin', 'warranty_returns', 'included_parts'}, 'offer terms')
        for value in terms.values():
            if value is not None:
                _text(value, 'offer term')
    refs = details['evidence']
    _require(isinstance(refs, list) and bool(refs) and len(refs) == len(set(refs)),
             'report details require evidence')
    expected = {key: value for key, value in details.items() if key != 'evidence'}
    for ref in refs:
        _require(ref in claims and claims[ref]['value'] == expected,
                 'report details require matching evidence')
        _require(any(s['support'] in {'SUPPORTED', 'RECORDED'} for s in claims[ref]['sources']),
                 'report details source support unreviewed')
        if 'YES' in {fabrication['complete_files'], fabrication['commercial_reuse'], fabrication['feasible']}:
            _require(any(s['support'] == 'SUPPORTED' for s in claims[ref]['sources']),
                     'positive fabrication answers require reviewed support')


def _validate_archive(archive: Any) -> None:
    if archive is None:
        return
    _fields(archive, {"source_path", "source_sha256", "prefix_markdown", "offers",
                      "suffix_markdown", "offer_count", "lookup_gaps"}, "research_archive")
    _require(isinstance(archive["offers"], list), "research_archive.offers: expected list")
    ids = []
    pieces = [archive["prefix_markdown"]]
    for offer in archive["offers"]:
        _fields(offer, {"id", "url", "markdown"}, "archived offer")
        _text(offer["id"], "archived offer.id")
        _https(offer["url"], "archived offer.url")
        _require(isinstance(offer["markdown"], str), "archived offer.markdown: expected text")
        ids.append(offer["id"])
        pieces.append(offer["markdown"])
    pieces.append(archive["suffix_markdown"])
    _require(len(ids) == len(set(ids)) == archive["offer_count"],
             "research_archive: duplicate or missing offers")
    digest = hashlib.sha256("".join(pieces).encode()).hexdigest()
    _require(digest == archive["source_sha256"], "research_archive: preservation hash mismatch")
    _require(isinstance(archive["lookup_gaps"], list), "lookup_gaps: expected list")
    for gap in archive["lookup_gaps"]:
        _fields(gap, {"id", "url"}, "lookup gap")
        _https(gap["url"], "lookup gap.url")
    gap_ids = [gap["id"] for gap in archive["lookup_gaps"]]
    _require(len(gap_ids) == len(set(gap_ids)) and not set(gap_ids).intersection(ids),
             "research_archive: duplicate lookup gap or inspected offer")


def validate_catalog(catalog: Any, evidence: Any, requirements: bytes) -> None:
    """Reject unsafe/inconsistent records; never fetch URLs or promote a fact."""
    _fields(catalog, {"schema_version", "updated", "requirements_sha256", "candidates"}, "catalog")
    _fields(evidence, {"schema_version", "claims", "research_archive", "research"}, "evidence")
    _require(type(catalog["schema_version"]) is int and catalog["schema_version"] == 1
             and type(evidence["schema_version"]) is int and evidence["schema_version"] == 1,
             "unsupported schema version")
    _day(catalog["updated"], "catalog.updated")
    _require(catalog["requirements_sha256"] == hashlib.sha256(requirements).hexdigest(),
             "requirements changed: review mandatory gate coverage before regenerating")
    _require(isinstance(catalog["candidates"], list), "candidates: expected list")
    _require(isinstance(evidence["claims"], dict), "evidence.claims: expected object")
    _fields(evidence["research"], set(SOURCE_FILES), "research subjects")
    for role, subjects in evidence["research"].items():
        _require(isinstance(subjects, dict), f"{role} research: expected named subjects")
        for subject, record in subjects.items():
            _text(subject, "research subject")
            _require(isinstance(record, dict), f"{subject}: expected research object")
    ids: set[str] = set()
    configurations: set[tuple[str, str, str]] = set()
    role_counts: Counter[str] = Counter()
    candidate_fields = {
        "id", "role", "exact_model", "configuration", "buying_url", "seller",
        "price_usd", "price_date", "specifications", "local_operation", "acceleration",
        "cost", "gates", "status", "blockers", "evidence", "sourcing", "report_details",
    }
    for candidate in catalog["candidates"]:
        _fields(candidate, candidate_fields, "candidate")
        identifier = candidate["id"]
        _text(identifier, "candidate.id")
        _require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", identifier)), "invalid candidate id")
        _require(identifier not in ids, f"duplicate candidate: {identifier}")
        ids.add(identifier)
        role = candidate["role"]
        _require(role in ROLE_GATES, f"{identifier}: invalid role")
        role_counts[role] += 1
        _require(role_counts[role] <= 3, f"{role}: maximum three active candidates")
        for name in ("exact_model", "configuration"):
            _text(candidate[name], f"{identifier}.{name}")
            _require(len(candidate[name]) <= 140, f"{identifier}.{name}: keep the report compact")
        configuration = (role, candidate["exact_model"].casefold(), candidate["configuration"].casefold())
        _require(configuration not in configurations, "duplicate model/configuration")
        configurations.add(configuration)
        _https(candidate["buying_url"], f"{identifier}.buying_url")
        price = _money(candidate["price_usd"], f"{identifier}.price_usd", nullable=True)
        if price is not None:
            _day(candidate["price_date"], f"{identifier}.price_date")
            _require(candidate["price_date"] <= catalog["updated"], "price date exceeds catalog date")
        else:
            _require(candidate["price_date"] is None, "unknown price must have null price_date")
        for name in ("seller", "specifications"):
            if candidate[name] is not None:
                _text(candidate[name], f"{identifier}.{name}")
                _require(len(candidate[name]) <= 180, f"{identifier}.{name}: move detail into evidence")

        claims = evidence["claims"].get(identifier)
        _require(isinstance(claims, dict), f"{identifier}: missing evidence claims")
        _require(isinstance(candidate["evidence"], dict), f"{identifier}: expected evidence map")
        for parameter, claim in claims.items():
            _fields(claim, {"value", "sources", "note"}, f"{identifier}.{parameter} evidence")
            _require(isinstance(claim["sources"], list) and bool(claim["sources"]),
                     f"{identifier}.{parameter}: missing source")
            _require(isinstance(claim["note"], str), "evidence note must be text")
            for source in claim["sources"]:
                _fields(source, {"url", "observed_on", "basis", "support", "locator", "excerpt"},
                        f"{identifier}.{parameter} source")
                _https(source["url"], f"{identifier}.{parameter} source")
                _day(source["observed_on"], "source.observed_on")
                _require(source["observed_on"] <= catalog["updated"], "source date exceeds catalog date")
                _require(source["basis"] in {"listing", "manufacturer_specification", "measurement", "search"},
                         "invalid source basis")
                _require(source["support"] in {"SUPPORTED", "RECORDED", "UNREVIEWED", "CONTRADICTED"},
                         "invalid support review")
                _require(source["support"] != "CONTRADICTED", f"{identifier}.{parameter}: unsupported claim")
                for field in ("locator", "excerpt"):
                    _text(source[field], f"source.{field}")
        for name in ("exact_model", "configuration", "buying_url", "seller", "price_usd",
                     "price_date", "specifications"):
            value = candidate[name]
            refs = candidate["evidence"].get(name, [])
            _require(isinstance(refs, list) and len(refs) == len(set(refs)), "invalid evidence references")
            if value is None:
                _require(not refs, f"{identifier}.{name}: unknown value must not claim evidence")
                continue
            _require(bool(refs), f"{identifier}.{name}: missing evidence")
            for ref in refs:
                _require(ref in claims and claims[ref]["value"] == value,
                         f"{identifier}.{name}: evidence value mismatch")
                _require(any(s["support"] in {"SUPPORTED", "RECORDED"} for s in claims[ref]["sources"]),
                         f"{identifier}.{name}: source support unreviewed; use UNKNOWN")
        _require(set(candidate["evidence"]) <= {
            "exact_model", "configuration", "buying_url", "seller", "price_usd", "price_date", "specifications"
        }, "unexpected evidence parameter")
        _validate_sourcing(candidate, claims, catalog["updated"])

        cost = candidate["cost"]
        _fields(cost, {"intended_selling_price_usd", "components", "known_subtotal_usd", "total_cost_usd"},
                f"{identifier}.cost")
        selling = _money(cost["intended_selling_price_usd"], "intended selling price",
                         nullable=role == "recorder")
        if role == "camera":
            _require(Decimal("0") < selling <= ROLE_CEILINGS[role], "selling price exceeds role ceiling")
        elif selling is not None:
            _require(selling > 0, "intended selling price must be positive when stated")
        _require(isinstance(cost["components"], list), "cost.components: expected list")
        component_ids = []
        amounts = []
        for component in cost["components"]:
            _fields(component, {"id", "amount_usd", "evidence"}, "cost component")
            component_ids.append(component["id"])
            amount = _money(component["amount_usd"], "component amount", nullable=True)
            amounts.append(amount)
            refs = component["evidence"]
            _require(isinstance(refs, list) and len(refs) == len(set(refs)), "invalid cost evidence")
            if amount is None:
                _require(not refs, "unknown cost must not claim evidence")
            else:
                _require(bool(refs), "known cost, including zero, requires evidence")
                for ref in refs:
                    _require(ref in claims and claims[ref]["value"] == component["amount_usd"],
                             "cost evidence value mismatch")
                    _require(any(s["support"] in {"SUPPORTED", "RECORDED"} for s in claims[ref]["sources"]),
                             "cost evidence support unreviewed")
            if component["id"] == "item":
                _require(amount == price, "item component differs from price_usd")
        _require(set(component_ids) == COST_COMPONENTS[role]
                 and len(component_ids) == len(set(component_ids)), "missing or duplicate cost component")
        subtotal = sum((amount for amount in amounts if amount is not None), Decimal("0"))
        _require(_money(cost["known_subtotal_usd"], "known subtotal") == subtotal, "costs do not add up")
        total = None if any(amount is None for amount in amounts) else subtotal
        _require(_money(cost["total_cost_usd"], "total cost", nullable=True) == total,
                 "total must be UNKNOWN while any cost is missing")

        gates = candidate["gates"]
        _require(isinstance(gates, dict) and set(gates) == required_gates(role),
                 f"{identifier}: mandatory gate coverage mismatch")
        for name, gate in gates.items():
            _fields(gate, {"status", "evidence", "reason"}, f"{identifier}.{name} gate")
            _require(gate["status"] in STATES, "invalid gate status")
            _text(gate["reason"], "gate.reason")
            _require(len(gate["reason"]) <= 120, "gate.reason: move detail into evidence")
            _require(isinstance(gate["evidence"], list), "gate.evidence must be list")
            for ref in gate["evidence"]:
                _require(ref in claims, f"{identifier}.{name}: dangling gate evidence")
            if gate["status"] == "FAIL":
                _require(bool(gate["evidence"]), f"{name}: FAIL requires evidence, not missing documentation")
            if gate["status"] == "PASS":
                _require(bool(gate["evidence"]), f"{name}: PASS requires evidence")
                for ref in gate["evidence"]:
                    sources = claims[ref]["sources"]
                    _require(any(s["support"] == "SUPPORTED" for s in sources),
                             f"{name}: PASS requires reviewed support")
                if name in MEASURED_GATES:
                    _require(any(s["basis"] == "measurement" and s["support"] == "SUPPORTED"
                                 for ref in gate["evidence"] for s in claims[ref]["sources"]),
                             f"{name}: vendor specifications are not measured performance")
        if role == "camera":
            budget_state = "FAIL" if subtotal > selling / 2 else "UNKNOWN" if total is None else "PASS"
            _require(gates["production_cost"]["status"] == budget_state,
                     "production cost gate contradicts complete cost / 50% ceiling")
        _require(candidate["local_operation"] == gates["offline_lifecycle"]["status"],
                 "local-operation summary contradicts gate")
        _fields(candidate["acceleration"], {"video_decode", "inference"}, "acceleration")
        for name in ("video_decode", "inference"):
            expected = "NOT_APPLICABLE" if role == "camera" else gates[f"maintained_linux_{name}"]["status"]
            _require(candidate["acceleration"][name] == expected,
                     "decoding and inference must match separate gates")
        expected_blockers = [{"gate": name, "message": gate["reason"]}
                             for name, gate in gates.items() if gate["status"] != "PASS"]
        _require(candidate["blockers"] == expected_blockers, "blockers must include every non-PASS gate")
        _require(candidate["status"] in STATUSES and candidate["status"] == derived_status(candidate),
                 "candidate status contradicts mandatory gates")
        _validate_report_details(candidate, claims, catalog['updated'])
    _require(set(evidence["claims"]) == ids, "orphan or missing candidate evidence")
    _validate_archive(evidence["research_archive"])


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_catalog(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one role or the candidates directory, without a mixed on-disk catalog."""
    def read(source: Path) -> dict[str, Any]:
        return json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    root = path if path.is_dir() else path.parent
    roles = list(CATALOG_FILES) if path.is_dir() else [
        role for role, filename in CATALOG_FILES.items() if path.name == filename]
    _require(bool(roles), "catalog must be the candidates directory, camera.json or controller.json")
    archive = read(root.parent / HISTORY_FILE)
    _require(isinstance(archive, dict), f"{HISTORY_FILE}: expected preservation object")
    requirements = (root / "../../requirements/requirements.md").read_bytes()
    claims: dict[str, Any] = {}
    research: dict[str, Any] = {role: {} for role in SOURCE_FILES}
    catalogs = []
    for role in roles:
        role_catalog = read(root / CATALOG_FILES[role])
        _require(isinstance(role_catalog, dict) and isinstance(role_catalog.get('candidates'), list),
                 f"{CATALOG_FILES[role]}: expected candidate object and records")
        _require(all(c.get('role') == role for c in role_catalog['candidates']),
                 f"{CATALOG_FILES[role]}: wrong-role candidate")
        filename = SOURCE_FILES[role]
        document = read(root / filename)
        _fields(document, {"schema_version", "role", "description", "claims", "research"}, filename)
        _require(type(document["schema_version"]) is int and document["schema_version"] == 1,
                 f"{filename}: unsupported source schema version")
        _require(document["role"] == role, f"{filename}: source role mismatch")
        _text(document["description"], f"{filename}.description")
        _require(isinstance(document["claims"], dict), f"{filename}: expected claims object")
        expected = {c["id"] for c in role_catalog["candidates"]}
        _require(set(document["claims"]) == expected,
                 f"{filename}: orphan, missing or wrong-role candidate evidence")
        _require(not set(claims).intersection(document["claims"]), "duplicate source candidate")
        role_research = {r: document["research"] if r == role else {} for r in SOURCE_FILES}
        validate_catalog(role_catalog, {"schema_version": 1, "claims": document["claims"],
                         "research_archive": archive, "research": role_research}, requirements)
        catalogs.append(role_catalog)
        claims.update(document["claims"])
        research[role] = document["research"]
    catalog = {**catalogs[0], "updated": max(c["updated"] for c in catalogs),
               "candidates": [record for c in catalogs for record in c["candidates"]]}
    evidence = {"schema_version": 1, "claims": claims,
                "research_archive": archive, "research": research}
    validate_catalog(catalog, evidence, requirements)
    return catalog, evidence


def _markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", value)


def _sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    total = candidate["cost"]["total_cost_usd"]
    if candidate["role"] == "recorder":
        # An incomplete subtotal is a disclosed lower bound, not a landed quote.
        # No currently obtainable item price means no purchasable price ranking.
        priced = candidate["price_usd"] is not None
        amount = total if total is not None else candidate["cost"]["known_subtotal_usd"]
        return (not priced, Decimal(amount) if priced else Decimal("0"), candidate["id"])
    return (STATUSES.index(candidate["status"]), total is None,
            Decimal(total) if total is not None else Decimal("0"), candidate["id"])


def _report_text(lines: list[str], text: str, *, indent: str = "") -> None:
    """Wrap prose for source reading; URL lines are emitted separately and intact."""
    continuation = indent + ("  " if text.startswith("- ") else "")
    lines.extend(wrap(text, width=96, initial_indent=indent, subsequent_indent=continuation,
                      break_long_words=False, break_on_hyphens=False))


def _vendor_name(vendor: str) -> str:
    return VENDOR_SEARCHES.get(vendor, (vendor.replace('_', ' ').title(), ''))[0]


def _main_gap(candidate: dict[str, Any]) -> str | None:
    blockers = candidate['blockers']
    first = next((b for b in blockers if candidate['gates'][b['gate']]['status'] == 'FAIL'),
                 blockers[0] if blockers else None)
    if candidate['role'] == 'recorder' and first and candidate['gates'][first['gate']]['status'] != 'FAIL':
        first = next((b for b in blockers if b['gate'] == 'independent_board_fabrication'), first)
    return first['message'] if first else None


def render_catalog(catalog: dict[str, Any], role: str | None = None) -> str:
    """One self-contained bullet tree per exact candidate, with direct answers."""
    roles = [role] if role is not None else list(CATALOG_FILES)
    title = 'Controller candidates' if role == 'recorder' else 'Camera candidates' if role == 'camera' else 'Hardware candidates'
    lines = [f"# {title}", ""]
    answer = {'PASS': 'YES', 'FAIL': 'NO', 'UNKNOWN': 'UNKNOWN'}
    money = lambda value: 'UNKNOWN' if value is None else f'USD {value}'

    def bullet(text: str, level: int = 1) -> None:
        _report_text(lines, '- ' + _markdown_text(text), indent='  ' * level)

    for selected_role in roles:
        eligible = sorted((c for c in catalog['candidates']
                           if c['role'] == selected_role and c['status'] != 'REJECTED'), key=_sort_key)
        if not eligible:
            bullet('No eligible candidate verified. Rejected records remain in JSON.', 0)
            lines.append("")
        for candidate in eligible:
            details = candidate['report_details']
            fabrication = details['fabrication']
            vendors = candidate['sourcing']['vendors']
            seen_urls: set[str] = set()

            def url_bullet(label: str, url: str | None, level: int = 2) -> None:
                if url is not None and url not in seen_urls:
                    lines.append('  ' * level + f'- {label}: {url}')
                    seen_urls.add(url)

            bullet(candidate['sourcing']['full_product_name'], 0)
            bullet('Bottom line')
            states = [g['status'] for g in candidate['gates'].values()]
            meets = 'NO' if 'FAIL' in states else 'YES' if all(s == 'PASS' for s in states) else 'UNKNOWN'
            bullet(f'Meets all our requirements: {meets}.', 2)
            gap = _main_gap(candidate)
            if gap:
                bullet(f'Main unresolved item: {gap}.', 2)
            lines.append('')
            bullet('Approximate cost')
            date = candidate['price_date'] or 'UNKNOWN'
            bullet(f'Board/assembled configuration: {money(candidate["price_usd"])}; price date {date}.', 2)
            bullet(f'Complete delivered hardware: {money(candidate["cost"]["total_cost_usd"])}.', 2)
            if candidate['price_usd'] is not None and candidate['cost']['known_subtotal_usd'] != candidate['price_usd']:
                bullet(f'Known item/parts subtotal: {money(candidate["cost"]["known_subtotal_usd"])}.', 2)
            bullet('Included: ' + ('; '.join(details['included']) or 'UNKNOWN') + '.', 2)
            bullet('Still needed: ' + ('; '.join(details['still_needed']) or 'UNKNOWN') + '.', 2)
            components = {c['id']: c['amount_usd'] for c in candidate['cost']['components']}
            completion = 'required_completion_parts' if selected_role == 'recorder' else 'required_power_mounts_and_cables'
            bullet(f'Required completion/accessories: {money(components[completion])}.', 2)
            bullet(f'Freight: {money(components["inbound_freight"])}. Duty: {money(components["duty"])}.', 2)
            bullet('Recording SSD: separately priced; not included in the controller estimate.' if selected_role == 'recorder'
                   else 'Recorder and recording drive: separate from the camera price.', 2)
            if selected_role == 'recorder':
                bullet('Price is for ranking only; no controller price gate. Unknown charges are not zero.', 2)
            lines.append('')
            bullet('Drawings and independent manufacture')
            bullet(f'Complete production files available: {fabrication["complete_files"]}.', 2)
            scope = {'complete_compute_board': 'whole compute board', 'module_and_carrier': 'compute module plus carrier',
                     'carrier_only': 'carrier only; compute module excluded',
                     'complete_camera': 'whole camera'}[fabrication['scope']]
            bullet(f'File scope under review: {scope}.', 2)
            bullet('Available: ' + ('; '.join(fabrication['available_files']) or 'UNKNOWN; no package established') + '.', 2)
            bullet('Missing/unverified: ' + ('; '.join(fabrication['missing_or_unverified']) or 'none') + '.', 2)
            for url in fabrication['download_urls']:
                url_bullet('Drawing/download', url)
            if not fabrication['download_urls']:
                bullet('Drawing/download links: UNKNOWN.', 2)
            bullet(f'Commercial manufacture/modification permitted: {fabrication["commercial_reuse"]}.', 2)
            bullet(f'Hardware license: {fabrication["license_name"] or "UNKNOWN"}.', 2)
            if fabrication['license_url']:
                url_bullet('License', fabrication['license_url'])
            else:
                bullet('License link: UNKNOWN.', 2)
            bullet(f'Independent fabrication/assembly feasible: {fabrication["feasible"]}.', 2)
            bullet('Evidence/limitations: ' + fabrication['evidence_note'], 2)
            for url in fabrication['evidence_urls']:
                url_bullet('Manufacturing evidence', url)
            quantity = fabrication['quantity_at_cost'] or 'UNKNOWN'
            bullet(f'Manufacturing unit cost: {money(fabrication["unit_cost_usd"])}; quantity {quantity}.', 2)
            bullet(f'Engineering/tooling: {money(fabrication["engineering_tooling_usd"])}; separate from unit cost.', 2)
            if selected_role == 'camera':
                bullet('Camera fabrication is not mandatory under the controller-only fabrication requirement.', 2)
            lines.append('')
            bullet('Technical fit')
            bullet('Published hardware: ' + (candidate['specifications'] or 'UNKNOWN').removeprefix('Published: '), 2)
            labels = {'processor': 'Processor', 'ram': 'RAM', 'boot_storage': 'Boot storage',
                      'video_decode': 'Hardware video decoding', 'inference': 'AI detection',
                      'network': 'Network', 'recording_connection': 'Recording connection',
                      'linux_support': 'Linux support', 'proprietary_dependencies': 'Proprietary dependencies'}
            for key, label in labels.items():
                value = details['technical'][key]
                if value is not None or selected_role == 'recorder':
                    bullet(f'{label}: {value or "UNKNOWN"}.', 2)
            if selected_role == 'recorder':
                bullet(f'Exact decode integration verified: {answer[candidate["acceleration"]["video_decode"]]}. '
                       f'Exact inference integration verified: {answer[candidate["acceleration"]["inference"]]}.', 2)
            bullet(f'App/account/cloud-free lifecycle verified: {answer[candidate["local_operation"]]}.', 2)
            for gate in ('signed_updates_and_recovery', 'system_acceptance'):
                label = 'Signed updates/recovery' if gate == 'signed_updates_and_recovery' else 'Full-system acceptance'
                bullet(f'{label} verified: {answer[candidate["gates"][gate]["status"]]}.', 2)
            vendor_urls = {v['product_url'] for v in vendors.values()}
            for url in details['technical']['documentation_urls']:
                if url not in vendor_urls:
                    url_bullet('Official documentation', url)
            lines.append('')
            bullet('Purchase options')
            for vendor, record in vendors.items():
                if record['result'] in {'NONE_FOUND', 'NOT_CHECKED'}:
                    meaning = 'NONE FOUND in recorded search' if record['result'] == 'NONE_FOUND' else 'NOT CHECKED'
                    bullet(f'{_vendor_name(vendor)}: {meaning}.', 2)
                    continue
                bullet(_vendor_name(vendor), 2)
                meaning = {'EXACT': 'exact configuration identified; not product qualification',
                           'SELECT_OPTIONS': 'select options; exact configured price/stock UNKNOWN',
                           'DIFFERENT_CONFIGURATION': 'different configuration; not an exact match',
                           'UNVERIFIED': 'exact model match UNKNOWN', 'BLOCKED': 'page read could not be verified'}[record['result']]
                bullet('Match: ' + meaning + '.', 3)
                bullet(f'Seller: {record["seller"] or "UNKNOWN"}. SKU: {record["seller_sku"] or "UNKNOWN"}.', 3)
                stock = {'IN_STOCK': 'seller reports in stock', 'OUT_OF_STOCK': 'not in stock', 'UNKNOWN': 'UNKNOWN'}[record['stock']]
                bullet(f'Price: {money(record["price_usd"])}. Stock: {stock}. Checked: {record["checked_on"] or "UNKNOWN"}.', 3)
                terms = details['offer_terms'][vendor]
                bullet(f'Quantity/MOQ: {terms["quantity_moq"] or "UNKNOWN"}. Shipping origin: {terms["shipping_origin"] or "UNKNOWN"}.', 3)
                bullet(f'Included parts: {terms["included_parts"] or "UNKNOWN; do not assume the same bundle"}.', 3)
                bullet(f'Warranty/returns: {terms["warranty_returns"] or "UNKNOWN"}.', 3)
                if record['result'] != 'EXACT':
                    bullet('Caution: ' + record['note'], 3)
                url_bullet('Product', record['product_url'], 3)
            if candidate['buying_url'] not in seen_urls:
                url_bullet('Primary buying link', candidate['buying_url'], 2)
            lines.append('')
            bullet('Search')
            bullet('Text: ' + candidate['sourcing']['search_terms'], 2)
            for vendor, record in vendors.items():
                url_bullet(_vendor_name(vendor), record['search_url'])
            lines.append('')
            bullet('Evidence date')
            bullet(f'Document review: {details["checked_on"]}; individual price/vendor dates shown above.', 2)
            bullet(f'Details: {CATALOG_FILES[selected_role]}. Sources: {SOURCE_FILES[selected_role]}.', 2)
            bullet('UNKNOWN is missing evidence, not failure. NONE FOUND is a bounded search, not global absence.', 2)
            lines.append('')
    _require(len(lines) <= MAX_REPORT_LINES, f"generated report exceeds the {MAX_REPORT_LINES}-line budget")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate hardware candidates and generate the compact report; offline only.")
    parser.add_argument("action", choices=("lint", "render"))
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG,
                        help="candidates directory (both roles), camera.json or controller.json")
    args = parser.parse_args(argv)
    try:
        catalog, _ = load_catalog(args.catalog)
        root = args.catalog if args.catalog.is_dir() else args.catalog.parent
        roles = list(CATALOG_FILES) if args.catalog.is_dir() else [
            role for role, filename in CATALOG_FILES.items() if args.catalog.name == filename]
        outputs = [(root / Path(CATALOG_FILES[role]).with_suffix('.md'), render_catalog(catalog, role))
                   for role in roles]
        if args.action == "render":
            for output, rendered in outputs:
                output.write_text(rendered, encoding="utf-8")
            shortlisted = sum(c['status'] != 'REJECTED' for c in catalog['candidates'])
            rejected = len(catalog['candidates']) - shortlisted
            print(f"Generated {', '.join(str(p) for p, _ in outputs)}; {shortlisted} shortlisted, "
                  f"{rejected} rejected records retained; no purchase approval.")
        else:
            for output, rendered in outputs:
                _require(output.exists() and output.read_text(encoding="utf-8") == rendered,
                         f"generated report is stale: {output}; run tools/candidates render")
            print("PASS: schema, evidence reviews, costs, mandatory gates and generated report.")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"candidate validation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
