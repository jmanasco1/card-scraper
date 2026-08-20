"""Group listings into card+grade buckets.

Bucket key: set + card_number + player + grader + grade, with the year held
separately so "1987 Fleer" and "1987 Fleer Basketball" collapse together.

The key is built from structured aspects when the listing has been enriched,
and from the title otherwise. Every record carries match_method so the share
of the dataset resting on the parser is measurable rather than assumed.
"""
import hashlib
import re

GRADERS = {
    "PSA": ["professional sports authenticator", "psa"],
    "BGS": ["beckett grading services", "beckett grading", "beckett", "bgs"],
    "BVG": ["beckett vintage grading", "bvg"],
    "SGC": ["sportscard guaranty", "sgc"],
    "CGC": ["certified guaranty company", "cgc"],
    "CSG": ["certified sports guaranty", "csg"],
    "HGA": ["hybrid grading approach", "hga"],
    "ISA": ["international sports authentication", "isa"],
    "TAG": ["technical authentication grading", "tag"],
    "GMA": ["gma grading", "gma"],
    "BCCG": ["beckett collectors club grading", "bccg"],
}
# Longest first so "beckett grading services" wins over bare "beckett".
_GRADER_PATTERNS = sorted(
    ((alias, code) for code, aliases in GRADERS.items() for alias in aliases),
    key=lambda pair: -len(pair[0]),
)

# Qualifiers are recorded but deliberately excluded from the bucket key: an
# OC or MK copy is the same card at the same grade for comparison purposes.
QUALIFIERS = ["OC", "ST", "MK", "PD", "OF", "MC"]
QUALIFIER_RE = re.compile(r"\((%s)\)|\b(%s)\b(?!\w)" % ("|".join(QUALIFIERS),
                                                        "|".join(QUALIFIERS)))

GRADE_WORDS = (r"GEM\s*-?\s*MT|GEM\s*MINT|GEM|MINT|NM\s*-?\s*MT|NM|EX\s*-?\s*MT"
               r"|EX|VG\s*-?\s*EX|VG|PR|FR|GD|AUTH(?:ENTIC)?")
_GRADER_TOKENS = "|".join(sorted(GRADERS, key=len, reverse=True))
TITLE_GRADE_RE = re.compile(
    rf"\b({_GRADER_TOKENS}|BECKETT)\b\s*\.?\s*(?:{GRADE_WORDS})?\s*\.?\s*"
    r"(10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5\.5|5|4\.5|4|3\.5|3|2\.5|2|1\.5|1)\b",
    re.I)
YEAR_RE = re.compile(r"\b(19[3-9]\d|20[0-4]\d)(?:\s*[-/]\s*\d{2,4})?\b")
CARD_NO_RE = re.compile(r"#\s*([A-Za-z]{0,4}-?\d+[A-Za-z]?)\b")

SPORT_WORDS = {"basketball", "baseball", "football", "hockey", "soccer",
               "golf", "boxing", "racing", "wrestling", "tennis"}
NOISE_WORDS = {
    "card", "cards", "rookie", "rc", "the", "and", "psa", "bgs", "sgc", "cgc",
    "csg", "hga", "bvg", "beckett", "gem", "mint", "mt", "nm", "graded",
    "grade", "lot", "rare", "hot", "wow", "look", "sharp", "nice", "case",
    "fresh", "pop", "low", "invest", "investment", "sp", "ssp", "hof",
}


def normalize_grader(value):
    """Map any grader spelling to a short code. Beckett -> BGS."""
    if not value:
        return None
    low = value.lower()
    for alias, code in _GRADER_PATTERNS:
        if alias in low:
            return code
    return None


def normalize_grade(value):
    """Numeric grade only. 'PSA 10 GEM MT' and 'PSA 10' are one bucket."""
    if value is None:
        return None
    text = str(value)
    match = re.search(r"(\d+(?:\.5)?)", text)
    if not match:
        return None
    number = float(match.group(1))
    if not 1 <= number <= 10:
        return None
    # 9.5 must stay distinct from 9, so keep the half but drop a bare ".0".
    return str(int(number)) if number == int(number) else str(number)


def extract_qualifiers(value):
    if not value:
        return []
    found = {g for pair in QUALIFIER_RE.findall(str(value)) for g in pair if g}
    return sorted(found)


def split_year(value):
    """Pull a leading year out of a set name: '1984 Donruss' -> (1984, donruss)."""
    if not value:
        return None, None
    match = YEAR_RE.search(value)
    year = match.group(1) if match else None
    rest = YEAR_RE.sub(" ", value, count=1) if match else value
    return year, normalize_set(rest)


def normalize_set(value):
    """Lowercase, drop punctuation and the sport suffix so '1987 Fleer' and
    '1987 Fleer Basketball' collapse to the same set."""
    if not value:
        return None
    tokens = [t for t in re.split(r"[^a-z0-9]+", value.lower()) if t]
    tokens = [t for t in tokens if t not in SPORT_WORDS]
    return " ".join(tokens) or None


def normalize_card_number(value):
    """Strip '#' and leading zeros: '#007' -> '7', 'CPA-SK' -> 'cpa-sk'."""
    if value is None:
        return None
    text = str(value).strip().lstrip("#").strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text).lower()
    # Leading zeros only on the numeric run, so 'us007' -> 'us7'.
    text = re.sub(r"(?<!\d)0+(\d)", r"\1", text)
    return text or None


def normalize_player(value):
    """Multi-value aspects arrive as 'Leroy Sane, Leroy Sané' — take the first
    and strip accents so spellings collapse."""
    if not value:
        return None
    first = re.split(r"[|,/]", str(value))[0]
    import unicodedata
    folded = unicodedata.normalize("NFKD", first)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    tokens = [t for t in re.split(r"[^a-z0-9]+", folded.lower()) if t]
    tokens = [t for t in tokens if t not in {"jr", "sr", "ii", "iii"}]
    return " ".join(tokens) or None


def parse_title(title):
    """Best-effort structured fields from a free-text title."""
    text = title or ""
    grader = grade = None
    match = TITLE_GRADE_RE.search(text)
    if match:
        grader = normalize_grader(match.group(1))
        grade = normalize_grade(match.group(2))

    year_match = YEAR_RE.search(text)
    year = year_match.group(1) if year_match else None
    card_match = CARD_NO_RE.search(text)
    card_number = normalize_card_number(card_match.group(1)) if card_match else None

    # Whatever is left after removing the parts we understood is a bag of
    # set and player words. Order varies wildly between sellers, so it is
    # kept as a sorted signature rather than a phrase.
    residue = text
    for pattern in (TITLE_GRADE_RE, YEAR_RE, CARD_NO_RE):
        residue = pattern.sub(" ", residue)
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", residue.lower()) if t]
    tokens = [t for t in tokens
              if t not in NOISE_WORDS and t not in SPORT_WORDS
              and not t.isdigit() and len(t) > 2]
    return {"grader": grader, "grade": grade, "year": year,
            "card_number": card_number, "tokens": sorted(set(tokens))}


def _signature(tokens, limit=4):
    """Stable short hash of the most distinctive title tokens."""
    if not tokens:
        return None
    picked = sorted(tokens, key=lambda t: (-len(t), t))[:limit]
    joined = "|".join(sorted(picked))
    return hashlib.sha1(joined.encode()).hexdigest()[:10]


def bucket_key(listing, aspect=None):
    """Return (key, method, reason). key is None when the listing cannot be
    bucketed, and reason says which field was missing."""
    # Slice-collected listings had set, grader and grade pinned by the query
    # itself, so those three are known exactly with no enrichment call. Within
    # a fixed set the card number determines the player, so player is stored
    # as metadata rather than required in the key — requiring a title-parsed
    # player here would split the same card into several buckets.
    if listing.get("sliceName") and listing.get("set_name"):
        year, set_core = split_year(listing["set_name"])
        grader = normalize_grader(listing.get("grader"))
        grade = normalize_grade(listing.get("grade"))
        card_number = normalize_card_number(listing.get("card_number"))
        if not card_number:
            parsed = parse_title(listing.get("title"))
            card_number = parsed["card_number"]
        if not card_number:
            return None, "slice", "missing:card_number"
        return (f"{year or '____'}|{set_core}|{card_number}"
                f"|{grader}|{grade}"), "slice", None

    if aspect:
        year, set_core = split_year(aspect.get("set_name"))
        player = normalize_player(aspect.get("player"))
        card_number = normalize_card_number(aspect.get("card_number"))
        grader = normalize_grader(aspect.get("grader"))
        grade = normalize_grade(aspect.get("grade"))
        # Aspects frequently omit grader/grade even when the title states it.
        if not (grader and grade):
            parsed = parse_title(listing.get("title"))
            grader = grader or parsed["grader"]
            grade = grade or parsed["grade"]
        missing = [name for name, value in
                   (("set", set_core), ("card_number", card_number),
                    ("player", player), ("grader", grader), ("grade", grade))
                   if not value]
        if missing:
            return None, "aspects", "missing:" + ",".join(missing)
        return (f"{year or '____'}|{set_core}|{card_number}|{player}"
                f"|{grader}|{grade}"), "aspects", None

    parsed = parse_title(listing.get("title"))
    missing = [name for name, value in
               (("card_number", parsed["card_number"]),
                ("grader", parsed["grader"]), ("grade", parsed["grade"]),
                ("year", parsed["year"]))
               if not value]
    signature = _signature(parsed["tokens"])
    if not signature:
        missing.append("descriptor")
    if missing:
        return None, "title", "missing:" + ",".join(missing)
    return (f"{parsed['year']}|~{signature}|{parsed['card_number']}|~"
            f"|{parsed['grader']}|{parsed['grade']}"), "title", None
