#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MODULE: speakers_model.py
ROLE: Logic (no GUI)
DESCRIPTION:
Who says what. Every spoken word carries a speaker number in w['spk'] (1..10); the list of
speakers (name, colour, role, left out of the edit or not) lives in the transcript's metadata,
words_data[0]['meta_speakers'], so it is saved with the project and every cut calculation sees it.
Separate from the marks (status): a word can be "Interviewer" and "retake" at once.
"""

MAX_SPEAKERS = 10
PALETTE = ["#4FA3FF", "#FF9F43", "#B78CFF", "#3DD68C", "#FF6B9A",
           "#E8C547", "#3DD6D0", "#FF6B5C", "#8CC84B", "#B0B0B0"]
ROLES = ("main", "interviewer", "other")
CUT_STATUS = "speaker_cut"  # status the cut calculation gives words of a left-out speaker
META = "meta_speakers"


def is_spoken(w):
    return not (w.get("type") in ("silence", "inaudible") or w.get("is_inaudible")
                or not (w.get("text") or "").strip())


def make(spk_id, name, role="other"):
    return {"id": int(spk_id), "name": name, "color": PALETTE[(int(spk_id) - 1) % len(PALETTE)],
            "role": role if role in ROLES else "other", "leave_out": False}


def get(words):
    """The speaker list (copies), sorted by id; [] when the transcript has no speakers."""
    if not words:
        return []
    return sorted((dict(s) for s in (words[0].get(META) or [])), key=lambda s: s["id"])


def put(words, speakers):
    if words:
        words[0][META] = [dict(s) for s in sorted(speakers, key=lambda s: s["id"])]


def has_speakers(words):
    return bool(words) and bool(words[0].get(META))


def by_id(words):
    return {s["id"]: s for s in get(words)}


def used_ids(words):
    return sorted({w["spk"] for w in words or [] if w.get("spk") and is_spoken(w)})


def talk_seconds(words):
    out = {}
    for w in words or []:
        if w.get("spk") and is_spoken(w):
            out[w["spk"]] = out.get(w["spk"], 0.0) + max(0.0, float(w.get("end", 0)) - float(w.get("start", 0)))
    return out


def new_id(speakers):
    taken = {s["id"] for s in speakers}
    return next((i for i in range(1, MAX_SPEAKERS + 1) if i not in taken), None)


def default_roles(count):
    """1 is the main voice; with exactly two voices the other one is the interviewer."""
    return {i: ("main" if i == 1 else "interviewer" if count == 2 else "other") for i in range(1, count + 1)}


def apply_labels(words, labels, name_fmt):
    """Speakers from find_speakers(): labels[i] is 1..k or None. Replaces the speaker list."""
    count = max([l for l in labels if l] or [0])
    roles = default_roles(count)
    for w, l in zip(words, labels):
        if l:
            w["spk"] = l
        else:
            w.pop("spk", None)
    put(words, [make(i, name_fmt.format(n=i), roles[i]) for i in range(1, count + 1)])
    return count


def from_resolve(words, name_fmt):
    """Speakers from Resolve's transcription (w['speaker'] names): numbered by how much they talk,
    keeping Resolve's names. Returns how many voices were found (0 when Resolve had none)."""
    talk = {}
    for w in words or []:
        if w.get("speaker") and is_spoken(w):
            talk[w["speaker"]] = talk.get(w["speaker"], 0.0) + max(0.0, float(w.get("end", 0)) - float(w.get("start", 0)))
    if not talk:
        return 0
    order = sorted(talk, key=lambda n: -talk[n])[:MAX_SPEAKERS]
    num = {n: i + 1 for i, n in enumerate(order)}
    roles = default_roles(len(order))
    for w in words:
        n = w.get("speaker")
        if n and is_spoken(w):
            w["spk"] = num.get(n, MAX_SPEAKERS)
    put(words, [make(num[n], str(n).strip() or name_fmt.format(n=num[n]), roles[num[n]]) for n in order])
    return len(order)


def migrate_marker(words, status, marker_name, name_fmt):
    """Older transcripts marked the second voice with a custom marker: turn it into speaker 2.
    The marks are left as they are. Returns True when something was migrated."""
    if not words or has_speakers(words) or not status:
        return False
    if not any(w.get("status") == status and is_spoken(w) for w in words):
        return False
    for w in words:
        if is_spoken(w):
            w["spk"] = 2 if w.get("status") == status else 1
    put(words, [make(1, name_fmt.format(n=1), "main"), make(2, marker_name or name_fmt.format(n=2), "interviewer")])
    return True


def assign(words, ids, spk_id):
    """Give the words at `ids` to speaker spk_id (None removes the speaker). Returns
    [(index, previous spk)] for undo."""
    changes = []
    for i in ids:
        if 0 <= i < len(words) and is_spoken(words[i]) and words[i].get("spk") != spk_id:
            changes.append((i, words[i].get("spk")))
            if spk_id:
                words[i]["spk"] = spk_id
            else:
                words[i].pop("spk", None)
    return changes


def restore(words, changes):
    for i, old in changes:
        if 0 <= i < len(words):
            if old:
                words[i]["spk"] = old
            else:
                words[i].pop("spk", None)


def turns(words):
    """[(first index, last index, speaker id)] over spoken words: a new turn starts whenever the
    speaker changes. Silences between two words of the same speaker don't split a turn."""
    out = []
    for i, w in enumerate(words or []):
        if not is_spoken(w):
            continue
        s = w.get("spk")
        if out and out[-1][2] == s:
            out[-1][1] = i
        else:
            out.append([i, i, s])
    return [tuple(t) for t in out]


def turn_at(words, index):
    """(first, last, spk) of the turn that holds word `index`, or None."""
    for t in turns(words):
        if t[0] <= index <= t[1]:
            return t
    return None


def merge(words, src, dst):
    """Speaker src becomes dst everywhere; src leaves the list. Returns the undo changes."""
    changes = assign(words, [i for i, w in enumerate(words) if w.get("spk") == src], dst)
    put(words, [s for s in get(words) if s["id"] != src])
    return changes


def update(words, spk_id, **fields):
    sp = get(words)
    for s in sp:
        if s["id"] == spk_id:
            s.update({k: v for k, v in fields.items() if k in ("name", "color", "role", "leave_out", "camera")})
    put(words, sp)


def add(words, name_fmt):
    """A new, empty speaker; returns its id or None when there are already 10."""
    sp = get(words)
    nid = new_id(sp)
    if nid is None:
        return None
    sp.append(make(nid, name_fmt.format(n=nid)))
    put(words, sp)
    return nid


def remove_unused(words):
    """Drop speakers nobody is assigned to any more (keeps the list tidy after merges/edits)."""
    used = set(used_ids(words))
    sp = get(words)
    kept = [s for s in sp if s["id"] in used]
    if len(kept) != len(sp):
        put(words, kept)


def cut_ids(words):
    return {s["id"] for s in get(words) if s.get("leave_out")}
