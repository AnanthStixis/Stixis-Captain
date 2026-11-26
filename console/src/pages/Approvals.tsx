import { Badge, Button, Group, SegmentedControl, Table, Text } from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Ticket } from "../api";
import { can, useMe } from "../App";
import { notifyOk, PageHeader, QueryState, ReasonModal } from "../components/common";
import { when } from "../format";

export function ApprovalsPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const [state, setState] = useState("PENDING");
  const q = useQuery({ queryKey: ["approvals", state], queryFn: () => api.get<Ticket[]>(`/approvals?state=${state}`) });
  const [act, setAct] = useState<{ t: Ticket; approve: boolean } | null>(null);
  const decide = can(me, "approval:decide");

  return (
    <>
      <PageHeader
        title="Approvals"
        description="High-autonomy agent actions held for a human decision. An approval applies to the exact arguments, once; you cannot approve your own request."
        actions={<SegmentedControl value={state} onChange={setState} data={["PENDING", "APPROVED", "REJECTED", "CONSUMED", "EXPIRED"]} aria-label="State" />}
      />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText={`No ${state.toLowerCase()} approvals.`}>
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Action</Table.Th>
              <Table.Th>Requested by</Table.Th>
              <Table.Th>System</Table.Th>
              <Table.Th>Approvers</Table.Th>
              <Table.Th>Expires</Table.Th>
              <Table.Th>State</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {q.data?.map((t) => {
              const mine = me && t.assigned_approvers.includes(me.id);
              return (
                <Table.Tr key={t.ticket_id}>
                  <Table.Td fw={600}>{t.action_summary}</Table.Td>
                  <Table.Td>{t.requested_by}</Table.Td>
                  <Table.Td>{t.register_id}</Table.Td>
                  <Table.Td>
                    <Text size="sm">{t.assigned_approvers.join(", ")}</Text>
                    {t.escalated_at && (
                      <Badge size="xs" color="orange">
                        escalated
                      </Badge>
                    )}
                  </Table.Td>
                  <Table.Td>{when(t.expires_at)}</Table.Td>
                  <Table.Td>
                    <Badge variant="light">{t.state}</Badge>
                    {t.reason && (
                      <Text size="xs" c="dimmed">
                        {t.approver_id}: {t.reason}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    {decide && t.state === "PENDING" && (
                      <Group gap={4} wrap="nowrap">
                        <Button size="xs" color="teal" disabled={!mine} title={mine ? undefined : "You are not an assigned approver"} onClick={() => setAct({ t, approve: true })}>
                          Approve
                        </Button>
                        <Button size="xs" color="red" variant="light" disabled={!mine} onClick={() => setAct({ t, approve: false })}>
                          Reject
                        </Button>
                      </Group>
                    )}
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </QueryState>
      <ReasonModal
        opened={!!act}
        onClose={() => setAct(null)}
        danger={act?.approve === false}
        title={act?.approve ? "Approve action" : "Reject action"}
        confirmLabel={act?.approve ? "Approve" : "Reject"}
        description={`${act?.t.action_summary} requested by ${act?.t.requested_by}. Policy ${act?.t.policy_version}. Policy, kill switch and budget are checked again when the agent uses the approval.`}
        onConfirm={async (reason) => {
          await api.post(`/approvals/${act!.t.ticket_id}/decide`, { approve: act!.approve, reason });
          notifyOk(act!.approve ? "Approved" : "Rejected");
          await qc.invalidateQueries({ queryKey: ["approvals"] });
        }}
      />
    </>
  );
}
