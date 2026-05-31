"""
planner/repair.py
=================
Implements the two WIP features for the Isabelle/HOL proof planner:

  1. fill_sorry  - calls the stepwise prover on each `sorry` hole.
  2. cegis_repair - CEGIS-style iterative proof repair loop:
       Generate -> Check -> Fill -> Repair (staged: local -> subproof -> whole)

Usage (standalone):
    python -m planner.repair "rev (rev xs) = xs" --timeout 120

Import in planner/cli.py:
    from planner.repair import cegis_repair, RepairConfig
"""

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports - resolved at call time so the module loads even when
# Isabelle / LLMs are not yet configured.
# ---------------------------------------------------------------------------

def _get_run_isabelle():
    """Return run_isabelle from the repo, or a stub that checks for sorry."""
    # Try known locations in the repo
    for mod, attr in [
        ("planner.isabelle_runner", "run_isabelle"),
        ("planner.planner",         "run_isabelle"),
        ("prover.isabelle_api",     "check_proof"),
    ]:
        try:
            import importlib
            m = importlib.import_module(mod)
            fn = getattr(m, attr, None)
            if fn is not None:
                return fn
        except Exception:
            pass
    # Stub: pass only when no sorry present (used for unit-testing)
    def _stub(script, goal="", **kw):
        ok = "sorry" not in script
        return ok, ("" if ok else "stub: contains sorry")
    return _stub


def _get_llm_complete():
    """Return llm_complete(prompt, model) -> str | None from the repo."""
    for mod, attr in [
        ("prover.llm",     "llm_complete"),
        ("planner.planner","llm_complete"),
    ]:
        try:
            import importlib
            m = importlib.import_module(mod)
            fn = getattr(m, attr, None)
            if fn is not None:
                return fn
        except Exception:
            pass
    # Stub - returns a sorry placeholder so the loop can continue
    def _stub(prompt, model=""):
        return "  by sorry"
    return _stub


def _get_stepwise_prover():
    """Return a prove() callable that uses the working prover CLI."""
    import subprocess
    import sys
    import re

    def _prove(goal, model="qwen2.5:3b", beam=3, max_depth=5, timeout=60):
        cmd = [
            sys.executable, "-m", "prover.cli",
            "--goal", goal,
            "--model", model or "qwen2.5:3b",
            "--beam", str(beam),
            "--max-depth", str(max_depth),
            "--timeout", str(int(timeout)),
            "--variants",
            "--sledge",
            "--no-reranker",
        ]

        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=float(timeout) + 30,
            )
        except subprocess.TimeoutExpired as e:
            raw = (e.stdout or "") + "\n" + (e.stderr or "")
            return {"success": False, "proof": None, "raw": raw, "timeout": True}

        raw = (completed.stdout or "") + "\n" + (completed.stderr or "")

        if "SUCCESS" not in raw:
            return {"success": False, "proof": None, "raw": raw}

        proof = "by simp"
        m = re.search(r'lemma\s+"[^"]+"\s*\n([^\n]+)', raw)
        if m:
            line = m.group(1).strip()
            if line.startswith("by ") or line.startswith("apply "):
                proof = line

        return {"success": True, "proof": proof, "raw": raw}

    return _prove


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

SORRY_RE = re.compile(r"\bsorry\b")


def _count_sorries(script: str) -> int:
    return len(SORRY_RE.findall(script))


def _first_error(error_output: str) -> tuple[int, str]:
    """
    Parse Isabelle error output and return (line_number, message).
    Returns (-1, "") when no line number is found.
    """
    for pat in (
        re.compile(r"line (\d+)[:\s]+(.*)", re.IGNORECASE),
        re.compile(r"at line (\d+)",        re.IGNORECASE),
    ):
        m = pat.search(error_output)
        if m:
            lineno = int(m.group(1))
            msg    = m.group(2).strip() if len(m.groups()) > 1 else ""
            return lineno, msg
    return -1, error_output[:300]


def _replace_first_sorry(script: str, replacement: str) -> str:
    """Replace the first sorry safely, including the common 'by sorry' case."""
    if re.search(r"\bby\s+sorry\b", script):
        return re.sub(r"\bby\s+sorry\b", replacement, script, count=1)
    return SORRY_RE.sub(replacement, script, count=1)


def _extract_sorry_context(script: str) -> str:
    """Return the lines immediately before the first sorry as context."""
    lines = script.splitlines()
    for i, line in enumerate(lines):
        if SORRY_RE.search(line):
            start = max(0, i - 20)
            return "\n".join(lines[start : i + 1])
    return script


# ---------------------------------------------------------------------------
# RepairConfig
# ---------------------------------------------------------------------------

@dataclass
class RepairConfig:
    """Tunable parameters for the CEGIS repair loop."""
    timeout: float          = 120.0   # overall wall-clock budget (seconds)
    local_attempts: int     = 3       # Stage-1 attempts per cycle
    subproof_attempts: int  = 2       # Stage-2 attempts per cycle
    whole_attempts: int     = 2       # Stage-3 attempts per cycle
    fill_attempts: int      = 3       # attempts per sorry in fill_sorry
    model: str              = "qwen2.5:3b"
    beam: int               = 3
    max_depth: int          = 8
    keep_candidates: int    = 3       # candidate pool size
 

# ---------------------------------------------------------------------------
# ProofState
# ---------------------------------------------------------------------------

@dataclass
class ProofState:
    """One candidate proof script and its verification status."""
    script: str
    verified: bool      = False
    sorry_count: int    = 0
    holes_filled: int   = 0

    def __post_init__(self):
        self.sorry_count = _count_sorries(self.script)


# ---------------------------------------------------------------------------
# 1. fill_sorry
# ---------------------------------------------------------------------------

def fill_sorry(
    proof_state: ProofState,
    config: RepairConfig,
    goal: str,
) -> ProofState:
    """
    Fill pass: for each sorry hole (top-down), call the stepwise prover on
    the extracted sub-goal context and replace the sorry with the result.

    If a hole cannot be filled, stop immediately (per the assignment spec).
    Returns a new ProofState (possibly with fewer sorries).
    """
    prover = _get_stepwise_prover()
    llm    = _get_llm_complete()
    script = proof_state.script
    filled = 0

    while _count_sorries(script) > 0:
        context     = _extract_sorry_context(script)
        replacement = None

        for attempt in range(config.fill_attempts):
            logger.debug("[Fill] attempt %d/%d", attempt + 1, config.fill_attempts)

            # Try the stepwise prover first
            if prover is not None:
                try:
                    result = prover(
                        goal=goal,
                        model=config.model,
                        beam=config.beam,
                        max_depth=config.max_depth,
                        timeout=30.0,
                    )
                    if result and result.get("success"):
                        replacement = result["proof"]
                        break
                except Exception as e:
                    logger.debug("[Fill] prover error: %s", e)

            # Fall back to LLM directly
            prompt = (
                f"Fill in this Isabelle/HOL proof hole (replace sorry):\n\n"
                f"{context}\n\n"
                "Write a short Isabelle proof fragment (e.g. `by auto`, "
                "`by (induction xs) auto`) that discharges this goal. "
                "Output ONLY the Isabelle tactic, no explanation."
            )
            raw = llm(prompt, model=config.model)
            if raw and "sorry" not in raw:
                replacement = raw.strip()
                break

        if replacement:
            script = _replace_first_sorry(script, replacement)
            filled += 1
            logger.info("[Fill] Filled a sorry with: %s", replacement[:60])
        else:
            logger.info("[Fill] Could not fill a sorry; stopping fill pass.")
            break   # per spec: stop if a hole cannot be filled

    new_state             = ProofState(script=script)
    new_state.holes_filled = proof_state.holes_filled + filled
    return new_state


# ---------------------------------------------------------------------------
# Repair stage helpers
# ---------------------------------------------------------------------------

def _local_repair(
    script: str, error_line: int, error_msg: str,
    config: RepairConfig, goal: str,
) -> Optional[str]:
    """Stage 1: regenerate the single have/show step nearest the error."""
    lines = script.splitlines()
    if not lines:
        return None

    idx = max(0, min(error_line - 1, len(lines) - 1))

    # Find enclosing have/show (search back up to 15 lines)
    step_start = idx
    for i in range(idx, max(-1, idx - 15), -1):
        if re.match(r"^\s*(have|show)\b", lines[i]):
            step_start = i
            break

    # Find end of that step
    step_end = step_start
    for i in range(step_start + 1, min(len(lines), step_start + 15)):
        if re.match(r"^\s*(have|show|qed|next|then|finally|ultimately|moreover)\b", lines[i]):
            step_end = i - 1
            break
    else:
        step_end = min(len(lines) - 1, step_start + 4)

    old_step = "\n".join(lines[step_start : step_end + 1])
    llm      = _get_llm_complete()
    prompt   = (
        f"The following Isabelle/HOL step is wrong:\n\n{old_step}\n\n"
        f"Isabelle error: {error_msg}\n"
        f"Overall goal: {goal}\n\n"
        "Rewrite ONLY this step correctly. "
        "You may use sorry as a placeholder if needed. "
        "Output only valid Isabelle syntax."
    )
    new_step = llm(prompt, model=config.model)
    if not new_step:
        return None
    repaired = lines[:step_start] + new_step.strip().splitlines() + lines[step_end + 1:]
    return "\n".join(repaired)


def _subproof_repair(
    script: str, error_line: int, error_msg: str,
    config: RepairConfig, goal: str,
) -> Optional[str]:
    """Stage 2: regenerate the enclosing proof...qed block."""
    lines = script.splitlines()
    if not lines:
        return None

    idx = max(0, min(error_line - 1, len(lines) - 1))

    # Walk back to the nearest `proof`
    block_start = idx
    for i in range(idx, max(-1, idx - 60), -1):
        if re.match(r"^\s*proof\b", lines[i]):
            block_start = i
            break

    # Walk forward to matching `qed` (track nesting depth)
    depth     = 0
    block_end = min(len(lines) - 1, block_start + 10)
    for i in range(block_start, min(len(lines), block_start + 250)):
        if re.match(r"^\s*proof\b", lines[i]):
            depth += 1
        if re.match(r"^\s*qed\b",   lines[i]):
            depth -= 1
            if depth <= 0:
                block_end = i
                break

    old_block = "\n".join(lines[block_start : block_end + 1])
    llm       = _get_llm_complete()
    prompt    = (
        f"The following Isabelle/HOL proof block has an error:\n\n{old_block}\n\n"
        f"Error at line {error_line}: {error_msg}\n"
        f"Goal: {goal}\n\n"
        "Rewrite this proof...qed block. Use sorry as a placeholder if needed. "
        "Output only the replacement block."
    )
    new_block = llm(prompt, model=config.model)
    if not new_block:
        return None
    repaired = lines[:block_start] + new_block.strip().splitlines() + lines[block_end + 1:]
    return "\n".join(repaired)


def _whole_proof_repair(
    script: str, error_msg: str,
    config: RepairConfig, goal: str,
) -> Optional[str]:
    """Stage 3: regenerate the full proof from scratch."""
    llm    = _get_llm_complete()
    prompt = (
        f"The following Isabelle/HOL proof has failed:\n\n{script}\n\n"
        f"Error: {error_msg}\nGoal: {goal}\n\n"
        "Write a complete Isar-style proof from `proof` to `qed`. "
        "Use induction or cases where needed. "
        "Use sorry only where unavoidable. "
        "Output only valid Isabelle proof text."
    )
    return llm(prompt, model=config.model)


def _verify(script: str, goal: str) -> tuple[bool, str]:
    """Run Isabelle and return (passed, error_text)."""
    if "sorry" in script:
        return False, "script still contains sorry"

    server_info = proc = isabelle = None

    try:
        from prover.isabelle_api import (
            start_isabelle_server,
            get_isabelle_client,
            run_theory,
            finished_ok,
        )
        from prover.cli import _extract_session_id

        server_info, proc = start_isabelle_server(name="isabelle", log_file="repair_server.log")
        isabelle = get_isabelle_client(server_info)

        start_result = isabelle.session_start(session="HOL")
        session_id = _extract_session_id(start_result)

        theory = f"""theory Scratch
imports Main
begin

lemma "{goal}"
{script}

end
"""

        responses = run_theory(isabelle, session_id, theory, timeout_s=30)
        ok, info = finished_ok(responses)
        return ok, "" if ok else str(responses)[-1500:]

    except Exception as e:
        return False, str(e)

    finally:
        try:
            if isabelle is not None:
                isabelle.shutdown()
        except Exception:
            pass

        try:
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=3)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2. cegis_repair
# ---------------------------------------------------------------------------

def cegis_repair(
    goal: str,
    initial_script: str,
    config: RepairConfig,
) -> ProofState:
    """
    CEGIS-style iterative proof repair loop (assignment spec WIP feature).

    Steps
    -----
    1. Start with initial_script (LLM outline, may contain sorry).
    2. Verify with Isabelle. Verified + no sorry -> done.
    3. If sorries present -> Fill (stepwise prover then LLM fallback).
    4. Re-verify. Clean -> done.
    5. Staged Repair on first error:
         Stage 1: local     - fix one have/show near the error.
         Stage 2: subproof  - regenerate enclosing proof...qed.
         Stage 3: whole     - regenerate the entire proof.
    6. After repair, apply Fill to any new sorries, then go to step 2.
    7. Stop when: verified, timeout reached, or no progress at any stage.

    Maintains a candidate pool (config.keep_candidates) preferring scripts
    with fewer sorries / that verify further.

    Returns the best ProofState seen.
    """
    deadline = time.monotonic() + config.timeout

    def timed_out():
        return time.monotonic() >= deadline

    candidates = [ProofState(script=initial_script)]
    best       = candidates[0]
    iteration  = 0

    while candidates and not timed_out():
        iteration += 1
        # Pick best candidate: prefer verified, then fewest sorries
        candidates.sort(key=lambda s: (not s.verified, s.sorry_count))
        state = candidates.pop(0)

        logger.info("[CEGIS] iter=%d  sorries=%d", iteration, state.sorry_count)

        # Step 2: verify
        passed, errors = _verify(state.script, goal)
        if passed and _count_sorries(state.script) == 0:
            state.verified = True
            logger.info("[CEGIS] Verified clean. Done.")
            return state

        # Steps 3/4: fill sorry holes
        if _count_sorries(state.script) > 0 and not timed_out():
            filled    = fill_sorry(state, config, goal)
            filled.sorry_count = _count_sorries(filled.script)
            p2, e2    = _verify(filled.script, goal)
            if p2 and _count_sorries(filled.script) == 0:
                filled.verified = True
                logger.info("[CEGIS] Verified after Fill. Done.")
                return filled
            # Keep filled version if it made progress (fewer sorries)
            if filled.sorry_count <= state.sorry_count:
                state  = filled
                errors = e2

        if timed_out():
            break

        # Step 5: staged repair
        error_line, error_msg = _first_error(errors)
        repaired_any          = False

        # Stage 1: local repair
        for _ in range(config.local_attempts):
            if timed_out():
                break
            new_script = _local_repair(state.script, error_line, error_msg, config, goal)
            if new_script and new_script != state.script:
                ns   = ProofState(script=new_script)
                p, e = _verify(new_script, goal)
                if p and _count_sorries(new_script) == 0:
                    ns.verified = True
                    logger.info("[CEGIS] Verified after Stage-1 repair.")
                    return ns
                if ns.sorry_count < state.sorry_count or len(e) < len(errors):
                    candidates.append(ns)
                    repaired_any = True
                    break
                error_line, error_msg = _first_error(e)

        # Stage 2: subproof repair
        if not repaired_any:
            for _ in range(config.subproof_attempts):
                if timed_out():
                    break
                new_script = _subproof_repair(state.script, error_line, error_msg, config, goal)
                if new_script and new_script != state.script:
                    ns   = ProofState(script=new_script)
                    p, e = _verify(new_script, goal)
                    if p and _count_sorries(new_script) == 0:
                        ns.verified = True
                        logger.info("[CEGIS] Verified after Stage-2 repair.")
                        return ns
                    if ns.sorry_count < state.sorry_count or len(e) < len(errors):
                        candidates.append(ns)
                        repaired_any = True
                        break
                    error_line, error_msg = _first_error(e)

        # Stage 3: whole proof repair
        if not repaired_any:
            for _ in range(config.whole_attempts):
                if timed_out():
                    break
                new_script = _whole_proof_repair(state.script, error_msg, config, goal)
                if new_script and new_script != state.script:
                    ns   = ProofState(script=new_script)
                    p, e = _verify(new_script, goal)
                    if p and _count_sorries(new_script) == 0:
                        ns.verified = True
                        logger.info("[CEGIS] Verified after Stage-3 repair.")
                        return ns
                    candidates.append(ns)
                    repaired_any = True
                    error_line, error_msg = _first_error(e)

        # Trim candidate pool
        candidates = sorted(
            candidates, key=lambda s: (not s.verified, s.sorry_count)
        )[:config.keep_candidates]

        # Track overall best
        if best.sorry_count > state.sorry_count or (state.verified and not best.verified):
            best = state

        if not repaired_any:
            logger.info("[CEGIS] No repair progress at any stage; stopping.")
            break

    logger.info(
        "[CEGIS] Finished: verified=%s sorries=%d iterations=%d",
        best.verified, best.sorry_count, iteration,
    )
    return best

# ---------------------------------------------------------------------------
# Compatibility layer for Part 2 planner/fill integration
# ---------------------------------------------------------------------------
# Part 2's planner.driver and planner.skeleton expect these names to exist.
# These wrappers keep the working Part 3 repair.py behaviour and only expose
# the API that Part 2 imports.

_APPLY_OR_BY = re.compile(r"^\s*(?:apply\b.*|by\b.*|done\b|\.)\s*$")


def _facts_from_state(state_block: str, limit: int = 8) -> list[str]:
    """
    Lightweight fact/definition extractor used by planner.skeleton for optional
    context hints. Safe fallback: returns a small list of useful names only if
    they appear in the Isabelle state text.
    """
    if not state_block:
        return []

    found = []

    # Common Isabelle fact/definition name shapes.
    patterns = [
        r"\b[A-Za-z_][A-Za-z0-9_']*_def\b",
        r"\b[A-Za-z_][A-Za-z0-9_']*\.simps\b",
        r"\b[A-Za-z_][A-Za-z0-9_']*\.induct\b",
        r"\b[A-Za-z_][A-Za-z0-9_']*\.cases\b",
        r"\bassms\b",
    ]

    for pat in patterns:
        for m in re.finditer(pat, state_block):
            name = m.group(0)
            if name not in found:
                found.append(name)
            if len(found) >= limit:
                return found

    return found


def _replace_span_safely(text: str, span: tuple[int, int], replacement: str) -> str:
    """
    Replace a specific sorry span safely.
    Important: if the line says 'by sorry', replacing only 'sorry' with
    'by simp' would create 'by by simp'. This avoids that.
    """
    s, e = span
    replacement = (replacement or "").strip()

    line_start = text.rfind("\n", 0, s) + 1
    line_end = text.find("\n", e)
    if line_end == -1:
        line_end = len(text)

    line = text[line_start:line_end]

    if re.search(r"\bby\s+sorry\b", line):
        new_line = re.sub(r"\bby\s+sorry\b", replacement, line, count=1)
        return text[:line_start] + new_line + text[line_end:]

    return text[:s] + replacement + text[e:]


def try_cegis_repairs(
    *,
    full_text: str,
    hole_span: tuple[int, int],
    goal_text: str,
    model: Optional[str] = None,
    isabelle=None,
    session=None,
    repair_budget_s: float = 30.0,
    max_ops_to_try: int = 2,
    beam_k: int = 2,
    allow_whole_fallback: bool = False,
    trace: bool = False,
    resume_stage: int = 0,
):
    """
    Part 2 compatibility wrapper.

    It tries the already-working Part 1 prover connection on the current goal
    and patches the selected sorry hole. The caller verifies the full proof after.
    """
    prover = _get_stepwise_prover()

    try:
        result = prover(
            goal=goal_text,
            model=model or "qwen2.5:3b",
            beam=max(1, int(beam_k or 2)),
            max_depth=8,
            timeout=min(float(repair_budget_s or 30.0), 60.0),
        )

        if result and result.get("success") and result.get("proof"):
            proof = result["proof"].strip()
            patched = _replace_span_safely(full_text, hole_span, proof)

            if trace:
                print(f"[repair-compat] Filled hole using prover: {proof}")

            return patched, True, {"method": "stepwise-prover", "proof": proof}

    except Exception as e:
        if trace:
            print(f"[repair-compat] Stepwise prover repair failed: {e}")

    return full_text, False, {"method": "none", "reason": "no compatible repair found"}


def regenerate_whole_proof(
    *,
    full_text: str,
    goal_text: str,
    model: Optional[str] = None,
    isabelle=None,
    session=None,
    budget_s: float = 30.0,
    trace: bool = False,
    prior_outline_text: Optional[str] = None,
):
    """
    Part 2 compatibility wrapper for whole-proof fallback.

    This keeps behaviour conservative: it uses the existing whole-proof repair
    helper if available, but returns failure safely if no clean proof is produced.
    """
    config = RepairConfig(
        timeout=float(budget_s or 30.0),
        model=model or "qwen2.5:3b",
    )

    try:
        script = _whole_proof_repair(
            prior_outline_text or full_text,
            "whole-proof fallback requested",
            config,
            goal_text,
        )

        if not script:
            return full_text, False, {"method": "whole-proof", "reason": "empty output"}

        script = script.strip()

        # If the LLM returned only a proof body, rebuild the full lemma text.
        if script.startswith("lemma "):
            candidate_full = script + "\n"
            ok = False
        else:
            lemma_line = next(
                (ln for ln in full_text.splitlines() if ln.strip().startswith("lemma ")),
                f'lemma "{goal_text}"',
            )
            candidate_full = lemma_line + "\n" + script + "\n"
            ok, _errors = _verify(script, goal_text)

        if trace:
            print(f"[repair-compat] Whole-proof fallback verified={ok}")

        return candidate_full, ok, {"method": "whole-proof"}

    except Exception as e:
        if trace:
            print(f"[repair-compat] Whole-proof fallback failed: {e}")

    return full_text, False, {"method": "whole-proof", "reason": "exception"}

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="CEGIS proof repair for Isabelle/HOL")
    # goal is a positional argument (not --goal) so it matches planner/cli.py convention
    parser.add_argument("goal",                  help="The lemma/goal string to prove")
    parser.add_argument("--timeout",             type=float, default=120.0)
    parser.add_argument("--model",               default="qwen2.5:3b")
    parser.add_argument("--beam",                type=int,   default=3)
    parser.add_argument("--max-depth",           type=int,   default=8)
    parser.add_argument("--local-attempts",      type=int,   default=3)
    parser.add_argument("--subproof-attempts",   type=int,   default=2)
    parser.add_argument("--whole-attempts",      type=int,   default=2)
    parser.add_argument("--fill-attempts",       type=int,   default=3)
    parser.add_argument("--verbose",             action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = RepairConfig(
        timeout           = args.timeout,
        model             = args.model,
        beam              = args.beam,
        max_depth         = args.max_depth,
        local_attempts    = args.local_attempts,
        subproof_attempts = args.subproof_attempts,
        whole_attempts    = args.whole_attempts,
        fill_attempts     = args.fill_attempts,
    )

    # Generate an initial outline via the LLM
    llm = _get_llm_complete()
    prompt = (
        f"Write an Isar-style proof outline for the following Isabelle/HOL goal:\n\n"
        f"lemma goal: \"{args.goal}\"\n\n"
        "Use sorry as placeholders where you are unsure. "
        "Output only valid Isabelle proof text."
    )
    logger.info("[Main] Generating initial proof outline...")
    initial_script = llm(prompt, model=config.model)
    if not initial_script:
        initial_script = "proof -\n  show ?thesis by sorry\nqed"

    logger.info("[Main] Initial script:\n%s", initial_script)

    result = cegis_repair(
        goal           = args.goal,
        initial_script = initial_script,
        config         = config,
    )

    print("\n" + "=" * 60)
    print(f"Verified     : {result.verified}")
    print(f"Sorries left : {result.sorry_count}")
    print(f"Holes filled : {result.holes_filled}")
    print("\nFinal script:")
    print(result.script)


if __name__ == "__main__":
    main()
