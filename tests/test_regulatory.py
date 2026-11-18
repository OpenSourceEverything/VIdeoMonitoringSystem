import unittest

from video_monitoring_system.regulatory import (
    ISO_3166_ALPHA2,
    validate_wifi_country,
)


class RegulatoryCountryTests(unittest.TestCase):
    def test_table_contains_only_unique_uppercase_two_letter_codes(self) -> None:
        self.assertGreater(len(ISO_3166_ALPHA2), 200)
        self.assertTrue(
            all(
                len(country) == 2 and country.isascii() and country.isupper()
                for country in ISO_3166_ALPHA2
            )
        )
        for expected in ("AU", "CA", "GB", "JP", "US", "ZA"):
            self.assertIn(expected, ISO_3166_ALPHA2)

    def test_validator_does_not_infer_or_normalize_a_country(self) -> None:
        self.assertEqual(validate_wifi_country("US"), "US")
        for value in ("us", "USA", "ZZ", "00", "", None, 1, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_wifi_country(value)


if __name__ == "__main__":
    unittest.main()
