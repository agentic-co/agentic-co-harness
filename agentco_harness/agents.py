"""Agents - DSPy-powered workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import dspy

from .beads import Beads, Task, TaskPriority
from .signatures import (
    AnalystReport,
    ClassifyEvent,
    CSRespond,
    DevImplementWithClaudeCode,
    DevOpsAnalyze,
    DevPlan,
    PMPrioritize,
)


@dataclass
class AgentResult:
    """Result of an agent execution."""

    success: bool
    output: dict[str, Any]
    next_agent: str | None = None
    subtasks: list[dict] | None = None
    error: str | None = None


def create_company_doc(
    area: str,
    filename: str,
    title: str,
    body: str,
    *,
    created_by: str,
    tags: list[str] | None = None,
    related: list[str] | None = None,
    company_path: Path | None = None,
) -> Path | None:
    """Create a document in the company/ directory.

    Returns the created file path, or None if company/ doesn't exist.
    """
    base = company_path or Path("company")
    if not base.is_dir():
        print(
            f"[docs] WARNING: company directory '{base}' does not exist — "
            f"document '{title}' is being DISCARDED. Run 'agentco init --company' to fix."
        )
        return None

    target = base / area / filename
    target.parent.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    tags_list = tags or []
    related_list = related or []
    tag_str = ", ".join(tags_list)
    related_lines = "\n".join(f'  - "[[{r}]]"' for r in related_list)

    frontmatter = (
        f"---\n"
        f"title: {title}\n"
        f"created_by: {created_by}\n"
        f"created_at: {today}\n"
        f"status: draft\n"
        f"tags: [{tag_str}]\n"
        f"related: {related_lines or '[]'}\n"
        f"---\n"
    )
    target.write_text(frontmatter + "\n" + body)
    return target


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    import re

    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:60].rstrip("-")


class Classifier:
    """Classifies incoming events and creates tasks."""

    def __init__(self, beads: Beads):
        self.beads = beads
        self.classify = dspy.ChainOfThought(ClassifyEvent)

    def process(self, source: str, content: str, context: str, source_id: str) -> Task | None:
        """Process an event and optionally create a task."""
        # Skip duplicates
        if self.beads.exists_source(source, source_id):
            return None

        result = self.classify(source=source, content=content, context=context)

        if not result.should_create_task:
            return None

        task = self.beads.create(
            title=result.title,
            description=result.description,
            priority=TaskPriority(result.priority),
            assigned_agent=result.assigned_agent,
            source=source,
            source_id=source_id,
            metadata={"category": result.category},
        )
        return task


class CSAgent:
    """Customer Success agent."""

    def __init__(self, beads: Beads, agent_config=None):
        self.beads = beads
        self.context = agent_config.context if agent_config else ""
        self.respond = dspy.ChainOfThought(CSRespond)

    def execute(self, task: Task) -> AgentResult:
        """Handle a CS task."""
        result = self.respond(
            task_title=task.title,
            task_description=task.description,
            customer_message=task.metadata.get("original_message", task.description),
            customer_context=task.metadata.get("customer_context")
            or self.context
            or "No context available",
        )

        output = {
            "response": result.response,
            "internal_notes": result.internal_notes,
        }

        # Create feedback document in company/
        slug = task.id.replace("ac-", "")
        doc_path = create_company_doc(
            area="customer-success/feedback",
            filename=f"{slug}-{_slugify(task.title)}.md",
            title=task.title,
            body=(
                f"# {task.title}\n\n"
                f"## Customer Message\n\n{task.description}\n\n"
                f"## Response\n\n{result.response}\n\n"
                f"## Internal Notes\n\n{result.internal_notes}\n"
            ),
            created_by="cs",
            tags=["feedback", "customer-success"],
        )
        if doc_path:
            output["doc_path"] = str(doc_path)

        subtasks = None
        next_agent = None

        if result.needs_escalation:
            next_agent = "pm"
            subtasks = [
                {
                    "title": f"[Escalated] {task.title}",
                    "description": f"{task.description}\n\nEscalation reason: {result.escalation_reason}",
                    "assigned_agent": "pm",
                }
            ]

        return AgentResult(
            success=True,
            output=output,
            next_agent=next_agent,
            subtasks=subtasks,
        )


class PMAgent:
    """Product Manager agent."""

    def __init__(self, beads: Beads, agent_config=None):
        self.beads = beads
        self.context = agent_config.context if agent_config else ""
        self.prioritize = dspy.ChainOfThought(PMPrioritize)

    def execute(self, task: Task) -> AgentResult:
        """Handle a PM task."""
        result = self.prioritize(
            task_title=task.title,
            task_description=task.description,
            product_context=task.metadata.get("product_context")
            or self.context
            or "No context available",
        )

        output = {
            "priority_rationale": result.priority_rationale,
            "acceptance_criteria": result.acceptance_criteria,
            "estimated_effort": result.estimated_effort,
            "dependencies": result.dependencies,
        }

        # Create PRD document in company/
        slug = task.id.replace("ac-", "")
        ac_text = "\n".join(f"- {ac}" for ac in result.acceptance_criteria)
        doc_path = create_company_doc(
            area="product/PRD",
            filename=f"{slug}-{_slugify(task.title)}.md",
            title=task.title,
            body=(
                f"# {task.title}\n\n"
                f"## Rationale\n\n{result.priority_rationale}\n\n"
                f"## Acceptance Criteria\n\n{ac_text}\n\n"
                f"## Effort Estimate\n\n{result.estimated_effort}\n\n"
                f"## Dependencies\n\n{result.dependencies}\n"
            ),
            created_by="pm",
            tags=["prd", "product"],
            related=["product/ROADMAP", "product/backlog"],
        )
        if doc_path:
            output["doc_path"] = str(doc_path)

        # Acceptance criteria are this task's VERIFICATION contract — they live on
        # the task as metadata and are never turned into work. Subtasks are a
        # separate, explicit decision (needs_decomposition), capped downstream.
        acceptance_criteria = list(result.acceptance_criteria)
        self.beads.update(
            task.id,
            metadata={**task.metadata, "acceptance_criteria": acceptance_criteria},
        )
        ac_meta = {"acceptance_criteria": acceptance_criteria}

        subtasks = None
        if getattr(result, "needs_decomposition", False) and result.subtasks:
            # Explicit, bounded decomposition into distinct work items.
            subtasks = [
                {
                    "title": st,
                    "description": st,
                    "assigned_agent": result.assigned_to,
                    "metadata": ac_meta,
                }
                for st in result.subtasks
            ]
        elif result.assigned_to in ("dev", "devops", "analyst"):
            # Single handoff — the work is one unit; ACs ride along as verification.
            subtasks = [
                {
                    "title": task.title,
                    "description": f"{task.description}\n\nAcceptance Criteria:\n{ac_text}",
                    "assigned_agent": result.assigned_to,
                    "metadata": ac_meta,
                }
            ]

        return AgentResult(
            success=True,
            output=output,
            next_agent=result.assigned_to,
            subtasks=subtasks,
        )


class DevAgent:
    """Developer agent."""

    def __init__(self, beads: Beads, use_claude_code: bool = False, agent_config=None):
        self.beads = beads
        self.use_claude_code = use_claude_code
        self.context = agent_config.context if agent_config else ""
        self.plan = dspy.ChainOfThought(DevPlan)
        self.implement_prompt = dspy.ChainOfThought(DevImplementWithClaudeCode)

    def _codebase_context(self, task: Task) -> str:
        return task.metadata.get("codebase_context") or self.context or "No context available"

    def execute(self, task: Task) -> AgentResult:
        """Handle a dev task (planning phase)."""
        result = self.plan(
            task_title=task.title,
            task_description=task.description,
            codebase_context=self._codebase_context(task),
        )

        output = {
            "approach": result.approach,
            "files_to_modify": result.files_to_modify,
            "risks": result.risks,
        }

        # Create ADR document for significant decisions
        if result.risks:
            slug = task.id.replace("ac-", "")
            risks_text = "\n".join(f"- {r}" for r in result.risks)
            files_text = "\n".join(f"- `{f}`" for f in result.files_to_modify)
            doc_path = create_company_doc(
                area="engineering/adr",
                filename=f"{slug}-{_slugify(task.title)}.md",
                title=f"ADR: {task.title}",
                body=(
                    f"# ADR: {task.title}\n\n"
                    f"## Context\n\n{task.description}\n\n"
                    f"## Decision\n\n{result.approach}\n\n"
                    f"## Files Affected\n\n{files_text}\n\n"
                    f"## Risks\n\n{risks_text}\n"
                ),
                created_by="dev",
                tags=["adr", "engineering"],
                related=["engineering/ARCHITECTURE"],
            )
            if doc_path:
                output["doc_path"] = str(doc_path)

        # If Claude Code is enabled and task is ready, execute implementation
        if self.use_claude_code and result.ready_to_implement:
            cc_result = self._execute_with_claude_code(task, result)
            if cc_result:
                output["claude_code_output"] = cc_result

        # No self-recursion: Dev records its breakdown in the ADR doc (above) but
        # does NOT spawn dev→dev subtasks. Decomposition is a deliberate PM act,
        # not an execution-agent reflex (Recorro runaway-tree fix, 2026-06-14).
        if result.subtasks:
            output["proposed_breakdown"] = list(result.subtasks)

        return AgentResult(
            success=True,
            output=output,
            subtasks=None,
        )

    def _execute_with_claude_code(self, task: Task, plan_result) -> str | None:
        """Execute implementation using Claude Code CLI."""
        import subprocess

        # Generate the Claude Code prompt
        impl = self.implement_prompt(
            task_title=task.title,
            task_description=task.description,
            codebase_context=self._codebase_context(task),
            approach=plan_result.approach,
        )

        prompt = impl.claude_code_prompt

        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--print"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                return result.stdout
            else:
                print(f"[dev] Claude Code error: {result.stderr}")
                return f"Error: {result.stderr}"
        except FileNotFoundError:
            print("[dev] Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code")
            return None
        except subprocess.TimeoutExpired:
            print("[dev] Claude Code timed out after 300s")
            return None


class DevOpsAgent:
    """DevOps agent."""

    def __init__(self, beads: Beads, agent_config=None):
        self.beads = beads
        self.context = agent_config.context if agent_config else ""
        self.analyze = dspy.ChainOfThought(DevOpsAnalyze)

    def execute(self, task: Task) -> AgentResult:
        """Handle a devops task."""
        result = self.analyze(
            task_title=task.title,
            task_description=task.description,
            system_context=task.metadata.get("system_context")
            or self.context
            or "No context available",
            logs=task.metadata.get("logs", "No logs available"),
            metrics=task.metadata.get("metrics", "No metrics available"),
        )

        output = {
            "root_cause": result.root_cause,
            "severity": result.severity,
            "remediation_steps": result.remediation_steps,
            "prevention": result.prevention,
            "can_auto_remediate": result.can_auto_remediate,
        }

        # Create incident document in company/
        slug = task.id.replace("ac-", "")
        steps_text = "\n".join(f"- {s}" for s in result.remediation_steps)
        doc_path = create_company_doc(
            area="operations/incidents",
            filename=f"{slug}-{_slugify(task.title)}.md",
            title=task.title,
            body=(
                f"# Incident: {task.title}\n\n"
                f"## Root Cause\n\n{result.root_cause}\n\n"
                f"## Severity\n\n{result.severity}\n\n"
                f"## Remediation Steps\n\n{steps_text}\n\n"
                f"## Prevention\n\n{result.prevention}\n"
            ),
            created_by="devops",
            tags=["incident", "operations"],
        )
        if doc_path:
            output["doc_path"] = str(doc_path)

        # No self-recursion: remediation steps are recorded in the doc/output (above)
        # but DevOps never spawns devops→devops subtasks. Decomposition is a PM decision.
        return AgentResult(
            success=True,
            output=output,
            subtasks=None,
        )


class AnalystAgent:
    """Analyst agent."""

    def __init__(self, beads: Beads, agent_config=None):
        self.beads = beads
        self.context = agent_config.context if agent_config else ""
        self.report = dspy.ChainOfThought(AnalystReport)

    def execute(self, task: Task) -> AgentResult:
        """Handle an analyst task."""
        result = self.report(
            task_title=task.title,
            task_description=task.description,
            business_context=task.metadata.get("business_context")
            or self.context
            or "No context available",
            data=task.metadata.get("data", "No data available"),
        )

        output = {
            "summary": result.summary,
            "key_findings": result.key_findings,
            "recommendations": result.recommendations,
        }

        # Create report document in company/
        slug = task.id.replace("ac-", "")
        findings_text = "\n".join(f"- {f}" for f in result.key_findings)
        recs_text = "\n".join(f"- {r}" for r in result.recommendations)
        doc_path = create_company_doc(
            area="finance/reports",
            filename=f"{slug}-{_slugify(task.title)}.md",
            title=task.title,
            body=(
                f"# {task.title}\n\n"
                f"## Summary\n\n{result.summary}\n\n"
                f"## Key Findings\n\n{findings_text}\n\n"
                f"## Recommendations\n\n{recs_text}\n"
            ),
            created_by="analyst",
            tags=["report", "analysis"],
        )
        if doc_path:
            output["doc_path"] = str(doc_path)

        subtasks = None
        if result.follow_up_tasks:
            subtasks = [
                {
                    "title": ft,
                    "description": ft,
                    "assigned_agent": "pm",  # Follow-ups go to PM for triage
                }
                for ft in result.follow_up_tasks
            ]

        return AgentResult(
            success=True,
            output=output,
            subtasks=subtasks,
        )


# Agent registry
AGENTS = {
    "cs": CSAgent,
    "pm": PMAgent,
    "dev": DevAgent,
    "devops": DevOpsAgent,
    "analyst": AnalystAgent,
}


def get_agent(name: str, beads: Beads, agent_config=None):
    """Get an agent by name. agent_config wires operator context into prompts."""
    if name not in AGENTS:
        raise ValueError(f"Unknown agent: {name}")

    if name == "dev":
        use_claude_code = (
            agent_config.settings.get("use_claude_code", False) if agent_config else False
        )
        return DevAgent(beads, use_claude_code=use_claude_code, agent_config=agent_config)

    return AGENTS[name](beads, agent_config=agent_config)
