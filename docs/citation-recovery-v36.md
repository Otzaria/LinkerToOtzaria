# Citation recovery and preservation after v36

The candidate engine is pinned to SHA256 `2d5276ef7326626a589b46aa3106c5593cc32370191b36f7b5495d623e174587`, `engine_src=02533813acd3443b`.
The compact validation summary is in
[`validation/citation-review-v36.json`](validation/citation-review-v36.json).

## Behavior

The linker preserves an established, independently verified Gemara address when
it cannot safely select its attributed commentary. This applies to direct
resolver candidates and the existing parenthesized, chapter and relative
recovery paths. It does not turn an ambiguous new text reconstruction into a
Gemara link.

A local, explicitly named Rashi or ordinary Tosafot citation is upgraded to the
real direct commentary at the same printed daf when that commentary has text.
If the title is ambiguous, belongs to another Tosafot work, or the corresponding
commentary is unavailable, the valid original Gemara link remains. Rabbenu Tam
is not automatically mapped to Tosafot, and a missing Rashi is not replaced by
Rashbam. Gemara segment numbers are never copied into commentary addresses.
An unproved chapter range is never collapsed into one of its pages.

Self-citations follow the same preservation policy. A proven printed address is
mapped to the same author's direct commentary when possible. Empty commentary
text, an unknown self-writing locator or an unsupported commentary range cannot
by themselves delete a verified existing Gemara link. For example,
Meiri on Megillah rows45/197 retain Taanit28a/23a, and RashbaShevuot170 retains
Chullin92a
when the corresponding author's text is empty. RashiShevuot1538 retains the
proven legacy relative Shevuot29b address.

Base work, daf, amud, chapter membership, direction, ibid support, canonical
address and text availability are checked before changing ownership. A chapter
NER span's immediately attached numeric location is still checked, including
when the entity has swallowed the opening parenthesis or stops before a
chapter-title continuation. Thus the wrong chapter 4 range at RanNedarim1398 and
the wrong chapter 1 range at TosafotMenachot1128 remain rejected: their printed
dafs22a/55a contradict those ranges.

Printed amud marks cut off by NER are read only with visible closing-parenthesis
or `ושם` evidence. In the attribution path a whole daf can narrow to its printed
amud before either self or external ownership is selected. An explicitly
different tractate, including one immediately outside the NER entity, cannot be upgraded without work proof. A strong self-citation can correct
a wrong upstream page to its independently printed canonical address before
selecting the commentary; that correction clears the stale old antecedent. Raw UTF-16 offsets and source
hashes remain bound to the original source text.

An explicitly printed foreign work inside location parentheses remains part of
the citation; punctuation is removed before matching its tractate name.
An own chapter without a named foreign work must be proved against the source
tractate and its chapter membership. MeiriYevamot534 therefore resolves to
Meiri on Yevamot13a, rather than inheriting an unrelated Nedarim13a guess.

Own DH candidates are independently checked against a unique matching opening
heading, including plain text before a spaced hyphen. The preceding accepted
base page and a locally proven self-writing scope are required.

A display upgrade occurs after base ibid validation. It keeps the proven
original antecedent for subsequent legacy `שם` citations and never seeds a new
Rashi/Tosafot history. Same-batch, split-batch and checkpoint-resume controls
verify this with both available and missing commentary text. Unchanged successful
own-commentary history also remains intact.

The earlier recovery improvements remain: read chapter/daf addresses in either
printed order, retain independent list-member anchors, require a unique chapter
within an explicitly cited tractate, respect a preceding explicitly cited
foreign tractate instead of guessing the source tractate, and use only positive
local writer/location evidence. Hidden HTML attributes do not supply evidence;
paragraph counters are not Bavli pagination. The original base-Gemara behavior
and published writer-prefix guard are retained.

## Verification

The exact frozen source completed:

| Check | Result |
| --- | --- |
| macOS, CPython 3.12 | 623 tests: 610 passed, 13 skipped |
| Linux, CPython 3.12 | 623 tests: 622 passed, 1 optional-jsonschema skip |
| Independent attribution QA | 44 passed |
| Recorded real-resolver acceptance | 140 source cases, 441 valid links |
| Saved complete-book controls | Same 140 cases, 444 records; all 146 required anchors and 59 forbidden controls pass |
| Original preservation controls | All 9 complete records remain exactly equal to baseline |
| Replay | 143 items, 95,498 rows: 38,170 → 38,795 records |
| Exact comparison | 37,869 unchanged; 629 added; 297 corrected targets; 4 old records replaced/rejected |

All 930 differences have an independently accepted contextual classification.
The final source differs from the fully reviewed preceding candidate in exactly
ten target fields: explicit foreign own-commentary citations whose work names
sit inside parentheses. A separate comparison of all 38,795 saved records
confirms every other target, offset, hash, relative/context field and own DH
record remains identical.

The four old records omitted from the anchor-key comparison are fully explained:

- RashbaShabbat456: wrong Sukkah20a is replaced by separate printed Sukkah13b/14a.
- TosafotMenachot1128: wrong chapter 1 range contradicts printed55a; own55a remains.
- RanNedarim1398: wrong chapter 4 range contradicts printed22a; own22a remains.
- TosafotAvodahZarah133: the old whole-daf anchor is replaced by its printed
  period and the correct Tosafot on Shabbat65a target.

No valid existing Gemara address in this audited sample is omitted merely
because its corresponding own or named commentary cannot be selected.
Independent QA's final verdict is `APPROVED_NO_KNOWN_BLOCKERS` for the exact
source digest above.

The replay uses the actual production NER captured on the Windows AMD/ROCm
runner and the pinned, production-patched Sefaria resolver. Both sides execute
against identical recorded NER and source rows, without another live NER call.
The candidate runs from an immutable source copy, with the executed bytes bound
to every report. Artifacts from another engine or input identity cannot satisfy
resumption checks.

The expanded sample adds all 18 complete available requested books: Meiri on
Yevamot, Dor Revii on Chullin and all 16 Penei Yehoshua books. They contribute
16,734 rows. The complete sample is 141 full books whose source hashes match v36,
plus two selected dictionary items (10 rows), totaling 95,498 rows. It includes
all 36 available complete books whose titles begin `תוספות על`. The dictionary
items are not claimed as whole books.

Every emitted record is checked for canonical target, available target text,
raw UTF-16 bounds, source hash and a unique source-anchor key. Independent QA
reviews the new differences against the previously reviewed PR candidate, all
differences in the 18 added books, every residual omission and the original nine
exact preservation anchors. Full paragraphs are opened for ownership and
ambiguous cases. This does not claim a new semantic review of every unchanged
baseline link or every long target passage.

## Reproduction, evidence and limits

The unused 583KB per-record validation fixture has been removed. The active
acceptance fixture is deterministically compressed to 122,534 bytes; its raw
NER, source paragraphs, expected anchors and forbidden controls remain executable.
From the pinned Sefaria project directory:

```sh
PYTHONPATH=. .venv/bin/python /path/to/repo/scripts/verify_citation_cases.py
```

The verifier accepts either the default `.json.gz` fixture or a plain JSON
`--fixture`. Ordinary tests need no Sefaria service:

```sh
python3 -m unittest discover -s tests -v
```

`scripts/compare_citation_replay.py` compares both immutable engines using the
same recorded NER, verifies emitted artifacts and writes every difference with
the full source line. Supply the raw-NER bundle, baseline engine, candidate
engine and published book hashes through its CLI. `--published-book-hashes`
distinguishes complete release books from targeted row selections. The compact
summary records the input, source, fixture and full-report digests. Detailed
machine reports are audit artifacts, not test fixtures; their per-record payloads
and host-specific paths are not copied into Git.

Some unproved new references still have no safe target. Preservation is not
permission to retain a demonstrably wrong chapter, printed address, foreign
work or unsupported ibid antecedent. The sample is broader than the prior PR
validation, but is not a correctness guarantee for the entire library.

This output-affecting change requires a **full relink** after merge. Never adopt
the old fingerprint as output-neutral. No production relink was run and this PR
was not merged. Compare the resulting full-library output with v36 before
publishing a production database.

Application-side commentator attachment/display issues remain separate. For
#3252 the source DB target/anchor is correct; rendering must handle intervening
`<i data-commentator>` tags. #2530/#3710/#1509/#3626/#3854/#3264 concern display or
commentator attachment; this linker PR does not close them.
