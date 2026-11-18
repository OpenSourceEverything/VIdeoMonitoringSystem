"""Wi-Fi regulatory-domain validation shared by every provisioning surface."""

from __future__ import annotations


# ISO 3166-1 alpha-2 assigned country codes.  Keeping this small immutable
# table in the application avoids a network lookup or an optional dependency
# at provisioning time.  The special wireless "world" code, 00, is
# intentionally absent: a product must name the country in which it operates.
ISO_3166_ALPHA2 = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
    BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ
    EC EE EG EH ER ES ET
    FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
    HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT
    JE JM JO JP
    KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY
    MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ
    OM
    PA PE PF PG PH PK PL PM PN PR PS PT PW PY
    QA
    RE RO RS RU RW
    SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
    TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
    UA UG UM US UY UZ
    VA VC VE VG VI VN VU
    WF WS
    YE YT
    ZA ZM ZW
    """.split()
)

WIFI_COUNTRY_ENVIRONMENT_VARIABLE = "VMS_WIFI_COUNTRY"


def validate_wifi_country(value: object) -> str:
    """Return an exact uppercase assigned country code or fail closed.

    Provisioning must not silently uppercase or infer this value: choosing the
    correct regulatory domain is an operator decision with legal and radio
    behavior consequences.
    """

    if not isinstance(value, str) or value not in ISO_3166_ALPHA2:
        raise ValueError(
            "wifi_country must be an assigned uppercase ISO 3166-1 alpha-2 code"
        )
    return value


__all__ = [
    "ISO_3166_ALPHA2",
    "WIFI_COUNTRY_ENVIRONMENT_VARIABLE",
    "validate_wifi_country",
]
