"""Sequential local multi-agent editorial workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class EditorialLoopError(RuntimeError):
    """Raised when an editorial role rejects or fails its assignment."""


@dataclass(frozen=True)
class EditorialResult:
    final_text: str
    writer_text: str
    factcheck_text: str
    roles: tuple[str, ...] = ("SCHRIJVER", "FACTCHECKER", "REDACTEUR")


LLMCaller = Callable[[str, str], str]


def run_editorial_loop(brief: str, vault_context: str, call_llm: LLMCaller) -> EditorialResult:
    """Run all roles and return content only after explicit factcheck approval."""
    writer_prompt = (
        "ROL: SCHRIJVER\nMaak een sterk Markdown-concept op basis van de briefing. "
        "Gebruik relevante vaultcontext, maar verzin geen feiten.\n\n"
        f"BRIEFING:\n{brief}\n\nVAULTCONTEXT:\n{vault_context or 'Geen relevante context gevonden.'}"
    )
    writer = call_llm("SCHRIJVER", writer_prompt).strip()
    if not writer:
        raise EditorialLoopError("Schrijver leverde geen tekst")

    fact_prompt = (
        "ROL: FACTCHECKER\nControleer het concept uitsluitend tegen de meegeleverde "
        "vaultcontext. Corrigeer niet-onderbouwde beweringen. Geef de gecontroleerde "
        "tekst terug en sluit exact af met FACTCHECK_STATUS: APPROVED wanneer publicatie "
        "verantwoord is; anders FACTCHECK_STATUS: REJECTED.\n\n"
        f"VAULTCONTEXT:\n{vault_context or 'Geen relevante context gevonden.'}\n\nCONCEPT:\n{writer}"
    )
    factcheck = call_llm("FACTCHECKER", fact_prompt).strip()
    if "FACTCHECK_STATUS: APPROVED" not in factcheck:
        raise EditorialLoopError("Factchecker gaf geen expliciet akkoord")
    checked_text = factcheck.replace("FACTCHECK_STATUS: APPROVED", "").strip()
    if not checked_text:
        raise EditorialLoopError("Factchecker leverde geen gecontroleerde tekst")

    editor_prompt = (
        "ROL: REDACTEUR\nOptimaliseer onderstaande gecontroleerde Markdown voor "
        "helderheid, ritme, structuur en consistente stijl. Voeg geen nieuwe feiten toe. "
        "Geef uitsluitend het definitieve Markdown-document terug.\n\n"
        f"GECONTROLEERD CONCEPT:\n{checked_text}"
    )
    final_text = call_llm("REDACTEUR", editor_prompt).strip()
    if not final_text:
        raise EditorialLoopError("Redacteur leverde geen eindversie")
    return EditorialResult(final_text, writer, factcheck)
