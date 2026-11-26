import { Badge, Code, Table, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";

import { api, type Alert } from "../api";
import { PageHeader, QueryState } from "../components/common";
import { when } from "../format";

export function AlertsPage() {
  const q = useQuery({ queryKey: ["alerts"], queryFn: () => api.get<Alert[]>("/alerts?limit=200") });
  return (
    <>
      <PageHeader title="Alerts" description="Budget thresholds, overrides, control downgrades, escalated approvals and ledger lag. Every alert is also recorded in the evidence ledger." />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No alerts.">
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>When</Table.Th>
              <Table.Th>Severity</Table.Th>
              <Table.Th>Alert</Table.Th>
              <Table.Th>Details</Table.Th>
              <Table.Th>Delivered</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {q.data?.map((a) => (
              <Table.Tr key={a.id}>
                <Table.Td>{when(a.created_at)}</Table.Td>
                <Table.Td>
                  <Badge color={a.severity === "CRITICAL" ? "red" : a.severity === "WARNING" ? "yellow" : "gray"}>{a.severity}</Badge>
                </Table.Td>
                <Table.Td>
                  <Text fw={600}>{a.subject}</Text>
                  <Code>{a.kind}</Code>
                </Table.Td>
                <Table.Td>
                  <Text size="xs">
                    {Object.entries(a.details)
                      .map(([k, v]) => `${k}: ${String(v)}`)
                      .join(" · ")}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {a.delivered ? <Badge color="teal" variant="light">sent</Badge> : <Badge color="orange" variant="light">pending ({a.attempts})</Badge>}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </QueryState>
    </>
  );
}
