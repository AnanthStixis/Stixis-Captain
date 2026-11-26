import { Alert, Badge, Button, Checkbox, Group, MultiSelect, Paper, ScrollArea, Select, SimpleGrid, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type PolicyInfo, type SimulationResult } from "../api";
import { DecisionBadge, PageHeader, QueryState, notifyError } from "../components/common";

const TIERS = ["TIER_1", "TIER_2", "TIER_3", "TIER_4"];
const AUTONOMY = ["A0", "A1", "A2", "A3", "A4"];
const CHANNELS = ["API", "SDK", "MCP", "IDE", "BROWSER", "HOOK", "DESKTOP"];
const FINDINGS = ["PII", "SECRET", "INJECTION", "UNSAFE", "BANNED_TERM", "SIZE_LIMIT"];

function cell(v: unknown): string {
  if (Array.isArray(v)) return v.join(", ") || "none";
  return v === undefined || v === null ? "–" : String(v);
}

export function PolicyPage() {
  const q = useQuery({ queryKey: ["policy"], queryFn: () => api.get<PolicyInfo>("/policy"), retry: false });
  const [tier, setTier] = useState("TIER_3");
  const [autonomy, setAutonomy] = useState("A2");
  const [channel, setChannel] = useState("API");
  const [action, setAction] = useState("MODEL_CALL");
  const [model, setModel] = useState("claude-sonnet");
  const [allowedModels, setAllowedModels] = useState("claude-sonnet");
  const [tool, setTool] = useState("");
  const [impact, setImpact] = useState<string | null>(null);
  const [findings, setFindings] = useState<string[]>([]);
  const [highRisk, setHighRisk] = useState(false);
  const sim = useMutation({
    mutationFn: () =>
      api.post<SimulationResult>("/policy/simulate", {
        tier,
        autonomy,
        channel,
        action,
        model: action === "MODEL_CALL" ? model : null,
        tool: action === "TOOL_CALL" && tool ? tool : null,
        tool_impact: action === "TOOL_CALL" ? impact : null,
        allowed_models: allowedModels.split(",").map((s) => s.trim()).filter(Boolean),
        allowed_tools: tool ? [tool] : [],
        high_risk: highRisk,
        findings: findings.map((type) => ({ type, score: 0.9 })),
      }),
    onError: notifyError,
  });
  const tiers = q.data?.tiers ?? {};
  const columns = Array.from(new Set(Object.values(tiers).flatMap((t) => Object.keys(t))));

  return (
    <>
      <PageHeader title="Policy" description="The policy that is active right now, and a safe way to ask what it would decide. Nothing here executes, charges or records anything." />
      <Stack>
        <Alert color="blue" variant="light" title="How policy changes are made">
          Policy is changed in the repository, reviewed by two named approvers, signed, and deployed as a signed bundle; OPA refuses an unsigned or altered one (ADR-003). Rolling back means deploying the previous signed bundle. This page shows what is active and lets you test it; it cannot change it.
        </Alert>
        <QueryState isLoading={q.isLoading} error={q.error} empty={false}>
          {q.data && (
            <Paper withBorder p="md" radius="md">
              <Group justify="space-between" mb="xs">
                <Title order={5}>Active policy</Title>
                <Badge variant="light">
                  {cell(q.data.bundle?.name)} @ {cell(q.data.bundle?.version)}
                </Badge>
              </Group>
              <ScrollArea>
                <Table striped miw={900}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Tier</Table.Th>
                      {columns.map((c) => (
                        <Table.Th key={c}>{c.replaceAll("_", " ")}</Table.Th>
                      ))}
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {Object.entries(tiers).map(([name, t]) => (
                      <Table.Tr key={name}>
                        <Table.Td>{name.replace("_", " ")}</Table.Td>
                        {columns.map((c) => (
                          <Table.Td key={c}>{cell(t[c])}</Table.Td>
                        ))}
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </ScrollArea>
            </Paper>
          )}
        </QueryState>
        <Paper withBorder p="md" radius="md">
          <Title order={5} mb="xs">
            What would the policy decide?
          </Title>
          <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }}>
            <Select label="Risk tier" data={TIERS} value={tier} onChange={(v) => setTier(v ?? "TIER_3")} allowDeselect={false} />
            <Select label="Autonomy" data={AUTONOMY} value={autonomy} onChange={(v) => setAutonomy(v ?? "A2")} allowDeselect={false} />
            <Select label="Channel" data={CHANNELS} value={channel} onChange={(v) => setChannel(v ?? "API")} allowDeselect={false} />
            <Select label="Action" data={["MODEL_CALL", "TOOL_CALL"]} value={action} onChange={(v) => setAction(v ?? "MODEL_CALL")} allowDeselect={false} />
            {action === "MODEL_CALL" ? (
              <>
                <TextInput label="Model requested" value={model} onChange={(e) => setModel(e.currentTarget.value)} />
                <TextInput label="Models the system may use" description="comma separated" value={allowedModels} onChange={(e) => setAllowedModels(e.currentTarget.value)} />
              </>
            ) : (
              <>
                <TextInput label="Tool" placeholder="server.tool_name" value={tool} onChange={(e) => setTool(e.currentTarget.value)} />
                <Select label="Tool impact" data={["low", "high"]} value={impact} onChange={setImpact} clearable placeholder="unknown (treated as high)" />
              </>
            )}
            <MultiSelect label="Content findings" description="what the guardrails found" data={FINDINGS} value={findings} onChange={setFindings} />
            <Checkbox label="EU AI Act high-risk system" mt="xl" checked={highRisk} onChange={(e) => setHighRisk(e.currentTarget.checked)} />
          </SimpleGrid>
          <Group mt="md">
            <Button onClick={() => sim.mutate()} loading={sim.isPending}>
              Ask the policy
            </Button>
          </Group>
          {sim.data && (
            <Paper withBorder p="md" mt="md" radius="md" bg="var(--mantine-color-gray-0)">
              <Group gap="sm" mb={4}>
                <DecisionBadge value={sim.data.decision} />
                <Badge variant="outline">{sim.data.reason_code}</Badge>
                <Text size="xs" c="dimmed">
                  {sim.data.policy_bundle}@{sim.data.policy_version}
                </Text>
              </Group>
              <Text size="sm">{sim.data.explanation}</Text>
              {sim.data.limits && Object.keys(sim.data.limits).length > 0 && (
                <Text size="xs" c="dimmed" mt={4}>
                  Limits: {Object.entries(sim.data.limits).map(([k, v]) => `${k} = ${cell(v)}`).join(" · ")}
                </Text>
              )}
              <Text size="xs" c="dimmed" mt={4}>
                Simulated: nothing was executed, charged or recorded.
              </Text>
            </Paper>
          )}
        </Paper>
      </Stack>
    </>
  );
}
