"""Constants for Regionalwerke AG Baden integration."""

DOMAIN = "regionalwerke_baden"
CONF_TOTP_SECRET = "totp_secret"

BASE_URL = "https://www.rwb-kundenportal.ch"
LOGIN_PATH = "/login"
TFA_PATH = "/2fa_check"
LASTGANG_PATH = "/lastgangdaten"

# Fallbacks only — the live routes come from data-urls in the /lastgangdaten HTML.
ENDPOINT_OBJEKTE = "/lastgangdaten/getObjekte"
ENDPOINT_MESSLINIEN = "/lastgangdaten/getMesslinien"
ENDPOINT_MESSDATEN = "/lastgangdaten/getMessdaten"

# HA update: RWB publishes D+1 after midnight, poll once per day at 03:30 local.
DEFAULT_SCAN_HOUR = 3
DEFAULT_SCAN_MINUTE = 30

# How many past days to backfill on regular daily poll (after historic import)
BACKFILL_DAYS = 2

# Full historic import settings
HISTORIC_EARLIEST_FALLBACK = (
    "2023-01-02"  # first Monday with data verified 2023-01-02, earlier empty
)
HISTORIC_CHUNK = "week2"  # kWh per 15min, 672 points per week, efficient
HISTORIC_CHUNK_DAYS = 7

# zeitraum mapping: day2 = kWh per 15min, day = kW (needs *0.25)
ZEITRAUM_KWH = "day2"
ZEITRAUM_KW = "day"

# -- Tariffs (Energy Dashboard cost) --
# Art. 7b StromVV obliges every Swiss grid operator to publish all of its tariffs
# as one machine-readable JSON at a single public, unauthenticated address. RWB's
# carries the year in the filename, so it is built per statistics year rather than
# hard-coded — 2025 and 2027 both 404 today.
TARIFF_BASE_URL = "https://www.regionalwerke.ch"
TARIFF_PATH_TEMPLATE = "/fileadmin/Strompreise_ElCom/Baden_tariffs_{year}.json"

CONF_COST_ENABLED = "cost_enabled"
CONF_COST_PRODUCT = "cost_product"
CONF_COST_GRID = "cost_grid_tariff"
CONF_COST_MUNICIPALITY = "cost_municipality"
CONF_COST_SURCHARGE = "cost_surcharge"

DEFAULT_COST_PRODUCT = (
    "primastrom"  # RWB's Standardprodukt; einfachstrom is the alternative
)
DEFAULT_COST_GRID = (
    "OL7"  # NE7, annual consumption up to 50'000 kWh — the household grid tariff
)
DEFAULT_COST_MUNICIPALITY = "Baden"
# Netzzuschlag (2.30) + Förderabgabe Baden (0.55), Rp./kWh. Both are levied on every
# kWh but neither appears in the JSON file, so they are added on top.
#
# Reconciled 2026-08-31 against ElCom's published H4 total of 27.27 Rp./kWh:
#   energy 10.50 + grid 9.20 + concession 0.58 + surcharges 2.85 = 23.13 Rp./kWh
#   + fixed fees (OL7 base 10.- and DM7 metering 5.- CHF/month, i.e. 180.-/year,
#     which at the H4 profile of 4'500 kWh/year is another 4.00 Rp./kWh) = 27.13.
# The remaining 0.14 is rounding in ElCom's profile. The fixed fees are deliberately
# NOT part of the rate — see parse_tariff_rate — so a user who wants their bill total
# rather than the marginal cost of a kWh can add their own share here.
DEFAULT_COST_SURCHARGE = 2.85
