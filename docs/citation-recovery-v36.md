# Citation recovery and work ownership after v36

The final candidate is pinned to source SHA256 `b1c2fb59656b889d2cafcb629acce56d6557916bd16d825a0247bf78e0b511d2` and
`engine_src=bb8b528aac4de527`. It repairs the reported citation recovery failures and
keeps ordinary Gemara quotations separate from authors referring to their own
commentaries. The final evidence is in
`tests/fixtures/citation_review_v36_validation.json`.

## Resolution behavior

- Restore a cut-off printed amud mark only with visible closing-parenthesis or
  `ושם` evidence; retain the original UTF-16 anchor and whole-daf narrowing.
- Read absolute chapter/daf addresses in either printed order, including lists.
  Require a unique chapter in the named tractate, membership of each printed daf
  in that chapter, canonical targets and actual target text. Each list member
  retains its own address and raw anchor.
- Determine ownership locally. A new native quotation predicate ends a prior
  self-explanation. A direct postfix `פי' רש"י` or `פירש בקונטרס` qualifies the
  cited work, as does an attached `מפורש בתוספות`; a coordinated `ורש"י פירש` or comparative `כדפירש בקונטרס` starts
  another explanation and cannot delete the preceding Gemara quotation.
- Recognize Hebrew and Aramaic self-writing, `כדפי'`, editorial brackets, precise
  chapter locators and attached postfix writing such as `הארכתי שם`/`הבאתי שם`.
  A proof FROM Gemara and a subordinate `שהוכחתי ממנה` remain distinct from
  writing one's own discussion there. Communal legal `כתבינן` does not mean the
  author wrote a commentary; explicitly comparative `כדכתבינן` can.
- Retarget a proven self-citation to a real direct commentary of the same author
  at the printed address. Preserve precise offered own candidates. Do not copy
  Gemara segments into commentaries, equate Rif and Bavli pagination, collapse
  lists/ranges, or bypass chapter/direction checks.
- A new unnamed ordinal chapter cannot default to the source tractate while the
  paragraph continues an explicitly cited foreign tractate. Ordinary words such
  as `ביצה` do not supply this evidence: a cited alias needs printed daf syntax.
  Ambiguous ownership cannot manufacture a new bare-Gemara target.
- Preserve successful unchanged candidates' legacy ibid role across batches and
  checkpoints. New reconstructions do not seed that history; a rejected citation
  supplies no antecedent. A sole existing ibid candidate can independently retain
  its exact printed amud when a quoted, globally unique chapter proves its work
  and contains that amud. This exception does not seed legacy history.
- Preserve base-Gemara behavior and the published `[במ]?` writer-prefix guard.
  Paragraph counters never provide Bavli pagination. Hidden HTML attributes
  never supply writer or address evidence.

## Final validation

- CPython 3.12, 604 tests: macOS 591 passed / 13 skipped; Linux 603 passed /
  one optional `jsonschema` skip.
- Real resolver acceptance: 100 complete recorded source lines, 349 valid links,
  103 required anchors and 11 anchored negative controls, plus forbidden targets.
- Immutable replay: 125 items, 78,764 source rows; 123 complete books match the
  published v36 source hashes (78,754 rows), plus two dictionary selections
  (10 rows).
- Baseline engine `4b913a3`: 34,451 records. Candidate: 34,966 records;
  34,231 records unchanged exactly, 607 added, 92 omitted, 128 targets corrected.
  Within the 123 complete books: 34,438 -> 34,953 records, with 34,218 unchanged
  and the same difference counts.
  Of the 92 omissions, 65 explicitly cite another commentary, 26 are own-text
  or duplicate-anchor abstentions, and one removes a misread collapsed address
  replaced by separate correct anchors.
- Final contextual audit covers all 827 differences. Independent QA rechecked
  all 14 differences from the preceding reviewed candidate and all 92 residual
  omissions. The original eight preservation cases retain all nine exact
  baseline records, including offsets, hashes and target metadata.

The replay uses recorded production NER captured on the Windows AMD/ROCm runner,
with identical model, Sefaria dump and resolver-patch pins. It executes an
immutable source copy and binds cached artifacts to the input bytes and the
executed engine bytes. Every emitted record is checked for canonical target,
target text, raw UTF-16 bounds, source hash and unique anchor key. The full source
book hashes identify 123 complete matching v36 books; the remaining two items are
selected dictionary rows, not complete books. All 36 available complete books
whose titles begin `תוספות על` are included. The inventory and exact counts are
recorded in the machine report.

Independent QA inspected every changed-record anchor with surrounding source
context, opened full paragraphs for ambiguous cases, verified identities and
coverage, and rechecked the final replay delta after its findings were fixed.
The review method and per-record outcomes are recorded with the validation.
This is contextual review of every difference, not a claim that every long
source paragraph or every target passage was manually read in full.

The recorded acceptance cases include the original Tosafot Shevuot18/29/254,
RanShevuot512, RashbaShabbat321/456 and Genesis48 reports. They also protect all
ordinary Gemara examples in the later review, TosafotShevuot374/503/554, original
own-commentary history, KerenOraNedarim305 with its actual heading stack, postfix
writers, legal/proof predicates, implicit foreign chapters, coordinated own
lists, quoted variants and all concrete independent QA counterexamples.

## Reproduction and limits

From the pinned, production-patched Sefaria project directory:

```sh
PYTHONPATH=. .venv/bin/python /path/to/repo/scripts/verify_citation_cases.py
```

The ordinary suite needs no Sefaria service:

```sh
python3 -m unittest discover -s tests -v
```

`scripts/compare_citation_replay.py` compares baseline and candidate using the same
recorded NER. It validates artifacts, supports bounded shards and exact
input/engine-bound resumption, and writes differences with full source lines.
Use `--published-book-hashes` to distinguish complete release books from selected
rows. Earlier November2 evidence remains separately identified in
`tests/fixtures/citation_recovery_v36_validation.json`; it certifies the earlier
engine only.

Some references remain conservatively omitted when the own commentary is empty,
the printed location or inherited work cannot be proved, or an explicit own
writing list cannot be safely reconstructed. For example, the printed `שם
(עה, א)` in RashbaShevuot175 is not reconstructed as RashbaGittin75a merely from
a distant earlier mention. RashiShevuot1538 also abstains from turning the
author’s own causal explanation into bare Gemara when its location grammar is
unproved. MeiriTaanit23a/28a and RashbaChullin92a have no available
own-commentary text in the pinned dump. A full-library correctness claim cannot
be inferred from this sample.

This output-affecting change requires a **full relink** after merge. Do not adopt
the old fingerprint as output-neutral. No production relink was run here and the
PR was not merged. Compare the resulting full-library output with v36 before
publishing the DB.

Application-side commentator attachment/display issues remain separate. For
#3252 the source DB target/anchor is correct; rendering must handle intervening
`<i data-commentator>` tags. #2530/#3710/#1509/#3626/#3854/#3264 concern display or
commentator attachment; this linker PR does not close those issues.
