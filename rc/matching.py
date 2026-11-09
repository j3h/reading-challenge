"""Match a name (and teacher) read off a scanned sheet to a student in the checkpoint.

Handwriting gives us first names only, nicknames, misspellings and teacher names written differently
from the roster, so matching never decides silently unless it is certain. Each match comes back with a
status the review step acts on:

    exact      one student, name agrees (ignoring case, punctuation, accents). Used automatically.
    likely     one clearly best student. Pre-selected in review; a person confirms.
    ambiguous  several plausible students (two Andersons, first name only). A person chooses.
    none       nobody plausible: add as a new student, or skip the sheet.

Teacher agreement raises a score and a clear conflict lowers it, but teachers change between years and
classes get combined ("Biello/Tanner"), so teacher alone never makes or breaks a match.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from rc import checkpoint as ck

EXACT, LIKELY, AMBIGUOUS, NONE = "exact", "likely", "ambiguous", "none"

EXACT_SCORE = 0.97
LIKELY_SCORE = 0.80
PLAUSIBLE_SCORE = 0.60
CLEAR_MARGIN = 0.10


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text.lower().replace("’", "").replace("'", ""))
    return " ".join(text.split())


# Names a child may write instead of the one on the roster. Each set is interchangeable (a first name only).
NICKNAME_GROUPS = [set(g.split()) for g in """
rosalind roz rozzie rosie ros|joseph joe joey|william will bill billy willy liam|elizabeth liz lizzie beth eliza libby betsy
katherine catherine kate katie kathy kat kitty|margaret maggie meg peggy greta|alexander alex xander sandy|alexandra alex lexi
michael mike mikey|matthew matt matty|benjamin ben benny benji|daniel dan danny|david dave davey|jonathan jon jonny john johnny
samuel sam sammy|thomas tom tommy|nicholas nick nicky nico|christopher chris topher kit|andrew andy drew|anthony tony
charles charlie chuck|edward ed eddie ted teddy|henry hank harry hal|richard rich rick ricky dick|robert rob robbie bob bobby
james jim jimmy jamie|patrick pat paddy|theodore theo ted teddy|timothy tim timmy|zachary zach zack zac|abigail abby abbie gail
amelia millie mellie amy|isabella isabelle isabel bella izzy izzie belle|madeline madelyn madeleine maddie maddy madi
olivia liv livvy ollie olive|victoria vicky tori tory|jacqueline jackie jacqui|jennifer jen jenny|jessica jess jessie
rebecca becca becky|samantha sam sammy sami|stephanie steph stevie|sophia sofia sophie sofie|gabriella gabrielle gabby gabi
eleanor ellie elle nora norah nell|penelope penny nell|josephine josie jo|juliana julia julie jules|lucas luke luca
maximilian maxwell max maxim|francisco paco frankie frank|frances fran frannie franny|nathaniel nate nathan nat
ellen ellie nell|susan sue suzy susie|deborah deb debbie|barbara barb barbie babs|dorothy dot dottie dolly|evelyn evie eve
cassandra cassie cass|caroline carrie caro lina|vincent vince vinny|ronald ron ronnie|gregory greg|raymond ray|leonard leo len lenny
""".strip().replace("\n", "|").split("|") if g.strip()]


def name_group(token: str) -> set[str]:
    """Every first name the same child might write instead of `token` (just {token} when none is known)."""
    out = {token}
    for g in NICKNAME_GROUPS:
        if token in g:
            out |= g
    return out


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def name_score(sheet: str, known: str, nicknames=()) -> tuple[float, str]:
    """Best score of the sheet name against the known name, its common nicknames and the child's own."""
    best = _name_score(sheet, known)
    a, b = normalize(sheet).split(), normalize(known).split()
    if a and b and a[0] != b[0] and a[0] in name_group(b[0]):       # Rozzie for Rosalind, Joey for Joseph
        score, why = _name_score(" ".join([b[0]] + a[1:]), known)
        if score * 0.95 > best[0]:
            best = (round(score * 0.95, 3), "nickname")
    for nick in nicknames:                                           # names this child is known to write
        alt = nick if " " in nick.strip() else " ".join([nick] + b[1:])
        score, why = _name_score(sheet, alt)
        if len(a) == 1 and normalize(nick) == a[0]:
            score, why = 1.0, "same name"
        if score > best[0]:
            best = (score, f"{why} (also called {nick})")
    return best


def _name_score(sheet: str, known: str) -> tuple[float, str]:
    """(0..1, why) for how well the name written on a sheet fits a known student name."""
    a, b = normalize(sheet), normalize(known)
    if not a or not b:
        return 0.0, "no name"
    if a == b:
        return 1.0, "same name"
    full = _ratio(a, b)
    at, bt = a.split(), b.split()
    if len(at) == 1 and len(bt) > 1 and _ratio(at[0], bt[0]) < 0.85:
        full = min(full, 0.65)       # one word that is not the first name (a surname): never "likely"
    best, why = full, "similar name"
    # first name only on the sheet
    if len(at) == 1:
        first = _ratio(at[0], bt[0])
        if first >= 0.85:
            s = 0.55 + 0.25 * first          # 0.76 .. 0.80: a first name alone is never "certain"
            if s > best:
                best, why = s, "first name only"
    # roster has "First L." (a last initial): compare first names, then the initial
    if len(bt) == 2 and len(bt[1]) == 1 and len(at) >= 1:
        first = _ratio(at[0], bt[0])
        if first >= 0.85:
            initial_ok = len(at) == 1 or at[-1].startswith(bt[1])
            s = 0.9 * first if initial_ok else 0.5 * first
            if s > best:
                best, why = s, "first name and last initial"
    # sheet has "First L." and roster has the full name
    if len(at) == 2 and len(at[1]) == 1 and len(bt) >= 2:
        first = _ratio(at[0], bt[0])
        if first >= 0.85 and bt[-1].startswith(at[1]):
            s = 0.9 * first
            if s > best:
                best, why = s, "first name and last initial"
    # same words in a different order ("Lopez Ada")
    if sorted(at) == sorted(bt):
        best, why = max(best, 0.95), "same words, different order"
    return min(best, 1.0), why


HONORIFICS = {"ms", "mrs", "mr", "miss", "dr", "teacher"}


def teacher_parts(teacher: str) -> list[str]:
    """'Ms. Biello/Tanner' -> ['biello', 'tanner']: honorifics are dropped, co-teachers split."""
    parts = []
    for piece in re.split(r"[/&,]|\band\b", teacher or ""):
        words = [w for w in normalize(piece).split() if w not in HONORIFICS]
        if words:
            parts.append(" ".join(words))
    return parts


def teacher_agreement(sheet_teacher: str, known_teacher: str) -> str:
    """'agree', 'conflict' or 'unknown'."""
    a, b = teacher_parts(sheet_teacher), teacher_parts(known_teacher)
    if not a or not b:
        return "unknown"
    best = max(_ratio(x, y) for x in a for y in b)
    if best >= 0.8:
        return "agree"
    return "conflict" if best < 0.5 else "unknown"


@dataclass(frozen=True)
class Candidate:
    student: ck.Student
    score: float
    reason: str


@dataclass(frozen=True)
class Match:
    status: str
    candidates: tuple[Candidate, ...]   # best first; empty for status none

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _same_name(c: "Candidate") -> bool:
    return c.reason.startswith(("same name", "same words"))


def _score_pool(name, teacher, students):
    out = []
    for s in students:
        score, why = name_score(name, s.name, s.nickname_list)
        if score < PLAUSIBLE_SCORE - 0.1:
            continue
        agreement = teacher_agreement(teacher, s.teacher)
        if agreement == "agree":
            score, why = min(1.0, score + 0.10), why + ", same teacher"
        elif agreement == "conflict":
            score, why = max(0.0, score - 0.15), why + ", different teacher"
        if score >= PLAUSIBLE_SCORE:
            out.append(Candidate(s, round(score, 3), why))
    # a student whose whole name matches always outranks one that is merely similar ("Ida" / "Mia")
    return sorted(out, key=lambda c: (not _same_name(c), -c.score, c.student.name))


def match(name: str, teacher: str, cp: ck.Checkpoint) -> Match:
    """Best students for a sheet. Students who have left are never offered."""
    pool = [s for s in cp.students.values() if s.status == ck.ACTIVE]
    cands = _score_pool(name, teacher, pool)
    if cands:
        same = [c for c in cands if _same_name(c)]
        if len(same) == 1:
            # the whole name matches one student; a clashing teacher still needs a person's OK
            return Match(LIKELY if "different teacher" in same[0].reason else EXACT, tuple(cands))
        if len(same) > 1:     # genuinely identical names: only the teacher can tell them apart
            agreeing = [c for c in same if "same teacher" in c.reason]
            return Match(LIKELY if len(agreeing) == 1 else AMBIGUOUS, tuple(cands))
        top = cands[0]
        runner_up = cands[1].score if len(cands) > 1 else 0.0
        clear = top.score - runner_up >= CLEAR_MARGIN
        return Match(LIKELY if top.score >= LIKELY_SCORE and clear else AMBIGUOUS, tuple(cands))
    return Match(NONE, ())
