from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from video_monitoring_system.sourcing_watch import DEFAULT_MANIFEST, check_source, load_manifest
from video_monitoring_system.candidate_catalog import CATALOG_FILES, load_catalog


class SourcingWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = {
            "id": "camera",
            "exact_product": "Camera CAM-123",
            "candidate_state": "active",
            "owner_price_ceiling_usd": 100,
        }
        self.source = {
            "channel": "manufacturer",
            "label": "Exact camera",
            "url": "https://example.com/camera",
            "required_terms": ["CAM-123"],
            "available_terms": ["Add to Cart"],
            "unavailable_terms": ["Sold out"],
        }

    def test_reports_exact_available_source_and_structured_offer(self) -> None:
        page = b"""<!doctype html><html><body>
        <h1>Camera CAM-123</h1><button>Add to Cart</button>
        <script type="application/ld+json">
        {"@type":"Product","name":"Camera CAM-123","sku":"CAM-123",
         "offers":{"@type":"Offer","price":"79.99","priceCurrency":"USD",
         "availability":"https://schema.org/InStock"}}
        </script></body></html>"""

        result = check_source(
            self.item,
            self.source,
            timeout=1,
            fetch=lambda _url, _timeout: (200, self.source["url"], page),
        )

        self.assertEqual(result["state"], "available_signal")
        self.assertTrue(result["identity_terms_present"])
        self.assertEqual(result["structured_offers"][0]["price"], "79.99")
        self.assertEqual(result["structured_offers"][0]["currency"], "USD")

    def test_unavailable_signal_wins_over_add_to_cart(self) -> None:
        page = b"<h1>CAM-123</h1><p>Sold out</p><p>Add to Cart</p>"

        result = check_source(
            self.item,
            self.source,
            timeout=1,
            fetch=lambda _url, _timeout: (200, self.source["url"], page),
        )

        self.assertEqual(result["state"], "unavailable_signal")

    def test_does_not_accept_a_similar_product(self) -> None:
        page = b"""<h1>Camera CAM-124</h1><button>Add to Cart</button>
        <script type="application/ld+json">
        {"@type":"Product","name":"Unrelated accessory","sku":"ACC-1",
         "offers":{"@type":"Offer","price":"0.99","priceCurrency":"USD"}}
        </script>"""

        result = check_source(
            self.item,
            self.source,
            timeout=1,
            fetch=lambda _url, _timeout: (200, self.source["url"], page),
        )

        self.assertEqual(result["state"], "identity_unverified")
        self.assertEqual(result["missing_identity_terms"], ["CAM-123"])
        self.assertEqual(result["structured_offers"], [])
        self.assertEqual(result["discarded_unmatched_offer_count"], 1)

    def test_manifest_rejects_non_https_source(self) -> None:
        document = {
            "schema_version": 1,
            "items": [
                {
                    "id": "bad",
                    "candidate_state": "active",
                    "sources": [
                        {
                            "channel": "marketplace",
                            "label": "bad",
                            "url": "http://example.com/item",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_manifest(path)

    def test_validated_catalog_is_the_default_watch_source(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / DEFAULT_MANIFEST).is_dir())
        result = load_manifest(root / DEFAULT_MANIFEST)
        catalog, _ = load_catalog(root / DEFAULT_MANIFEST)
        expected = {c['id'] for c in catalog['candidates'] if c['status'] != 'REJECTED'}
        self.assertEqual({item['id'] for item in result['items']}, expected)
        self.assertTrue(all(item["candidate_state"] == "investigated"
                            for item in result["items"]))
        self.assertEqual(result["items"][0]["id"], "annke-c500p-i51hj")
        self.assertEqual(result["items"][0]["sources"][0]["required_terms"],
                         ["ANNKE", "C500P", "I51HJ"])
        self.assertNotIn("annke:c500p-4pcs", [item["id"] for item in result["items"]])
        self.assertNotIn("B0CHWPQNGF", [item["id"] for item in result["items"]])
        recorder_ids = {c['id'] for c in catalog['candidates'] if c['role'] == 'recorder'}
        for item in result['items']:
            if item['id'] in recorder_ids:
                self.assertIsNone(item['owner_price_ceiling_usd'])
                self.assertEqual(item['sources'][0]['channel'], 'manufacturer')
        for item in result['items']:
            candidate = next(c for c in catalog['candidates'] if c['id'] == item['id'])
            self.assertEqual([s['url'] for s in item['sources']], [candidate['buying_url']])
            search_urls = {v['search_url'] for v in candidate['sourcing']['vendors'].values()}
            self.assertNotIn(item['sources'][0]['url'], search_urls)
        for role, filename in CATALOG_FILES.items():
            selected = load_manifest(root / DEFAULT_MANIFEST / filename)
            self.assertEqual({item['id'] for item in selected['items']},
                             {c['id'] for c in catalog['candidates']
                              if c['role'] == role and c['status'] != 'REJECTED'})

    def test_markdown_reader_does_not_watch_archived_or_uninspected_links(self) -> None:
        document = """# Production candidates
## Buy-link shortlist
### Controllers to compare
- **Selected PC** — [Amazon CURRENT123](https://www.amazon.com/dp/CURRENT123).
## Coverage and limits
- **Old PC** — [Amazon OLD123](https://www.amazon.com/dp/OLD123).
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.md"
            path.write_text(document)
            items = load_manifest(path)["items"]
            self.assertEqual([p["id"] for p in items], ["CURRENT123"])

    def test_markdown_reader_rejects_a_link_with_mismatched_marketplace(self) -> None:
        document = """## Buy-link shortlist
- **PC** — [Amazon ASIN123](https://unrelated.example/dp/ASIN123).
## Coverage
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.md"
            path.write_text(document)
            with self.assertRaisesRegex(ValueError, "marketplace"):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
