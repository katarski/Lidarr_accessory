# Audit: what is still open

Generated 29 Sep 2026 from the read-only audit (6 areas, each finding
adversarially verified). 91 findings were confirmed and 3 refuted
(CLI-04, CLI-06, CLI-07: do not redo them). **91 are fixed and pushed**;
the 0 below are open, in the order to fix them (data loss first).

**Line numbers are from commit 98d2656, the commit the audit read.** They
have shifted since, so find the code by the function or the quoted text.
Where a verifier corrected the proposed fix, **Fix** is the corrected one.

How each fix was done so far: a Python edit script (exact-match replace,
abort on a count mismatch), one unittest per rule in
`tests/test_deleters.py`, the suite in the image on PARK, push, then
`tools/deploy.sh` (see HANDOFF section 1). Grep the suite for the changed
symbol first: green tests that never call it prove nothing. Where a fix
changes a verdict, diff old against new over real data before shipping (as
orch2 F4 did over Lidarr's 31,496 past grabs) and read the changed rows.

