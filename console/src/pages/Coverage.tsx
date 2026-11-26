import { Alert, Badge, Group, Paper, ScrollArea, Select, SimpleGrid, Stack, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Coverage } from "../api";
import { PageHeader, QueryState, StatCard } from "../components/common";

const LABEL: Record<string, string> = {
  GOVERNED: "Governed",
  OBSERVED: "Observed only",
  BYPASS_DETECTED: "Bypass detected",
  UNOBSERVED: "Unobserved",
};
const COLOR: Record<string, string> = { GOVERNED: "teal", OBSERVED: "blue", BYPASS_DETECTED: "red", UNOBSERVED: "gray" };
const CLASSES = ["GOVERNED", "OBSERVED", "BYPASS_DETECTED", "UNOBSERVED"] as const;

export function CoveragePage() {
  const [days, setDays] = useState("30");
  const q = useQuery({ queryKey: ["coverage", days], queryFn: () => api.get<Coverage>(`/coverage?days=${days}`) });
  const d = q.data;

  return (
    <>
      <PageHeader
        title="Coverage"
        description="How much AI activity Captain governs before it happens, versus only sees afterwards or suspects."
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
      <QueryState isLoading={q.isLoading} error={q.error} empty={d?.total_events === 0} emptyText="No activity in this period.">
        {d && (
          <Stack>
            <SimpleGrid cols={{ base: 2, md: 4 }}>
              {d.classes.map((c) => (
                <StatCard key={c.coverage} label={LABEL[c.coverage] ?? c.coverage} value={c.events.toLocaleString()} hint={`${c.share_pct}% of events`} color={COLOR[c.coverage]} />
              ))}
            </SimpleGrid>
            <Alert color="blue" variant="light">
              {d.note}
            </Alert>
            <Paper withBorder p="md" radius="md">
              <Title order={5} mb="xs">
                By channel
              </Title>
              <ScrollArea>
                <Table striped miw={560}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Channel</Table.Th>
                      {CLASSES.map((c) => (
                        <Table.Th key={c} ta="right">
                          {LABEL[c]}
                        </Table.Th>
                      ))}
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {d.by_channel.map((r) => (
                      <Table.Tr key={r.channel}>
                        <Table.Td>{r.channel}</Table.Td>
                        {CLASSES.map((c) => (
                          <Table.Td key={c} ta="right">
                            {r[c] ? r[c].toLocaleString() : "–"}
                          </Table.Td>
                        ))}
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </ScrollArea>
            </Paper>
            <Paper withBorder p="md" radius="md">
              <Group justify="space-between" mb="xs">
                <Title order={5}>By person</Title>
                <Text size="xs" c="dimmed">
                  Top {d.by_person.length}
                </Text>
              </Group>
              <ScrollArea>
                <Table striped miw={560}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Person or service</Table.Th>
                      {CLASSES.map((c) => (
                        <Table.Th key={c} ta="right">
                          {LABEL[c]}
                        </Table.Th>
                      ))}
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {d.by_person.map((r) => (
                      <Table.Tr key={r.principal_id}>
                        <Table.Td>{r.principal_id}</Table.Td>
                        {CLASSES.map((c) => (
                          <Table.Td key={c} ta="right">
                            {r[c] ? <Badge color={COLOR[c]} variant="light">{r[c].toLocaleString()}</Badge> : "–"}
                          </Table.Td>
                        ))}
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
