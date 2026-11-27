import { BarChart } from "@mantine/charts";
import { Badge, Group, Paper, SegmentedControl, SimpleGrid, Stack, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Overview } from "../api";
import { PageHeader, QueryState, StatCard } from "../components/common";
import { money, when } from "../format";

export function OverviewPage() {
  const [days, setDays] = useState("30");
  const q = useQuery({ queryKey: ["overview", days], queryFn: () => api.get<Overview>(`/overview?days=${days}`) });
  const o = q.data;
  const refused = o ? Object.values(o.refusals_by_reason).reduce((a, b) => a + b, 0) : 0;

  return (
    <>
      <PageHeader
        title="Overview"
        description="Governed AI usage, spend and policy outcomes across the organisation."
        actions={
          <SegmentedControl
            value={days}
            onChange={setDays}
            aria-label="Period"
            data={[
              { label: "24 h", value: "1" },
              { label: "7 days", value: "7" },
              { label: "30 days", value: "30" },
            ]}
          />
        }
      />
      <QueryState isLoading={q.isLoading} error={q.error}>
        {o && (
          <Stack>
            <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }}>
              <StatCard label="AI calls" value={o.governed_calls.toLocaleString()} />
              <StatCard label="Refused" value={refused.toLocaleString()} color={refused ? "red" : undefined} hint="Stopped by your rules" />
              <StatCard
                label="Spend"
                value={money(o.spend_micros)}
                hint={o.estimated_spend_micros ? `includes ${money(o.estimated_spend_micros)} estimated` : undefined}
              />
              <StatCard label="Approvals pending" value={o.approvals_pending} color={o.approvals_pending ? "yellow" : undefined} />
            </SimpleGrid>

            <Paper withBorder p="md">
              <Title order={4} mb="sm">
                Calls per day
              </Title>
              {o.daily.length ? (
                <BarChart
                  h={240}
                  data={o.daily}
                  dataKey="day"
                  series={[
                    { name: "calls", color: "indigo.6", label: "Calls" },
                    { name: "refused", color: "red.6", label: "Refused" },
                  ]}
                />
              ) : (
                <Text c="dimmed">No calls in this period.</Text>
              )}
            </Paper>

            <SimpleGrid cols={{ base: 1, md: 2 }}>
              <Paper withBorder p="md">
                <Title order={4} mb="sm">
                  Why calls were stopped
                </Title>
                <Table>
                  <Table.Tbody>
                    {Object.entries(o.refusals_by_reason).map(([k, v]) => (
                      <Table.Tr key={k}>
                        <Table.Td>{k.replaceAll("_", " ")}</Table.Td>
                        <Table.Td ta="right">{v}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
                {!refused && <Text c="dimmed">No refusals.</Text>}
              </Paper>
              <Paper withBorder p="md">
                <Title order={4} mb="sm">
                  
                  Spend by model
                </Title>
                <Table>
                  <Table.Tbody>
                    {Object.entries(o.spend_by_model).map(([k, v]) => (
                      <Table.Tr key={k}>
                        <Table.Td>{k}</Table.Td>
                        <Table.Td ta="right">{money(v)}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Paper>
            </SimpleGrid>

            <Paper withBorder p="md">
              <Title order={4} mb="sm">
                Recent alerts
              </Title>
              {o.alerts_recent.length ? (
                o.alerts_recent.map((a) => (
                  <Group key={a.id} justify="space-between" py={4}>
                    <Group gap="xs">
                      <Badge color={a.severity === "CRITICAL" ? "red" : a.severity === "WARNING" ? "yellow" : "gray"}>{a.severity}</Badge>
                      <Text size="sm">{a.subject}</Text>
                    </Group>
                    <Text size="xs" c="dimmed">
                      {when(a.created_at)}
                    </Text>
                  </Group>
                ))
              ) : (
                <Text c="dimmed">No alerts.</Text>
              )}
            </Paper>
            <Text size="xs" c="dimmed">
              Governed: {o.definitions.governed} Observed: {o.definitions.observed} Estimated spend: {o.definitions.estimated_spend}
            </Text>
          </Stack>
        )}
      </QueryState>
    </>
  );
}
