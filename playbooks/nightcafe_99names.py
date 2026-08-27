#!/usr/bin/env python3
"""Daily 99 Names prompt and NightCafe Media Store workflow."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import connect_database
from core.processes import popen_process_group, terminate_process_group
from core.setup_database import initialize_database
from core.md_subject_parser import SubjectDocument, load_subject_document, select_next_subject

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_DB: Final = PROJECT_ROOT / "db/events.db"
AUTOMATION: Final = PROJECT_ROOT / "plugins/media/nightcafe_automation.py"
LOGGER = logging.getLogger("nightcafe_99names")

# Conventional devotional ordering; meanings are concise English glosses used for prompt context.
NAMES: Final[tuple[tuple[str, str, str], ...]] = (
    ("الرحمن", "Ar-Rahman", "The Most Compassionate"),
    ("الرحيم", "Ar-Rahim", "The Most Merciful"),
    ("الملك", "Al-Malik", "The King"),
    ("القدوس", "Al-Quddus", "The Most Holy"),
    ("السلام", "As-Salam", "The Source of Peace"),
    ("المؤمن", "Al-Mu'min", "The Giver of Safety"),
    ("المهيمن", "Al-Muhaymin", "The Guardian"),
    ("العزيز", "Al-Aziz", "The Almighty"),
    ("الجبار", "Al-Jabbar", "The Compeller"),
    ("المتكبر", "Al-Mutakabbir", "The Supremely Great"),
    ("الخالق", "Al-Khaliq", "The Creator"),
    ("البارئ", "Al-Bari", "The Originator"),
    ("المصور", "Al-Musawwir", "The Fashioner"),
    ("الغفار", "Al-Ghaffar", "The Constant Forgiver"),
    ("القهار", "Al-Qahhar", "The All-Subduer"),
    ("الوهاب", "Al-Wahhab", "The Bestower"),
    ("الرزاق", "Ar-Razzaq", "The Provider"),
    ("الفتاح", "Al-Fattah", "The Opener"),
    ("العليم", "Al-Alim", "The All-Knowing"),
    ("القابض", "Al-Qabid", "The Withholder"),
    ("الباسط", "Al-Basit", "The Extender"),
    ("الخافض", "Al-Khafid", "The Reducer"),
    ("الرافع", "Ar-Rafi", "The Exalter"),
    ("المعز", "Al-Mu'izz", "The Honourer"),
    ("المذل", "Al-Mudhill", "The Humbling One"),
    ("السميع", "As-Sami", "The All-Hearing"),
    ("البصير", "Al-Basir", "The All-Seeing"),
    ("الحكم", "Al-Hakam", "The Judge"),
    ("العدل", "Al-Adl", "The Utterly Just"),
    ("اللطيف", "Al-Latif", "The Most Gentle"),
    ("الخبير", "Al-Khabir", "The All-Aware"),
    ("الحليم", "Al-Halim", "The Most Forbearing"),
    ("العظيم", "Al-Azim", "The Magnificent"),
    ("الغفور", "Al-Ghafur", "The Great Forgiver"),
    ("الشكور", "Ash-Shakur", "The Most Appreciative"),
    ("العلي", "Al-Ali", "The Most High"),
    ("الكبير", "Al-Kabir", "The Most Great"),
    ("الحفيظ", "Al-Hafiz", "The Preserver"),
    ("المقيت", "Al-Muqit", "The Sustainer"),
    ("الحسيب", "Al-Hasib", "The Reckoner"),
    ("الجليل", "Al-Jalil", "The Majestic"),
    ("الكريم", "Al-Karim", "The Most Generous"),
    ("الرقيب", "Ar-Raqib", "The Watchful"),
    ("المجيب", "Al-Mujib", "The Responsive"),
    ("الواسع", "Al-Wasi", "The All-Encompassing"),
    ("الحكيم", "Al-Hakim", "The All-Wise"),
    ("الودود", "Al-Wadud", "The Most Loving"),
    ("المجيد", "Al-Majid", "The Glorious"),
    ("الباعث", "Al-Ba'ith", "The Resurrector"),
    ("الشهيد", "Ash-Shahid", "The Witness"),
    ("الحق", "Al-Haqq", "The Truth"),
    ("الوكيل", "Al-Wakil", "The Trustee"),
    ("القوي", "Al-Qawiyy", "The All-Strong"),
    ("المتين", "Al-Matin", "The Firm"),
    ("الولي", "Al-Waliyy", "The Protecting Friend"),
    ("الحميد", "Al-Hamid", "The Praiseworthy"),
    ("المحصي", "Al-Muhsi", "The Accounter"),
    ("المبدئ", "Al-Mubdi", "The Originator"),
    ("المعيد", "Al-Mu'id", "The Restorer"),
    ("المحيي", "Al-Muhyi", "The Giver of Life"),
    ("المميت", "Al-Mumit", "The Bringer of Death"),
    ("الحي", "Al-Hayy", "The Ever-Living"),
    ("القيوم", "Al-Qayyum", "The Self-Sustaining"),
    ("الواجد", "Al-Wajid", "The Perceiver"),
    ("الماجد", "Al-Maajid", "The Illustrious"),
    ("الواحد", "Al-Wahid", "The One"),
    ("الاحد", "Al-Ahad", "The Unique"),
    ("الصمد", "As-Samad", "The Eternal Refuge"),
    ("القادر", "Al-Qadir", "The All-Powerful"),
    ("المقتدر", "Al-Muqtadir", "The Determiner"),
    ("المقدم", "Al-Muqaddim", "The Expediter"),
    ("المؤخر", "Al-Mu'akhkhir", "The Delayer"),
    ("الأول", "Al-Awwal", "The First"),
    ("الآخر", "Al-Akhir", "The Last"),
    ("الظاهر", "Az-Zahir", "The Manifest"),
    ("الباطن", "Al-Batin", "The Hidden"),
    ("الوالي", "Al-Waali", "The Governor"),
    ("المتعالي", "Al-Muta'ali", "The Most Exalted"),
    ("البر", "Al-Barr", "The Source of Goodness"),
    ("التواب", "At-Tawwab", "The Accepter of Repentance"),
    ("المنتقم", "Al-Muntaqim", "The Just Avenger"),
    ("العفو", "Al-Afuww", "The Pardoner"),
    ("الرؤوف", "Ar-Ra'uf", "The Most Kind"),
    ("مالك الملك", "Malik-ul-Mulk", "Master of the Kingdom"),
    ("ذو الجلال والإكرام", "Dhul-Jalali wal-Ikram", "Lord of Majesty and Honour"),
    ("المقسط", "Al-Muqsit", "The Equitable"),
    ("الجامع", "Al-Jami", "The Gatherer"),
    ("الغني", "Al-Ghani", "The Self-Sufficient"),
    ("المغني", "Al-Mughni", "The Enricher"),
    ("المانع", "Al-Mani", "The Preventer"),
    ("الضار", "Ad-Darr", "The Distresser"),
    ("النافع", "An-Nafi", "The Benefactor"),
    ("النور", "An-Nur", "The Light"),
    ("الهادي", "Al-Hadi", "The Guide"),
    ("البديع", "Al-Badi", "The Incomparable Originator"),
    ("الباقي", "Al-Baqi", "The Everlasting"),
    ("الوارث", "Al-Warith", "The Inheritor"),
    ("الرشيد", "Ar-Rashid", "The Guide to the Right Path"),
    ("الصبور", "As-Sabur", "The Most Patient"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "vault/prompts/nightcafe_99names.md",
                        help="Markdown backlog/configuration (rules plus numbered subjects).")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--claim-daily", action="store_true")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--llm-timeout", type=int, default=120)
    return parser.parse_args()


def seed_names(db_path: Path) -> None:
    if len(NAMES) != 99:
        raise RuntimeError(f"Expected 99 names, found {len(NAMES)}")
    with connect_database(db_path) as db:
        db.executemany(
            """INSERT INTO nightcafe_names(sequence,arabic_name,transliteration,meaning)
               VALUES (?,?,?,?) ON CONFLICT(sequence) DO UPDATE SET
               arabic_name=excluded.arabic_name,transliteration=excluded.transliteration,meaning=excluded.meaning""",
            ((index, *entry) for index, entry in enumerate(NAMES, 1)),
        )
        db.commit()


def seed_names_from_document(db_path: Path, document: SubjectDocument) -> None:
    """Mirror a Markdown backlog into the legacy run table for Media Store lineage."""
    if len(document.subjects) > 99:
        raise ValueError("NightCafe automation supports at most 99 subjects")
    with connect_database(db_path) as db:
        db.executemany(
            """INSERT INTO nightcafe_names(sequence,arabic_name,transliteration,meaning)
               VALUES (?,?,?,?) ON CONFLICT(sequence) DO UPDATE SET
               arabic_name=excluded.arabic_name,transliteration=excluded.transliteration,meaning=excluded.meaning""",
            ((subject.sequence, subject.title, subject.title, subject.context or subject.title)
             for subject in document.subjects),
        )
        db.commit()


def select_daily_name(db_path: Path, run_date: str):
    with connect_database(db_path) as db:
        existing = db.execute(
            """SELECT n.* FROM nightcafe_daily_runs r JOIN nightcafe_names n
               ON n.sequence=r.name_sequence WHERE r.run_date=?""", (run_date,)
        ).fetchone()
        if existing:
            return existing
        last = db.execute("SELECT name_sequence FROM nightcafe_daily_runs ORDER BY run_date DESC,id DESC LIMIT 1").fetchone()
        sequence = (int(last[0]) % 99) + 1 if last else 1
        db.execute("INSERT INTO nightcafe_daily_runs(run_date,name_sequence) VALUES (?,?)", (run_date, sequence))
        db.commit()
        return db.execute("SELECT * FROM nightcafe_names WHERE sequence=?", (sequence,)).fetchone()


def fallback_prompt(name: str, meaning: str, constraints: str = "") -> str:
    return (
        f"A contemplative fine-art visual inspired by {name}, {meaning}; symbolic rather than figurative, "
        "luminous sacred geometry, intricate arabesque patterns, celestial atmosphere, deep indigo and warm gold, "
        "volumetric light, museum-quality composition, cinematic detail, respectful Islamic aesthetic, "
        "no depiction of Allah, no people, no faces, no readable text, no watermark, high resolution"
        + (f"; {constraints.replace(chr(10), '; ')}" if constraints else "")
    )


def generate_prompt(name: str, meaning: str, timeout: int, constraints: str = "") -> str:
    command = shlex.split(os.getenv("LOCAL_LLM_COMMAND", "ollama run llama3.1:8b"))
    instruction = (
        "Return only one image-generation prompt, at most 120 words. Create a respectful symbolic artwork "
        f"inspired by the Divine Name {name} ({meaning}). Include composition, palette, light, medium and negative "
        "constraints. Never depict Allah; avoid people, faces, readable text and watermarks.\n"
        f"Strict Markdown configuration rules:\n{constraints}"
    )
    process = None
    try:
        process = popen_process_group(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
        stdout, _ = process.communicate(instruction, timeout=timeout)
        if process.returncode == 0 and stdout.strip():
            return " ".join(stdout.split())[:1200]
    except (OSError, subprocess.TimeoutExpired):
        if process is not None:
            terminate_process_group(process)
    return fallback_prompt(name, meaning, constraints)


def run_automation(args: argparse.Namespace, row, prompt: str) -> dict[str, object]:
    command = [sys.executable, str(AUTOMATION), "--db", str(args.db), "--prompt", prompt,
               "--name", row["transliteration"], "--sequence", str(row["sequence"]),
               "--run-date", args.date]
    if args.live:
        command.append("--live")
    if args.claim_daily:
        command.append("--claim-daily")
    command.append("--headless" if args.headless else "--no-headless")
    completed = subprocess.run(command, text=True, capture_output=True, timeout=300, check=False)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode or not lines:
        raise RuntimeError(lines[-1] if lines else f"automation exited {completed.returncode}")
    return json.loads(lines[-1])


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    args.db = args.db.expanduser().resolve()
    initialize_database(args.db)
    document = load_subject_document(args.config.expanduser().resolve())
    generator_key = f"nightcafe:{document.path}"
    seed_names_from_document(args.db, document)
    with connect_database(args.db) as db:
        existing = db.execute("SELECT name_sequence FROM nightcafe_daily_runs WHERE run_date=?", (args.date,)).fetchone()
    if existing:
        subject = document.subjects[int(existing[0]) - 1]
    else:
        subject = select_next_subject(args.db, document, generator_key)
        with connect_database(args.db) as db:
            db.execute("INSERT INTO nightcafe_daily_runs(run_date,name_sequence) VALUES (?,?)", (args.date, subject.sequence))
            db.commit()
    with connect_database(args.db) as db:
        row = db.execute("SELECT * FROM nightcafe_names WHERE sequence=?", (subject.sequence,)).fetchone()
    prompt = generate_prompt(subject.title, subject.context or subject.title, args.llm_timeout, document.prompt_constraints())
    LOGGER.info("Selected %02d/%02d %s — %s", subject.sequence, len(document.subjects), subject.title, subject.context)
    try:
        result = run_automation(args, row, prompt)
        with connect_database(args.db) as db:
            db.execute(
                """UPDATE nightcafe_daily_runs SET prompt=?,status=?,asset_id=?,output_path=?,error_log=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE run_date=?""",
                (prompt, result["status"], result["asset_id"], result["output_path"], args.date),
            )
            db.commit()
        print(json.dumps({"date": args.date, "name": row["transliteration"], "prompt": prompt, **result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        with connect_database(args.db) as db:
            db.execute("UPDATE nightcafe_daily_runs SET prompt=?,status='FAILED',error_log=?,updated_at=CURRENT_TIMESTAMP WHERE run_date=?",
                       (prompt, str(exc)[:2000], args.date))
            db.commit()
        LOGGER.exception("Daily NightCafe workflow failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
