import { Badge, Button, Group, Paper, Progress, ScrollArea, SegmentedControl, Select, SimpleGrid, Stack, Table, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type UsageBreakdown, type UserView } from "../api";
import { PageHeader, QueryState, StatCard } from "../components/common";
import { money } from "../format";

/** Tabs: who the spend belongs to. People and Apps both come from the key's owner
 * (an app's key belongs to "app:<name>"); Teams from the team chosen on the key. */
const DIMENSIONS = [
  { value: "people", by: "principal", label: "People", singular: "Person" },
  { value: "teams", by: "project", label: "Teams", singular: "Team" },
  { value: "apps", by: "principal", label: "Apps", singular: "App" },
  { value: "models", by: "model", label: "AI models", singular: "Model" },
];

export function UsageBreakdownPage() {
  const [tab, setTab] = useState("people");
  const [days, setDays] = useState("30");
  const dim = DIMENSIONS.find((x) => x.value === tab) ?? DIMENSIONS[0]!;
  const by = dim.by;
  const q = useQuery({ queryKey: ["usage-breakdown", by, days], queryFn: () => api.get<UsageBreakdown>(`/usage/breakdown?by=${by}&days=${days}`) });
  const people = useQuery({ queryKey: ["users"], queryFn: () => api.get<UserView[]>("/users") });
  const raw = q.data;
  const items = (raw?.items ?? []).filter((i) =>
    tab === "apps" ? i.key.startsWith("app:") : tab === "people" ? !i.key.startsWith("app:") : true,
  );
  const total = items.reduce((a, i) => a + i.cost_micros, 0);
  const d = raw && { ...raw, total_cost_micros: total, items: items.map((i) => ({ ...i, share_pct: total ? Math.round((i.cost_micros * 1000) / total) / 10 : 0 })) };
  const label = (k: string) =>
    tab === "apps" ? k.slice(4) : tab === "people" ? (people.data?.find((u) => u.id === k)?.name ?? k) : k;
  const calls = d?.items.reduce((a, i) => a + i.calls, 0) ?? 0;

  return (
    <>
      <PageHeader
        title="Usage"
        description="AI calls and cost by person, team and app. Each call counts for the person or app that owns the Captain key and the team chosen on that key."
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
        <SegmentedControl value={tab} onChange={setTab} data={DIMENSIONS.map(({ value, label }) => ({ value, label }))} aria-label="Group by" />
        <QueryState isLoading={q.isLoading} error={q.error} empty={d?.items.length === 0} emptyText="No AI calls in this period.">
          {d && (
            <>
              <SimpleGrid cols={{ base: 1, xs: 3 }}>
                <StatCard label="Spend" value={money(d.total_cost_micros)} hint={`last ${d.period_days} days`} />
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
                        {by === "principal" && <Table.Th ta="right">Stopped</Table.Th>}
                        <Table.Th ta="right">Tokens in / out</Table.Th>
                        <Table.Th ta="right">Cost</Table.Th>
                        <Table.Th w={180}>Share of spend</Table.Th>
                      </Table.Tr>
                    </Table.Thead>
                    <Table.Tbody>
                      {d.items.map((i) => (
                        <Table.Tr key={i.key}>
                          <Table.Td>{label(i.key)}</Table.Td>
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
