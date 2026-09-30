# Audit: what is still open

Generated 29 Sep 2026 from the read-only audit (6 areas, each finding
adversarially verified). 91 findings were confirmed and 3 refuted
(CLI-04, CLI-06, CLI-07: do not redo them). **80 are fixed and pushed**;
the 11 below are open, in the order to fix them (data loss first).

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

## 1. loops F10 (low) -- Lifecycle re-check state (lifecycle_checked, completed_seen) lives only in memory, so every restart re-plans every completed torrent...

`main.py:879`

**Evidence:** 'LLM pick_owned_album: '07.08 - NO MÁS vs 4 owned'' was asked at 05:49:18 and again at 06:38:01, after the 06:36 restart. 70 of the 174 distinct LLM questions in the current log were asked more than once. 48 LLM calls in the first 9 minutes after the 06:36 start, mostly lifecycle re-plans of completed torrents (Pharrell's 40 singles at 06:38:01-06:38:11).

**Root cause:** qbt_auto_deselect_loop keeps completed_seen (876) and lifecycle_checked (879) as plain in-memory objects. The 6 h re-check throttle and the -1 'waiting for the LLM' stamps are lost on every container recreate, which happens on every deploy. deselect_planned.json already solves this for the deselect.

**Fix:** Persist lifecycle_checked as {hash: (disk signature, ts)}, plus completed_seen, the same way as deselect_planned.json: load at start, tmp and os.replace, checkpoint every N torrents during the pass, prune by age. Must not break: an immediate re-check whenever the folder's disk signature changes, and negative stamps meaning 'waiting for the LLM'.

## 2. loops F11 (low) -- The single CUE worker waits 60 s on every queued .cue whose folder was already removed

`orchestrator.py:1713`

**Evidence:** After the deploy: 06:38:46 'lifecycle: REMOVED (fully imported) 'Cream - Disraeli Gears Remastered 2004...''. The CUEs queued at startup were then processed on the deleted folder: 06:40:12 '=== Processing .../Disraeli Gears - flac.cue ===' to 06:41:12 '... never stabilized, skipping', and 06:41:12 wav.cue to 06:42:13 'never stabilized'. The folder no longer exists on PARK. That is 120 s of the only CUE worker spent on files that were gone.

**Root cause:** _wait_for_stability (1707-1724) treats a missing file as still being written (`if not path.exists(): sleep; continue`) until its max(stable_seconds*6, 60) deadline. Queue entries are never invalidated when their folder is removed, and _cue_ledger_verdict returns None for a missing file, so nothing short-circuits.

**Fix:** An absent .cue means gone: the worker checks existence at dequeue and _wait_for_stability returns 'gone' immediately (debug log), dropping the entry from cue_seen. Must not break: a .cue that watchdog reports on_created already exists, so stability waiting for real in-progress writes is unchanged.

## 3. loops F12 (low) -- Keep-folder consolidation renames files already in _harvest_pending on every pass until the name length hits the limit

`song_harvest.py:879`

**Evidence:** 6x 'harvest keep: /downloads/_harvest_pending/10. Going To The River (_harvest_pending) (_harvest_pending) ... -- [Errno 36] File name too long' (e.g. 29 Sep 02:11:38). Right now 18 of the 20 files in the keep folder have 255-character names.

**Root cause:** harvest_pass adds keep_dir as a source (717-718). In purge_leftovers the consolidation target for a file already inside keep_dir is itself (dest == full, which exists), so the collision branch appends ' (_harvest_pending)' and renames it every time the keep folder is harvested.

**Fix:** Consolidate only files whose dirpath is outside keep_dir; a file already in the keep folder stays as it is. Must not break: collision renaming for same-named songs arriving from two different boxes.

## 4. loops F14 (low) -- Converter-tab library tree stats all 95k library files every hour, whether or not anyone uses the tab

`main.py:1757`

**Evidence:** 29 lines of 'converter: library tree: scanned 9077 folder(s), 95872 audio files', hourly (29 Sep 03:02:42, 04:04:11, 05:05:56, 06:06:51). _lib_rescan_loop calls LibraryTree.maybe_scan(3600), which does os.walk plus os.stat on every audio file under the array-backed /music and exists() on every cached detail (converter.py 212-257).

**Root cause:** A background timer maintains a WebUI view nobody requested: a timer taking a resource on its own. The library audit already walks the same tree, and refresh_dir already updates the cache after conversions and deletes.

**Fix:** Build and refresh the tree on demand, when the Converter tab is opened and the cache is stale, or feed it from the library audit's walk; keep refresh_dir for writes. Must not break: instant tab rendering from the file-backed cache and the delete/convert refreshes.

## 5. loops missed (low) -- _clean_album reads 'Title - YYYY.MM.DD' as 'Artist - Year - Album' and reduces the album to 'MM.DD'

`qbt_deselect.py:203`

**Evidence:** _ARTIST_YEAR_ALBUM (line 53) matches 'Hidden Figures (Original Score) - 2017.01.06' and returns '01.06' (reproduced locally). The 06:19:04 Pharrell plan shows 'Pharrell Williams / 01.06 (not in library)' for that 26-file, 119 MB folder. The Lidarr lookup therefore cannot find the album, it comes back total==0, and F2's lifecycle deleted the folder as an 'un-wanted leftover'. The outcome was the same here, because Lidarr has no monitored Hidden Figures album. For a wanted album with this naming, it would be data loss.

**Fix:** Strip the year only when what follows it is a title rather than a date remainder (require a separator plus a word, and never an MM.DD tail). Better, look the album up with the raw folder name as a second candidate (folder_hint already carries it) before concluding total==0.

## 6. orch3 COMP-BATCH-1 (low) -- comp_hunt_grabs_per_pass > 1 silently discards all but the first candidate of each batch

`orchestrator.py:11139`

**Evidence:** assembly_find_compilation takes batch, queue = queue[:cap], queue[cap:] with cap = comp_hunt_grabs_per_pass (10965-10967) and saves the shortened queue. _assembly_grab_for_songs then uses cap = 1 whenever hunt is given (11139-11141), so it tries only batch[0] and adds only that guid to 'tried'. batch[1:] is in neither queue nor tried. _comp_search_titles never re-searches a title already in 'searched' (11022), so those candidates are never tried. The WebUI help says 'How many candidates are grabbed and verified per pass'.

**Root cause:** Two layers each enforce their own per-pass cap. The caller consumes the batch from the queue on the assumption that the callee tries all of it.

**Fix:** Keep a single cap. Either _assembly_grab_for_songs tries every candidate it is handed when hunt is given (the caller already sliced), or assembly_find_compilation puts back into comp['queue'] every batch entry not in hunt['tried'] after the call. Must not break: one grab per pass for the artist-scope hunt, and the 'tried' bookkeeping.

## 7. orch3 SETTINGS-REC-1 (low) -- The 'Recommended' button in Library & sweeps turns the cueless sweep off (recommended keys use the wrong section)

`orchestrator.py:14351`

**Evidence:** _SETTINGS_RECOMMENDED has 'lidarr.sweep_cueless_pre_split': True and 'lidarr.sweep_interval_seconds': 300 (14351-14352). The schema ids are 'watch.sweep_cueless_pre_split' and 'watch.sweep_interval_seconds' (14310-14313). get_settings looks the value up by id (14531) and falls back to the schema default (False, 0). Live values are True / 60 (config and overrides). Recommended then Save writes sweep off with interval 0 ('only at startup'), so cueless downloads sit forever. A cross-check of all registry ids finds these two and no other drift.

**Root cause:** Recommended values live in a separate dict keyed by hand-typed ids, so nothing ties them to the schema.

**Fix:** Key them by the real ids. Better, make 'recommended' a column of the _SETTINGS_SCHEMA tuple and check at class load that every id in _SETTINGS_GROUPS and any remaining side table exists in the schema. Must not break: 'anything not listed recommends its own default'.

## 8. orch3 LOG-TAIL-1 (low) -- The Log tab's 400-line view splices the tail of the rotated log in front of the live tail and skips the middle

`orchestrator.py:13986`

**Evidence:** read_log reads want = max(4096, need*220) bytes from the end (13983-13988). If that chunk holds fewer than `need` lines (average line over 220 bytes), it keeps the chunk, including its partial first line (it trims only when nl > need), and moves on to pipeline.log.1 (13999-14002). The result is the .1 tail, a partial line, then the live tail, with the rest of the live file missing and no marker. Measured on PARK: 400-line windows over 88,000 bytes occur 1,048 times in the live pipeline.log, 17,055 times in .1 and 18,086 in .2 (outage error lines). The UI default is 'last 400 lines' (webui.py:395).

**Root cause:** A fixed bytes-per-line estimate stands in for reading backwards until enough newlines are found in the current file.

**Fix:** Read the current file backwards in blocks until it yields `need` newlines or offset 0, and move to the next rotation only after the current file is exhausted. Drop the leading partial line whenever the read did not start at offset 0. Must not break: small reads for a 400-line refresh (no multi-MB reads), whole-file mode for lines<=0.

## 9. orch3 SETTINGS-FLOAT-1 (low) -- Every float setting is rendered with min=0 max=1 step=0.05, so one spinner or wheel step on 'Min track match (%)' 50 sets it to 1

`webui.py:1091`

**Evidence:** webui.py:1091 adds step="0.05" min="0" max="1" to every type=='float' row. Six float settings are not 0-1 ratios: min_match_percent (live 50), assembly_min_pct 10, interactive_search_max_gb_per_album 2.5, harvest_duration_tolerance 10, assembly_lossless_bonus 30, assembly_lossy_penalty 15. On a number input whose value exceeds max, the browser's step-down clamps it to max (1). Save then accepts that value, since save_settings only float()s it (14567-14573). A single down-arrow on 'Min track match (%)' makes 1% of tracks enough to treat a download as the album.

**Root cause:** Input bounds come from the value's type, not from each setting's meaning.

**Fix:** Give each schema row an optional (min, max, step) and emit those attributes per row, with no bounds when none is declared. Keep float() parsing server-side, and optionally clamp there to the same declared range. Must not break: the 0-1 ratio fields keep their 0..1 bounds.

## 10. llm LLM-5 (medium) -- Content-identify's LLM 'tie-break' is given the leader alone (the rival is filtered out) and answers by title, and the result drove...

`orchestrator.py:4045`

**Status:** Needs the album id passed through the name-based hand-off (content-identify returns a TITLE).

**Evidence:** clear_margin (4038) is computed against results[1], but the tie-break list is `top = [r[2] for r in results[:5] if r[1] >= min_cov]` (4045). When cov1 only just clears min_cov (60), a rival within the margin (m2 >= m1-1) is always below it, so the LLM gets exactly one option and confirm_album_match can only rubber-stamp it. Live, 17 Sep 18:44:22: "LLM confirm_album: 'folder: CD1/Rollin_ and Tumblin vs 1 candidates' -> 'The Sky Is Crying'", then "content-identify: ... via titles ambiguous, LLM pick (11/18)", then "Force-import (superset) ... flipping release", then "Switched album 46858 monitored release to 117720". At 18:40:37 (8/13 = 61.5%, same shape), the returned TITLE was re-resolved downstream to a different album: "Pre-flight match: Elmore James / The Sky Is Crying (stats=12/12...)" and "Switched album 45212 monitored release to 112781" (twice, 18:40:46 and 18:40:49) on an...

**Root cause:** The ambiguity is measured on one set and the LLM is asked on another (filtered) set, so 'ambiguous' becomes 'one candidate'. Both the LLM interface (a list of titles in, a title out) and _identify_album_by_content (returns winner title) also drop the album id, so duplicate titles collapse and get re-resolved by a different heuristic.

**Fix:** Build the tie-break set from the same rule that made clear_margin false: every result within the margin of the leader. Do not accept a pick below min_cov, though. If the LLM picks a rival under min_cov, return None (undecided) rather than accept an album that failed the coverage floor. The clause 'if that set has one member, return None' can never fire, because when clear_margin is false results[1] is always within the margin, so the set always has at least 2 members. Drop it. Offer numbered options carrying year and track count, map the index back to the album record, and have _identify_album_by_content return the album id so callers stop re-matching by title. The same title-to-record collapse exists in dedup_downloads' LLM fallback (see the missed finding). Use one title/record resolver for both.

## 11. llm missed (medium) -- The LLM's owned-album pick is resolved to the FIRST album with that title, skipping the same-title guard, so a missing edition can be...

`dedup_downloads.py:277`

**Status:** Same refactor as LLM-5.

**Evidence:** album_complete_in_library guards the deterministic same-title case (202-225: when several albums share a title and any is incomplete, answer 'not owned'). The LLM fallback (270-279) loses that guard. It sends the LLM a de-duplicated SET of complete titles, then maps the answer back with `for a in albums: if a.get('title') == picked: alb = a; break`, which is the first same-titled album in Lidarr's order, whether or not it is the one being downloaded. The word-overlap guard in pick_owned_album passes trivially for self-titled albums, because the shared word is the artist's name. Path: a download named 'Evanescence EP 1998' gives norm_title 'evanescenceep', which differs from 'evanescence', so there is no same_title hit. Word-subset skips it because the title is self-titled. The LLM gets owned={'Evanescence'} and answers 'Evanescence'. The overlap guard passes on 'evanescence'. The...

**Fix:** Use one title-to-album-record resolver for every match source, deterministic and LLM alike. Send the LLM's picked title through the same same-title, year and 'any incomplete means not owned' logic as the exact match, rather than a first-match loop. Offer the LLM numbered album records carrying year and track count, not a de-duplicated title set, so same-titled albums can be told apart. This is the same resolver the LLM-5 fix needs in _identify_album_by_content. It must not break: an LLM miss never deselects, and the overlap and release-type guards stay as they are.
