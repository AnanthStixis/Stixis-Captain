# Captain tier × autonomy decision policy.
#
# Input contract: backend/captain/core/policy/opa.py (build_input).
# Output: exactly one result object. Rules form a single ordered `else` chain so two
# rules can never both match (CTO F14), and every outcome names a specific reason.
package captain.decision

import rego.v1

tier := data.captain.tiers[input.register.tier]

bundle := data.captain.bundle

# Tool impact comes from the Model/Tool Registry facts Captain sends in input.tool;
# a static data entry is a fallback; anything unknown is treated as high impact.
tool_impact := input.tool.impact if {
	input.tool.impact
} else := data.captain.tools[input.request.tool].impact if {
	input.request.tool
} else := "high"

secret_found if {
	some f in input.findings
	f.type == "SECRET"
}

injection_found if {
	some f in input.findings
	f.type == "INJECTION"
	f.score >= tier.injection_threshold
}

pii_found if {
	some f in input.findings
	f.type == "PII"
}

needs_approval if {
	input.request.action == "TOOL_CALL"
	input.register.autonomy in tier.approval_autonomy
	tier.approval_scope == "all"
}

needs_approval if {
	input.request.action == "TOOL_CALL"
	input.register.autonomy in tier.approval_autonomy
	tier.approval_scope == "high_impact"
	tool_impact == "high"
}

# Missing, null or non-numeric max_output_tokens all get the tier cap
# (`not x` alone is false for null in Rego, which would skip the cap).
output_capped if {
	input.request.action == "MODEL_CALL"
	not is_number(input.request.max_output_tokens)
}

output_capped if {
	input.request.action == "MODEL_CALL"
	input.request.max_output_tokens > tier.max_output_tokens
}

limits := {"max_output_tokens": tier.max_output_tokens, "streaming": tier.streaming}

tier_id := sprintf("captain.tier.%s", [input.register.tier])

out(decision, code, msg, rule) := {
	"decision": decision,
	"reason_code": code,
	"explanation": msg,
	"policy_ids": [tier_id, sprintf("captain.rule.%s", [rule])],
	"policy_bundle": bundle.name,
	"policy_version": bundle.version,
	"limits": limits,
}

result := out("OBSERVE_ONLY", "OBSERVED_ONLY", "Event was observed, not routed through Captain; no pre-enforcement was possible.", "coverage") if {
	input.request.coverage != "GOVERNED"
} else := out("BLOCK", "TOOL_NOT_ALLOWED", sprintf("Tool '%s' is not allowlisted for %s.", [input.request.tool, input.register.id]), "tool_allowlist") if {
	input.request.action == "TOOL_CALL"
	not input.request.tool in input.register.allowed_tools
} else := out("BLOCK", "MODEL_NOT_ALLOWED", sprintf("Model '%s' is not approved for %s.", [input.request.model, input.register.id]), "model_allowlist") if {
	input.request.action == "MODEL_CALL"
	not input.request.model in input.register.allowed_models
} else := out("BLOCK", "SECRET_DETECTED", sprintf("A secret or credential was detected; %s blocks secrets.", [input.register.tier]), "secrets") if {
	secret_found
	tier.secrets == "block"
} else := out("BLOCK", "INJECTION_DETECTED", sprintf("Prompt injection detected above the %s threshold.", [input.register.tier]), "injection") if {
	injection_found
	tier.injection == "block"
} else := out("REQUIRE_APPROVAL", "APPROVAL_REQUIRED", sprintf("%s actions on %s tools require human approval.", [input.register.autonomy, tool_impact]), "approval") if {
	needs_approval
} else := out("ALLOW_WITH_REDACTION", "PII_REDACTED", "Personal data will be masked before the request leaves Captain.", "pii_mask") if {
	pii_found
	tier.pii == "mask"
} else := out("ALLOW_WITH_LIMITS", "ALLOWED", sprintf("Allowed; output capped at %d tokens for %s.", [tier.max_output_tokens, input.register.tier]), "output_cap") if {
	output_capped
} else := out("ALLOW", "ALLOWED", "All policy checks passed.", "allow")
