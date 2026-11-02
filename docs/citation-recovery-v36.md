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

The fixture `tests/fixtures/citation_cases_v36.json` contains ten complete source
lines and their actual raw NER responses, captured on the Windows AMD/ROCm runner
on 2026-11-02. The recorded normalization is checked before replay, and the real
production-patched Sefaria resolver is used without a running NER service.
Hand-specified expectations cover the failures above, the existing Sanhedrin and
Bava Metzia recoveries, Tosafot's own explicitly qualified DH, and all seven
`שם שם` references to Genesis 48 in Agadat Bereishit line 17. The ten source lines
produce 45 validated links. Every emitted canonical target, source hash, UTF-16
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
