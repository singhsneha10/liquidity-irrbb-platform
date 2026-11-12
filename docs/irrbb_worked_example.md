# Worked Example: IRRBB (Delta EVE and Delta NII) by Hand, Basel and UK PRA

> A small, made-up GBP book worked out **before** any IRRBB code was written.
> `tests/test_irrbb_worked_example.py` holds these answers, and the code must reproduce them.
> The figures were rechecked with a plain arithmetic script. The same script reproduces the
> Basel Framework's own example in SRP31.92: steepener **+25.4 bp** and flattener **-1.6 bp** at
> 3.5 years with 100 bp shocks. Parameters come from `config/irrbb/basel.yaml` and `pra.yaml`.

A **flat 1% base curve** is used so that the UK post-shock floor (-100 bp rising to 0% at 20
years) actually binds under the "down" shocks. That is where Basel and the UK differ.

## 1. The book and its repricing cash flows

| Item | Amount | Repricing | Bucket (SRP31.96 Table 3) | Cash flow time *t* |
|---|---:|---|---|---:|
| Fixed-rate corporate loan (asset) | 1,000 | at maturity, about 4.7 years | 4Y-5Y | 4.5 |
| Stable retail deposits (liability, non-maturity) | 800 | core 70% = **560** at 4 years; non-core 30% = **240** overnight | 3Y-4Y; overnight | 3.5; 0.0028 |

- **Non-maturity deposits:** core share 70% and core maturity 4 years, from config. These are
  judgement values inside the caps (90% and 5 years for transactional retail). Non-core
  reprices overnight (SRP31.111).
- **Cash flows** are principal at the repricing date, signed (+ asset, - liability). Commercial
  margins and coupons are excluded (SRP31.100 allows this).
- **Equity** is excluded from the cash flows (SRP31.97). It is 1,000 - 800 = **200**, and is
  used as Tier 1 for the outlier test.

## 2. Shocked rates

GBP shock sizes (SRP31.90 Table 2 = ICAA 9.11 Table 1): parallel **275 bp**, short **425 bp**,
long **250 bp**. Decay *x* = 4 years.

- Short shock at *t* = 0.0425 x e^(-t/4); long shock = 0.0250 x (1 - e^(-t/4))
- Steepener = -0.65 x short + 0.9 x long; flattener = +0.8 x short - 0.6 x long
- **UK floor** (SS31/15 2.9B(iii)): floor(*t*) = -1% + 1% x min(*t*, 20) / 20. A shocked rate
  below the floor is raised to it (a base rate already below the floor would be kept).

Shocked rates at each cash flow time (base 1%):

| Scenario | *t* = 0.0028 Basel | *t* = 0.0028 UK | *t* = 3.5 Basel | *t* = 3.5 UK | *t* = 4.5 Basel | *t* = 4.5 UK |
|---|---:|---:|---:|---:|---:|---:|
| parallel up | 3.7500% | 3.7500% | 3.7500% | 3.7500% | 3.7500% | 3.7500% |
| parallel down | -1.7500% | **-0.9999%** | -1.7500% | **-0.8250%** | -1.7500% | **-0.7750%** |
| steepener | -1.7590% | **-0.9999%** | 1.1605% | 1.1605% | 1.6227% | 1.6227% |
| flattener | 4.3966% | 4.3966% | 1.5426% | 1.5426% | 1.0908% | 1.0908% |
| short up | 5.2470% | 5.2470% | 2.7717% | 2.7717% | 2.3798% | 2.3798% |
| short down | -3.2470% | **-0.9999%** | -0.7717% | -0.7717% | -0.3798% | -0.3798% |

Bold: the UK floor binds. (At *t* = 3.5 the floor is -0.825%, so short down at -0.7717% is
above it and is not floored.)

## 3. Delta EVE

Discount factor DF(*t*) = e^(-rate x *t*) (continuous compounding; see the note at the end).
Delta EVE = sum of cash flow x (DF shocked - DF base). A negative value is a loss.

**Parallel up, line by line:**

| Cash flow | DF base (1%) | DF shocked (3.75%) | Cash flow x difference |
|---|---:|---:|---:|
| +1,000 at 4.5 | e^(-0.045) = 0.955997 | e^(-0.16875) = 0.844720 | 1,000 x -0.111277 = **-111.28** |
| -560 at 3.5 | e^(-0.035) = 0.965605 | e^(-0.13125) = 0.876998 | -560 x -0.088607 = **+49.62** |
| -240 at 0.0028 | 0.999972 | 0.999895 | -240 x -0.000077 = **+0.02** |
| | | **Delta EVE** | **-61.64** |

The loan loses more value than the deposits, because it reprices later, so the bank loses
when rates rise.

**All six scenarios:**

| Scenario | Basel delta EVE | UK delta EVE |
|---|---:|---:|
| parallel up | -61.639071 | -61.639071 |
| parallel down | +71.284659 | **+43.812659** |
| steepener | -23.405629 | -23.400527 |
| flattener | +6.297434 | +6.297434 |
| short up | -25.012091 | -25.012091 |
| short down | +26.618959 | +26.634061 |

**EVE risk measure** = the worst loss across scenarios, after adding currencies together. Basel
counts losses only (SRP31.129(5)). The UK counts gains at 50% (SS31/15 2.9B(iv)). With one
currency this changes only the gain scenarios, not the worst loss:

- Worst loss = **61.639071** (parallel up), the same in both regimes.
- **Outlier test:** 15% x equity 200 = **30**. 61.64 > 30, so the bank **is an outlier** under
  both regimes. Under the UK rules it must notify the PRA (ICAA 9.4A).

## 4. Delta NII (12 months, constant balance sheet)

Only cash flows repricing **within** 12 months change NII. Each earns or pays the rate change
for the rest of the year:

Delta NII = sum of cash flow x (shocked rate - base rate) x (1 - *t*), for *t* < 1.

Here only the non-core deposits (-240 at *t* = 0.0028) reprice within the year:

| Scenario | Rate change at *t* = 0.0028 | Basel delta NII | UK delta NII |
|---|---|---:|---:|
| parallel up | +2.75% (both) | -240 x 0.0275 x 0.9972 = **-6.581520** | **-6.581520** |
| parallel down | Basel -2.75%; UK floored to -1.99986% | **+6.581520** | **+4.786225** |

## Answers the code must reproduce

| Measure | Basel | UK PRA |
|---|---:|---:|
| Steepener / flattener at 3.5y, 100 bp shocks (SRP31.92) | +25.4 bp / -1.6 bp | same formula |
| Delta EVE, parallel up | -61.639071 | -61.639071 |
| Delta EVE, parallel down | +71.284659 | +43.812659 |
| Delta EVE, steepener | -23.405629 | -23.400527 |
| Delta EVE, short down | +26.618959 | +26.634061 |
| Worst EVE loss / scenario | 61.639071 / parallel up | 61.639071 / parallel up |
| Outlier (loss > 15% x 200 = 30) | yes | yes |
| Delta NII, parallel up / down | -6.581520 / +6.581520 | -6.581520 / +4.786225 |

## Method notes (labelled)

1. Cash flows are principal at the repricing date. Coupons and commercial margins are excluded.
2. Discounting uses continuous compounding, DF = e^(-r x t). The standardised framework's
   formulas are images on bis.org, so their exact form was not checked in the text.
3. Non-maturity deposit core share and maturity are judgement values within the regulatory
   caps.
4. The two NII scenarios (parallel up and down), the 12-month horizon and the constant balance
   sheet are assumptions. Neither text names the scenarios.
5. Tier 1 capital is taken as the implied equity.
