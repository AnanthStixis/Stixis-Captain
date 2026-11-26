import { Accordion, Badge, Code, Table, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";

import { api, type Alert } from "../api";
import { PageHeader, QueryState } from "../components/common";
import { when } from "../format";

const SEVERITY: Record<string, { label: string; color: string }> = {
  CRITICAL: { label: "Urgent", color: "red" },
  WARNING: { label: "Needs attention", color: "yellow" },
  INFO: { label: "For information", color: "gray" },
};

export function AlertsPage() {
  const q = useQuery({ queryKey: ["alerts"], queryFn: () => api.get<Alert[]>("/alerts?limit=200") });
  return (
    <>
      <PageHeader
        title="Alerts"
        description="Things that need someone's attention: budgets nearly used up, safety controls weakened, approvals waiting too long, and problems recording evidence. Each alert says what to do."
      />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No alerts. Nothing needs attention.">
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>When</Table.Th>
              <Table.Th>How urgent</Table.Th>
              <Table.Th>What happened</Table.Th>
              <Table.Th>What to do</Table.Th>
              <Table.Th>Notified</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {q.data?.map((a) => {
              const sev = SEVERITY[a.severity] ?? { label: a.severity, color: "gray" };
              return (
                <Table.Tr key={a.id}>
                  <Table.Td miw={150}>{when(a.created_at)}</Table.Td>
                  <Table.Td>
                    <Badge color={sev.color} variant="light" miw="max-content">
                      {sev.label}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    <Text fw={600} size="sm">
                      {a.title}
                    </Text>
                    <Text size="sm">{a.subject}</Text>
                    <Accordion variant="default" chevronPosition="left" mt={4}>
                      <Accordion.Item value="tech" style={{ border: 0 }}>
                        <Accordion.Control p={0}>
                          <Text size="xs" c="dimmed">
                            Technical details
                          </Text>
                        </Accordion.Control>
                        <Accordion.Panel>
                          <Code>{a.kind}</Code>
                          <Text size="xs" mt={4}>
                            {Object.entries(a.details)
                              .map(([k, v]) => `${k}: ${String(v)}`)
                              .join(" · ")}
                          </Text>
                        </Accordion.Panel>
                      </Accordion.Item>
                    </Accordion>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{a.what_to_do}</Text>
                  </Table.Td>
                  <Table.Td>
                    {a.delivered ? (
                      <Badge color="teal" variant="light">
                        Sent
                      </Badge>
                    ) : (
                      <Badge color="orange" variant="light">
                        Not sent yet ({a.attempts} tries)
                      </Badge>
                    )}
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </QueryState>
    </>
  );
}
