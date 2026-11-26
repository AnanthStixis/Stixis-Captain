import { BarChart } from "@mantine/charts";
import { Alert, Badge, Paper, ScrollArea, SegmentedControl, SimpleGrid, Stack, Table, Text, Title } from "@mantine/core";
import { IconEye } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type ObservedUsage } from "../api";
import { PageHeader, QueryState, StatCard } from "../components/common";
import { money, sourceLabel } from "../format";

export function ObservedPage() {
  const [days, setDays] = useState("30");
  const q = useQuery({ queryKey: ["observed", days], queryFn: () => api.get<ObservedUsage>(`/observed?days=${days}`) });
  const o = q.data;

  return (
    <>
      <PageHeader
        title="Observed usage"
        description="AI usage that happened outside Captain, reported by vendors and coding tools after the fact."
        actions={
          <SegmentedControl
            value={days}
            onChange={setDays}
            aria-label="Period"
            data={[
              { label: "7 days", value: "7" },
              { label: "30 days", value: "30" },
              { label: "90 days", value: "90" },
            ]}
          />
        }
      />
      <QueryState isLoading={q.isLoading} error={q.error} empty={o?.totals.events === 0} emptyText="No observed usage in this period. Import vendor usage or install the coding-tool hook.">
        {o && (
          <Stack>
            <Alert color="yellow" icon={<IconEye size={16} />} title="Observed is not governed">
              {o.note}
            </Alert>
            <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }}>
              <StatCard label="Observed events" value={o.totals.events.toLocaleString()} hint={`${o.totals.sources} source(s)`} />
              <StatCard label="People / keys" value={o.totals.principals.toLocaleString()} hint={`${o.totals.unmapped_events} events not matched to a Captain principal`} color={o.totals.unmapped_events ? "orange" : undefined} />
              <StatCard label="Vendor-reported cost" value={money(o.totals.cost_micros)} hint="Not added to governed spend" />
              <StatCard label="Governed spend (same period)" value={money(o.governed_spend_micros)} hint="Shown for comparison only; the two may overlap" />
            </SimpleGrid>

            {o.daily.length > 0 && (
              <Paper withBorder p="md" radius="md">
                <Title order={5} mb="sm">
                  Observed events per day
                </Title>
                <BarChart h={200} data={o.daily} dataKey="day" series={[{ name: "events", color: "yellow.6" }]} withLegend={false} />
              </Paper>
            )}

            <Paper withBorder p="md" radius="md">
              <Title order={5} mb="sm">
                By source
              </Title>
              <ScrollArea>
                <Table striped miw={760}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Source</Table.Th>
                      <Table.Th>How it arrives</Table.Th>
                      <Table.Th ta="right">Events</Table.Th>
                      <Table.Th ta="right">People / keys</Table.Th>
                      <Table.Th ta="right">Tokens in / out</Table.Th>
                      <Table.Th ta="right">Cost (vendor)</Table.Th>
                      <Table.Th>Days covered</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {o.by_source.map((s) => (
                      <Table.Tr key={s.source}>
                        <Table.Td>{sourceLabel(s.source)}</Table.Td>
                        <Table.Td>
                          <Badge variant="light" color={s.kind === "hook" ? "blue" : "grape"}>
                            {s.kind === "hook" ? "hook" : "import"}
                          </Badge>
                        </Table.Td>
                        <Table.Td ta="right">{s.events.toLocaleString()}</Table.Td>
                        <Table.Td ta="right">{s.principals}</Table.Td>
                        <Table.Td ta="right">{s.tokens_in || s.tokens_out ? `${s.tokens_in.toLocaleString()} / ${s.tokens_out.toLocaleString()}` : "—"}</Table.Td>
                        <Table.Td ta="right">{s.cost_micros ? money(s.cost_micros) : "—"}</Table.Td>
                        <Table.Td>{s.first_day === s.last_day ? s.first_day : `${s.first_day} to ${s.last_day}`}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </ScrollArea>
            </Paper>

            <Paper withBorder p="md" radius="md">
              <Title order={5} mb={4}>
                Top people and keys
              </Title>
              <Text size="xs" c="dimmed" mb="sm">
                Identities that are not in the actor map appear as a short hash, never as the vendor's email or login.
              </Text>
              <ScrollArea>
                <Table striped miw={700}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Principal</Table.Th>
                      <Table.Th>Source</Table.Th>
                      <Table.Th ta="right">Events</Table.Th>
                      <Table.Th ta="right">Tokens in / out</Table.Th>
                      <Table.Th ta="right">Cost (vendor)</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {o.actors.map((a) => (
                      <Table.Tr key={`${a.source}|${a.principal}`}>
                        <Table.Td>
                          {a.principal}{" "}
                          {!a.mapped && (
                            <Badge size="xs" color="orange" variant="light">
                              not matched
                            </Badge>
                          )}
                        </Table.Td>
                        <Table.Td>{sourceLabel(a.source)}</Table.Td>
                        <Table.Td ta="right">{a.events.toLocaleString()}</Table.Td>
                        <Table.Td ta="right">{a.tokens_in || a.tokens_out ? `${a.tokens_in.toLocaleString()} / ${a.tokens_out.toLocaleString()}` : "—"}</Table.Td>
                        <Table.Td ta="right">{a.cost_micros ? money(a.cost_micros) : "—"}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </ScrollArea>
            </Paper>
          </Stack>
        )}
      </QueryState>
    </>
  );
}
