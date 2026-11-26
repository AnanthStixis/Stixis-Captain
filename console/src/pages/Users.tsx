import {
  Autocomplete,
  Badge,
  Button,
  Checkbox,
  Group,
  Modal,
  ScrollArea,
  Stack,
  Table,
  Text,
  TextInput,
} from "@mantine/core";
import { IconUserPlus } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type UserView } from "../api";
import { can, useMe, useStixis, roleText } from "../App";
import {
  notifyError,
  notifyOk,
  PageHeader,
  QueryState,
} from "../components/common";

/** What each console access level lets someone do, in plain words. */
export const ROLE_HELP: Record<string, string> = {
  "org-admin": "Runs Captain for the company: users, keys, AI keys, settings",
  approver: "Approves or rejects AI actions that wait for a person",
  finops: "Sees AI spending and sets budgets",
  auditor: "Reads the audit trail and reports",
  viewer: "Sees the dashboards, changes nothing",
  content_reviewer:
    "May read the text of held prompts (sensitive; give rarely)",
  "platform-admin": "Manages client companies (Stixis only)",
};

const JOB_TITLES = [
  "Trainee",
  "Intern",
  "Developer",
  "Senior developer",
  "Tech lead",
  "Architect",
  "QA engineer",
  "Data scientist",
  "Manager",
  "Director",
  "HR",
  "Finance",
  "Sales",
  "Support",
  "Marketing",
  "Legal",
];

export const ROLES = [
  "org-admin",
  "approver",
  "finops",
  "auditor",
  "viewer",
  "content_reviewer",
];

export function UsersPage() {
  const stixis = useStixis();
  const me = useMe().data;
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<UserView[]>("/users"),
  });
  const write = can(me, "users:write");
  const [editing, setEditing] = useState<UserView | "new" | null>(null);

  const setActive = async (u: UserView, active: boolean) => {
    if (
      !active &&
      !window.confirm(
        `Deactivate ${u.email}? Their console access ends and all their Captain keys are revoked.`,
      )
    )
      return;
    try {
      const r = await api.put<UserView>(`/users/${encodeURIComponent(u.id)}`, {
        active,
      });
      notifyOk(
        active
          ? "User reactivated"
          : `User deactivated; ${r.keys_revoked ?? 0} key(s) revoked`,
      );
      await qc.invalidateQueries({ queryKey: ["users"] });
      await qc.invalidateQueries({ queryKey: ["keys"] });
    } catch (e) {
      notifyError(e);
    }
  };

  return (
    <>
      <PageHeader
        title={stixis ? "Stixis staff" : "Users"}
        description={
          stixis
            ? "Stixis people who manage client companies. Give them the platform-admin role. Roles given here are added to what your sign-in provider grants; deactivating someone ends their access."
            : "Everyone at your company who uses AI through Captain. Most people need no console access: they only use AI with their Captain key. Deactivating someone ends their access and revokes their keys."
        }
        actions={
          write && (
            <Button
              leftSection={<IconUserPlus size={16} />}
              onClick={() => setEditing("new")}
            >
              Add user
            </Button>
          )
        }
      />
      <UserModal
        key={
          editing === null ? "closed" : editing === "new" ? "new" : editing.id
        }
        user={editing}
        onClose={() => setEditing(null)}
      />
      <QueryState
        isLoading={q.isLoading}
        error={q.error}
        empty={q.data?.length === 0}
        emptyText="No users yet."
      >
        <ScrollArea>
          <Table striped miw={760}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Name</Table.Th>
                <Table.Th>Email</Table.Th>
                <Table.Th>Job title</Table.Th>
                <Table.Th>Team</Table.Th>
                <Table.Th>Console access</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((u) => (
                <Table.Tr key={u.id}>
                  <Table.Td>{u.name}</Table.Td>
                  <Table.Td>{u.email}</Table.Td>
                  <Table.Td>{u.job_title ?? "—"}</Table.Td>
                  <Table.Td>{u.team ?? "—"}</Table.Td>
                  <Table.Td>
                    <Group gap={4}>
                      {u.roles.length ? (
                        u.roles.map((r) => (
                          <Badge
                            key={r}
                            variant="light"
                            style={{ textTransform: "none" }}
                          >
                            {roleText([r], false)}
                          </Badge>
                        ))
                      ) : (
                        <Text size="sm" c="dimmed">
                          Uses AI only
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
                        <Button
                          size="xs"
                          variant="default"
                          onClick={() => setEditing(u)}
                        >
                          Edit
                        </Button>
                        {u.active ? (
                          <Button
                            size="xs"
                            color="red"
                            variant="light"
                            onClick={() => void setActive(u, false)}
                          >
                            Deactivate
                          </Button>
                        ) : (
                          <Button
                            size="xs"
                            variant="light"
                            onClick={() => void setActive(u, true)}
                          >
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

function UserModal({
  user,
  onClose,
}: {
  user: UserView | "new" | null;
  onClose: () => void;
}) {
  const stixis = useStixis();
  const qc = useQueryClient();
  const isNew = user === "new";
  const existing = user && user !== "new" ? user : null;
  const [email, setEmail] = useState(existing?.email ?? "");
  const [name, setName] = useState(existing?.name ?? "");
  const [team, setTeam] = useState(existing?.team ?? "");
  const [jobTitle, setJobTitle] = useState(existing?.job_title ?? "");
  const [roles, setRoles] = useState<string[]>(existing?.roles ?? []);
  const [busy, setBusy] = useState(false);
  return (
    <Modal
      opened={user !== null}
      onClose={onClose}
      title={isNew ? "Add user" : `Edit ${existing?.email ?? ""}`}
    >
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            if (isNew)
              await api.post("/users", {
                email,
                name,
                team: team || null,
                job_title: jobTitle || null,
                roles,
              });
            else if (existing)
              await api.put(`/users/${encodeURIComponent(existing.id)}`, {
                name,
                team,
                job_title: jobTitle,
                roles,
              });
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
          <TextInput
            label="Email"
            description="Must match the email they sign in with"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.currentTarget.value)}
            disabled={!isNew}
            required
          />
          <TextInput
            label="Name"
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
            required
          />
          <Group grow align="flex-start">
            <Autocomplete
              label="Job title"
              description="For reports; does not change access"
              placeholder="e.g. Developer"
              data={JOB_TITLES}
              value={jobTitle}
              onChange={setJobTitle}
            />
            <TextInput
              label="Team or department"
              placeholder="e.g. Payments"
              value={team}
              onChange={(e) => setTeam(e.currentTarget.value)}
            />
          </Group>
          <Checkbox.Group
            label="Access to the Captain console"
            description="Leave all unticked for people who only use AI (most developers, trainees, HR...)."
            value={roles}
            onChange={setRoles}
          >
            <Stack gap={6} mt={6}>
              {(stixis ? ["platform-admin"] : ROLES).map((r) => (
                <Checkbox
                  key={r}
                  value={r}
                  label={roleText([r], false)}
                  description={ROLE_HELP[r]}
                />
              ))}
            </Stack>
          </Checkbox.Group>
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
