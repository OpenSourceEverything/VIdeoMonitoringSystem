"""Keep the current human entry points small, coherent and navigable."""

import re
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


class DocumentationTests(unittest.TestCase):
    def test_requirements_have_an_explicit_filename_and_no_stale_links(self):
        self.assertEqual({p.name for p in (DOCS / "requirements").glob("*.md")}, {"requirements.md"})
        for document in [ROOT / "README.md", *DOCS.rglob("*.md")]:
            self.assertNotIn("requirements/README.md", document.read_text())
            for address in re.findall(r"\]\(([^)\n]+)\)", document.read_text()):
                if "requirements/requirements.md" in address:
                    self.assertTrue((document.parent / address.split("#", 1)[0]).exists(), (document, address))

    def test_single_current_design_and_no_parallel_design_directory(self):
        self.assertEqual(
            {path.name for path in DOCS.iterdir() if path.is_dir()},
            {"requirements", "research", "archive"},
        )
        self.assertEqual({path.name for path in DOCS.glob("*.md")}, {"README.md", "design.md"})

    def test_archive_contains_only_research_reviews_and_measurements(self):
        allowed = {"research", "reviews", "diagnostics"}
        for path in (DOCS / "archive").rglob("*"):
            if path.is_file():
                relative = path.relative_to(DOCS / "archive")
                self.assertTrue(relative.parts[0] in allowed or str(relative) == "README.md", relative)

    def test_consolidated_design_is_source_readable_and_keeps_component_boundaries(self):
        design = (DOCS / "design.md").read_text()
        self.assertNotRegex(design, r"(?m)^\s*\|?\s*:?-{3,}:?\s*\|")
        anchors = re.findall(r'<a id="([^"]+)"></a>', design)
        self.assertEqual(len(anchors), len(set(anchors)))
        for anchor in ("architecture", "hardware-and-bom", "camera-onboarding",
                       "camera-route-handoff", "provisioning", "event-storage",
                       "media-delivery", "owner-interface", "diagnostics",
                       "notifications", "integrations", "security", "remote-access"):
            self.assertIn(anchor, anchors)

    def test_current_design_section_links_resolve(self):
        design = DOCS / "design.md"
        anchors = set(re.findall(r'<a id="([^"]+)"></a>', design.read_text()))
        documents = [ROOT / "README.md", *DOCS.rglob("*.md"),
                     ROOT / "platforms/linux/recorder/README.md"]
        for document in documents:
            if "archive" in document.relative_to(ROOT).parts:
                continue
            for address in re.findall(r"\]\(([^)\n]+)\)", document.read_text()):
                if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", address):
                    continue
                target, separator, fragment = address.partition("#")
                path = (document.parent / unquote(target)).resolve() if target else document
                if path == design and separator:
                    with self.subTest(document=document, fragment=fragment):
                        self.assertIn(fragment, anchors)

    def test_current_relative_document_links_resolve(self):
        for document in DOCS.rglob("*.md"):
            if "archive" in document.relative_to(DOCS).parts:
                continue
            for address in re.findall(r"\]\(([^)\n]+)\)", document.read_text()):
                address = address.strip("<>")
                if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", address) or address.startswith("#"):
                    continue
                target = unquote(address.split("#", 1)[0])
                with self.subTest(document=str(document.relative_to(ROOT)), target=target):
                    self.assertTrue((document.parent / target).exists())

    def test_current_entry_pages_do_not_send_readers_to_old_task_queue(self):
        for path in (DOCS / "README.md", DOCS / "design.md"):
            self.assertNotIn("archive/tasks/README.md", path.read_text())
        self.assertTrue((DOCS / "README.md").read_text().isascii())
