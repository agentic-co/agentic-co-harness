"""DSPy optimization - MIPROv2 training utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import dspy

from .signatures import (
    AnalystReport,
    ClassifyEvent,
    CSRespond,
    DevOpsAnalyze,
    DevPlan,
    PMPrioritize,
)

# Map signature names to classes
SIGNATURES = {
    "classify": ClassifyEvent,
    "cs_respond": CSRespond,
    "pm_prioritize": PMPrioritize,
    "dev_plan": DevPlan,
    "devops_analyze": DevOpsAnalyze,
    "analyst_report": AnalystReport,
}

OPTIMIZED_DIR = Path(".agentco/optimized")


def load_examples(path: Path) -> list[dspy.Example]:
    """Load training examples from a JSONL file."""
    examples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            examples.append(dspy.Example(**data).with_inputs(*data.get("_inputs", [])))
    return examples


def get_metric_for_signature(sig_name: str):
    """Get a simple validation metric for a signature."""

    def metric(example, prediction, trace=None) -> float:
        """Basic metric: check that all output fields are non-empty."""
        sig_cls = SIGNATURES[sig_name]
        score = 0.0
        total = len(sig_cls.output_fields)
        if total == 0:
            return 1.0
        for field_name in sig_cls.output_fields:
            value = getattr(prediction, field_name, None)
            if value is not None and value != "" and value != []:
                score += 1.0
        return score / total

    return metric


def optimize_signature(
    sig_name: str,
    examples_path: Path,
    num_candidates: int = 7,
    max_bootstrapped_demos: int = 3,
    max_labeled_demos: int = 4,
) -> Path:
    """Run MIPROv2 optimization on a signature.

    Args:
        sig_name: Name of the signature to optimize.
        examples_path: Path to JSONL training examples.
        num_candidates: Number of candidate programs to evaluate.
        max_bootstrapped_demos: Max bootstrapped demos per candidate.
        max_labeled_demos: Max labeled demos per candidate.

    Returns:
        Path to the saved optimized program.
    """
    if sig_name not in SIGNATURES:
        raise ValueError(f"Unknown signature: {sig_name}. Available: {list(SIGNATURES.keys())}")

    sig_cls = SIGNATURES[sig_name]
    examples = load_examples(examples_path)

    if len(examples) < 2:
        raise ValueError(f"Need at least 2 examples, got {len(examples)}")

    # Split into train/val
    split = max(1, len(examples) // 2)
    trainset = examples[:split]
    valset = examples[split:]

    # Create the base program
    program = dspy.ChainOfThought(sig_cls)

    # Create optimizer
    optimizer = dspy.MIPROv2(
        metric=get_metric_for_signature(sig_name),
        num_candidates=num_candidates,
        max_bootstrapped_demos=max_bootstrapped_demos,
        max_labeled_demos=max_labeled_demos,
        auto="light",
    )

    # Run optimization
    optimized = optimizer.compile(program, trainset=trainset, valset=valset)

    # Save optimized program
    OPTIMIZED_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OPTIMIZED_DIR / f"{sig_name}.json"
    optimized.save(str(output_path))

    return output_path


def load_optimized(sig_name: str, sig_cls: type) -> dspy.Module | None:
    """Load an optimized program if available.

    Args:
        sig_name: Name of the signature.
        sig_cls: The signature class.

    Returns:
        Optimized module or None if not available.
    """
    path = OPTIMIZED_DIR / f"{sig_name}.json"
    if not path.exists():
        return None

    program = dspy.ChainOfThought(sig_cls)
    program.load(str(path))
    return program


def list_optimized() -> list[str]:
    """List available optimized signatures."""
    if not OPTIMIZED_DIR.exists():
        return []
    return [p.stem for p in OPTIMIZED_DIR.glob("*.json")]
