import {
  Alert,
  Badge,
  Button,
  Code,
  CopyButton,
  Group,
  Modal,
  ScrollArea,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
} from "@mantine/core";
import { IconKey, IconUserX } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type KeyView, type RegisterEntry, type UserView } from "../api";
import { can, useMe } from "../App";
import {
  notifyError,
  notifyOk,
  PageHeader,
  QueryState,
  ReasonModal,
} from "../components/common";
import { SetupSteps, useReadiness } from "../components/SetupSteps";
import { when } from "../format";

interface Issued {
  key: KeyView;
  raw_key: string;
  notice: string;
}

export function KeysPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["keys"],
    queryFn: () => api.get<KeyView[]>("/keys"),
  });
  const [issueOpen, setIssueOpen] = useState(false);
  const [issued, setIssued] = useState<Issued | null>(null);
  const [revoke, setRevoke] = useState<KeyView | null>(null);
  const write = can(me, "keys:write");
  const erase = can(me, "subjects:erase");
  const [eraseOpen, setEraseOpen] = useState(false);

  return (
    <>
      <PageHeader
        title="Captain keys"
        description="A Captain key lets a person or an app use AI through Captain (instead of a provider's own key), so every use is checked and recorded. A key is shown once, when you create it; Captain keeps only a fingerprint."
        actions={
          <Group gap="xs">
            {erase && (
              <Button
                variant="outline"
                color="red"
                leftSection={<IconUserX size={16} />}
                onClick={() => setEraseOpen(true)}
              >
                Erase a person
              </Button>
            )}
            {write && (
              <Button
                leftSection={<IconKey size={16} />}
                onClick={() => setIssueOpen(true)}
              >
                Create a key
              </Button>
            )}
          </Group>
        }
      />
      <EraseModal
        opened={eraseOpen}
        onClose={() => setEraseOpen(false)}
        keys={q.data ?? []}
      />
      <QueryState
        isLoading={q.isLoading}
        error={q.error}
        empty={q.data?.length === 0}
        emptyText="No keys yet. Press Create a key."
      >
        <ScrollArea>
          <Table striped miw={900}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Key</Table.Th>
                <Table.Th>For</Table.Th>
                <Table.Th>Used for · tool</Table.Th>
                <Table.Th>Stage</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Last used</Table.Th>
                <Table.Th>Expires</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {q.data?.map((k) => (
                <Table.Tr key={k.key_id}>
                  <Table.Td>
                    <Code>{k.key_id}</Code>
                    {k.label && (
                      <Text size="xs" c="dimmed">
                        {k.label}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    {k.principal_id.startsWith("app:")
                      ? `App: ${k.principal_id.slice(4)}`
                      : k.principal_id}
                  </Table.Td>
                  <Table.Td>
                    {k.register_id}
                    <Text size="xs" c="dimmed">
                      {TOOL_LABEL[k.application_id] ?? k.application_id}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {STAGE_LABEL[k.environment] ?? k.environment}
                  </Table.Td>
                  <Table.Td>
                    <Badge
                      color={k.status === "ACTIVE" ? "teal" : "red"}
                      variant="light"
                    >
                      {k.status === "ACTIVE"
                        ? "Active"
                        : k.status === "REVOKED"
                          ? "Revoked"
                          : k.status.toLowerCase()}
                    </Badge>
                    {k.revoked_reason && (
                      <Text size="xs" c="dimmed">
                        {k.revoked_reason}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>{when(k.last_used_at)}</Table.Td>
                  <Table.Td>
                    {k.expires_at ? when(k.expires_at) : "never"}
                  </Table.Td>
                  <Table.Td>
                    {write && k.status === "ACTIVE" && (
                      <Group gap={4} wrap="nowrap">
                        <Button
                          size="xs"
                          variant="default"
                          onClick={async () => {
                            try {
                              setIssued(
                                await api.post<Issued>(
                                  `/keys/${k.key_id}/rotate`,
                                ),
                              );
                              await qc.invalidateQueries({
                                queryKey: ["keys"],
                              });
                            } catch (e) {
                              notifyError(e);
                            }
                          }}
                        >
                          Rotate
                        </Button>
                        <Button
                          size="xs"
                          color="red"
                          variant="light"
                          onClick={() => setRevoke(k)}
                        >
                          Revoke
                        </Button>
                      </Group>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </QueryState>

      <IssueModal
        opened={issueOpen}
        onClose={() => setIssueOpen(false)}
        onIssued={async (i) => {
          setIssueOpen(false);
          setIssued(i);
          await qc.invalidateQueries({ queryKey: ["keys"] });
        }}
      />
      <Modal
        opened={!!issued}
        onClose={() => setIssued(null)}
        title="Copy the new key now"
        centered
        size="lg"
        closeOnClickOutside={false}
      >
        {issued && (
          <Stack>
            <Alert color="yellow">{issued.notice}</Alert>
            <Code block style={{ wordBreak: "break-all" }}>
              {issued.raw_key}
            </Code>
            <Group justify="flex-end">
              <CopyButton value={issued.raw_key}>
                {({ copied, copy }) => (
                  <Button onClick={copy} color={copied ? "teal" : undefined}>
                    {copied ? "Copied" : "Copy key"}
                  </Button>
                )}
              </CopyButton>
              <Button variant="default" onClick={() => setIssued(null)}>
                I have stored it
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>
      <ReasonModal
        opened={!!revoke}
        onClose={() => setRevoke(null)}
        title="Revoke key"
        danger
        confirmLabel="Revoke now"
        description={
          <>
            Key <Code>{revoke?.key_id}</Code> stops working on every Captain
            instance within seconds. This cannot be undone.
          </>
        }
        onConfirm={async (reason) => {
          await api.post(`/keys/${revoke!.key_id}/revoke`, { reason });
          notifyOk("Key revoked");
          await qc.invalidateQueries({ queryKey: ["keys"] });
        }}
      />
    </>
  );
}

const TOOLS = [
  { value: "vscode", label: "VS Code / GitHub Copilot" },
  { value: "claude-code", label: "Claude Code" },
  { value: "browser", label: "Browser extension" },
  { value: "mcp", label: "MCP tools (VS Code, Claude Desktop, Cursor)" },
  { value: "desktop", label: "Desktop agent" },
  { value: "sdk", label: "Python / JS / .NET code (SDK)" },
  { value: "api", label: "Direct API calls" },
  { value: "other", label: "Something else" },
];
const TOOL_LABEL: Record<string, string> = Object.fromEntries(
  TOOLS.map((t) => [t.value, t.label]),
);
const STAGES = [
  { value: "DEVELOPMENT", label: "Trying out" },
  { value: "QA", label: "Testing" },
  { value: "PRODUCTION", label: "Real work" },
];
const STAGE_LABEL: Record<string, string> = Object.fromEntries(
  STAGES.map((t) => [t.value, t.label]),
);

function IssueModal({
  opened,
  onClose,
  onIssued,
}: {
  opened: boolean;
  onClose: () => void;
  onIssued: (i: Issued) => Promise<void>;
}) {
  const reg = useQuery({
    queryKey: ["register"],
    queryFn: () => api.get<RegisterEntry[]>("/register"),
    enabled: opened,
  });
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<UserView[]>("/users"),
    enabled: opened,
  });
  const readiness = useReadiness();
  const [forWhom, setForWhom] = useState<"person" | "app">("person");
  const [person, setPerson] = useState<string | null>(null);
  const [appName, setAppName] = useState("");
  const [use, setUse] = useState<string | null>(null);
  const [tool, setTool] = useState<string | null>("vscode");
  const [team, setTeam] = useState("");
  const [stage, setStage] = useState("DEVELOPMENT");
  const [expiry, setExpiry] = useState<string | null>("90");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const uses = (reg.data ?? []).filter((r) => r.active);
  const chosenUse = use ?? (uses.length === 1 ? (uses[0]?.id ?? null) : null);
  const chosen = users.data?.find((u) => u.id === person);
  const ready =
    (forWhom === "person" ? !!person : appName.trim().length >= 2) &&
    !!chosenUse &&
    !!tool;
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="Create a Captain key"
      centered
      size="lg"
    >
      {readiness.data && !readiness.data.ready_for_keys ? (
        <Stack>
          <Text size="sm">
            Before the first key, finish these steps. Each opens right here;
            they tick themselves when done.
          </Text>
          <SetupSteps upTo="budget" />
        </Stack>
      ) : (
        <Stack>
          <SegmentedControl
            data={[
              { value: "person", label: "For a person" },
              { value: "app", label: "For an app or service" },
            ]}
            value={forWhom}
            onChange={(v) => setForWhom(v as "person" | "app")}
          />
          {forWhom === "person" ? (
            <Select
              label="Who will use it?"
              description="People are added on the Users page"
              placeholder="Search by name or email"
              data={(users.data ?? [])
                .filter((u) => u.active)
                .map((u) => ({
                  value: u.id,
                  label: `${u.name} (${u.email})${u.job_title ? ` · ${u.job_title}` : ""}`,
                }))}
              value={person}
              onChange={(v) => {
                setPerson(v);
                const u = users.data?.find((x) => x.id === v);
                if (u?.team && !team) setTeam(u.team);
              }}
              searchable
              nothingFoundMessage="No such user: add them on the Users page first"
            />
          ) : (
            <TextInput
              label="App or service name"
              description="For example: Support chatbot, Invoice reader"
              value={appName}
              onChange={(e) => setAppName(e.currentTarget.value)}
            />
          )}
          {reg.data && uses.length === 0 ? (
            <Alert color="yellow">
              First describe at least one approved AI use on the{" "}
              <b>AI Register</b> page (for example "Coding assistant"). Every
              key belongs to one, so Captain knows which rules apply.
            </Alert>
          ) : (
            <Select
              label="What is it for?"
              description="The approved AI use from the AI Register; its rules (models, data, budget) apply to this key"
              data={uses.map((r) => ({
                value: r.id,
                label: r.purpose ? `${r.name}: ${r.purpose}` : r.name,
              }))}
              value={chosenUse}
              onChange={setUse}
              searchable
            />
          )}
          <Group grow align="flex-start">
            <Select
              label="Which tool will use it?"
              data={TOOLS}
              value={tool}
              onChange={setTool}
              allowDeselect={false}
            />
            <TextInput
              label="Team or project"
              description="Used to group costs"
              placeholder={chosen?.team ?? "general"}
              value={team}
              onChange={(e) => setTeam(e.currentTarget.value)}
            />
          </Group>
          <Group grow align="flex-start">
            <div>
              <Text size="sm" fw={500} mb={4}>
                Stage
              </Text>
              <SegmentedControl
                fullWidth
                data={STAGES}
                value={stage}
                onChange={setStage}
              />
            </div>
            <Select
              label="Stops working after"
              data={[
                { value: "30", label: "30 days" },
                { value: "90", label: "90 days (recommended)" },
                { value: "180", label: "180 days" },
                { value: "365", label: "1 year" },
                { value: "", label: "Never (not recommended)" },
              ]}
              value={expiry}
              onChange={setExpiry}
              allowDeselect={false}
            />
          </Group>
          <TextInput
            label="Note (optional)"
            placeholder="e.g. Ravi's laptop"
            value={note}
            onChange={(e) => setNote(e.currentTarget.value)}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button
              loading={busy}
              disabled={!ready}
              onClick={async () => {
                setBusy(true);
                try {
                  await onIssued(
                    await api.post<Issued>("/keys", {
                      principal_id: forWhom === "person" ? person : "",
                      app_name: forWhom === "app" ? appName.trim() : null,
                      register_id: chosenUse,
                      project_id: team.trim() || chosen?.team || "general",
                      application_id: tool,
                      environment: stage,
                      ttl_days: expiry ? Number(expiry) : null,
                      label: note.trim(),
                    }),
                  );
                } catch (e) {
                  notifyError(e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              Create key
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}

/** Erasure (ADR-005): deletes the person's key so their evidence can never be linked to
 * them again. Typing the id a second time guards against erasing the wrong person. */
function EraseModal({
  opened,
  onClose,
  keys,
}: {
  opened: boolean;
  onClose: () => void;
  keys: KeyView[];
}) {
  const [id, setId] = useState("");
  const [again, setAgain] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const live = keys.filter(
    (k) => k.principal_id === id.trim() && k.status === "ACTIVE",
  );
  const close = () => {
    setId("");
    setAgain("");
    setReason("");
    onClose();
  };
  const ready =
    id.trim() !== "" &&
    again.trim() === id.trim() &&
    reason.trim().length >= 3 &&
    live.length === 0;
  return (
    <Modal
      opened={opened}
      onClose={close}
      title="Erase a person"
      centered
      size="lg"
    >
      <Stack>
        <Alert color="red" title="This cannot be undone">
          The person's events and records stay, and stay in every total, but
          from now on they are shown as "erased subject" and can never be linked
          to this person again. Their console sign-in stops working. Backups
          taken before now still hold the link until they expire (at most 30
          days).
        </Alert>
        <TextInput
          label="Person (principal id)"
          value={id}
          onChange={(e) => setId(e.currentTarget.value)}
          data-autofocus
          required
        />
        {live.length > 0 && (
          <Alert color="yellow">
            This person still has {live.length} active key
            {live.length > 1 ? "s" : ""} ({live.map((k) => k.key_id).join(", ")}
            ). Revoke {live.length > 1 ? "them" : "it"} first, so no new
            evidence is written for them.
          </Alert>
        )}
        <TextInput
          label="Type the id again to confirm"
          value={again}
          onChange={(e) => setAgain(e.currentTarget.value)}
          required
        />
        <TextInput
          label="Reason (recorded in the evidence ledger)"
          description="Use a ticket number. Do not write the person's name here: the reason is kept for good."
          value={reason}
          onChange={(e) => setReason(e.currentTarget.value)}
          required
        />
        <Text size="xs" c="dimmed">
          Their principal record, keys, approval tickets, alerts, budgets, rate
          limits and kill switches are changed to the same "erased subject" at
          the same time.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={close}>
            Cancel
          </Button>
          <Button
            color="red"
            loading={busy}
            disabled={!ready}
            onClick={async () => {
              setBusy(true);
              try {
                const r = await api.post<{ status: string }>(
                  `/subjects/${encodeURIComponent(id.trim())}/erase`,
                  { reason: reason.trim() },
                );
                notifyOk(
                  r.status === "erased"
                    ? `${id.trim()} erased. Their events now show as "erased subject".`
                    : "Nothing to erase: no evidence links to this id.",
                );
                close();
              } catch (e) {
                notifyError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Erase permanently
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
