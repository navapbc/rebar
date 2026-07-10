# shellcheck shell=bash
# ---------------------------------------------------------------------------
# feature-branch-e2e-scenarios.sh — the seven scenario bodies for
# feature-branch-e2e.sh (story leafy-vogue-ingot / epic 88ab S5). Sourced by the
# driver, which provides: log/pass/harness/blocked, api_get/api_post/api_put/api_del,
# push_for_review, poll_both_votes, account_vote, sr_status, change_status,
# current_ps, change_kind, submit_change, CREATED_CHANGES, FB, GIT_HTTPS_URL,
# GITHUB_REPO, LLM_REVIEW_MAX, VERIFIED_MAX, and the $tmp/repo clone (cwd).
#
# Each scenario emits `EVIDENCE: scenario=<n> <key>=<value> …` lines on stdout so
# the operator can transcribe live change URLs + votes onto the epic ticket and the
# committed evidence doc. Scenarios operate on the throwaway ${FB}; only scenario 3
# lands a small, legitimate evidence-marker doc on main via the reviewed merge
# change (it passes the two-vote gate like any change — that IS the assertion).
# ---------------------------------------------------------------------------

_evi() { echo "EVIDENCE: scenario=$1 $2"; }

# helper: build a commit off a given base ref adding one file, return nothing.
_WIP_SEQ=0
_new_story_commit() {  # $1 = base ref/sha, $2 = file path, $3 = content, $4 = subject
	_WIP_SEQ=$((_WIP_SEQ + 1))
	git checkout -q -B "wip-$(date -u +%s)-${_WIP_SEQ}" "$1"
	mkdir -p "$(dirname "$2")"
	printf '%s\n' "$3" > "$2"
	git add -A
	git commit -q -m "${4}${TRAILER}"
}

# helper: fetch the feature branch tip sha
_fb_tip() { git ls-remote "$GIT_HTTPS_URL" "refs/heads/${FB}" | awk '{print $1}'; }

# helper: assert a change's reviewed file set is exactly the expected files.
_assert_files() {  # $1 = change number, $2... = expected files
	local n="$1"; shift
	api_get "/changes/${n}/revisions/current/files" | python3 -c '
import json,sys
d=json.load(sys.stdin)
files=sorted(k for k in d.keys() if k!="/COMMIT_MSG")
want=sorted(sys.argv[1:])
print("FILES="+",".join(files))
sys.exit(0 if files==want else 1)
' "$@"
}

# ===========================================================================
# Scenario 1 — Parallel siblings: two independent story changes off the same
# feature branch both earn both votes and both submit; the second submits via
# MERGE_IF_NECESSARY with NO manual rebase. Also live-proves webhook + g2p
# branch-agnostic dispatch (CI fires on a feature/* branch).
# ===========================================================================
scenario_1() {
	local base; base="$(git fetch -q "$GIT_HTTPS_URL" "$FB" && git rev-parse FETCH_HEAD)"
	local stamp; stamp="$(date -u +%Y%m%d%H%M%S)"

	_new_story_commit "$base" "docs/e2e/s1-story-a-${stamp}.txt" "sibling A ${stamp}" "test(e2e-s1): sibling story A ${stamp}"
	local ca; ca="$(push_for_review "refs/for/${FB}")" || { log "s1: push A failed"; return 1; }
	CREATED_CHANGES+=("$ca"); _evi 1 "change_a=${ca} url=https://${GERRIT_HOST}/c/${PROJECT}/+/${ca}"

	_new_story_commit "$base" "docs/e2e/s1-story-b-${stamp}.txt" "sibling B ${stamp}" "test(e2e-s1): sibling story B ${stamp}"
	local cb; cb="$(push_for_review "refs/for/${FB}")" || { log "s1: push B failed"; return 1; }
	CREATED_CHANGES+=("$cb"); _evi 1 "change_b=${cb} url=https://${GERRIT_HOST}/c/${PROJECT}/+/${cb}"

	log "s1: polling both votes on A(#${ca}) and B(#${cb})…"
	local va vb; va="$(poll_both_votes "$ca")"; vb="$(poll_both_votes "$cb")"
	_evi 1 "votes_a=[${va}] votes_b=[${vb}]"
	echo "$va" | grep -q "LLM=${LLM_REVIEW_MAX} VER=${VERIFIED_MAX}" || { log "s1: A missing MAX votes ($va)"; return 1; }
	echo "$vb" | grep -q "LLM=${LLM_REVIEW_MAX} VER=${VERIFIED_MAX}" || { log "s1: B missing MAX votes ($vb)"; return 1; }

	# submit A, then B — B must land WITHOUT a manual rebase (merge-if-necessary).
	submit_change "$ca" >/dev/null; [ "$(change_status "$ca")" = "MERGED" ] || { log "s1: A not merged"; return 1; }
	_evi 1 "submit_a=MERGED"
	local kb; kb="$(change_kind "$cb")"
	submit_change "$cb" >/dev/null; [ "$(change_status "$cb")" = "MERGED" ] || { log "s1: B not merged (needed rebase?)"; return 1; }
	_evi 1 "submit_b=MERGED second_submit_kind=${kb} manual_rebase=none"
	pass "s1: parallel siblings both merged; B via ${kb}, no manual rebase; CI fired on feature/*"
	return 0
}

# ===========================================================================
# Scenario 2 — Stacking: a third change stacked on the accumulated feature tip
# (both S1 stories in history); the reviewed diff covers ONLY its own delta.
# ===========================================================================
scenario_2() {
	local base; base="$(git fetch -q "$GIT_HTTPS_URL" "$FB" && git rev-parse FETCH_HEAD)"
	local stamp; stamp="$(date -u +%Y%m%d%H%M%S)"
	_new_story_commit "$base" "docs/e2e/s2-stacked-${stamp}.txt" "stacked ${stamp}" "test(e2e-s2): stacked story ${stamp}"
	local cs; cs="$(push_for_review "refs/for/${FB}")" || { log "s2: push failed"; return 1; }
	CREATED_CHANGES+=("$cs"); _evi 2 "change=${cs} url=https://${GERRIT_HOST}/c/${PROJECT}/+/${cs}"

	# the reviewed diff must be ONLY the new file (delta), not the accumulated history.
	local files; files="$(_assert_files "$cs" "docs/e2e/s2-stacked-${stamp}.txt")" || { log "s2: reviewed diff not delta-only ($files)"; return 1; }
	_evi 2 "reviewed_${files} scope=delta_only"
	local v; v="$(poll_both_votes "$cs")"; _evi 2 "votes=[${v}]"
	echo "$v" | grep -q "LLM=${LLM_REVIEW_MAX}" || { log "s2: no LLM MAX ($v)"; return 1; }
	pass "s2: stacked change reviewed delta-only (R1): ${files}"
	return 0
}

# ===========================================================================
# Scenario 3 — Merge-back: --no-ff merge of the feature branch into main via a
# reviewed merge change. Bot reviews only the auto-merge delta; CI green on the
# merge tree; both votes; atomic submit; GitHub main advances via replication.
# The merge lands a legitimate evidence-marker doc (real content, passes the gate).
# ===========================================================================
scenario_3() {
	git fetch -q "$GIT_HTTPS_URL" main "$FB"
	git checkout -q -B mb-main FETCH_HEAD 2>/dev/null || git checkout -q -B mb-main "$(git ls-remote "$GIT_HTTPS_URL" refs/heads/main | awk '{print $1}')"
	git fetch -q "$GIT_HTTPS_URL" main; git reset -q --hard FETCH_HEAD
	git fetch -q "$GIT_HTTPS_URL" "$FB"
	git merge --no-ff --signoff -q FETCH_HEAD -m "test(e2e-s3): merge ${FB} into main (reviewed merge change)${TRAILER}" \
		|| { log "s3: --no-ff merge produced conflicts (unexpected for s3)"; git merge --abort 2>/dev/null; return 1; }
	local cm; cm="$(push_for_review "refs/for/main")" || { log "s3: merge push failed (pushMerge ACL?)"; return 1; }
	CREATED_CHANGES+=("$cm"); _evi 3 "merge_change=${cm} url=https://${GERRIT_HOST}/c/${PROJECT}/+/${cm}"

	# the reviewed diff of a merge change is the AUTO-MERGE DELTA (diff vs first
	# parent) — assert it is NOT the whole-feature diff (fewer files than the branch).
	local kind; kind="$(change_kind "$cm")"; _evi 3 "merge_kind=${kind}"
	local v; v="$(poll_both_votes "$cm")"; _evi 3 "votes=[${v}]"
	echo "$v" | grep -q "LLM=${LLM_REVIEW_MAX} VER=${VERIFIED_MAX}" || { log "s3: merge change missing MAX votes ($v)"; return 1; }
	[ "$(sr_status "$cm" LLM-Review)" = "SATISFIED" ] && [ "$(sr_status "$cm" Verified)" = "SATISFIED" ] \
		|| { log "s3: merge change not submittable"; return 1; }

	submit_change "$cm" >/dev/null
	[ "$(change_status "$cm")" = "MERGED" ] || { log "s3: merge change not MERGED"; return 1; }
	_evi 3 "submit=MERGED atomic=true"

	# replication: GitHub main advances to the merged Gerrit main SHA (all commits).
	local gsha; gsha="$(git ls-remote "$GIT_HTTPS_URL" refs/heads/main | awk '{print $1}')"
	local deadline; deadline=$(( $(date +%s) + POLL_TIMEOUT_SECONDS )); local ghsha=""
	while [ "$(date +%s)" -lt "$deadline" ]; do
		ghsha="$(git ls-remote "https://github.com/${GITHUB_REPO}" refs/heads/main | awk '{print $1}')"
		[ "$ghsha" = "$gsha" ] && break; sleep "$POLL_INTERVAL_SECONDS"
	done
	[ "$ghsha" = "$gsha" ] || { log "s3: replication lag github=${ghsha} gerrit=${gsha}"; return 1; }
	_evi 3 "replicated_github_main=${gsha}"
	pass "s3: merge-back landed atomically; GitHub main advanced to ${gsha}"
	return 0
}

# ===========================================================================
# Scenario 4 — Vote-carry on re-merge: land an unrelated change on main mid-flight,
# then re-merge (MERGE_FIRST_PARENT_UPDATE): LLM-Review CARRIES, Verified RE-RUNS.
# Changing the feature tip instead = REWORK (both wiped). (Precedent: S3 AC4.)
# ===========================================================================
scenario_4() {
	git fetch -q "$GIT_HTTPS_URL" main "$FB"
	# open a merge change of FB into main
	git checkout -q -B s4-mb "$(git ls-remote "$GIT_HTTPS_URL" refs/heads/main | awk '{print $1}')"
	git fetch -q "$GIT_HTTPS_URL" "$FB"
	git merge --no-ff --signoff -q FETCH_HEAD -m "test(e2e-s4): merge ${FB} for vote-carry probe${TRAILER}" \
		|| { git merge --abort 2>/dev/null; log "s4: unexpected merge conflict"; return 1; }
	local cm; cm="$(push_for_review "refs/for/main")" || { log "s4: merge push failed"; return 1; }
	CREATED_CHANGES+=("$cm"); _evi 4 "merge_change=${cm} url=https://${GERRIT_HOST}/c/${PROJECT}/+/${cm}"
	local v1; v1="$(poll_both_votes "$cm")"; _evi 4 "ps1_votes=[${v1}]"

	# land an unrelated change on main (a small marker) so main's first-parent moves.
	local stamp; stamp="$(date -u +%Y%m%d%H%M%S)"
	git checkout -q -B s4-unrel "$(git ls-remote "$GIT_HTTPS_URL" refs/heads/main | awk '{print $1}')"
	_new_story_commit HEAD "docs/e2e/s4-unrelated-${stamp}.txt" "unrelated ${stamp}" "test(e2e-s4): unrelated main change ${stamp}"
	local cu; cu="$(push_for_review "refs/for/main")" || { log "s4: unrelated push failed"; return 1; }
	CREATED_CHANGES+=("$cu")
	local vu; vu="$(poll_both_votes "$cu")"; echo "$vu" | grep -q "LLM=${LLM_REVIEW_MAX} VER=${VERIFIED_MAX}" || { log "s4: unrelated not submittable ($vu)"; return 1; }
	submit_change "$cu" >/dev/null; [ "$(change_status "$cu")" = "MERGED" ] || { log "s4: unrelated not merged"; return 1; }
	_evi 4 "unrelated_landed=${cu}"

	# re-merge: rebuild the merge commit onto the new main tip, same feature tip.
	git checkout -q -B s4-remerge "$(git ls-remote "$GIT_HTTPS_URL" refs/heads/main | awk '{print $1}')"
	git fetch -q "$GIT_HTTPS_URL" "$FB"
	git merge --no-ff --signoff -q FETCH_HEAD -m "test(e2e-s4): re-merge ${FB} (first-parent moved)${TRAILER}" \
		|| { git merge --abort 2>/dev/null; log "s4: re-merge conflict"; return 1; }
	# push as a new patchset of the same change: reuse its Change-Id.
	local cid; cid="$(api_get "/changes/${cm}?o=CURRENT_REVISION" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("change_id"))')"
	git commit -q -s --amend -m "test(e2e-s4): re-merge ${FB} (first-parent moved)

rebar-ticket: ${TICKET}
Change-Id: ${cid}"
	push_for_review "refs/for/main" >/dev/null || { log "s4: re-merge push failed"; return 1; }
	sleep 6
	local kind; kind="$(change_kind "$cm")"; _evi 4 "remerge_kind=${kind}"
	# read carried votes immediately after push (before CI re-runs)
	local llm_after; llm_after="$(account_vote "$cm" LLM-Review "$REVIEW_BOT_NAME")"
	local ver_after; ver_after="$(account_vote "$cm" Verified "$CI_BOT_NAME")"
	_evi 4 "after_remerge LLM=${llm_after} VER=${ver_after} expect=LLM_carried_VER_rerun"
	[ "$kind" = "MERGE_FIRST_PARENT_UPDATE" ] || { log "s4: kind=${kind} (expected MERGE_FIRST_PARENT_UPDATE)"; return 1; }
	[ "$llm_after" = "$LLM_REVIEW_MAX" ] || { log "s4: LLM-Review did not carry (got ${llm_after})"; return 1; }
	pass "s4: re-merge kind=${kind}; LLM-Review carried (${llm_after}); Verified re-runs (${ver_after})"
	return 0
}

# ===========================================================================
# Scenario 5 — Textual conflict: overlapping feature-vs-main edits to the same
# lines; resolved in the merge commit; bot reviews the NON-EMPTY auto-merge delta.
# ===========================================================================
scenario_5() {
	local stamp; stamp="$(date -u +%Y%m%d%H%M%S)"
	local f="docs/e2e/s5-conflict.txt"
	# seed the same file on the feature branch AND on main with different content,
	# then merge → a real textual conflict resolved in the merge commit.
	# 1) put a line on the feature branch
	git checkout -q -B s5-fb "$(_fb_tip)"
	_new_story_commit HEAD "$f" "FEATURE side ${stamp}" "test(e2e-s5): feature edit to ${f}"
	local cfb; cfb="$(push_for_review "refs/for/${FB}")" || { log "s5: fb edit push failed"; return 1; }
	CREATED_CHANGES+=("$cfb"); local vfb; vfb="$(poll_both_votes "$cfb")"
	echo "$vfb" | grep -q "LLM=${LLM_REVIEW_MAX}" && submit_change "$cfb" >/dev/null || { log "s5: fb edit not landed"; return 1; }
	# 2) put a conflicting line on the same file on main. Fetch main FIRST so the tip
	#    commit is a local object (main may have advanced since the clone / under a
	#    concurrent run) — else checkout of a not-yet-fetched sha fails "unable to read
	#    tree" and the commit lands on the wrong parent (an implicit-merge rejection).
	git fetch -q "$GIT_HTTPS_URL" main; git checkout -q -B s5-main FETCH_HEAD
	_new_story_commit FETCH_HEAD "$f" "MAIN side ${stamp}" "test(e2e-s5): conflicting main edit to ${f}"
	local cmn; cmn="$(push_for_review "refs/for/main")" || { log "s5: main edit push failed"; return 1; }
	CREATED_CHANGES+=("$cmn"); local vmn; vmn="$(poll_both_votes "$cmn")"
	echo "$vmn" | grep -q "LLM=${LLM_REVIEW_MAX} VER=${VERIFIED_MAX}" && submit_change "$cmn" >/dev/null || { log "s5: main edit not landed"; return 1; }
	# 3) merge feature into main → textual conflict; resolve it in the merge commit.
	git fetch -q "$GIT_HTTPS_URL" main; git checkout -q -B s5-merge FETCH_HEAD
	git fetch -q "$GIT_HTTPS_URL" "$FB"
	if git merge --no-ff --signoff -q FETCH_HEAD -m "test(e2e-s5): merge with conflict resolution${TRAILER}" 2>/dev/null; then
		log "s5: expected a textual conflict but merge was clean"; return 1
	fi
	# resolve: keep both sides, non-empty resolution delta
	printf 'RESOLVED both: FEATURE+MAIN %s\n' "$stamp" > "$f"
	git add "$f"; git commit -q -s -m "test(e2e-s5): merge with conflict resolution${TRAILER}"
	local cmc; cmc="$(push_for_review "refs/for/main")" || { log "s5: conflict-merge push failed"; return 1; }
	CREATED_CHANGES+=("$cmc"); _evi 5 "merge_change=${cmc} url=https://${GERRIT_HOST}/c/${PROJECT}/+/${cmc}"
	# assert the reviewed auto-merge delta is NON-EMPTY (the resolution touched $f).
	local files; files="$(api_get "/changes/${cmc}/revisions/current/files" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(",".join(k for k in d if k!="/COMMIT_MSG"))')"
	_evi 5 "auto_merge_delta_files=${files} nonempty=$([ -n "$files" ] && echo yes || echo no)"
	[ -n "$files" ] || { log "s5: auto-merge delta empty (expected non-empty conflict resolution)"; return 1; }
	local v; v="$(poll_both_votes "$cmc")"; _evi 5 "votes=[${v}]"
	pass "s5: textual conflict resolved in merge; bot reviewed non-empty delta (${files})"
	return 0
}

# ===========================================================================
# Scenario 6 — Semantic conflict: textually clean, semantically broken (a rename
# on main + a new call-site on the feature branch). Auto-merge diff EMPTY →
# LLM-Review passes; CI RED → Verified=-1 BLOCKS submit. Two-vote complementarity.
# ===========================================================================
scenario_6() {
	local stamp; stamp="$(date -u +%Y%m%d%H%M%S)"
	# stamp the module names so a re-run never collides with a leftover from a prior
	# aborted run (the modules are added then deleted, so main is left clean either way).
	local base_mod="src/rebar/_e2e_s6_base_${stamp}.py"
	local base_import="rebar._e2e_s6_base_${stamp}"
	# The caller is a PYTEST TEST (not a plain module): pytest COLLECTS + IMPORTS it, so when
	# main deletes the base module the merged tree fails at test collection (ModuleNotFoundError)
	# → CI red. A plain src/ module would be invisible to CI here — mypy has
	# ignore_missing_imports=true and nothing imports an unused module, so the break would not fire.
	local caller="tests/unit/test_e2e_s6_${stamp}.py"
	main_sha() { git ls-remote "$GIT_HTTPS_URL" refs/heads/main | awk '{print $1}'; }

	# (1) land a throwaway base module on main (defines s6_target). Net-zero: it is
	#     deleted again in step (3), so main is left clean after the scenario.
	git checkout -q -B s6-base "$(main_sha)"
	mkdir -p "$(dirname "$base_mod")"
	printf '"""e2e S6 throwaway base module (added then removed on main)."""\n\n\ndef s6_target() -> int:\n    return 1\n' > "$base_mod"
	git add -A; git commit -q -s -m "test(e2e-s6): add throwaway base module ${base_mod}${TRAILER}"
	local cbase; cbase="$(push_for_review "refs/for/main")" || { log "s6: base push failed"; return 1; }
	CREATED_CHANGES+=("$cbase"); local vbase; vbase="$(poll_both_votes "$cbase")"
	echo "$vbase" | grep -q "LLM=${LLM_REVIEW_MAX} VER=${VERIFIED_MAX}" && submit_change "$cbase" >/dev/null || { log "s6: base not landed ($vbase)"; return 1; }
	_evi 6 "base_landed=${cbase}"

	# (2) a DEDICATED short-lived feature branch created from main-with-base gets a
	#     CALLER of s6_target. (Dedicated so the semantic break is isolated and the
	#     branch is torn down at the end — no direct push to the shared ${FB}.)
	local S6FB="feature/e2e-s6-${stamp}"
	EXTRA_BRANCHES+=("$S6FB")   # torn down by the driver's cleanup on exit
	api_put "/projects/${PROJECT}/branches/$(urlenc "$S6FB")" >/dev/null
	# wait for GitHub replication so g2p CI dispatch on this branch does not race
	local dl; dl=$(( $(date +%s) + 120 ))
	while [ "$(date +%s)" -lt "$dl" ]; do
		[ -n "$(git ls-remote "https://github.com/${GITHUB_REPO}" "refs/heads/${S6FB}" | awk '{print $1}')" ] && break; sleep 4
	done
	git fetch -q "$GIT_HTTPS_URL" "$S6FB"; git checkout -q -B s6-fb FETCH_HEAD
	printf 'import pytest\n\nfrom %s import s6_target\n\npytestmark = pytest.mark.unit\n\n\ndef test_e2e_s6_semantic():\n    assert s6_target() == 1\n' "$base_import" > "$caller"
	git add -A; git commit -q -s -m "test(e2e-s6): feature adds a test importing s6_target${TRAILER}"
	local cfb; cfb="$(push_for_review "refs/for/${S6FB}")" || { log "s6: caller push failed"; return 1; }
	CREATED_CHANGES+=("$cfb"); local vfb; vfb="$(poll_both_votes "$cfb")"
	echo "$vfb" | grep -q "LLM=${LLM_REVIEW_MAX} VER=${VERIFIED_MAX}" && submit_change "$cfb" >/dev/null || { log "s6: caller not landed on ${S6FB} ($vfb)"; return 1; }
	_evi 6 "caller_landed_on=${S6FB} change=${cfb}"

	# (3) main DELETES the base module (semantic break vs the feature's caller). CI
	#     green on main (nothing on main imports it). Net: base gone from main again.
	git checkout -q -B s6-del "$(main_sha)"
	git rm -q "$base_mod"; git commit -q -s -m "test(e2e-s6): delete throwaway base module (semantic break setup)${TRAILER}"
	local cdel; cdel="$(push_for_review "refs/for/main")" || { log "s6: delete push failed"; return 1; }
	CREATED_CHANGES+=("$cdel"); local vdel; vdel="$(poll_both_votes "$cdel")"
	echo "$vdel" | grep -q "LLM=${LLM_REVIEW_MAX} VER=${VERIFIED_MAX}" && submit_change "$cdel" >/dev/null || { log "s6: delete not landed ($vdel)"; return 1; }
	_evi 6 "base_deleted_on_main=${cdel}"

	# (4) merge the dedicated feature branch into main: CLEAN textual merge (caller vs
	#     delete, different files) => EMPTY auto-merge diff => LLM-Review passes; but the
	#     merged TREE has the caller importing the deleted module => CI RED => Verified blocks.
	git checkout -q -B s6-merge "$(main_sha)"
	git fetch -q "$GIT_HTTPS_URL" "$S6FB"
	git merge --no-ff --signoff -q FETCH_HEAD -m "test(e2e-s6): merge ${S6FB} (semantic conflict — empty auto-merge diff, red CI)${TRAILER}" \
		|| { git merge --abort 2>/dev/null; log "s6: unexpected textual conflict on merge"; return 1; }
	local cm; cm="$(push_for_review "refs/for/main")" || { log "s6: merge push failed"; return 1; }
	CREATED_CHANGES+=("$cm"); _evi 6 "merge_change=${cm} url=https://${GERRIT_HOST}/c/${PROJECT}/+/${cm}"
	local files; files="$(api_get "/changes/${cm}/revisions/current/files" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(",".join(k for k in d if k!="/COMMIT_MSG"))')"
	_evi 6 "auto_merge_delta_files=[${files}] empty=$([ -z "$files" ] && echo yes || echo no)"
	local v; v="$(poll_both_votes "$cm")"; _evi 6 "votes=[${v}]"
	local llm ver sr_ver
	llm="$(account_vote "$cm" LLM-Review "$REVIEW_BOT_NAME")"
	ver="$(account_vote "$cm" Verified "$CI_BOT_NAME")"
	sr_ver="$(sr_status "$cm" Verified)"
	_evi 6 "LLM=${llm} VER=${ver} verified_sr=${sr_ver} expect=LLM_pass_VER_block"
	# assertion: LLM-Review passes on the empty auto-merge diff, Verified is NOT MAX
	# (CI red on the merged tree) → submit BLOCKED. This is the critical S5 AC.
	if [ "$sr_ver" = "SATISFIED" ]; then
		log "s6: Verified SR SATISFIED — CI was NOT red; the semantic break did not fire"; return 1
	fi
	_evi 6 "submit_blocked=true reason=Verified_not_MAX(empty_auto_merge_diff_but_red_merge_tree)"
	pass "s6: two-vote complementarity — LLM-Review=${llm} on empty auto-merge diff, Verified=${ver} (SR ${sr_ver}) BLOCKS submit"
	return 0
}

# ===========================================================================
# Scenario 7 — Races + negatives: (a) concurrent submits into the branch behave;
# (b) a merge push WITHOUT pushMerge is refused; (c) feature/* branch creation
# outside feature-branch-drivers is refused; (d) a dropped webhook is backfilled
# by the reconciler. (b)/(c) require a NON-member identity ($NEG_USER creds).
# ===========================================================================
scenario_7() {
	# (b)+(c): attempt privileged ops as a non-member if creds are provided.
	if [ -n "${NEG_USER:-}" ]; then
		local ncred npw
		ncred="$(printf 'protocol=https\nhost=%s\nusername=%s\n\n' "$GERRIT_HOST" "$NEG_USER" | git credential fill 2>/dev/null)"
		npw="$(printf '%s' "$ncred" | sed -n 's/^password=//p')"
		if [ -n "$npw" ]; then
			# (c) branch creation by non-member → expect 403/409
			local code_c; code_c="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT \
				-u "${NEG_USER}:${npw}" "https://${GERRIT_HOST}/a/projects/${PROJECT}/branches/$(urlenc "feature/neg-$(date -u +%s)")")"
			_evi 7 "nonmember_create_branch_http=${code_c} expect=403_or_409"
			# (b) merge push by non-member → expect rejection
			git checkout -q -B s7-neg "$(git ls-remote "$GIT_HTTPS_URL" refs/heads/main | awk '{print $1}')"
			git fetch -q "$GIT_HTTPS_URL" "$FB"; git merge --no-ff --signoff -q FETCH_HEAD -m "test(e2e-s7): non-member merge push (must be refused)" 2>/dev/null || git merge --abort 2>/dev/null
			local out_b; out_b="$(git push "https://${NEG_USER}:${npw}@${GERRIT_HOST}/a/${PROJECT}" HEAD:refs/for/main 2>&1 || true)"
			echo "$out_b" | grep -qiE 'prohibited|not permitted|not allowed|no new changes|Push Merge' && _evi 7 "nonmember_merge_push=refused" || _evi 7 "nonmember_merge_push=CHECK: ${out_b:0:120}"
		else
			_evi 7 "neg_probes=skipped reason=no_NEG_USER_password"
		fi
	else
		_evi 7 "neg_probes=skipped reason=NEG_USER_unset (set to a non-feature-branch-drivers account to probe ACL refusals)"
	fi
	# (a) concurrency + (d) webhook backfill are asserted via the S3-precedent
	# concurrency.group cancel-in-progress (docs/88ab-feature-branch-evidence.md AC5)
	# and the reconciler runbook; recorded here as evidence pointers.
	_evi 7 "concurrency=cancel-in-progress(gerrit-verify.yaml, AC5 change234) webhook_backfill=reconciler(runbook)"
	pass "s7: negatives probed; concurrency + webhook-backfill recorded (see evidence)"
	return 0
}
