# Citation recovery and preservation after v36

The frozen candidate is `8407561`, SHA256
`f5291e3c0b9c0a0e8833b06e11cbc3eef52f1aad702dedb834f79ddfcdd763ae`, `engine_src=bbcf4ff94398e7e8`.
[Compact validation evidence](validation/citation-review-v36.json) records the
input identities, counts, acceptance controls and independent QA report digest.

## General ownership rules

An established Gemara address is retained when its attributed commentary cannot
be selected safely. A named Rashi/ordinary Tosafot or explicit self-citation is
upgraded only when the printed work, address and actual direct commentary text
support it. Other Tosafot works, Rabbenu Tam and unavailable commentary texts
cannot acquire an invented counterpart. Gemara segment numbers are not copied
into commentary addresses. A writer name is not evidence for a new base recovery.

Prefix and postfix writers use the same late display stage, including validated
relative reconstructions. Base work, daf, amud, chapter, direction, availability
and IBID checks run first. The base history remains separate from the displayed
commentary, including checkpoint resumption. A proved address correction clears
its stale antecedent rather than creating a commentary IBID namespace.

Every intervening locator word must fit the local scope, including words inside
the NER entity. A new subject or coordinated native Gemara quotation ends a prior
writer's scope. An attached `ושם` or a native predicate swallowed after the numeric
parenthesis cannot hide that boundary. Ordinary grammatical forms such as future
`אבאר`, locator arguments `בו`/`קצת` and intensifiers are supported consistently.

Chapter ownership evidence requires a positive chapter marker and the longest
complete canonical phrase. A printed tractate title is not silently treated as
its homonymous first chapter; a partial ordinal cannot override a longer title.
Ellipsis is local display evidence tied to an explicit edge and a validated
amud's chapter membership, and does not create global chapter aliases.
A postfix self predicate retains the current attached chapter even outside a
numeric-only NER span. A completed earlier citation's chapter is separated only
when an explicit coordinated work/chapter starts the current location.

An attached editorial `[בד״ה X שם ע״א/ב]` can correct the side of the same printed
daf only with the actual source commentary, chapter/direction proof and one
unique opening heading on that explicitly stated side. There is no search of
neighbouring pages or body text. This resolves the reviewed Tosafot Shabbat
citation to its actual opening heading at 111a:9:2 without a row-specific rule.

There are **no new source-book, row-number or personal chapter-name exceptions**
in the production rules. Concrete book/row examples belong to the recorded test
data. Non-Sefaria Keren Ora has no canonical direct commentary index in the
pinned resolver; its proved Gemara links remain without inventing an alias.

## Final validation

- macOS CPython 3.12: **636 tests, 623 passed, 13 skipped**.
- Linux CPython 3.12: **636 tests, 635 passed, 1 skipped** (optional jsonschema).
- Independent QA reran **57 attribution tests, 1,216 scope variants and 54 postfix
  variants**, all passing on these exact source bytes.
- Pinned real resolver plus recorded AMD/ROCm production NER: **164 source cases,
  501 valid links**, 169 required anchors in isolated runs.
- The same 164 source paragraphs contain **504 records** under complete-book
  history. All **170 required anchors**, 49 forbidden targets, 26 forbidden
  anchor/target pairs, 9 exact original preservation records and 3 own-DH rows pass.

Full-book acceptance explicitly retains Meiri on Yevamot, row 534 at Meiri on Yevamot 13a;
the cold isolated resolver intentionally abstains there without a candidate.
Meiri on Yevamot, row 1130 retains the independently printed Meiri on Chagigah 4b correction.
Unavailable self commentaries retain verified Gemara fallbacks. The two new
coordinated native quotation controls permit the previous engine's abstention,
forbid a false Rashi target and preserve their other proved paragraph links.

## Paired replay and independent review

The replay covers **143 items, 95,498 source rows**: **141 complete books with
source hashes matching published v36 (95,488 rows)** and two targeted dictionary
selections (10 rows). It includes all 36 available `תוספות על` complete books,
Meiri on Yevamot, Dor Revii and all 16 available Penei Yehoshua books. The expanded 18
complete books contribute 16,734 rows; their hashes remain in the compact evidence.

The baseline is commit `4b913a3`. Both engines use the identical recorded NER
bundle, actual production-patched Sefaria resolver and pinned database. The
comparison binds each report to executed engine bytes, raw input and published
source manifest. It checks canonical nonempty targets, raw UTF-16 anchors,
content hashes and unique record keys. Exact baseline artifacts can be reused
only with matching engine/input identities and artifact digests; candidate
outputs were generated by the frozen final source.

Across the sample, records change **38,170 → 38,800**: **37,856 unchanged,
634 added, 310 changed and 4 old-key records omitted/replaced**. All **948 final
differences** were classified across successive independent audits. The final
seven differences from the fully reviewed preceding replay received a fresh
contextual review and an independent comparison of all saved output records.
Rejected intermediate ownership outcomes were fixed before push. Compared with
the preceding published PR head (`2541da8`), the final output adds 5 anchors and
changes 23 existing records, with no removed anchor.

The four old-key omissions are the same reviewed corrections: a collapsed
Sukkah 20a is replaced by separately printed 13b/14a; wrong Menachot chapter 1 and
Nedarim ordinal chapter 4 ranges contradict 55a/22a; the old Shabbat 65 anchor is
replaced by the printed dot and Tosafot on Shabbat 65a. They are not four unexplained
losses of valid citations.

**Independent final verdict: `APPROVED_NO_KNOWN_BLOCKERS`** for this exact source.
The full audit payload is retained as a separate artifact, with its digest in
`validation/citation-review-v36.json`; it is not a bulky test fixture. The active
fixture is deterministically compressed (about 145KB), and its verifier also
supports plain JSON.

## Reproducing the checks

Run `python3.12 -m unittest discover -s tests -v` from the repository. Run
`scripts/verify_citation_cases.py` from the pinned production-patched Sefaria
project with its virtual environment, Mongo dump and `PYTHONPATH=.`; the active
compressed fixture supplies recorded NER and requires no live GPU request.
`scripts/compare_citation_replay.py` performs the complete paired replay with
the recorded full-book bundle and published hash manifest identified in the
compact evidence. Those large raw/audit artifacts are retained separately.

## Limits and release policy

This sample is not a full-library correctness guarantee. QA did not semantically
reread every unchanged baseline link. Recorded NER replay verifies resolver and
postprocessing behavior for the captured inputs, rather than fresh NER quality.

One printed contradiction is deliberately retained as a conservative fallback:
Tosafot Bava Kamma 99b:17:1 (source row 1289) prints 20b while naming `המניח`, whose
canonical range is 27a:13–36a:8. The chapter cannot prove the previous own 20b
target. The verified legacy printed **Gemara 20b** address remains; this does not
assert that the source numeral is correct. No 27b guess or personal exception is
introduced. Such contradictory inputs require editorial evidence for a semantic
correction, like the explicit amud correction described above.

The fingerprint change is **output-affecting** and requires a **full relink after
merge**. Output-neutral adoption is forbidden. This work does not merge the PR
or run a production relink.
