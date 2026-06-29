"""Entity resolution: cluster raw records into unique people.

Life-safety design choices:
  * Strong link = matching cédula (CI). Digit-flips are detected and the cluster
    is flagged ci_conflict so a human can verify, but they are NOT auto-merged
    unless the name also matches.
  * Name+age links are allowed (that is how we find a person "dispersed" across
    lists that lack CI), but a no-CI merge is flagged so reviewers can check.
  * Every person keeps ALL raw appearances (hospital, source, raw row) so nothing
    is hidden by a merge.
"""
from __future__ import annotations

from collections import defaultdict


def _ci_relation(a: str, b: str) -> str:
    """Return 'equal', 'flip' (likely same person, one digit/transposition off),
    or 'diff'."""
    if not a or not b or len(a) < 6 or len(b) < 6:
        return "none"
    if a == b:
        return "equal"
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diffs) == 1:
            return "flip"
        if len(diffs) == 2 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]:
            return "flip"  # adjacent/any transposition
    # one extra/missing digit (e.g. 1234567 vs 12345678)
    if abs(len(a) - len(b)) == 1:
        lo, hi = (a, b) if len(a) < len(b) else (b, a)
        for i in range(len(hi)):
            if hi[:i] + hi[i+1:] == lo:
                return "flip"
    return "diff"


def _age_years(rec: dict) -> int | None:
    if rec.get("age_unit") == "months":
        return 0
    a = rec.get("age", "")
    if a.isdigit():
        return int(a)
    return None


def _age_compatible(a: dict, b: dict) -> bool:
    ya, yb = _age_years(a), _age_years(b)
    if ya is None or yb is None:
        return True
    return abs(ya - yb) <= 1


# --- common-name guard -----------------------------------------------------
# A merge with NO strong identifier (CI/phone/photo) on either side must not
# collapse a common name. "José Rodríguez" appears many times and is many
# different people; merging them would falsely tell a family their relative was
# found. So a name-only link is refused when the name is corpus-ambiguous, and
# those pairs drop to the human review queue instead (build.add_possible_same).

def _build_name_stats(records: list[dict]) -> dict:
    tok_df: dict[str, int] = defaultdict(int)        # records containing a token
    name_cis: dict[str, set] = defaultdict(set)      # distinct CIs seen per folded name
    for r in records:
        for t in set(r["name_key"].split()):
            tok_df[t] += 1
        ci = r.get("ci", "")
        if ci:
            name_cis[r["name_key"]].add(ci)
    n = len(records)
    # a token is "common" if it shows up in >= ~0.4% of records (min 4)
    return {"tok_df": tok_df, "name_cis": name_cis, "n": n, "freq_min": max(4, int(0.004 * n))}


def _name_ambiguous(ta: set, tb: set, shared: set, stats: dict) -> bool:
    # 1) proven shared by multiple real people: same folded name already has >=2 CIs
    for key in (" ".join(sorted(ta)), " ".join(sorted(tb))):
        if len(stats["name_cis"].get(key, ())) >= 2:
            return True
    # 2) generic name: <=2 shared tokens and every shared token is common
    if shared and len(shared) <= 2 and all(stats["tok_df"].get(t, 0) >= stats["freq_min"] for t in shared):
        return True
    return False


def same_person(a: dict, b: dict, stats: dict | None = None) -> tuple[bool, str]:
    """Decide if two records are the same person. Returns (is_same, reason).

    `stats` (from _build_name_stats) enables the common-name guard on no-CI
    name-only merges. Omitted -> legacy behaviour (no guard)."""
    ta = set(a["name_key"].split())
    tb = set(b["name_key"].split())
    if not ta or not tb:
        return False, ""
    shared = ta & tb
    overlap = len(shared) / max(1, min(len(ta), len(tb)))
    ci_rel = _ci_relation(a.get("ci", ""), b.get("ci", ""))

    # Strong: same CI + at least one shared name token.
    if ci_rel == "equal" and shared:
        return True, "ci_equal"
    # CI digit-flip + strong name agreement => same person (the digit-flip case).
    if ci_rel == "flip" and overlap >= 0.5 and len(shared) >= 1:
        return True, "ci_flip+name"
    # If both rows have clearly different CIs, do not merge by name alone.
    # Same-name/different-CI pairs are review candidates, not automatic identity.
    if ci_rel == "diff":
        return False, ""
    if not _age_compatible(a, b):
        return False, ""
    # Common-name guard: from here on a match is name-only (no equal/flip CI).
    # If the name is corpus-ambiguous, refuse -> goes to review, not auto-merged.
    if stats is not None and _name_ambiguous(ta, tb, shared, stats):
        return False, "common_name_needs_strong_id"
    # Identical folded name (token set) => same, even with no CI.
    if ta == tb:
        return True, "name_exact"
    # One name is a subset of the other (split surname/extra middle name).
    if shared and (shared == ta or shared == tb) and len(shared) >= 2:
        return True, "name_subset"
    # High overlap with >=2 shared tokens.
    if overlap >= 0.67 and len(shared) >= 2:
        return True, "name_overlap"
    return False, ""


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _split_ci_incompatible_members(records: list[dict], members: list[int]) -> list[list[int]]:
    """Prevent no-CI rows from bridging two clearly different CI identities.

    Pairwise linkage can safely connect "GARCIA JUAN + no CI" to either
    CI-bearing row, but the no-CI row must not make two incompatible CIs become
    one person. Keep close CI typos together and split clear differences.
    """
    ci_members = [idx for idx in members if records[idx].get("ci")]
    if len(ci_members) <= 1:
        return [members]

    buckets: list[dict[str, object]] = []
    no_ci = [idx for idx in members if not records[idx].get("ci")]

    for idx in ci_members:
        ci = records[idx].get("ci", "")
        placed = False
        for bucket in buckets:
            bucket_cis = bucket["cis"]
            if all(_ci_relation(ci, existing) in {"equal", "flip"} for existing in bucket_cis):
                bucket["cis"].add(ci)
                bucket["members"].append(idx)
                placed = True
                break
        if not placed:
            buckets.append({"cis": {ci}, "members": [idx]})

    if len(buckets) <= 1:
        return [members]

    split = [bucket["members"] for bucket in buckets]
    if no_ci:
        split.append(no_ci)
    return split


def cluster(records: list[dict]) -> list[dict]:
    n = len(records)
    uf = _UF(n)
    reasons: dict[tuple[int, int], str] = {}
    stats = _build_name_stats(records)  # for the common-name guard in same_person

    # Block by surname initial + first surname token to cut comparisons, but also
    # block by CI prefix so CI-only links across different name spellings are found.
    blocks: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        toks = r["name_key"].split()
        if toks:
            blocks[toks[0][:3]].append(i)
        ci = r.get("ci", "")
        if len(ci) >= 6:
            blocks["CI" + ci[:4]].append(i)

    seen_pairs = set()
    for idxs in blocks.values():
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                i, j = idxs[x], idxs[y]
                key = (i, j) if i < j else (j, i)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                is_same, reason = same_person(records[i], records[j], stats)
                if is_same:
                    uf.union(i, j)
                    reasons[key] = reason

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(i)

    people = []
    for gi, members in groups.items():
        for split_members in _split_ci_incompatible_members(records, members):
            member_set = set(split_members)
            recs = [records[m] for m in split_members]
            merge_reasons = {reasons[k] for k in reasons
                             if k[0] in member_set and k[1] in member_set}
            person = _summarize(recs, merge_reasons)
            people.append(person)
    # sort by surname
    people.sort(key=lambda p: p["display_name_key"])
    return people


def _best(values):
    """Pick the most common non-empty value."""
    counts = defaultdict(int)
    for v in values:
        if v:
            counts[v] += 1
    return max(counts, key=counts.get) if counts else ""


def _summarize(recs: list[dict], merge_reasons: set[str]) -> dict:
    hospitals = sorted({r["hospital"] for r in recs if r["hospital"]})
    cis = sorted({r["ci"] for r in recs if r.get("ci")})
    ages = sorted({r["age"] for r in recs if r.get("age")})
    sexes = sorted({r["sex"] for r in recs if r.get("sex")})
    origins = sorted({r["origin"] for r in recs if r.get("origin")})
    statuses = sorted({r["status"] for r in recs if r.get("status")})
    apellidos = _best([r["apellidos"] for r in recs])
    nombres = _best([r["nombres"] for r in recs])
    display = (apellidos + (" " + nombres if nombres else "")).strip()
    # collapse consecutive duplicate tokens ("ABREU PAULINA PAULINA" -> "ABREU PAULINA")
    _toks, _out = display.split(), []
    for _t in _toks:
        if not _out or _out[-1].upper() != _t.upper():
            _out.append(_t)
    display = " ".join(_out)

    deceased = any(r["status"] == "fallecido" for r in recs)
    ci_conflict = len(cis) > 1
    only_name_merge = bool(merge_reasons) and merge_reasons <= {"name_exact", "name_subset", "name_overlap"}

    appearances = [{
        "record_id": r.get("record_id", ""),
        "hospital": r["hospital"], "source": r["source"], "status": r["status"],
        "ci": r.get("ci", ""), "age": r.get("age", ""), "sex": r.get("sex", ""),
        "origin": r.get("origin", ""), "date": r.get("date", ""),
        "obs": r.get("obs", ""), "row_raw": r.get("row_raw", ""),
    } for r in recs]

    return {
        "display_name": display,
        "display_name_key": " ".join(sorted(set(" ".join(
            r["name_key"] for r in recs).split()))),
        "apellidos": apellidos, "nombres": nombres,
        "all_name_keys": sorted({r["name_key"] for r in recs}),
        "hospitals": hospitals,
        "ci": cis[0] if cis else "", "all_ci": cis,
        "age": ages[0] if ages else "", "all_ages": ages,
        "sex": sexes[0] if sexes else "",
        "origins": origins,
        "statuses": statuses,
        "deceased": deceased,
        "n_records": len(recs),
        "n_hospitals": len(hospitals),
        "in_multiple_hospitals": len(hospitals) > 1,
        "ci_conflict": ci_conflict,
        "only_name_merge": only_name_merge,
        "appearances": appearances,
    }
