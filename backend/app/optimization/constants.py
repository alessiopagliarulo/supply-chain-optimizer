"""
Shared freight and transport constants.

All values are cited from published industry sources. Defined once here and
imported wherever they are needed.
"""

# -- Physical / unit constants ------------------------------------------------
KM_PER_MILE = 1.60934
LBS_PER_KG = 2.20462
CWT_PER_LB = 0.01  # 1 hundredweight = 100 lbs

# -- Freight cost constants (cited) -------------------------------------------
# ATRI 2023: An Analysis of the Operational Costs of Trucking
TL_RATE_USD_PER_MILE = 2.271

# FreightWaves SONAR Q4 2023 + Old Dominion 2023 published tariff
LTL_BASE_FEE_USD = 75.0
LTL_RATE_USD_PER_CWT_MILE = 0.43

# BTS Commodity Flow Survey 2022
GROUND_KM_PER_DAY = 800.0

# Heavy-duty truck factor: 161.8 g CO2e per US SHORT ton-mile (2,000 lb =
# 907.18474 kg -- see costs.KG_PER_SHORT_TON, which is the divisor co2_kg uses).
# Source: "EPA SmartWay Shipper Partner Tool: Technical Documentation" (2013),
# as cited in EDF's Green Freight Handbook (2014) p.11, where the units are
# printed verbatim as "grams per short ton-mile".
# NOT from the EPA GHG Emission Factors Hub: no edition of the Hub contains
# 161.8 (Table 8 shows 170 in 2023, 168 in 2024, 186 in 2025). The label
# "EPA SmartWay 2023" that this repo used until 2026-09-03 was the wrong
# vintage -- the value is a genuine SmartWay figure, but from 2013.
CO2_G_PER_TON_MILE = 161.8

# -- International air freight constants (electronics, avg ~0.05 kg/unit) ------
# IATA Cargo Market Report 2023: average all-in airfreight rate for electronics
# to US: $3-7/kg depending on origin; $5.0/kg is mid-market.
# Minimum consignment handling charge (DHL/FedEx commercial): ~$150 base.
AIR_FREIGHT_BASE_USD = 150.0
AIR_FREIGHT_RATE_USD_PER_KG = 5.0
