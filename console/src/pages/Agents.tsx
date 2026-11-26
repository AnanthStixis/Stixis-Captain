import { Badge, Group, Paper, ScrollArea, Select, Table, Text, Tooltip } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type AgentRow } from "../api";
import { PageHeader, QueryState } from "../components/common";

export function AgentsPage() {
  const [days, setDays] = useState("30");
  const q = useQuery({ queryKey: ["agents", days], queryFn: () => api.get<AgentRow[]>(`/agents?days=${days}`) });

  return (
    <>
      <PageHeader
        title="Agents & systems"
        description="Each registered AI system: what it is allowed to use, and what it actually did. Evaluation is the average judge score (0 to 1) of the sampled Tier 1-2 calls, when live evaluation is on."
        actions={
          <Select
            aria-label="Period"
            value={days}
            onChange={(v) => setDays(v ?? "30")}
            data={[
              { value: "7", label: "Last 7 days" },
              { value: "30", label: "Last 30 days" },
              { value: "90", label: "Last 90 days" },
            ]}
            w={170}
          />
        }
      />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No systems registered.">
        <Paper withBorder p="md" radius="md">
          <ScrollArea>
            <Table striped miw={1080}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>System</Table.Th>
                  <Table.Th>Tier / autonomy</Table.Th>
                  <Table.Th ta="right">Calls</Table.Th>
                  <Table.Th ta="right">Refused</Table.Th>
                  <Table.Th ta="right">Tools run</Table.Th>
                  <Table.Th ta="right">Held for approval</Table.Th>
                  <Table.Th ta="right">Evaluation</Table.Th>
                  <Table.Th>Allowed tools</Table.Th>
                  <Table.Th>Last active</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {q.data?.map((a) => (
                  <Table.Tr key={a.id}>
                    <Table.Td>
                      <Text fw={500}>{a.name}</Text>
                      <Text size="xs" c="dimmed">
                        {a.id} · owner {a.owner_id}
                        {a.active ? "" : " · retired"}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Group gap={4} wrap="nowrap">
                        <Badge miw="max-content" variant="light">
                          {a.tier.replace("_", " ")}
                        </Badge>
                        <Badge miw="max-content" variant="outline">
                          {a.autonomy}
                        </Badge>
                      </Group>
                    </Table.Td>
                    <Table.Td ta="right">{a.calls.toLocaleString()}</Table.Td>
                    <Table.Td ta="right">{a.refused ? <Badge color="red" variant="light">{a.refused}</Badge> : 0}</Table.Td>
                    <Table.Td ta="right">{a.tools_run.toLocaleString()}</Table.Td>
                    <Table.Td ta="right">{a.tools_held ? <Badge color="yellow" variant="light">{a.tools_held}</Badge> : 0}</Table.Td>
                    <Table.Td ta="right">
                      <EvalCell a={a} />
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{a.allowed_tools.length ? a.allowed_tools.join(", ") : "—"}</Text>
                    </Table.Td>
                    <Table.Td>{a.last_active ? new Date(a.last_active).toLocaleString() : "never"}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        </Paper>
      </QueryState>
    </>
  );
}

function EvalCell({ a }: { a: AgentRow }) {
  if (!a.eval_scored && !a.eval_failed) {
    return <Text size="sm" c="dimmed">—</Text>;
  }
  const color = a.eval_mean === null ? "gray" : a.eval_low ? "orange" : "green";
  return (
    <Tooltip
      label={`${a.eval_scored} scored · ${a.eval_low} below threshold · ${a.eval_failed} judge failures`}
    >
      <Group gap={4} justify="flex-end" wrap="nowrap">
        {a.eval_mean !== null && (
          <Badge color={color} variant="light">
            {a.eval_mean.toFixed(2)}
          </Badge>
        )}
        {a.eval_low > 0 && <Text size="xs" c="orange">{a.eval_low} low</Text>}
        {a.eval_failed > 0 && <Text size="xs" c="dimmed">{a.eval_failed} failed</Text>}
      </Group>
    </Tooltip>
  );
}
