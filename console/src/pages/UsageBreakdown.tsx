import { Badge, Button, Group, Paper, Progress, ScrollArea, SegmentedControl, Select, SimpleGrid, Stack, Table, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type UsageBreakdown } from "../api";
import { PageHeader, QueryState, StatCard } from "../components/common";
import { money } from "../format";

const DIMENSIONS = [
  { value: "principal", label: "People", singular: "Person" },
  { value: "project", label: "Projects", singular: "Project" },
  { value: "app", label: "Applications", singular: "Application" },
  { value: "cost_centre", label: "Cost centres", singular: "Cost centre" },
  { value: "team", label: "Teams", singular: "Team" },
  { value: "model", label: "Models", singular: "Model" },
];

export function UsageBreakdownPage() {
  const [by, setBy] = useState("principal");
  const [days, setDays] = useState("30");
  const q = useQuery({ queryKey: ["usage-breakdown", by, days], queryFn: () => api.get<UsageBreakdown>(`/usage/breakdown?by=${by}&days=${days}`) });
  const d = q.data;
  const dim = DIMENSIONS.find((x) => x.value === by) ?? DIMENSIONS[0]!;
  const calls = d?.items.reduce((a, i) => a + i.calls, 0) ?? 0;

  return (
    <>
      <PageHeader
        title="Usage by…"
        description="Who and what the governed spend belongs to. Only calls routed through Captain are counted here; vendor-reported usage is on the Observed usage page."
        actions={
          <Group>
          <Button component="a" variant="default" href={`/admin/v1/usage/breakdown.csv?by=${by}&days=${days}`} download>
            Download CSV
          </Button>
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
          </Group>
        }
      />
      <Stack>
        <SegmentedControl value={by} onChange={setBy} data={DIMENSIONS.map(({ value, label }) => ({ value, label }))} aria-label="Group by" />
        <QueryState isLoading={q.isLoading} error={q.error} empty={d?.items.length === 0} emptyText="No governed usage in this period.">
          {d && (
            <>
              <SimpleGrid cols={{ base: 1, xs: 3 }}>
                <StatCard label="Governed spend" value={money(d.total_cost_micros)} hint={`last ${d.period_days} days`} />
                <StatCard label="Calls" value={calls.toLocaleString()} />
                <StatCard label={`${dim.label} with usage`} value={d.items.length.toLocaleString()} />
              </SimpleGrid>
              <Paper withBorder p="md" radius="md">
                <ScrollArea>
                  <Table striped miw={760}>
                    <Table.Thead>
                      <Table.Tr>
                        <Table.Th>{dim.singular}</Table.Th>
                        <Table.Th ta="right">Calls</Table.Th>
                        {by === "principal" && <Table.Th ta="right">Refused</Table.Th>}
                        <Table.Th ta="right">Tokens in / out</Table.Th>
                        <Table.Th ta="right">Cost</Table.Th>
                        <Table.Th w={180}>Share of spend</Table.Th>
                      </Table.Tr>
                    </Table.Thead>
                    <Table.Tbody>
                      {d.items.map((i) => (
                        <Table.Tr key={i.key}>
                          <Table.Td>{i.key}</Table.Td>
                          <Table.Td ta="right">{i.calls.toLocaleString()}</Table.Td>
                          {by === "principal" && <Table.Td ta="right">{i.refused ? <Badge color="red" variant="light">{i.refused}</Badge> : 0}</Table.Td>}
                          <Table.Td ta="right">{`${i.tokens_in.toLocaleString()} / ${i.tokens_out.toLocaleString()}`}</Table.Td>
                          <Table.Td ta="right">
                            {money(i.cost_micros)}
                            {i.estimated_cost_micros > 0 ? " (part est.)" : ""}
                          </Table.Td>
                          <Table.Td>
                            <Group gap="xs" wrap="nowrap">
                              <Progress value={i.share_pct} w={110} aria-label={`${i.share_pct}% of spend`} />
                              <Text size="xs" c="dimmed">
                                {i.share_pct}%
                              </Text>
                            </Group>
                          </Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                </ScrollArea>
              </Paper>
            </>
          )}
        </QueryState>
      </Stack>
    </>
  );
}
