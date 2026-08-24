import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.cleaning import (  # noqa: E402
    SchemaError,
    clean_dataframe,
    describe_columns,
    detect_mapping,
    levenshtein,
    normalise_name,
    parse_number,
    read_upload,
)

FIXTURE = ROOT / "tests" / "fixtures" / "dirty.csv"
REAL = ROOT / "data" / "sample_raw.csv"


@pytest.fixture(scope="module")
def dirty():
    raw = pd.read_csv(FIXTURE, dtype=str)
    return clean_dataframe(raw)


@pytest.fixture(scope="module")
def real():
    raw = pd.read_csv(REAL, dtype=str)
    return clean_dataframe(raw)


# --- unit level -----------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("28 marks", 28), ("  47 ", 47), ("82.0", 82), ("1,00", 100),
    ("", None), ("N/A", None), (None, None), ("marks", None),
])
def test_parse_number(value, expected):
    assert parse_number(value) == expected


@pytest.mark.parametrize("value,expected", [
    ('"Aarav"', "Aarav"), ("Navya'", "Navya"), ("ROHAN", "Rohan"),
    ("  myra  ", "Myra"), ("", None), (None, None),
])
def test_normalise_name(value, expected):
    assert normalise_name(value) == expected


def test_levenshtein_early_exit():
    assert levenshtein("isha", "ishaan") == 2
    assert levenshtein("kabir", "kabir") == 0
    assert levenshtein("zara", "krishna") > 2


# --- pipeline behaviour on the dirty fixture ------------------------------

def test_duplicates_removed(dirty):
    df, report = dirty
    assert report.duplicates_removed == 3  # 2x Aarav, 1x Meera
    aarav = df[df.Name == "Aarav"]
    assert len(aarav) == 1


def test_typo_corrected_to_frequent_name(dirty):
    df, _ = dirty
    assert "Meraa" not in set(df.Name)
    assert (df.Name == "Meera").sum() >= 1


def test_total_always_recomputed(dirty):
    df, report = dirty
    live = df[~df.quarantined]
    assert (live.subject_1 + live.subject_2 + live.subject_3 == live.Total).all()
    assert report.counts["total_mismatch"] >= 2  # the 999 and the 1


def test_marks_suffix_stripped(dirty):
    df, _ = dirty
    assert df[df.Name == "Kabir"].subject_1.notna().all()


def test_single_missing_mark_is_imputed(dirty):
    df, _ = dirty
    zara = df[df.Name == "Zara"]
    assert zara.imputed.any()
    assert not zara.quarantined.all()


def test_two_missing_marks_are_quarantined(dirty):
    df, _ = dirty
    ishaan = df[df.Name == "Ishaan"]
    assert ishaan.quarantined.all()


def test_row_with_no_name_is_quarantined(dirty):
    df, _ = dirty
    assert df[df.Name.isna()].quarantined.all() if df.Name.isna().any() else True


# --- column mapping -------------------------------------------------------

UCI = pd.DataFrame({
    "school": ["GP"] * 8,
    "sex": ["F", "M", "F", "M", "F", "M", "F", "M"],
    "age": ["18", "17", "15", "15", "16", "16", "16", "17"],
    "guardian": ["mother", "father", "mother", "mother", "father", "mother", "mother", "mother"],
    "absences": ["6", "4", "10", "2", "4", "10", "0", "6"],
    "G1": ["5", "5", "7", "15", "6", "15", "12", "6"],
    "G2": ["6", "5", "8", "14", "10", "15", "12", "5"],
    "G3": ["6", "6", "10", "15", "10", "15", "11", "60"],
})


def test_detect_mapping_on_the_assessment_schema():
    raw = pd.read_csv(REAL, dtype=str)
    m = detect_mapping(raw)
    assert m["name"] == "Name" and m["gender"] == "Gender" and m["grade"] == "Grade"
    assert m["subjects"] == ["Math", "Science", "English"]
    assert m["total"] == "Total"


def test_detect_mapping_on_an_unrelated_schema():
    m = detect_mapping(UCI)
    assert m["gender"] == "sex"
    # 'guardian' has two distinct values, so it must not be taken for a name.
    assert m["name"] is None
    assert len(m["subjects"]) == 3


def test_unrelated_file_cleans_with_generated_names():
    df, report = clean_dataframe(UCI)
    assert len(df) == 8
    assert df.Name.str.startswith("Student").all()
    assert report.counts["name_generated"] == 1
    assert set(df.Gender) == {"Male", "Female"}


def test_explicit_mapping_overrides_detection():
    mapping = {"name": "guardian", "gender": "sex", "grade": None,
               "total": None, "subjects": ["G1", "G2", "G3"]}
    df, report = clean_dataframe(UCI, mapping=mapping)
    assert report.subject_labels == ["G1", "G2", "G3"]
    assert set(df.Name) <= {"Mother", "Father"}


def test_mapping_with_a_single_subject_column():
    df, report = clean_dataframe(UCI, mapping={"name": None, "gender": None, "grade": None,
                                               "total": None, "subjects": ["G1"]})
    assert report.subject_labels == ["G1"]
    assert (df.Total == df.subject_1).all()
    # With one score there is nothing to impute from, so no row is invented.
    assert report.counts.get("marks_imputed", 0) == 0


def test_mapping_with_no_subjects_is_rejected():
    with pytest.raises(SchemaError):
        clean_dataframe(UCI, mapping={"name": "guardian", "subjects": []})


def test_unknown_columns_in_a_mapping_are_ignored():
    df, report = clean_dataframe(
        UCI, mapping={"name": "does_not_exist", "subjects": ["G1", "nope"]})
    assert report.subject_labels == ["G1"]
    assert df.Name.str.startswith("Student").all()


def test_describe_columns_reports_shape():
    described = {d["column"]: d for d in describe_columns(UCI)}
    assert described["G1"]["numeric_fraction"] == 1.0
    assert described["school"]["distinct"] == 1
    assert len(described["sex"]["samples"]) == 3


def test_out_of_range_marks_rejected(dirty):
    df, report = dirty
    assert report.counts["marks_out_of_range"] == 2  # 150 and -5
    live = df[~df.quarantined]
    assert live.subject_1.between(0, 100).all()


def test_invalid_grade_nulled(dirty):
    _, report = dirty
    assert report.counts["grade_invalid"] == 1  # grade 13


def test_gender_normalised_to_three_values(dirty):
    df, _ = dirty
    assert set(df.Gender) <= {"Male", "Female", "Unknown"}


def test_numeric_gender_codes_are_unknown_not_guessed(dirty):
    _, report = dirty
    assert report.counts["gender_ambiguous"] >= 2  # the 0 and the 1


def test_quarantined_rows_are_retained_not_dropped(dirty):
    df, report = dirty
    assert report.quarantined > 0
    assert report.quarantined == int(df.quarantined.sum())


# --- pipeline behaviour on the real dataset -------------------------------

def test_real_dataset_shape(real):
    df, report = real
    assert report.rows_in == 99
    assert report.rows_out == 99          # no true duplicates in this file
    assert report.duplicates_removed == 0
    assert report.quarantined == 0


def test_real_dataset_totals_already_correct(real):
    _, report = real
    assert report.counts.get("total_mismatch", 0) == 0


def test_real_dataset_finds_the_planted_typo(real):
    df, report = real
    assert report.counts.get("name_typo", 0) == 1
    assert "Isha" not in set(df.Name)
    assert "Ishaan" in set(df.Name)


def test_real_dataset_name_roster_collapses(real):
    df, _ = real
    assert df.Name.nunique() == 19  # 20 raw distinct, minus the corrected typo


def test_real_dataset_marks_all_parsed(real):
    df, _ = real
    for column in ("subject_1", "subject_2", "subject_3"):
        assert df[column].notna().all()
        assert df[column].between(0, 100).all()


def test_real_dataset_ambiguous_gender_count(real):
    _, report = real
    assert report.counts.get("gender_ambiguous", 0) == 14  # seven 0s, seven 1s


# --- ingestion robustness -------------------------------------------------

def test_semicolon_delimiter_sniffed():
    content = b"Name;Gender;Grade;Math;Science;English\nAarav;M;9;50;60;70\n"
    df = read_upload(content, "x.csv")
    assert list(df.columns)[:2] == ["Name", "Gender"]


def test_header_aliases_accepted():
    raw = pd.DataFrame({
        "Student Name": ["Aarav"], "Sex": ["M"], "Class": ["9"],
        "Maths": ["50"], "Science": ["60"], "Eng": ["70"],
    })
    df, _ = clean_dataframe(raw)
    assert df.at[0, "Total"] == 180


def test_missing_column_raises_schema_error():
    raw = pd.DataFrame({"Name": ["Aarav"], "Gender": ["M"]})
    with pytest.raises(SchemaError):
        clean_dataframe(raw)


def test_bom_encoded_file_reads():
    content = "Name,Gender,Grade,Math,Science,English\nAarav,M,9,50,60,70\n".encode("utf-8-sig")
    df = read_upload(content, "x.csv")
    assert df.iloc[0]["Name"] == "Aarav"
