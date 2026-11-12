# Worked Example: LCR, NSFR and Maturity Ladder by Hand (Basel and UK PRA)

> A small, made-up portfolio worked out by hand **before** any calculation code was written.
> `tests/test_liquidity_worked_example.py` holds the same positions and these answers. The
> LCR and NSFR code must reproduce them. Amounts are GBP millions. Factors come from
> `config/liquidity/basel.yaml` and `config/liquidity/pra.yaml`, with their references.

The portfolio was chosen so that **the Level 2B cap and the 75% inflow cap both bite**. It also
contains two edge cases:
- **A4:** a corporate bond maturing in 20 days counts as HQLA, not as an inflow.
- **L5, L7, L9:** funding maturing after 30 days has no LCR outflow but still counts in the NSFR.

## The portfolio (BANK_UK, GBP, one date)

| ID | Category | Side | Balance | Residual days |
|---|---|---|---:|---:|
| A1 | central_bank_reserves | asset | 100 | 1 |
| A2 | govt_bonds | asset | 200 | 400 |
| A3 | covered_bonds | asset | 100 | 900 |
| A4 | corporate_bonds | asset | 150 | 20 |
| A5 | corporate_loans | asset | 600 | 10 |
| A6 | corporate_loans | asset | 400 | 800 |
| A7 | retail_mortgages | asset | 1,500 | 5,000 |
| A8 | retail_mortgages | asset | 100 | 25 |
| L1 | retail_stable_deposits | liability | 1,200 | 1 |
| L2 | retail_less_stable_deposits | liability | 600 | 1 |
| L3 | wholesale_operational | liability | 200 | 10 |
| L4 | wholesale_non_operational | liability | 150 | 20 |
| L5 | wholesale_non_operational | liability | 100 | 90 |
| L6 | financial_institution_deposits | liability | 80 | 5 |
| L7 | financial_institution_deposits | liability | 50 | 250 |
| L8 | term_debt | liability | 100 | 15 |
| L9 | term_debt | liability | 150 | 700 |
| U1 | undrawn_committed_lines | off_balance_sheet | 200 | 400 |

- Total assets: 100 + 200 + 100 + 150 + 600 + 400 + 1,500 + 100 = **3,150**
- Total liabilities: 1,200 + 600 + 200 + 150 + 100 + 80 + 50 + 100 + 150 = **2,630**
- Equity (implied, assets minus liabilities): 3,150 - 2,630 = **520**

---

## LCR

### Stock of HQLA, before caps (after haircuts)

| ID | Basel level, haircut (ref) | Basel value | UK level, haircut (ref) | UK value |
|---|---|---:|---|---:|
| A1 | 1, 0% (LCR30.40) | 100 | 1, 0% (Art 10(2)) | 100 |
| A2 | 1, 0% (LCR30.41(3)) | 200 | 1, 0% (Art 10(1)(c)) | 200 |
| A3 | 2A, 15% (LCR30.43) | 100 x 0.85 = 85 | 1 covered bond, 7% (Art 10(1)(f)) | 100 x 0.93 = 93 |
| A4 | 2B, 50% (LCR30.45(2)) | 150 x 0.50 = 75 | 2B, 50% (Art 12(2)(b)) | 150 x 0.50 = 75 |

| | Basel | UK |
|---|---:|---:|
| Level 1 (other than covered bonds) | 300 | 300 |
| Level 1 covered bonds | n/a | 93 |
| Level 2A | 85 | 0 |
| Level 2B | 75 | 75 |

### Caps

**Basel** (LCR30.31-30.39). There is no short-term secured funding to unwind, so the "adjusted"
amounts equal the amounts above.

- Adjustment for the 15% cap = max(L2B - 15/85 x (L1 + L2A), L2B - 15/60 x L1, 0)
  = max(75 - 15/85 x 385, 75 - 15/60 x 300, 0) = max(75 - 67.941176, 75 - 75, 0)
  = **7.058824**
- Adjustment for the 40% cap = max(L2A + L2B - adj15 - 2/3 x L1, 0)
  = max(85 + 75 - 7.058824 - 200, 0) = max(-47.06, 0) = **0**
- **HQLA = 300 + 85 + 75 - 7.058824 - 0 = 452.941176**
- Check: Level 2B after the cap is 67.941176, and 67.941176 / 452.941176 = 15.0%.

**UK** (LCR Art 17, Annex I). Each lower-quality layer is capped in turn:

- Level 1 covered bonds at most 70% of the buffer, so at most 70/30 x 300 = 700. 93 is kept.
- Level 2 at most 40% of the buffer, so at most 40/60 x (300 + 93) = 262. Level 2A of 0 is kept.
- Level 2B at most 15% of the buffer, so at most 15/85 x (300 + 93 + 0) = 69.352941, which is
  below 75. **Cut to 69.352941.**
- **Liquidity buffer = 300 + 93 + 0 + 69.352941 = 462.352941**
- Check: 69.352941 / 462.352941 = 15.0%.

### Outflows (the same rates in both regimes)

| ID | Rate (Basel ref / UK ref) | Due within 30 days? | Outflow |
|---|---|---|---:|
| L1 | 5% (LCR40.7 / Art 24(1)) | always counted | 1,200 x 0.05 = 60 |
| L2 | 10% (LCR40.13 / Art 25(1)) | always counted | 600 x 0.10 = 60 |
| L3 | 25% (LCR40.26 / Art 27(1)) | yes (10 days) | 200 x 0.25 = 50 |
| L4 | 40% (LCR40.40 / Art 28(1)) | yes (20 days) | 150 x 0.40 = 60 |
| L5 | 40% | **no (90 days)** | 0 |
| L6 | 100% (LCR40.42 / Art 31a(1)) | yes (5 days) | 80 x 1.00 = 80 |
| L7 | 100% | **no (250 days)** | 0 |
| L8 | 100% (LCR40.42-43 / Art 28(6)) | yes (15 days) | 100 x 1.00 = 100 |
| L9 | 100% | **no (700 days)** | 0 |
| U1 | 10% (LCR40.64(2) / Art 31(4)) | always counted (undrawn) | 200 x 0.10 = 20 |
| | | **Total outflows** | **430** |

### Inflows (the same rates in both regimes)

| ID | Rate (Basel ref / UK ref) | Due within 30 days? | Inflow |
|---|---|---|---:|
| A4 | none: HQLA are not inflows (LCR40.88 / Art 32(6)) | yes (20 days) | 0 |
| A5 | 50% (LCR40.87 / Art 32(3)(a)) | yes (10 days) | 600 x 0.50 = 300 |
| A6 | 50% | no (800 days) | 0 |
| A7 | 50% | no | 0 |
| A8 | 50% (LCR40.86 / Art 32(3)(a)) | yes (25 days) | 100 x 0.50 = 50 |
| | | **Total inflows** | **350** |

- Inflow cap = 75% x 430 = **322.5** (LCR40.77 / Art 33(1)). 350 is above it, so inflows are
  **capped at 322.5**.
- Net outflows = 430 - 322.5 = **107.5**

### LCR

| | Basel | UK |
|---|---:|---:|
| HQLA / liquidity buffer | 452.941176 | 462.352941 |
| Net outflows | 107.5 | 107.5 |
| **LCR** | 452.941176 / 107.5 = **421.34%** | 462.352941 / 107.5 = **430.10%** |

The UK figure is higher because its covered bonds are Level 1 with a 7% haircut, against Basel's
Level 2A with 15%.

---

## NSFR

### Available stable funding (the same factors in both regimes)

| ID | Residual | Factor (Basel ref / UK ref) | ASF |
|---|---:|---|---:|
| Equity | n/a | 100% (NSF30.10(1) / Art 428o(a)) | 520 x 1.00 = 520 |
| L1 | 1 | 95% (NSF30.11 / Art 428n) | 1,200 x 0.95 = 1,140 |
| L2 | 1 | 90% (NSF30.12 / Art 428m) | 600 x 0.90 = 540 |
| L3 | 10 | 50% (NSF30.13(2) / Art 428l(a)) | 200 x 0.50 = 100 |
| L4 | 20 | 50% (NSF30.13(1) / Art 428l(b)(v)) | 150 x 0.50 = 75 |
| L5 | 90 | 50% | 100 x 0.50 = 50 |
| L6 | 5 | 0%, below 183 days (NSF30.14(1) / Art 428k(3)(c)) | 0 |
| L7 | 250 | 50%, 183 to 364 days (NSF30.13(4) / Art 428l(c)) | 50 x 0.50 = 25 |
| L8 | 15 | 0%, below 183 days (NSF30.14(1) / Art 428k(3)(d)) | 0 |
| L9 | 700 | 100%, one year or more (NSF30.10(3) / Art 428o(e)) | 150 x 1.00 = 150 |
| | | **Total ASF** | **2,600** |

### Required stable funding

| ID | Residual | Basel factor (ref) | Basel RSF | UK factor (ref) | UK RSF |
|---|---:|---|---:|---|---:|
| A1 | 1 | 0% (NSF30.25(2)) | 0 | 0% (Art 428r(1)(c)) | 0 |
| A2 | 400 | 5% (NSF30.26) | 10 | **0%** (Art 428r(1)(a)) | 0 |
| A3 | 900 | 15% (NSF30.28(1)) | 15 | **7%** (Art 428t) | 7 |
| A4 | 20 | 50% (NSF30.29(1)) | 75 | 50% (Art 428ad(a)) | 75 |
| A5 | 10 | 50%, under 1 year (NSF30.29(5)) | 300 | 50% (Art 428ad(c)(v)) | 300 |
| A6 | 800 | 85%, 1 year or more (NSF30.31(2)) | 340 | 85% (Art 428ag(c)) | 340 |
| A7 | 5,000 | 65%, 1 year or more (NSF30.30(1)) | 975 | 65% (Art 428af(1)(a)) | 975 |
| A8 | 25 | 50%, under 1 year (NSF30.29(5)) | 50 | 50% (Art 428ad(c)) | 50 |
| U1 | 400 | 5% of undrawn (NSF30.34) | 10 | 5% (Art 428s(1)(c)) | 10 |
| | | **Total RSF** | **1,775** | | **1,757** |

### NSFR

| | Basel | UK |
|---|---:|---:|
| ASF | 2,600 | 2,600 |
| RSF | 1,775 | 1,757 |
| **NSFR** | 2,600 / 1,775 = **146.48%** | 2,600 / 1,757 = **147.98%** |

The UK figure is higher because it requires no stable funding for Level 1 government bonds (0%
against 5%) and less for its Level 1 covered bonds (7% against 15%).

---

## Maturity ladder (contractual; the same in both regimes)

Each asset's principal flows in, and each liability's flows out, in the bucket where it matures
(buckets from `config/liquidity/ladder.yaml`; `up_to_days` is the last day included).

| Bucket | Inflows | Outflows | Net | Cumulative |
|---|---|---|---:|---:|
| overnight (day 1) | A1 100 | L1 1,200 + L2 600 = 1,800 | -1,700 | -1,700 |
| 2-7 days | 0 | L6 80 | -80 | -1,780 |
| 8-30 days | A4 150 + A5 600 + A8 100 = 850 | L3 200 + L4 150 + L8 100 = 450 | +400 | -1,380 |
| 31-90 days | 0 | L5 100 | -100 | -1,480 |
| 91-180 days | 0 | 0 | 0 | -1,480 |
| 181-365 days | 0 | L7 50 | -50 | -1,530 |
| 1-2 years | A2 200 | L9 150 | +50 | -1,480 |
| 2-5 years | A3 100 + A6 400 = 500 | 0 | +500 | -980 |
| over 5 years | A7 1,500 | 0 | +1,500 | **+520** |

- **Contingent** (off balance sheet, outside the buckets): U1 undrawn lines **200**.
- **Cross-check:** every asset and liability falls in exactly one bucket, so the final
  cumulative gap equals assets minus liabilities: 3,150 - 2,630 = **520**, the equity.
- The large overnight gap is normal. Retail deposits are contractually repayable on demand,
  which is why the LCR applies behavioural run-off rates (5% and 10%) to them instead.

---

## Answers the code must reproduce

| Measure | Basel | UK PRA |
|---|---:|---:|
| HQLA before caps (after haircuts) | 460 | 468 |
| HQLA after caps | 452.941176 | 462.352941 |
| Outflows | 430 | 430 |
| Inflows before cap | 350 | 350 |
| Inflows after cap | 322.5 | 322.5 |
| Net outflows | 107.5 | 107.5 |
| LCR | 4.213406 | 4.300958 |
| ASF | 2,600 | 2,600 |
| RSF | 1,775 | 1,757 |
| NSFR | 1.464789 | 1.479795 |
| Ladder: final cumulative gap / contingent | 520 / 200 | 520 / 200 |
