"""Data cleaning pipeline.

Free of any web-framework imports so it can be unit-tested and benchmarked on
its own.

The pipeline does not hard-code the assessment file's column names. It works
against a *mapping* from roles (name, gender, grade, subjects, total) to
whichever columns a given file happens to have. `detect_mapping` guesses that
mapping from headers and data types; the UI lets an admin correct the guess
before committing. Only one thing is truly required: at least one numeric
column to score on.

Stage order (documented in README.md):
    resolve mapping -> name format -> name typos -> gender -> grade
    -> marks -> impute -> recompute Total -> quarantine -> dedupe
"""

from __future__ import annotations

import io
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

MAX_SUBJECTS = 3
SUBJECT_SLOTS = [f"subject_{i}" for i in range(1, MAX_SUBJECTS + 1)]

NAME_ALIASES = {"name", "studentname", "fullname", "student", "candidate", "candidatename"}
GENDER_ALIASES = {"gender", "sex"}
GRADE_ALIASES = {"grade", "class", "gradelevel", "std", "standard", "year"}
TOTAL_ALIASES = {"total", "totalmarks", "aggregate", "sum", "overall"}
SUBJECT_ALIASES = {
    "math", "maths", "mathematics", "marksmath",
    "science", "sci", "marksscience", "physics", "chemistry", "biology",
    "english", "eng", "marksenglish", "language",
}

GENDER_MAP = {
    "m": "Male", "male": "Male", "man": "Male", "boy": "Male",
    "f": "Female", "female": "Female", "woman": "Female", "girl": "Female",
}

NULL_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-", "--", "?"}

MAX_MARK = 100
MIN_MARK = 0
MAX_TYPO_DISTANCE = 2


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

@dataclass
class CleaningReport:
    rows_in: int = 0
    rows_out: int = 0
    duplicates_removed: int = 0
    quarantined: int = 0
    duration_ms: float = 0.0
    mapping: dict[str, Any] = field(default_factory=dict)
    subject_labels: list[str] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)

    def log(self, category: str, row_ref: Any, before: Any, after: Any, detail: str = "") -> None:
        self.counts[category] += 1
        self.entries.append({
            "category": category,
            "row_ref": str(row_ref),
            "before": "" if before is None else str(before),
            "after": "" if after is None else str(after),
            "detail": detail,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "duplicates_removed": self.duplicates_removed,
            "quarantined": self.quarantined,
            "duration_ms": round(self.duration_ms, 2),
            "mapping": self.mapping,
            "subject_labels": self.subject_labels,
            "counts": dict(self.counts),
            "entries": self.entries,
        }


class SchemaError(ValueError):
    """The file cannot be scored: no usable numeric column was identified."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def levenshtein(a: str, b: str, max_dist: int = MAX_TYPO_DISTANCE) -> int:
    """Edit distance with early exit. Returns max_dist + 1 once the bound is exceeded."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > max_dist:
            return max_dist + 1
        previous = current
    return previous[-1]


def _is_null(value: Any) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return True
    return str(value).strip().lower() in NULL_TOKENS


def parse_number(value: Any) -> int | None:
    """Pull an integer out of a messy cell: '28 marks' -> 28, '82.0' -> 82, '' -> None."""
    if _is_null(value):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    return int(round(float(match.group())))


def normalise_name(value: Any) -> str | None:
    """Strip quoting and casing noise: '\"Aarav\"' / \"Navya'\" / 'ROHAN' -> title case."""
    if _is_null(value):
        return None
    text = str(value).strip().strip("\"'").strip()
    text = re.sub(r"[^A-Za-z\s.\-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title() if text else None


def _slug(column: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(column).lower())


def _numeric_score(series: pd.Series) -> float:
    """Fraction of non-empty cells that parse to a number inside the mark range."""
    sample = series.dropna().astype(str).head(200)
    if sample.empty:
        return 0.0
    values = [parse_number(v) for v in sample]
    usable = [v for v in values if v is not None and MIN_MARK <= v <= MAX_MARK]
    return len(usable) / len(sample)


def _text_score(series: pd.Series) -> float:
    sample = series.dropna().astype(str).head(200)
    if sample.empty:
        return 0.0
    return sum(1 for v in sample if re.search(r"[A-Za-z]{2,}", v)) / len(sample)


# --------------------------------------------------------------------------
# mapping
# --------------------------------------------------------------------------

def detect_mapping(df: pd.DataFrame) -> dict[str, Any]:
    """Guess which columns play which role.

    Header aliases win. Where they are absent the guess falls back to the data:
    a name is the textual column with the most distinct values, subjects are
    columns that parse cleanly as numbers inside the mark range.
    """
    columns = list(df.columns)
    slugs = {c: _slug(c) for c in columns}
    taken: set[Any] = set()

    def claim(column):
        taken.add(column)
        return column

    name = next((c for c in columns if slugs[c] in NAME_ALIASES), None)
    gender = next((c for c in columns if slugs[c] in GENDER_ALIASES), None)
    grade = next((c for c in columns if slugs[c] in GRADE_ALIASES), None)
    total = next((c for c in columns if slugs[c] in TOTAL_ALIASES), None)
    for c in (name, gender, grade, total):
        if c is not None:
            claim(c)

    subjects = [c for c in columns if slugs[c] in SUBJECT_ALIASES and c not in taken]
    for c in subjects:
        claim(c)

    # Fall back to the data for anything still unidentified. A plausible score
    # column parses as a number in range and actually varies — a binary flag or
    # a four-level ordinal is numeric but is not a mark. Among equally
    # plausible candidates the rightmost win, since score and outcome columns
    # conventionally sit at the end of a record.
    if len(subjects) < MAX_SUBJECTS:
        candidates = [
            (position, column) for position, column in enumerate(columns)
            if column not in taken
            and df[column].nunique() >= 5
            and _numeric_score(df[column]) >= 0.9
        ]
        chosen = sorted(candidates, key=lambda pair: -pair[0])[:MAX_SUBJECTS - len(subjects)]
        for _, column in sorted(chosen):
            subjects.append(claim(column))

    if name is None:
        # A name identifies a person, so it should be mostly distinct. A
        # two-value column like "guardian" is text but is not a name.
        floor = max(3, len(df) * 0.5)
        text_candidates = [
            (c, df[c].nunique()) for c in columns
            if c not in taken and _text_score(df[c]) > 0.8 and df[c].nunique() >= floor
        ]
        text_candidates.sort(key=lambda pair: -pair[1])
        if text_candidates:
            name = claim(text_candidates[0][0])

    return {
        "name": name,
        "gender": gender,
        "grade": grade,
        "total": total,
        "subjects": subjects[:MAX_SUBJECTS],
    }


def describe_columns(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-column summary used by the mapping UI."""
    out = []
    for column in df.columns:
        series = df[column]
        out.append({
            "column": str(column),
            "distinct": int(series.nunique()),
            "numeric_fraction": round(_numeric_score(series), 2),
            "samples": [str(v) for v in series.dropna().astype(str).head(3).tolist()],
        })
    return out


def validate_mapping(mapping: dict[str, Any], columns: list[Any]) -> dict[str, Any]:
    known = {str(c) for c in columns}

    def pick(role):
        value = mapping.get(role)
        return value if value is not None and str(value) in known else None

    subjects = [s for s in (mapping.get("subjects") or []) if str(s) in known][:MAX_SUBJECTS]
    resolved = {
        "name": pick("name"),
        "gender": pick("gender"),
        "grade": pick("grade"),
        "total": pick("total"),
        "subjects": subjects,
    }
    if not resolved["subjects"]:
        raise SchemaError(
            "No score columns selected. Choose at least one numeric column to score students on."
        )
    return resolved


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------

def _fix_name_typos(df: pd.DataFrame, report: CleaningReport, log_details: bool) -> pd.DataFrame:
    """Correct singleton names to the nearest frequent name within MAX_TYPO_DISTANCE.

    The canonical vocabulary is derived from the file itself: a name occurring
    twice or more is treated as correct. This means the same value can be a
    typo in one cohort and a legitimate name in another, which is the intended
    behaviour — it is a frequency judgement, not a fixed roster.
    """
    counts = df["Name"].dropna().value_counts()
    canonical = counts[counts >= 2]
    singletons = counts[counts == 1].index.tolist()
    if canonical.empty or not singletons:
        return df

    corrections: dict[str, str] = {}
    for candidate in singletons:
        best_name, best_dist, best_freq = None, MAX_TYPO_DISTANCE + 1, 0
        for target, freq in canonical.items():
            dist = levenshtein(str(candidate).lower(), str(target).lower())
            if dist < best_dist or (dist == best_dist and freq > best_freq):
                best_name, best_dist, best_freq = target, dist, freq
        if best_name is not None and best_dist <= MAX_TYPO_DISTANCE:
            corrections[candidate] = best_name

    if not corrections:
        return df
    for wrong, right in corrections.items():
        if log_details:
            for idx in df.index[df["Name"] == wrong]:
                report.log("name_typo", df.at[idx, "source_row"], wrong, right,
                           f"nearest frequent name, distance <= {MAX_TYPO_DISTANCE}")
        else:
            report.counts["name_typo"] += 1
    df["Name"] = df["Name"].replace(corrections)
    return df


def _impute_marks(df: pd.DataFrame, slots: list[str], report: CleaningReport,
                  log_details: bool, labels: dict[str, str]) -> pd.DataFrame:
    """Fill a single missing mark with the grade-level median.

    Rows missing more than one are quarantined instead: imputing enough of a
    score to change a shortlisting outcome would be fabricating results.
    """
    if len(slots) < 2:
        return df
    missing = df[slots].isna().sum(axis=1)
    targets = df.index[missing == 1]
    if len(targets) == 0:
        return df

    by_grade = df.groupby("Grade")[slots].median() if df["Grade"].notna().any() else None
    overall = df[slots].median()

    for idx in targets:
        for slot in slots:
            if pd.notna(df.at[idx, slot]):
                continue
            grade = df.at[idx, "Grade"]
            value = None
            if by_grade is not None and pd.notna(grade) and grade in by_grade.index:
                value = by_grade.at[grade, slot]
            if value is None or pd.isna(value):
                value = overall[slot]
            if pd.isna(value):
                continue
            value = int(round(value))
            df.at[idx, slot] = value
            df.at[idx, "imputed"] = True
            if log_details:
                report.log("marks_imputed", df.at[idx, "source_row"], "missing", value,
                           f"{labels[slot]}: median for grade {grade}")
            else:
                report.counts["marks_imputed"] += 1
    return df


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def clean_dataframe(raw: pd.DataFrame, mapping: dict[str, Any] | None = None,
                    log_details: bool = True) -> tuple[pd.DataFrame, CleaningReport]:
    started = time.perf_counter()
    report = CleaningReport(rows_in=len(raw))

    resolved = validate_mapping(mapping or detect_mapping(raw), list(raw.columns))
    report.mapping = {k: (list(v) if isinstance(v, list) else v) for k, v in resolved.items()}

    subject_columns = resolved["subjects"]
    slots = SUBJECT_SLOTS[:len(subject_columns)]
    labels = {slot: str(col) for slot, col in zip(slots, subject_columns)}
    report.subject_labels = [labels[s] for s in slots]

    df = pd.DataFrame(index=range(len(raw)))
    df["source_row"] = df.index + 2
    df["Name"] = raw[resolved["name"]].values if resolved["name"] else None
    df["Gender"] = raw[resolved["gender"]].values if resolved["gender"] else None
    df["Grade"] = raw[resolved["grade"]].values if resolved["grade"] else None
    for slot, column in zip(slots, subject_columns):
        df[slot] = raw[column].values
    stated_total = raw[resolved["total"]].map(parse_number).values if resolved["total"] else None

    df["imputed"] = False
    df["quarantined"] = False
    df["quarantine_reason"] = ""

    # --- Name -------------------------------------------------------------
    if resolved["name"] is None:
        df["Name"] = [f"Student {i:04d}" for i in range(1, len(df) + 1)]
        report.log("name_generated", "-", "no name column", "Student 0001…",
                   "the file has no name column; placeholder identifiers were generated")
    else:
        original = df["Name"].copy()
        df["Name"] = df["Name"].map(normalise_name)
        changed = df.index[(original.astype(str) != df["Name"].astype(str)) & df["Name"].notna()]
        if log_details:
            for idx in changed:
                report.log("name_format", df.at[idx, "source_row"], original[idx], df.at[idx, "Name"])
        else:
            report.counts["name_format"] += len(changed)
        df = _fix_name_typos(df, report, log_details)

    # --- Gender -----------------------------------------------------------
    if resolved["gender"] is None:
        df["Gender"] = "Unknown"
    else:
        original = df["Gender"].copy()
        df["Gender"] = df["Gender"].map(
            lambda v: "Unknown" if _is_null(v) else GENDER_MAP.get(str(v).strip().lower(), "Unknown")
        )
        if log_details:
            for idx in df.index:
                before, after = original[idx], df.at[idx, "Gender"]
                if str(before) == after:
                    continue
                if after == "Unknown":
                    report.log("gender_ambiguous", df.at[idx, "source_row"], before, after,
                               "no documented mapping for this value")
                else:
                    report.log("gender_normalised", df.at[idx, "source_row"], before, after)
        else:
            report.counts["gender_normalised"] += int((original.astype(str) != df["Gender"]).sum())

    # --- Grade ------------------------------------------------------------
    if resolved["grade"] is None:
        df["Grade"] = None
    else:
        original = df["Grade"].copy()
        df["Grade"] = df["Grade"].map(parse_number)
        for idx in df.index[df["Grade"].notna() & ~df["Grade"].between(1, 12)]:
            report.log("grade_invalid", df.at[idx, "source_row"], original[idx], None, "outside 1-12")
            df.at[idx, "Grade"] = None
        if log_details:
            for idx in df.index:
                if pd.notna(df.at[idx, "Grade"]) and str(original[idx]).strip() != str(df.at[idx, "Grade"]):
                    report.log("grade_parsed", df.at[idx, "source_row"], original[idx], int(df.at[idx, "Grade"]))

    # --- Marks ------------------------------------------------------------
    for slot in slots:
        original = df[slot].copy()
        df[slot] = df[slot].map(parse_number)
        if log_details:
            dirty = df.index[original.astype(str).str.contains(r"[^0-9.\s]", na=False) & df[slot].notna()]
            for idx in dirty:
                report.log("marks_parsed", df.at[idx, "source_row"], original[idx], int(df.at[idx, slot]),
                           f"{labels[slot]}: stripped non-numeric text")
        for idx in df.index[df[slot].notna() & ~df[slot].between(MIN_MARK, MAX_MARK)]:
            report.log("marks_out_of_range", df.at[idx, "source_row"], original[idx], None,
                       f"{labels[slot]}: outside {MIN_MARK}-{MAX_MARK}")
            df.at[idx, slot] = None

    df = _impute_marks(df, slots, report, log_details, labels)

    # --- Total: always recomputed -----------------------------------------
    df["Total"] = df[slots].sum(axis=1, min_count=1)
    if stated_total is not None:
        stated = pd.Series(stated_total, index=df.index)
        if log_details:
            for idx in df.index:
                if pd.notna(stated[idx]) and pd.notna(df.at[idx, "Total"]) and int(stated[idx]) != int(df.at[idx, "Total"]):
                    report.log("total_mismatch", df.at[idx, "source_row"], stated[idx], int(df.at[idx, "Total"]),
                               "recomputed from the score columns")
        else:
            report.counts["total_mismatch"] += int(
                (stated.notna() & df["Total"].notna() & (stated != df["Total"])).sum())

    # --- Quarantine -------------------------------------------------------
    missing = df[slots].isna().sum(axis=1)
    for idx in df.index[(missing >= 1) | df["Name"].isna()]:
        reasons = []
        if pd.isna(df.at[idx, "Name"]):
            reasons.append("missing name")
        if missing[idx]:
            reasons.append(f"{int(missing[idx])} unusable score(s)")
        df.at[idx, "quarantined"] = True
        df.at[idx, "quarantine_reason"] = "; ".join(reasons)
        report.log("quarantined", df.at[idx, "source_row"], "-", "-", df.at[idx, "quarantine_reason"])
    df.loc[df["quarantined"], "Total"] = df.loc[df["quarantined"], slots].sum(axis=1, min_count=0)

    # --- Deduplicate ------------------------------------------------------
    # The key includes every score. Common first names repeat across distinct
    # students, so name alone is not a valid identity.
    dupes = df.duplicated(subset=["Name", "Grade"] + slots, keep="first")
    if dupes.any():
        for idx in df.index[dupes]:
            report.log("duplicate_removed", df.at[idx, "source_row"],
                       f"{df.at[idx, 'Name']} / grade {df.at[idx, 'Grade']}", "dropped",
                       "identical name, grade and scores")
        report.duplicates_removed = int(dupes.sum())
        df = df[~dupes].reset_index(drop=True)

    for slot in SUBJECT_SLOTS:
        if slot not in df.columns:
            df[slot] = pd.NA
        df[slot] = df[slot].astype("Int64")
    df["Grade"] = df["Grade"].astype("Int64")
    df["Total"] = df["Total"].fillna(0).astype(int)

    report.rows_out = len(df)
    report.quarantined = int(df["quarantined"].sum())
    report.duration_ms = (time.perf_counter() - started) * 1000
    return df, report


def read_upload(content: bytes, filename: str) -> pd.DataFrame:
    """Read an upload into a raw string DataFrame, sniffing format and encoding."""
    if filename.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(content), dtype=str)

    text = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise SchemaError("Could not decode the file as UTF-8 or Latin-1.")

    sample = text[:4096]
    delimiter = max([",", ";", "\t", "|"], key=sample.count)
    frame = pd.read_csv(io.StringIO(text), sep=delimiter, dtype=str, skip_blank_lines=True)
    if frame.empty:
        raise SchemaError("The file has a header but no data rows.")
    return frame
