# Citation recovery after v36

This change addresses the remaining linker cases in the owner's `ai.md`, against
`main` at `4b913a3`. It changes emitted links and requires a **full relink**. Do not
adopt the previous engine fingerprint as output-neutral.

## Reported cases

| Source, zero-based line | Printed citation | Result |
| --- | --- | --- |
| Tosafot Shevuot, 18 | `בספ"ב (לקמן דף יט:)` | `Shevuot 19b` |
| Tosafot Shevuot, 18 | `בפ"ב (לקמן דף יד: ושם)` | `Shevuot 14b` |
| Tosafot Shevuot, 29 | `בפ"ג (לקמן דף כא: ושם)` | `Shevuot 21b` |
| Tosafot Shevuot, 254 | `בפ"ק (לעיל דף ב.)` | `Shevuot 2a`, replacing `Tosafot on Shevuot 2a` |
| Ran on Shevuot, 512 | `בפרק שבועת העדות (דף לה.)` | `Shevuot 35a` |
| Rashba Shabbat, 456 | `בפרק קמא דסוכה (יג, ב. יד, א)` | Separate anchors for `Sukkah 13b` and `Sukkah 14a`, replacing `Sukkah 20a` |
| Rashba Shabbat, 321 | `לקמן בסמוך (מ, א)` | `Shabbat 40a`, replacing `Rashba on Shabbat 40a` |

The daf target includes its opening Mishnah where appropriate; `Shevuot 2a` is
not a reference to the independently numbered *Mishnah Shevuot* book.

## Evidence and constraints

The fixture `tests/fixtures/citation_cases_v36.json` contains twelve complete source
lines and their actual raw NER responses, captured on the Windows AMD/ROCm runner
on 2026-11-02. The recorded normalization is checked before replay, and the real
production-patched Sefaria resolver is used without a running NER service.
Hand-specified expectations cover the failures above, the existing Sanhedrin and
Bava Metzia recoveries, Tosafot's own explicitly qualified DH, the inflected
self-reference at Rashba Shabbat 720, the existing Gemara reference after a prior
self-explanation at Tosafot Shevuot 255, and all seven
`שם שם` references to Genesis 48 in Agadat Bereishit line 17. The twelve source lines
produce 53 validated links. Every emitted canonical target, source hash, UTF-16
anchor and unique anchor key is checked.

A cut-off amud mark is accepted only immediately before a closing bracket or
`ושם`; quoted HTML attributes cannot supply that evidence. Relative addresses
still require a canonical numeral, a valid source position, the correct direction,
and the citing tractate's identity. A coordinated `ואמר נמי` / `והא דאמר` begins
a new governing clause only when the citation itself names a verified chapter.
Writer/self-reference and DH qualifiers remain relevant within that clause.

A chapter/daf citation supplies absolute addresses. The tractate must be explicitly
named, established by the directly paged source, or proven by a globally unique
Bavli chapter name, including competing longer chapter names. A Rif commentary's
page is never treated as the underlying Bavli page. Every member of a split list
must lie in the named chapter and have text. Existing precise correct segment
references are preserved. Rebuilt citations do not rewrite the resolver or legacy
emitted-reference history; the last validated address still travels in the
separate preceding-citation checkpoint state.

## Complete-book comparison

The final engine replayed seven complete books, 4,555 source rows. Each book's
hash exactly matches the committed v36 snapshot baseline. Raw NER was captured
on the AMD runner; both engines resolved the same responses with the production
Sefaria patch. All 976 baseline Rashba records also match the published v36
payload exactly.

| Book | Source rows | Previous links | New links |
| --- | ---: | ---: | ---: |
| תוספות על שבועות | 618 | 501 | 504 |
| משנה תורה, הלכות חגיגה | 37 | 39 | 39 |
| ר"ן על שבועות | 810 | 267 | 273 |
| רשב״א על שבת | 1,167 | 976 | 1,022 |
| פסקי תוספות על ברכות | 399 | 17 | 17 |
| אגדת בראשית | 307 | 1,782 | 1,782 |
| אמרי דעת על משלי | 1,217 | 4 | 4 |

Across the sample: 3,586 → 3,641 links; 3,583 existing records retained exactly,
58 added records and three replaced records. The replacements are precisely the
three incorrect targets in the reported Tosafot/Rashba cases; the Sukkah list now
has two separate anchors. There are no unrelated omissions. All source hashes,
canonical targets, UTF-16 anchors and unique anchor keys were validated.

All 47 literal `לעיל/לקמן דף X` occurrences in Tosafot Shevuot were checked:
44 link, and the three remaining omissions are the two explicit Rashi citations
and the foreign-tractate `סט:` citation identified in the owner's report.
Other abbreviated or unproven forms can still remain unlinked.

Every changed source line and all added targets were reviewed in context. In
particular the inflected self-reference at Rashba 720 remains outside Gemara
reconstruction, while its subsequent explicit Gemara quotation can link normally.
Machine-readable counts, source hashes and the engine identity are recorded in
`tests/fixtures/citation_recovery_v36_validation.json`.

## Local test environments

The final suite ran on CPython 3.12 in both environments:

- macOS: 561 tests, 548 passed, 13 platform/optional-dependency skips.
- Linux ARM64: 561 tests, 560 passed, one optional `jsonschema` skip. The
  committed-engine fingerprint contract also ran in this isolated git checkout.

The Linux process-observation fixture explicitly creates its mocked `os.waitid`
attribute so it also runs on macOS Python 3.12, where that function is absent.

## Reproduce

The ordinary dependency-free suite runs from this repository:

```sh
python3 -m unittest discover -s tests -v
```

For the real-engine acceptance test, use the pinned Sefaria checkout with this
repository's `ci/sefaria_resolver.patch`, its Python environment and the pinned
Mongo dump. From the Sefaria project directory:

```sh
PYTHONPATH=. .venv/bin/python /absolute/path/to/LinkerToOtzaria/scripts/verify_citation_cases.py
```

The replay reads the recorded model responses and makes no live NER requests.
The fixture records the Sefaria/model/server identities used for capture.

## Issues outside linker output

The owner's recheck already found #2695, #3453, #3029 and #2366 corrected. The
Genesis preservation cases above explicitly protect #2695.
#3480's originally reported wrong-daf behavior was not reproduced; the suspicious
nearby Rashba reference at line 321 is corrected here.

For #3252 the emitted DB anchor and target are correct; `<i data-commentator>`
tags between the visible words require application-side rendering work. #2530,
#3710, #1509, #3626, #3854 and #3264 concern commentator attachment or display.
This linker PR does not close those application/build issues. Validation of a
sample, even complete books, does not certify every citation in the full corpus.
