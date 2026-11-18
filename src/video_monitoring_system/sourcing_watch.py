"""Read-only availability checks for exact production hardware sources."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_MANIFEST = Path("docs/research/candidates")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
USER_AGENT = "OSE-Cameras-source-watch/1.0 (read-only availability check)"


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.json_ld_documents: list[Any] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        lowered = tag.lower()
        attributes = {name.lower(): value for name, value in attrs}
        if lowered in {"style", "noscript"}:
            self._ignored_depth += 1
        if lowered == "script":
            script_type = (attributes.get("type") or "").lower()
            if script_type == "application/ld+json":
                self._json_ld_depth += 1
                self._json_ld_parts = []
            else:
                self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "script" and self._json_ld_depth:
            payload = "".join(self._json_ld_parts).strip()
            self._json_ld_depth -= 1
            self._json_ld_parts = []
            if payload:
                try:
                    self.json_ld_documents.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
            return
        if lowered in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self._json_ld_parts.append(data)
        elif not self._ignored_depth:
            stripped = data.strip()
            if stripped:
                self.visible_parts.append(stripped)


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _product_offers(documents: Iterable[Any]) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for document in documents:
        for node in _walk_json(document):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if "Product" not in types:
                continue
            product_name = str(node.get("name") or "")
            product_id = str(node.get("sku") or node.get("mpn") or "")
            raw_offers = node.get("offers")
            candidates = raw_offers if isinstance(raw_offers, list) else [raw_offers]
            for raw_offer in candidates:
                if not isinstance(raw_offer, dict):
                    continue
                price = raw_offer.get("price") or raw_offer.get("lowPrice")
                currency = raw_offer.get("priceCurrency")
                availability = raw_offer.get("availability")
                record = {
                    "product_name": product_name or None,
                    "product_id": product_id or None,
                    "price": str(price) if price is not None else None,
                    "currency": str(currency) if currency is not None else None,
                    "availability": str(availability) if availability else None,
                }
                key = tuple(
                    str(record[name] or "")
                    for name in (
                        "product_name",
                        "product_id",
                        "price",
                        "availability",
                    )
                )
                if key not in seen:
                    seen.add(key)
                    offers.append(record)
    return offers


def _normalize_text(parts: Iterable[str]) -> str:
    return re.sub(r"\s+", " ", html.unescape(" ".join(parts))).strip()


def _validated_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("source URL must be an HTTPS URL without credentials")
    return raw_url


def _fetch(url: str, timeout: float) -> tuple[int, str, bytes]:
    request = Request(
        _validated_url(url),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        status = int(response.status)
        final_url = response.geturl()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
        return status, final_url, body


def check_source(
    item: dict[str, Any],
    source: dict[str, Any],
    *,
    timeout: float,
    fetch: Callable[[str, float], tuple[int, str, bytes]] = _fetch,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "item_id": item["id"],
        "exact_product": item["exact_product"],
        "candidate_state": item["candidate_state"],
        "owner_price_ceiling_usd": item.get("owner_price_ceiling_usd"),
        "channel": source["channel"],
        "label": source["label"],
        "url": source["url"],
        "seller_expectation": source.get("seller_expectation"),
    }
    try:
        status, final_url, body = fetch(source["url"], timeout)
        result["http_status"] = status
        result["final_url"] = final_url
        charset_match = re.search(
            br"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", body[:8192], re.I
        )
        charset = (
            charset_match.group(1).decode("ascii", "replace")
            if charset_match
            else "utf-8"
        )
        page = body.decode(charset, "replace")
        parser = _PageParser()
        parser.feed(page)
        visible_text = _normalize_text(parser.visible_parts)
        lowered = visible_text.casefold()
        required = [str(value) for value in source.get("required_terms", [])]
        missing = [term for term in required if term.casefold() not in lowered]
        result["identity_terms_present"] = not missing
        result["missing_identity_terms"] = missing
        unavailable = [
            marker
            for marker in source.get("unavailable_terms", [])
            if str(marker).casefold() in lowered
        ]
        available = [
            marker
            for marker in source.get("available_terms", [])
            if str(marker).casefold() in lowered
        ]
        result["unavailable_signals"] = unavailable
        result["available_signals"] = available
        page_offers = _product_offers(parser.json_ld_documents)
        matching_offers = []
        for offer in page_offers:
            offer_identity = " ".join(
                str(offer.get(name) or "") for name in ("product_name", "product_id")
            ).casefold()
            if all(term.casefold() in offer_identity for term in required):
                matching_offers.append(offer)
        result["structured_offers"] = matching_offers
        result["discarded_unmatched_offer_count"] = len(page_offers) - len(
            matching_offers
        )
        if missing:
            result["state"] = "identity_unverified"
        elif unavailable:
            result["state"] = "unavailable_signal"
        elif available:
            result["state"] = "available_signal"
        else:
            result["state"] = "reachable_unverified"
    except HTTPError as error:
        result.update(state="http_error", http_status=error.code, error=str(error))
    except (URLError, TimeoutError, OSError, ValueError) as error:
        result.update(state="fetch_error", error=str(error))
    return result


def _markdown_watch_sources(text: str) -> dict[str, Any]:
    """Read only explicitly ranked comparison links, never the historical ledger."""
    section = re.search(
        r"^## Buy-link shortlist\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S
    )
    if not section:
        raise ValueError("candidate document has no buy-link shortlist")
    pattern = re.compile(
        r"^- \*\*(.+?)\*\* — \[(Amazon|Alibaba|Ebay) ([^\]\s]+)\]\(([^)\s]+)\)",
        re.M | re.I,
    )
    items = []
    seen = set()
    for match in pattern.finditer(section[1]):
        product, marketplace, offer_id, url = match.groups()
        marketplace = marketplace.lower()
        parsed = urlsplit(_validated_url(url))
        expected_host = {"amazon": "www.amazon.com", "alibaba": "www.alibaba.com",
                         "ebay": "www.ebay.com"}[marketplace]
        if parsed.hostname != expected_host:
            raise ValueError(f"buy-link marketplace does not match URL: {offer_id}")
        if offer_id not in parsed.path or offer_id in seen:
            raise ValueError(f"duplicate or mismatched buy-link identity: {offer_id}")
        seen.add(offer_id)
        items.append({
            "id": offer_id, "exact_product": product,
            "candidate_state": "investigated",
            "sources": [{
                "channel": marketplace, "label": product, "url": url,
                "required_terms": [offer_id],
                "available_terms": ["In Stock", "Buy It Now"],
                "unavailable_terms": ["Currently unavailable", "Out of stock",
                                      "This listing was ended", "no longer available"],
            }],
        })
    return {"schema_version": 1, "items": items}


def load_manifest(path: Path) -> dict[str, Any]:
    if path.is_dir():
        document = {"candidates": []}
    else:
        text = path.read_text(encoding="utf-8")
        document = _markdown_watch_sources(text) if path.suffix.lower() == ".md" else json.loads(text)
    if "candidates" in document:
        from .candidate_catalog import load_catalog

        catalog, _ = load_catalog(path)
        document = {"schema_version": 1, "items": []}
        for candidate in catalog["candidates"]:
            if candidate["status"] == "REJECTED":
                continue
            selected_vendor = next((vendor for vendor, observation in candidate['sourcing']['vendors'].items()
                                    if observation['product_url'] == candidate['buying_url']), None)
            channel = ('manufacturer' if selected_vendor == 'manufacturer' else
                       'distributor' if selected_vendor in {'digikey', 'mouser'} else 'marketplace')
            document["items"].append({
                "id": candidate["id"],
                "exact_product": candidate["exact_model"] + " / " + candidate["configuration"],
                "candidate_state": "investigated",
                "owner_price_ceiling_usd": (candidate["cost"]["intended_selling_price_usd"]
                                            if candidate['role'] == 'camera' else None),
                "sources": [{
                    "channel": channel,
                    "label": candidate["exact_model"],
                    "url": candidate["buying_url"],
                    # Every identity term must occur in visible text AND any parsed offer.
                    "required_terms": candidate["exact_model"].split(),
                    "available_terms": ["In Stock", "Add to Cart", "Buy It Now"],
                    "unavailable_terms": ["Currently unavailable", "Out of stock", "Sold out",
                                          "This listing was ended", "no longer available"],
                }],
            })
    if document.get("schema_version") != 1:
        raise ValueError("unsupported sourcing-watch schema version")
    items = document.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("manifest must contain at least one item")
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or not item.get("sources"):
            raise ValueError("each item requires id and sources")
        if item.get("candidate_state") not in {"active", "investigated"}:
            raise ValueError(f"invalid candidate_state for {item.get('id')}")
        for source in item["sources"]:
            _validated_url(source.get("url", ""))
    return document


def run_checks(
    manifest: dict[str, Any],
    *,
    selected_items: set[str] | None = None,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    found: set[str] = set()
    for item in manifest["items"]:
        if selected_items and item["id"] not in selected_items:
            continue
        found.add(item["id"])
        for source in item["sources"]:
            results.append(check_source(item, source, timeout=timeout))
    if selected_items:
        missing = selected_items - found
        if missing:
            raise ValueError(f"unknown item id(s): {', '.join(sorted(missing))}")
    return results


def _print_human(results: list[dict[str, Any]]) -> None:
    for result in results:
        offers = result.get("structured_offers") or []
        offer_text = ""
        if offers:
            prices = [
                f"{offer.get('currency') or '?'} {offer['price']}"
                for offer in offers
                if offer.get("price")
            ]
            if prices:
                offer_text = f"; structured price(s): {', '.join(prices)}"
        print(
            f"{result['item_id']} [{result['channel']}]: {result['state']}"
            f"{offer_text} -- {result['url']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check exact hardware source pages without purchasing or messaging sellers."
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="candidates directory (default), camera.json or controller.json; explicit legacy watch JSON/Markdown also supported")
    parser.add_argument("--item", action="append", dest="items")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--json", action="store_true", help="emit one JSON document")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        results = run_checks(
            manifest,
            selected_items=set(args.items) if args.items else None,
            timeout=args.timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"sourcing watch failed: {error}", file=sys.stderr)
        return 2
    checked_at = datetime.now(timezone.utc).isoformat()
    if args.json:
        json.dump(
            {"schema_version": 1, "checked_at": checked_at, "results": results},
            sys.stdout,
            indent=2,
        )
        print()
    else:
        print(f"Checked {checked_at}")
        _print_human(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
