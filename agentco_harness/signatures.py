"""DSPy Signatures - All prompts as optimizable modules."""

import dspy


# ============================================================
# CLASSIFIER
# ============================================================


class ClassifyEvent(dspy.Signature):
    """Classify an incoming event from any source.
    
    Determine the category, priority, and which agent should handle it.
    """

    source: str = dspy.InputField(desc="Source of the event (gmail, logs, feedback, etc.)")
    content: str = dspy.InputField(desc="The event content/message")
    context: str = dspy.InputField(desc="Additional context (sender, timestamp, etc.)")

    category: str = dspy.OutputField(
        desc="Category: bug, feature_request, support, incident, info, spam"
    )
    priority: int = dspy.OutputField(desc="Priority 0-3 (0=critical, 3=low)")
    assigned_agent: str = dspy.OutputField(
        desc="Agent to handle: cs, pm, dev, devops, analyst"
    )
    title: str = dspy.OutputField(desc="Short title for the task (max 80 chars)")
    description: str = dspy.OutputField(desc="Task description with relevant details")
    should_create_task: bool = dspy.OutputField(
        desc="Whether this needs a task (false for spam/noise)"
    )


# ============================================================
# TRIAGE (heartbeat cycle)
# ============================================================


class TriageCycle(dspy.Signature):
    """Triage the open task queue for one heartbeat cycle.

    Decide which tasks should execute this cycle, which can wait, and
    which need a human. Triage is advisory: tasks not mentioned in any
    output list will be executed anyway.
    """

    open_tasks: str = dspy.InputField(
        desc="JSON list of open tasks: id, title, agent, priority, created_at"
    )

    run_now: list[str] = dspy.OutputField(
        desc="Task IDs to execute this cycle, highest value first"
    )
    defer: list[str] = dspy.OutputField(desc="Task IDs that can wait for a later cycle")
    needs_human: list[str] = dspy.OutputField(
        desc="Task IDs that need human attention before execution"
    )
    needs_planner: list[str] = dspy.OutputField(
        desc="Task IDs that deserve planner decomposition/routing before execution. "
        "Advisory only — these are surfaced for a human to act on and NEVER auto-spawn "
        "a planner bead. Return an empty list if none."
    )


# ============================================================
# CS AGENT
# ============================================================


class CSRespond(dspy.Signature):
    """Customer Success agent responds to customer feedback or support requests."""

    task_title: str = dspy.InputField(desc="Task title")
    task_description: str = dspy.InputField(desc="Task description")
    customer_message: str = dspy.InputField(desc="Original customer message")
    customer_context: str = dspy.InputField(desc="Customer info, history, etc.")

    response: str = dspy.OutputField(desc="Professional response to the customer")
    internal_notes: str = dspy.OutputField(desc="Notes for internal team")
    needs_escalation: bool = dspy.OutputField(desc="Whether to escalate to PM/Dev")
    escalation_reason: str = dspy.OutputField(desc="Why escalation is needed (if any)")


# ============================================================
# PM AGENT
# ============================================================


class PMPrioritize(dspy.Signature):
    """Product Manager prioritizes and specs a feature/fix request."""

    task_title: str = dspy.InputField(desc="Task title")
    task_description: str = dspy.InputField(desc="Task description")
    product_context: str = dspy.InputField(desc="Current product state, roadmap, etc.")

    priority_rationale: str = dspy.OutputField(desc="Why this priority level")
    acceptance_criteria: list[str] = dspy.OutputField(
        desc="Verification checks for THIS task — how we know it is done. "
        "These are NOT work items and must never become separate tasks."
    )
    estimated_effort: str = dspy.OutputField(desc="Effort estimate: small/medium/large")
    dependencies: list[str] = dspy.OutputField(desc="Dependencies or blockers")
    assigned_to: str = dspy.OutputField(desc="Next agent: dev, devops, analyst")
    needs_decomposition: bool = dspy.OutputField(
        desc="True ONLY if this genuinely must be split into multiple separate work "
        "items; False if it can be handed off and done as a single unit. Default False."
    )
    subtasks: list[str] = dspy.OutputField(
        desc="The distinct work items to create — ONLY when needs_decomposition is True. "
        "Separate from acceptance_criteria (those verify; these are new work). "
        "Keep it minimal; empty when not decomposing."
    )


# ============================================================
# DEV AGENT
# ============================================================


class DevPlan(dspy.Signature):
    """Developer plans implementation for a task."""

    task_title: str = dspy.InputField(desc="Task title")
    task_description: str = dspy.InputField(desc="Task description with acceptance criteria")
    codebase_context: str = dspy.InputField(desc="Relevant codebase info")

    approach: str = dspy.OutputField(desc="High-level implementation approach")
    files_to_modify: list[str] = dspy.OutputField(desc="Files that need changes")
    subtasks: list[str] = dspy.OutputField(desc="Breakdown into subtasks")
    risks: list[str] = dspy.OutputField(desc="Potential risks or concerns")
    ready_to_implement: bool = dspy.OutputField(
        desc="Whether we have enough info to implement"
    )


class DevImplement(dspy.Signature):
    """Developer implements a specific subtask."""

    subtask: str = dspy.InputField(desc="Subtask to implement")
    file_path: str = dspy.InputField(desc="File to modify")
    current_content: str = dspy.InputField(desc="Current file content")
    context: str = dspy.InputField(desc="Implementation context and constraints")

    new_content: str = dspy.OutputField(desc="Updated file content")
    explanation: str = dspy.OutputField(desc="What was changed and why")


class DevImplementWithClaudeCode(dspy.Signature):
    """Developer uses Claude Code CLI to implement a task.

    Generates a prompt for Claude Code CLI to execute actual code changes.
    """

    task_title: str = dspy.InputField(desc="Task title")
    task_description: str = dspy.InputField(desc="Task description with acceptance criteria")
    codebase_context: str = dspy.InputField(desc="Relevant codebase info")
    approach: str = dspy.InputField(desc="Implementation approach from planning phase")

    claude_code_prompt: str = dspy.OutputField(
        desc="Detailed prompt for Claude Code CLI to implement the changes"
    )
    expected_files: list[str] = dspy.OutputField(desc="Files expected to be modified")
    verification_steps: list[str] = dspy.OutputField(
        desc="Steps to verify the implementation worked"
    )


# ============================================================
# DEVOPS AGENT
# ============================================================


class DevOpsAnalyze(dspy.Signature):
    """DevOps analyzes an incident or infrastructure issue."""

    task_title: str = dspy.InputField(desc="Task title")
    task_description: str = dspy.InputField(desc="Task description")
    system_context: str = dspy.InputField(desc="Infrastructure layout, services, environments")
    logs: str = dspy.InputField(desc="Relevant log entries")
    metrics: str = dspy.InputField(desc="Relevant metrics if available")

    root_cause: str = dspy.OutputField(desc="Root cause analysis")
    severity: str = dspy.OutputField(desc="Severity: critical/high/medium/low")
    remediation_steps: list[str] = dspy.OutputField(desc="Steps to fix")
    prevention: list[str] = dspy.OutputField(desc="How to prevent in future")
    can_auto_remediate: bool = dspy.OutputField(
        desc="Whether we can auto-fix this"
    )


class DevOpsRemediate(dspy.Signature):
    """DevOps executes remediation for an issue."""

    issue: str = dspy.InputField(desc="Issue description")
    remediation_step: str = dspy.InputField(desc="Remediation step to execute")
    system_context: str = dspy.InputField(desc="System state and constraints")

    command: str = dspy.OutputField(desc="Command or action to execute")
    expected_outcome: str = dspy.OutputField(desc="What should happen")
    rollback_command: str = dspy.OutputField(desc="How to rollback if needed")
    requires_approval: bool = dspy.OutputField(desc="Whether human approval needed")


# ============================================================
# ANALYST AGENT
# ============================================================


class AnalystReport(dspy.Signature):
    """Analyst creates insights from data."""

    task_title: str = dspy.InputField(desc="Task title")
    task_description: str = dspy.InputField(desc="What to analyze")
    business_context: str = dspy.InputField(desc="Business model, metrics that matter, market")
    data: str = dspy.InputField(desc="Data to analyze")

    summary: str = dspy.OutputField(desc="Executive summary")
    key_findings: list[str] = dspy.OutputField(desc="Key findings")
    recommendations: list[str] = dspy.OutputField(desc="Recommended actions")
    follow_up_tasks: list[str] = dspy.OutputField(desc="Suggested follow-up tasks")
