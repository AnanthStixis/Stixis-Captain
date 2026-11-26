import { Badge, Button, Group, Modal, MultiSelect, ScrollArea, Stack, Table, Text, TextInput } from "@mantine/core";
import { IconUserPlus } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type UserView } from "../api";
import { can, useMe } from "../App";
import { notifyError, notifyOk, PageHeader, QueryState } from "../components/common";

export const ROLES = ["org-admin", "approver", "finops", "auditor", "viewer", "content_reviewer"];

export function UsersPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["users"], queryFn: () => api.get<UserView[]>("/users") });
  const write = can(me, "users:write");
  const [editing, setEditing] = useState<UserView | "new" | null>(null);

  const setActive = async (u: UserView, active: boolean) => {
    if (!active && !window.confirm(`Deactivate ${u.email}? Their console access ends and all their Captain keys are revoked.`)) return;
    try {
      const r = await api.put<UserView>(`/users/${encodeURIComponent(u.id)}`, { active });
      notifyOk(active ? "User reactivated" : `User deactivated; ${r.keys_revoked ?? 0} key(s) revoked`);
      await qc.invalidateQueries({ queryKey: ["users"] });
      await qc.invalidateQueries({ queryKey: ["keys"] });
    } catch (e) {
      notifyError(e);
    }
  };

  return (
    <>
      <PageHeader
        title="Users"
        description="People who use the console. Roles given here are added to the roles your sign-in provider grants. Deactivating someone ends their access and revokes their keys."
        actions={
          write && (
            <Button leftSection={<IconUserPlus size={16} />} onClick={() => setEditing("new")}>
              Add user
            </Button>
          )
        }
      />
      <UserModal key={editing === null ? "closed" : editing === "new" ? "new" : editing.id} user={editing} onClose={() => setEditing(null)} />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No users yet.">
        <ScrollArea>
          <Table striped miw={760}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Name</Table.Th>
                <Table.Th>Email</Table.Th>
                <Table.Th>Team</Table.Th>
                <Table.Th>Console roles</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((u) => (
                <Table.Tr key={u.id}>
                  <Table.Td>{u.name}</Table.Td>
                  <Table.Td>{u.email}</Table.Td>
                  <Table.Td>{u.team ?? "—"}</Table.Td>
                  <Table.Td>
                    <Group gap={4}>
                      {u.roles.length ? (
                        u.roles.map((r) => (
                          <Badge key={r} variant="light">
                            {r}
                          </Badge>
                        ))
                      ) : (
                        <Text size="sm" c="dimmed">
                          none
                        </Text>
                      )}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <Badge color={u.active ? "teal" : "gray"} variant="light">
                      {u.active ? "Active" : "Deactivated"}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    {write && (
                      <Group gap={4} wrap="nowrap">
                        <Button size="xs" variant="default" onClick={() => setEditing(u)}>
                          Edit
                        </Button>
                        {u.active ? (
                          <Button size="xs" color="red" variant="light" onClick={() => void setActive(u, false)}>
                            Deactivate
                          </Button>
                        ) : (
                          <Button size="xs" variant="light" onClick={() => void setActive(u, true)}>
                            Reactivate
                          </Button>
                        )}
                      </Group>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>
    </>
  );
}

function UserModal({ user, onClose }: { user: UserView | "new" | null; onClose: () => void }) {
  const qc = useQueryClient();
  const isNew = user === "new";
  const existing = user && user !== "new" ? user : null;
  const [email, setEmail] = useState(existing?.email ?? "");
  const [name, setName] = useState(existing?.name ?? "");
  const [team, setTeam] = useState(existing?.team ?? "");
  const [roles, setRoles] = useState<string[]>(existing?.roles ?? []);
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened={user !== null} onClose={onClose} title={isNew ? "Add user" : `Edit ${existing?.email ?? ""}`}>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            if (isNew) await api.post("/users", { email, name, team: team || null, roles });
            else if (existing) await api.put(`/users/${encodeURIComponent(existing.id)}`, { name, team, roles });
            notifyOk(isNew ? "User added" : "User updated");
            await qc.invalidateQueries({ queryKey: ["users"] });
            onClose();
          } catch (err) {
            notifyError(err);
          } finally {
            setBusy(false);
          }
        }}
      >
        <Stack>
          <TextInput label="Email" description="Must match the email they sign in with" type="email" value={email} onChange={(e) => setEmail(e.currentTarget.value)} disabled={!isNew} required />
          <TextInput label="Name" value={name} onChange={(e) => setName(e.currentTarget.value)} required />
          <TextInput label="Team" value={team} onChange={(e) => setTeam(e.currentTarget.value)} />
          <MultiSelect label="Console roles" data={ROLES} value={roles} onChange={setRoles} clearable />
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={busy}>
              {isNew ? "Add" : "Save"}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
