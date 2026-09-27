"""Built-in, OPT-IN integrations shipped with the Harness itself.

Nothing under `agentco_harness.ext` is imported by the runtime on its own —
each module here is a `register_source_factory` (or cycle-handler, or
completion-hook) provider an operator turns on by naming it in `extensions:`
in config.yaml, exactly like an operator's own private integration module.
The only difference from a company's own extension is that these ship in the
package, because they name a vendor (GitHub) rather than a company.
"""
