# Audit: what is still open

Generated 29 Sep 2026 from the read-only audit (6 areas, each finding
adversarially verified). 91 findings were confirmed and 3 refuted
(CLI-04, CLI-06, CLI-07: do not redo them). **67 are fixed and pushed**;
the 24 below are open, in the order to fix them (data loss first).

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

## 1. orch2 F7 (medium) -- Audit caches a failed per-artist album fetch as 'this artist has no albums' for the whole pass

`orchestrator.py:8091`

**Evidence:** _album_index (8086-8115): list_albums_for_artist swallows errors and returns []. built={} is stored in album_index_by_artist and reused for the rest of the pass. _album_lookup then returns None for every album folder of that artist, so each one gets reason='album not in Lidarr' and goes down the act-mode repair path: refresh_artist, manual_import_candidates, _resolve_library_album, _align_release_to_disk(allow_when_populated=True), then the tag-number move import from F1, possibly on an album that is already complete. In _resolve_album_by_song_titles (5233-5236), a failed list_tracks_for_album also silently drops that album, which can remove the real competitor from the 'decisive margin' test. LIVE: list_albums_for_artist failed 22 times (19 Sep 10:19, 'Connection aborted' during a Lidarr restart). Not caught inside an audit pass in this log window.

**Root cause:** A transient error is turned into an empty answer and cached, then treated as the fact that triggers repairs.

**Fix:** The fix is correct. Also re-evaluate the green gate on the resolved rec: _bail_if_green must check whichever album is about to be acted on, not only album_rec. Mark the artist as unknown for the pass (skip it, no row) when its album fetch fails, and don't cache [].

## 2. orch2 missed (low) -- The external audit records a failed Lidarr album fetch as 'Lidarr has none of these albums' and does not re-check for the recheck period

`orchestrator.py:11262`

**Status:** External audit side of the same outage-is-not-an-answer rule.

**Evidence:** external_album_audit_pass (25ed5ec 11261-11277; HEAD 11608) sets have=[] on exception (and list_albums_for_artist itself returns [] on error), then stores seen_state[aid] with checked=now and missing=all MB albums. Live 19 Sep 10:19:13: 'list_albums_for_artist(121) failed: Connection aborted' then 'external audit: Enrique Iglesias -- MusicBrainz lists 14 studio album(s) that Lidarr has no record of'; Eric Benét (11) followed at 10:19:14. The MB side of the same function already refuses to record an empty result.

**Fix:** Apply the same 'could not ask is not no' rule on the Lidarr side. If the Lidarr generation changed during the call, or the call failed, skip the artist without writing seen_state. The report is advisory, so severity stays low.

## 3. orch3 ISEARCH-STATE-1 (high) -- Interactive-search state is saved only at the end of a pass: today's 4-hour pass (82 grabs accepted) was lost at restart

`orchestrator.py:12827`

**Evidence:** _save_isearch_state is called only at 12827, after the whole pass. last_attempt (12784, 12811) and the in-memory blocklisted guids (12042, 12139) live only in memory until then. Live: '2026-09-29 02:18:19,992 interactive search: 588 eligible album(s); trying 300 this pass across 129 artist(s)', then no 'pass done' before '06:25:03 Signal 15 received'. Between 02:18 and 06:25 the log shows 216 '-> grabbing', 72 ACCEPTED, 10 'ACCEPTED via Prowlarr' and 75 rejections. interactive_search.json mtime is 2026-09-29 01:38:40 and the file holds 582 entries with 0 last_attempt values. So the next pass restarts the same 300 albums in the same order. Prowlarr-added grabs are not attributed in Lidarr's queue (queued_ids keeps only rows with an albumId), so those albums can be grabbed again, and Prowlarr rejections (blocked only by the lost in-memory list) can be retried.

**Root cause:** The pass keeps its state in memory and commits once, which breaks the owner's law that state is checkpointed during a pass. A long pass (300 albums, max_candidates 1000, up to about 180 s per candidate) cannot survive a deploy.

**Fix:** Checkpoint after each album and after each artist fallback. _save_isearch_state is already tmp+os.replace, so call it right after st['last_attempt'] and ast['last_attempt'] are set. Let the loop honour the stop event so SIGTERM ends the pass between albums with state written. Keep the prune of non-missing albums only at the end of a completed pass. Must not break: the generation guard that skips the pass on a failed wanted_missing; the pruning rule; state stays bounded to missing albums.

## 4. orch3 WEBUI-CSRF-1 (medium) -- Unauthenticated WebUI POST endpoints accept cross-site requests, so any web page opened on the LAN can delete library folders

`webui.py:2387`

**Evidence:** do_POST (webui.py:2387 onward) dispatches /api/library/delete (2541: lt.delete, which shutil.rmtree's library folders, converter.py:459-480), /api/held/discard, /api/settings, /api/shutdown and /api/log/clear. It checks no Origin, Referer, token or Content-Type, and parses the body with json.loads regardless of type. A cross-site fetch('http://192.168.1.200:8830/api/library/delete',{method:'POST',mode:'no-cors',body:'{"paths":["Some Artist"]}'}) is a CORS 'simple' request (text/plain), sent without preflight. The server acts on it; the attacker only cannot read the reply. The server binds 0.0.0.0:8830 ('WebUI: enabled on http://0.0.0.0:8830') and get_settings itself notes 'The WebUI has no authentication'.

**Root cause:** State-changing endpoints trust any request that reaches the port. The browser same-origin policy does not protect simple POSTs.

**Fix:** One gate at the top of do_POST: reject with 403 unless Content-Type is application/json (which forces a preflight this server never approves) and any Origin/Referer host equals the Host header. Add the JSON header to the page's own header-less POSTs (convert/cancel, convert/pause, ctrl() restart/stop, the held actions). Must not break: the WebUI's own fetch calls; LAN access without login.

## 5. orch3 SETTINGS-SAVE-1 (medium) -- Settings Save writes every field, not just changed ones: every container template variable is silently frozen, and the warning never...

`webui.py:1117`

**Evidence:** saveSettings (webui.py:1117-1121) posts every [data-sid] control. save_settings (orchestrator.py:14554-14583) writes each one to webui_overrides.json, and apply_webui_overrides gives that file highest precedence. Live: the overrides file holds 125 keys, all 125 schema rows. docker logs (stderr only) show 'Settings tab value for lidarr.interactive_search_interval_seconds (1000) overrides the container variable ISEARCH_INTERVAL (3600)', the same for QBIT_DEAD_GRAB_GRACE_MINUTES (1440 vs 360), DELETE_SOURCE_FOLDER (False vs True) and ISEARCH_MAX_ALBUMS (values hidden because '_per_pass' contains 'pass', main.py:323). grep 'overrides the container' on pipeline.log and all rotations finds 0, because apply_webui_overrides runs at main.py:1198 before configure_logging at 1199. Separately, get_settings shows the schema default when a key is absent (14524): interactive_search_enabled True and...

**Root cause:** The form serialises its whole state as 'changes', and the override layer cannot tell a deliberate change from an echo. The precedence warning is emitted into an unconfigured logger, and the displayed defaults come from a second table that disagrees with main.py.

**Fix:** save_settings cannot tell 'equals the env/config value' from what it has: self._raw_cfg is the post-override merge (it is updated in place at 14677). main.py must keep a snapshot of cfg after the yaml and env layers and before apply_webui_overrides, and hand it to the Orchestrator. save_settings then removes a key from the overrides file when the submitted value equals that snapshot value. Posting only changed controls (client side) remains the primary fix.

## 6. orch3 ENV-BOOL-1 (low) -- Five env overrides cast with bool(), so a template value of 'false' turns the feature on

`main.py:169`

**Status:** Same finding as loops F13.

**Evidence:** main.py:159 put('lidarr','comp_hunt_enabled','COMP_HUNT_ENABLED', bool); 162-163 comp_hunt_lidarr_indexers_only; 169 harvest_enabled; 170 harvest_dry_run; 173 recheck_skip_unchanged. put() calls cast(v) on the non-empty env string (74), and bool('false') == bool('0') == True. So HARVEST_ENABLED=false leaves the harvest running (live config: move mode with harvest_purge_leftovers on), and RECHECK_SKIP_UNCHANGED=false cannot disable skipping. Every other boolean put uses _as_bool.

**Root cause:** The cast is chosen per line by hand instead of from the setting's declared type in the one registry (_SETTINGS_SCHEMA already says 'bool').

**Fix:** Use _as_bool for these five. Better, derive the env cast from the schema row's type (bool/int/float/str) so env parsing, WebUI parsing (save_settings) and main.py cannot diverge. Must not break: env still loses to Settings-tab values the user actually changed.

## 7. orch2 F5 (medium) -- The audit's skip-if-unchanged check never works (signature file can't be written), would record an aborted pass as done, and the report...

`orchestrator.py:8003`

**Evidence:** maybe_audit_library 7998-8005: sig_path.write_text(post) (not atomic); OSError is logged at debug. LIVE: /config/library_audit.sig is owned 0:0, mode 644, mtime 2026-08-15 22:27; the container runs as 99:100, and `test -w` says NOT-writable. So every pass is a full walk: 17 Sep 18:55->19:42, 20:04->20:53, 21:15->22:05 (~50 min each, 7176 albums), and each pass sends the same imports again (Rock Me cmd 508966 at 19:19:47 and 509411 at 20:29:13; 42 folders imported by track number three times each, 124 commands). On 26-27 Sep 'Library audit starting' appears every ~22 min. When a pass aborts (27 Sep 13:33:22 'Lidarr returned no artists ... Aborting this pass'), the function still writes the post-audit signature. Once the file is writable, that would mark the library as audited and skip later passes until the library changes. library_audit.csv is append-only: 98,428 rows and 33.5 MB...

**Root cause:** The check persists its state whatever the pass outcome was, hides write failures at debug level, and the report file doubles as long-lived state that is appended to and never replaced.

**Fix:** The fix is correct. One point to add: writing a tmp file in /config (which 99:100 can write) and then calling os.replace also gets past the root-owned file, because replace needs permission on the directory, not the file. Keep the first-run marker separate as proposed. Until F1 is fixed, a working skip also cuts down the repeated destructive imports.

## 8. orch2 F8 (medium) -- Library paths go through the downloads path translator (1,612 warnings); only works because the two roots happen to be identical

`orchestrator.py:5384`

**Status:** CLI-10 fixed windows_to_lidarr's library fallback; open: one translator for library paths at the orchestrator call sites.

**Evidence:** _import_library_folder_by_tracknumber calls self.lidarr.windows_to_lidarr(parent) at 5384 and windows_to_lidarr(p) at 5430. _prefer_lossless_in_album calls rescan_folder(windows_to_lidarr(folder)) at 4653. windows_to_lidarr (lidarr.py 216-226) maps only the downloads prefix; anything else is logged as a WARNING and returned unchanged. LIVE: 1,612 'Path /music/Music/... is not under mapped prefix /downloads' warnings and 0 for any other path. Example: 17 Sep 19:19:46, the Sister Rosetta Tharpe/Rock Me files just before 'audit: importing ... by TAG TRACK NUMBER'. It works only because config has library_root_windows == library_root_lidarr == /music/Music. On a deployment where they differ (the Windows \\PARK\Audio\Music setup), the qmap keys, which are paths as Lidarr sees them, never match, so every file is 'unmapped' and the prefer-lossless rescan points at a path Lidarr can't see.

**Root cause:** Each caller has to pick one of two translators (windows_to_lidarr vs library_windows_to_lidarr), and these call sites picked the wrong one.

**Fix:** Use one translator that tries each configured root pair (downloads mapping, library mapping) and raises when none matches, and remove the choice from callers. It must not change how any /downloads path is translated today.

## 9. orch2 F10 (low) -- Positional force-import is attempted when it can never succeed (±1 release match vs exact-count check); it changes Lidarr settings every...

`orchestrator.py:8452`

**Evidence:** Audit Step 3 (8449-8524) and _nudge_positional_force_import (7499-7583) pick a release with find_release_matching_track_count (lidarr.py 349: best_delta <= 1). manual_import_positional then requires an exact count (lidarr.py 899). LIVE: 17 'count mismatch' warnings, all off by one (11/12, 7/6, 24/23, 19/18, 13/12), repeated each audit pass (7/6 at 19:00:56, 20:10:20 and 21:22:01). Every attempt comes after RefreshArtist plus set_album_auto_switch(True), a lasting Lidarr setting change (19:02:37 'Set anyReleaseOk=True on album 4425').

**Root cause:** Two definitions of 'matching release' disagree, so this step fails by design whenever the counts differ by one, after it has already changed Lidarr's settings.

**Fix:** Positional pairing must take an exact trackCount release. Decide that before set_album_monitored_release, so a ±1 match never flips the monitored release. Leave Step 1 (anyReleaseOk) alone, since it is not part of this disagreement. As noted, positional pairing must also agree with the file titles (F1).

## 10. loops F8 (medium) -- Lead 7: audit and nudge retry impossible positional imports every pass (±1 release pick, stale track rows, no memory), writing to Lidarr...

`lidarr.py:323`

**Status:** Same as orch2 F10.

**Evidence:** 36 'manual_import_positional: count mismatch ... refusing', always the same pairs: (11,12) 'My Kind of Blues' id=45140 on 17 Sep at 18:57, 20:07 and 21:18 and on 29 Sep at 02:26, 03:52 and 05:37; (19,18) 'Gloria!' 45038; (13,12), (7,6), (24,23). 'audit: acting on' appears 6-7x per album. Each attempt writes: 'Set anyReleaseOk=True on album 45038' x6, 45140 x6, 28154 x7, each followed by 'Triggered RefreshArtist' (e.g. 28 Sep 21:09:19-21:09:31).

**Root cause:** find_release_matching_track_count accepts the closest release within ±1 (323), so for 11 disk files it can return a 12-track release; manual_import_positional (873-879) then requires exact equality, so the attempt can never succeed. The audit and nudge read rows with list_tracks_for_album (orchestrator 8940 and 7999), which is the selected release; the list_tracks_for_release fix was applied only to force-import. Nothing remembers the structural refusal, so every pass repeats the PUT, the...

**Fix:** Choose the release by exact equality with the manualimport candidate count (the list that is actually zipped), not len(audios). Read its rows with list_tracks_for_release(album_id, rid) before any PUT or RefreshArtist. The remembered 'impossible' fact must be keyed to that successful candidates call and the release set.

## 11. loops F7 (medium) -- CUE decoding tries cp1251 before cp1252/Shift-JIS/GB18030, so non-UTF-8 Western and CJK CUEs become Cyrillic mojibake in tags

`cue_parser.py:149`

**Evidence:** 29 Sep 04:26:58 'Parsed Ojos De Brujo - Bari.cue deterministically: 13 tracks', then 'Splitting track 02 ... Tiempo De Soleб.flac', 'Quien Engaсa No Gana', 'Acciуn Reacciуn Repercusiуn', and tagger 'Tagged ... Calй Barн / Bari'. Those files went into the library via ManualImport 511416 (completed 02:36:33Z). 28 such split/tag lines in the logs. Reproduced: b'Beyonc\xe9' decodes to 'Beyoncй'; Shift-JIS 'ドラゴンの歌' decodes to 'ѓhѓ‰ѓSѓ“‚М‰М'.

**Root cause:** _read_cue_text (146-172) takes the first encoding that does not raise. cp1251 maps every byte except 0x98, so after UTF-8 fails it almost always 'succeeds', and cp1252, shift_jis, gb18030 and latin-1 are effectively unreachable. The wrong text becomes Vorbis tags and file names.

**Fix:** After UTF-8 fails, choose the encoding by evidence rather than by 'first that decodes': run charset detection over the whole text and score each candidate for plausibility (mostly one script, agreement with the folder name and the companion audio's tags). Must not break: Cyrillic cp1251 CUEs (cp1251 still wins when the text is Cyrillic words), UTF-8 BOM handling, and the LLM repair fallback.

## 12. loops F9 (medium) -- Harvest re-reads every source's tags on nearly every pass: global wanted-signature gate, an easy=True sniff, no tag cache, end-of-pass...

`song_harvest.py:631`

**Evidence:** 63 harvest passes in the logs; 16 show '0 skipped unchanged', including every pass on 29 Sep: 02:50:01 '189 source(s), 0 skipped unchanged, 2116 file(s) scanned' ... 06:45:02 '198 source(s), 0 skipped unchanged, 2245 file(s) scanned'. Pass length (end minus previous end minus the 1800 s sleep): 04:07:02 to 04:44:24 is 7m22s; to 05:19:26 is 5m02s; to 05:56:27 is 7m01s, on a container capped at 2 CPUs.

**Root cause:** The skip gate includes wanted_signature (622-634), a hash of every wanted track id in the whole library, so any import anywhere invalidates every source. read_tags calls audio_open.File(path, easy=True) (313), and audio_open (65-66) bypasses the typed open whenever easy is set: a full sniff, 631 ms vs 264 ms per FLAC per HANDOFF §4. There is no tag cache, although orchestrator._read_song_tags already caches (path, size, mtime) for the assembly over the same folders. HarvestLedger.save() runs...

**Fix:** Also required: a source must not be marked when lidarr.failure_generation changed while build_wanted_index ran. With a per-source signature, a partial index (list_tracks_for_album returns [] on failure) would otherwise be remembered as 'nothing wanted here', which is a failed call cached as a negative.

## 13. loops F10 (low) -- Lifecycle re-check state (lifecycle_checked, completed_seen) lives only in memory, so every restart re-plans every completed torrent...

`main.py:879`

**Evidence:** 'LLM pick_owned_album: '07.08 - NO MÁS vs 4 owned'' was asked at 05:49:18 and again at 06:38:01, after the 06:36 restart. 70 of the 174 distinct LLM questions in the current log were asked more than once. 48 LLM calls in the first 9 minutes after the 06:36 start, mostly lifecycle re-plans of completed torrents (Pharrell's 40 singles at 06:38:01-06:38:11).

**Root cause:** qbt_auto_deselect_loop keeps completed_seen (876) and lifecycle_checked (879) as plain in-memory objects. The 6 h re-check throttle and the -1 'waiting for the LLM' stamps are lost on every container recreate, which happens on every deploy. deselect_planned.json already solves this for the deselect.

**Fix:** Persist lifecycle_checked as {hash: (disk signature, ts)}, plus completed_seen, the same way as deselect_planned.json: load at start, tmp and os.replace, checkpoint every N torrents during the pass, prune by age. Must not break: an immediate re-check whenever the folder's disk signature changes, and negative stamps meaning 'waiting for the LLM'.

## 14. loops F11 (low) -- The single CUE worker waits 60 s on every queued .cue whose folder was already removed

`orchestrator.py:1713`

**Evidence:** After the deploy: 06:38:46 'lifecycle: REMOVED (fully imported) 'Cream - Disraeli Gears Remastered 2004...''. The CUEs queued at startup were then processed on the deleted folder: 06:40:12 '=== Processing .../Disraeli Gears - flac.cue ===' to 06:41:12 '... never stabilized, skipping', and 06:41:12 wav.cue to 06:42:13 'never stabilized'. The folder no longer exists on PARK. That is 120 s of the only CUE worker spent on files that were gone.

**Root cause:** _wait_for_stability (1707-1724) treats a missing file as still being written (`if not path.exists(): sleep; continue`) until its max(stable_seconds*6, 60) deadline. Queue entries are never invalidated when their folder is removed, and _cue_ledger_verdict returns None for a missing file, so nothing short-circuits.

**Fix:** An absent .cue means gone: the worker checks existence at dequeue and _wait_for_stability returns 'gone' immediately (debug log), dropping the entry from cue_seen. Must not break: a .cue that watchdog reports on_created already exists, so stability waiting for real in-progress writes is unchanged.

## 15. loops F12 (low) -- Keep-folder consolidation renames files already in _harvest_pending on every pass until the name length hits the limit

`song_harvest.py:879`

**Evidence:** 6x 'harvest keep: /downloads/_harvest_pending/10. Going To The River (_harvest_pending) (_harvest_pending) ... -- [Errno 36] File name too long' (e.g. 29 Sep 02:11:38). Right now 18 of the 20 files in the keep folder have 255-character names.

**Root cause:** harvest_pass adds keep_dir as a source (717-718). In purge_leftovers the consolidation target for a file already inside keep_dir is itself (dest == full, which exists), so the collision branch appends ' (_harvest_pending)' and renames it every time the keep folder is harvested.

**Fix:** Consolidate only files whose dirpath is outside keep_dir; a file already in the keep folder stays as it is. Must not break: collision renaming for same-named songs arriving from two different boxes.

## 16. loops F14 (low) -- Converter-tab library tree stats all 95k library files every hour, whether or not anyone uses the tab

`main.py:1757`

**Evidence:** 29 lines of 'converter: library tree: scanned 9077 folder(s), 95872 audio files', hourly (29 Sep 03:02:42, 04:04:11, 05:05:56, 06:06:51). _lib_rescan_loop calls LibraryTree.maybe_scan(3600), which does os.walk plus os.stat on every audio file under the array-backed /music and exists() on every cached detail (converter.py 212-257).

**Root cause:** A background timer maintains a WebUI view nobody requested: a timer taking a resource on its own. The library audit already walks the same tree, and refresh_dir already updates the cache after conversions and deletes.

**Fix:** Build and refresh the tree on demand, when the Converter tab is opened and the cache is stale, or feed it from the library audit's walk; keep refresh_dir for writes. Must not break: instant tab rendering from the file-backed cache and the delete/convert refreshes.

## 17. loops missed (low) -- _clean_album reads 'Title - YYYY.MM.DD' as 'Artist - Year - Album' and reduces the album to 'MM.DD'

`qbt_deselect.py:203`

**Evidence:** _ARTIST_YEAR_ALBUM (line 53) matches 'Hidden Figures (Original Score) - 2017.01.06' and returns '01.06' (reproduced locally). The 06:19:04 Pharrell plan shows 'Pharrell Williams / 01.06 (not in library)' for that 26-file, 119 MB folder. The Lidarr lookup therefore cannot find the album, it comes back total==0, and F2's lifecycle deleted the folder as an 'un-wanted leftover'. The outcome was the same here, because Lidarr has no monitored Hidden Figures album. For a wanted album with this naming, it would be data loss.

**Fix:** Strip the year only when what follows it is a title rather than a date remainder (require a separator plus a word, and never an MM.DD tail). Better, look the album up with the raw folder name as a second candidate (folder_hint already carries it) before concluding total==0.

## 18. orch3 COMP-BATCH-1 (low) -- comp_hunt_grabs_per_pass > 1 silently discards all but the first candidate of each batch

`orchestrator.py:11139`

**Evidence:** assembly_find_compilation takes batch, queue = queue[:cap], queue[cap:] with cap = comp_hunt_grabs_per_pass (10965-10967) and saves the shortened queue. _assembly_grab_for_songs then uses cap = 1 whenever hunt is given (11139-11141), so it tries only batch[0] and adds only that guid to 'tried'. batch[1:] is in neither queue nor tried. _comp_search_titles never re-searches a title already in 'searched' (11022), so those candidates are never tried. The WebUI help says 'How many candidates are grabbed and verified per pass'.

**Root cause:** Two layers each enforce their own per-pass cap. The caller consumes the batch from the queue on the assumption that the callee tries all of it.

**Fix:** Keep a single cap. Either _assembly_grab_for_songs tries every candidate it is handed when hunt is given (the caller already sliced), or assembly_find_compilation puts back into comp['queue'] every batch entry not in hunt['tried'] after the call. Must not break: one grab per pass for the artist-scope hunt, and the 'tried' bookkeeping.

## 19. orch3 SETTINGS-REC-1 (low) -- The 'Recommended' button in Library & sweeps turns the cueless sweep off (recommended keys use the wrong section)

`orchestrator.py:14351`

**Evidence:** _SETTINGS_RECOMMENDED has 'lidarr.sweep_cueless_pre_split': True and 'lidarr.sweep_interval_seconds': 300 (14351-14352). The schema ids are 'watch.sweep_cueless_pre_split' and 'watch.sweep_interval_seconds' (14310-14313). get_settings looks the value up by id (14531) and falls back to the schema default (False, 0). Live values are True / 60 (config and overrides). Recommended then Save writes sweep off with interval 0 ('only at startup'), so cueless downloads sit forever. A cross-check of all registry ids finds these two and no other drift.

**Root cause:** Recommended values live in a separate dict keyed by hand-typed ids, so nothing ties them to the schema.

**Fix:** Key them by the real ids. Better, make 'recommended' a column of the _SETTINGS_SCHEMA tuple and check at class load that every id in _SETTINGS_GROUPS and any remaining side table exists in the schema. Must not break: 'anything not listed recommends its own default'.

## 20. orch3 LOG-TAIL-1 (low) -- The Log tab's 400-line view splices the tail of the rotated log in front of the live tail and skips the middle

`orchestrator.py:13986`

**Evidence:** read_log reads want = max(4096, need*220) bytes from the end (13983-13988). If that chunk holds fewer than `need` lines (average line over 220 bytes), it keeps the chunk, including its partial first line (it trims only when nl > need), and moves on to pipeline.log.1 (13999-14002). The result is the .1 tail, a partial line, then the live tail, with the rest of the live file missing and no marker. Measured on PARK: 400-line windows over 88,000 bytes occur 1,048 times in the live pipeline.log, 17,055 times in .1 and 18,086 in .2 (outage error lines). The UI default is 'last 400 lines' (webui.py:395).

**Root cause:** A fixed bytes-per-line estimate stands in for reading backwards until enough newlines are found in the current file.

**Fix:** Read the current file backwards in blocks until it yields `need` newlines or offset 0, and move to the next rotation only after the current file is exhausted. Drop the leading partial line whenever the read did not start at offset 0. Must not break: small reads for a 400-line refresh (no multi-MB reads), whole-file mode for lines<=0.

## 21. orch3 SETTINGS-FLOAT-1 (low) -- Every float setting is rendered with min=0 max=1 step=0.05, so one spinner or wheel step on 'Min track match (%)' 50 sets it to 1

`webui.py:1091`

**Evidence:** webui.py:1091 adds step="0.05" min="0" max="1" to every type=='float' row. Six float settings are not 0-1 ratios: min_match_percent (live 50), assembly_min_pct 10, interactive_search_max_gb_per_album 2.5, harvest_duration_tolerance 10, assembly_lossless_bonus 30, assembly_lossy_penalty 15. On a number input whose value exceeds max, the browser's step-down clamps it to max (1). Save then accepts that value, since save_settings only float()s it (14567-14573). A single down-arrow on 'Min track match (%)' makes 1% of tracks enough to treat a download as the album.

**Root cause:** Input bounds come from the value's type, not from each setting's meaning.

**Fix:** Give each schema row an optional (min, max, step) and emit those attributes per row, with no bounds when none is declared. Keep float() parsing server-side, and optionally clamp there to the same declared range. Must not break: the 0-1 ratio fields keep their 0..1 bounds.

## 22. clients CLI-03 (high) -- 'Could not ask Lidarr' is returned as an empty answer, and callers act on it and persist it

`lidarr.py:1258`

**Status:** PARTLY DONE: failure_generation guards on the deleting/recording callers. Open: the remaining read helpers' callers that still treat [] as 'none'.

**Evidence:** On a transport failure, list_albums_for_artist (1258-1264), list_all_albums (1266-1278), queue_list (1073-1086), wanted_missing (389-391), get_album (1457-1463) and artists() (1143-1147) all return []/None/stale. Consumers: (a) The external audit, orchestrator.py:11262, sets have=[], and 11272 then writes seen_state[aid] with checked=now and EVERY MusicBrainz album listed as missing_from_lidarr. That is persisted and not re-checked for 7 days. (b) Interactive search, orchestrator.py:12329-12336: a failed queue_list gives queued_ids = {}, so albums with a live download become eligible for a second grab. The 'except Exception: pass' there is unreachable. (c) The cueless sweep's _drop_duplicate_editions (orchestrator.py:4325-4336). Live, 26 Sep 20:19:01: 10 'has 2 editions -- keeping ... (most tracks (Lidarr count unknown)), skipping ...' (Bon Jovi, Agnetha Faltskog x3, Frida Leider,...

**Root cause:** The client API has two answers (value/empty) for three states (value, genuinely empty, could not ask).

**Fix:** wanted_missing (lidarr.py:376-408) must also raise when a page FAILS MID-WALK. Today it breaks and returns the partial list, which the pass then treats as complete. The interactive-search prune at orchestrator.py:12442-12455 must run only on a complete answer: every page fetched and len(out)==totalRecords. In the external audit, a LidarrUnavailable from list_albums_for_artist must skip the artist without writing seen_state. The held curator must keep the stored 'existing' summary when the summary call fails, not replace it with {} plus a new _ts.

## 23. llm LLM-5 (medium) -- Content-identify's LLM 'tie-break' is given the leader alone (the rival is filtered out) and answers by title, and the result drove...

`orchestrator.py:4045`

**Status:** Needs the album id passed through the name-based hand-off (content-identify returns a TITLE).

**Evidence:** clear_margin (4038) is computed against results[1], but the tie-break list is `top = [r[2] for r in results[:5] if r[1] >= min_cov]` (4045). When cov1 only just clears min_cov (60), a rival within the margin (m2 >= m1-1) is always below it, so the LLM gets exactly one option and confirm_album_match can only rubber-stamp it. Live, 17 Sep 18:44:22: "LLM confirm_album: 'folder: CD1/Rollin_ and Tumblin vs 1 candidates' -> 'The Sky Is Crying'", then "content-identify: ... via titles ambiguous, LLM pick (11/18)", then "Force-import (superset) ... flipping release", then "Switched album 46858 monitored release to 117720". At 18:40:37 (8/13 = 61.5%, same shape), the returned TITLE was re-resolved downstream to a different album: "Pre-flight match: Elmore James / The Sky Is Crying (stats=12/12...)" and "Switched album 45212 monitored release to 112781" (twice, 18:40:46 and 18:40:49) on an...

**Root cause:** The ambiguity is measured on one set and the LLM is asked on another (filtered) set, so 'ambiguous' becomes 'one candidate'. Both the LLM interface (a list of titles in, a title out) and _identify_album_by_content (returns winner title) also drop the album id, so duplicate titles collapse and get re-resolved by a different heuristic.

**Fix:** Build the tie-break set from the same rule that made clear_margin false: every result within the margin of the leader. Do not accept a pick below min_cov, though. If the LLM picks a rival under min_cov, return None (undecided) rather than accept an album that failed the coverage floor. The clause 'if that set has one member, return None' can never fire, because when clear_margin is false results[1] is always within the margin, so the set always has at least 2 members. Drop it. Offer numbered options carrying year and track count, map the index back to the album record, and have _identify_album_by_content return the album id so callers stop re-matching by title. The same title-to-record collapse exists in dedup_downloads' LLM fallback (see the missed finding). Use one title/record resolver for both.

## 24. llm missed (medium) -- The LLM's owned-album pick is resolved to the FIRST album with that title, skipping the same-title guard, so a missing edition can be...

`dedup_downloads.py:277`

**Status:** Same refactor as LLM-5.

**Evidence:** album_complete_in_library guards the deterministic same-title case (202-225: when several albums share a title and any is incomplete, answer 'not owned'). The LLM fallback (270-279) loses that guard. It sends the LLM a de-duplicated SET of complete titles, then maps the answer back with `for a in albums: if a.get('title') == picked: alb = a; break`, which is the first same-titled album in Lidarr's order, whether or not it is the one being downloaded. The word-overlap guard in pick_owned_album passes trivially for self-titled albums, because the shared word is the artist's name. Path: a download named 'Evanescence EP 1998' gives norm_title 'evanescenceep', which differs from 'evanescence', so there is no same_title hit. Word-subset skips it because the title is self-titled. The LLM gets owned={'Evanescence'} and answers 'Evanescence'. The overlap guard passes on 'evanescence'. The...

**Fix:** Use one title-to-album-record resolver for every match source, deterministic and LLM alike. Send the LLM's picked title through the same same-title, year and 'any incomplete means not owned' logic as the exact match, rather than a first-match loop. Offer the LLM numbered album records carrying year and track count, not a de-duplicated title set, so same-titled albums can be told apart. This is the same resolver the LLM-5 fix needs in _identify_album_by_content. It must not break: an LLM miss never deselects, and the overlap and release-type guards stay as they are.
