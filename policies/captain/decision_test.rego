package captain.decision_test

import rego.v1

import data.captain.decision

base_input(tier, autonomy) := {
	"request": {"channel": "API", "action": "MODEL_CALL", "coverage": "GOVERNED", "model": "claude-sonnet", "tool": null, "max_output_tokens": 1000},
	"register": {"id": "AI-1", "tier": tier, "autonomy": autonomy, "allowed_models": ["claude-sonnet"], "allowed_tools": ["jira.create_issue"]},
	"findings": [],
}

tool_input(tier, autonomy, tool) := object.union(base_input(tier, autonomy), {"request": {"channel": "MCP", "action": "TOOL_CALL", "coverage": "GOVERNED", "model": null, "tool": tool, "max_output_tokens": null}})

with_findings(inp, fs) := object.union(inp, {"findings": fs})

injection := [{"type": "INJECTION", "entity": "prompt_injection", "score": 0.9}]

test_allow_tier_3_and_4 if {
	# Regression for CTO F14: Tier 3–4 must not fall through to a deny.
	decision.result.decision == "ALLOW" with input as base_input("TIER_3", "A1")
	decision.result.decision == "ALLOW" with input as base_input("TIER_4", "A1")
}

test_model_not_allowed if {
	inp := object.union(base_input("TIER_2", "A2"), {"request": {"model": "other"}})
	decision.result.reason_code == "MODEL_NOT_ALLOWED" with input as inp
}

test_injection_blocked_tier_1_2 if {
	decision.result.reason_code == "INJECTION_DETECTED" with input as with_findings(base_input("TIER_1", "A1"), injection)
	decision.result.reason_code == "INJECTION_DETECTED" with input as with_findings(base_input("TIER_2", "A1"), injection)
}

test_injection_logged_not_blocked_tier_3_4 if {
	decision.result.decision == "ALLOW" with input as with_findings(base_input("TIER_3", "A1"), injection)
	decision.result.decision == "ALLOW" with input as with_findings(base_input("TIER_4", "A1"), injection)
}

test_secret_blocked_with_specific_reason if {
	fs := [{"type": "SECRET", "entity": "AWS_ACCESS_KEY", "score": 1}]
	decision.result.reason_code == "SECRET_DETECTED" with input as with_findings(base_input("TIER_2", "A1"), fs)
}

test_pii_masked_tier_1_3_logged_tier_4 if {
	fs := [{"type": "PII", "entity": "EMAIL_ADDRESS", "score": 1}]
	decision.result.decision == "ALLOW_WITH_REDACTION" with input as with_findings(base_input("TIER_1", "A1"), fs)
	decision.result.decision == "ALLOW_WITH_REDACTION" with input as with_findings(base_input("TIER_3", "A1"), fs)
	decision.result.decision == "ALLOW" with input as with_findings(base_input("TIER_4", "A1"), fs)
}

test_output_cap_applied_when_missing if {
	inp := object.union(base_input("TIER_2", "A1"), {"request": {"max_output_tokens": null}})
	decision.result.decision == "ALLOW_WITH_LIMITS" with input as inp
	decision.result.limits.max_output_tokens == 4096 with input as inp
}

test_tool_not_allowlisted if {
	decision.result.reason_code == "TOOL_NOT_ALLOWED" with input as tool_input("TIER_2", "A2", "erp.create_po")
}

test_a3_tool_needs_approval_tier_2 if {
	decision.result.decision == "REQUIRE_APPROVAL" with input as tool_input("TIER_2", "A3", "jira.create_issue")
}

test_a2_tool_allowed if {
	decision.result.decision == "ALLOW" with input as tool_input("TIER_2", "A2", "jira.create_issue")
}

test_tier_3_approval_only_for_high_impact if {
	low := {"captain": {"tools": {"jira.create_issue": {"impact": "low"}}}}
	decision.result.decision == "ALLOW" with input as tool_input("TIER_3", "A3", "jira.create_issue") with data.captain.tools as low.captain.tools
	decision.result.decision == "REQUIRE_APPROVAL" with input as tool_input("TIER_3", "A3", "jira.create_issue")
}

test_observed_events if {
	inp := object.union(base_input("TIER_1", "A1"), {"request": {"coverage": "OBSERVED"}})
	decision.result.decision == "OBSERVE_ONLY" with input as inp
}

test_block_beats_redaction if {
	fs := array.concat(injection, [{"type": "PII", "entity": "EMAIL_ADDRESS", "score": 1}])
	decision.result.reason_code == "INJECTION_DETECTED" with input as with_findings(base_input("TIER_1", "A1"), fs)
}

test_result_always_names_version if {
	decision.result.policy_version == "0.1.0" with input as base_input("TIER_4", "A0")
}

test_registry_tool_impact_drives_tier_3_approval if {
	low := object.union(tool_input("TIER_3", "A3", "jira.create_issue"), {"tool": {"impact": "low", "risk": "LOW", "read_only": true}})
	decision.result.decision == "ALLOW" with input as low
	high := object.union(tool_input("TIER_3", "A3", "jira.create_issue"), {"tool": {"impact": "high", "risk": "HIGH", "read_only": false}})
	decision.result.decision == "REQUIRE_APPROVAL" with input as high
}
