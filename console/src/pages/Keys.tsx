import { Alert, Badge, Button, Code, CopyButton, Group, Modal, NumberInput, ScrollArea, Select, Stack, Table, Text, TextInput } from "@mantine/core";
import { IconKey, IconUserX } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type KeyView, type RegisterEntry } from "../api";
import { can, useMe } from "../App";
import { notifyError, notifyOk, PageHeader, QueryState, ReasonModal } from "../components/common";
import { when } from "../format";

interface Issued {
  key: KeyView;
  raw_key: string;
  notice: string;
}

export function KeysPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["keys"], queryFn: () => api.get<KeyView[]>("/keys") });
  const [issueOpen, setIssueOpen] = useState(false);
  const [issued, setIssued] = useState<Issued | null>(null);
  const [revoke, setRevoke] = useState<KeyView | null>(null);
  const write = can(me, "keys:write");
  const erase = can(me, "subjects:erase");
  const [eraseOpen, setEraseOpen] = useState(false);

  return (
    <>
      <PageHeader
        title="Virtual keys"
        description="Captain keys used by applications and agents instead of provider keys. Only a hash is stored; a key is shown once, when issued."
        actions={
          <Group gap="xs">
            {erase && (
              <Button variant="outline" color="red" leftSection={<IconUserX size={16} />} onClick={() => setEraseOpen(true)}>
                Erase a person
              </Button>
            )}
            {write && <Button leftSection={<IconKey size={16} />} onClick={() => setIssueOpen(true)}>Issue key</Button>}
          </Group>
        }
      />
      <EraseModal opened={eraseOpen} onClose={() => setEraseOpen(false)} keys={q.data ?? []} />
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.length === 0} emptyText="No keys issued yet.">
        <ScrollArea>
          <Table striped miw={900}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Key id</Table.Th>
                <Table.Th>Principal</Table.Th>
                <Table.Th>System / app</Table.Th>
                <Table.Th>Env</Table.Th>
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
                  <Table.Td>{k.principal_id}</Table.Td>
                  <Table.Td>
                    {k.register_id}
                    <Text size="xs" c="dimmed">
                      {k.application_id}
                    </Text>
                  </Table.Td>
                  <Table.Td>{k.environment}</Table.Td>
                  <Table.Td>
                    <Badge color={k.status === "ACTIVE" ? "teal" : "red"} variant="light">
                      {k.status}
                    </Badge>
                    {k.revoked_reason && (
                      <Text size="xs" c="dimmed">
                        {k.revoked_reason}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>{when(k.last_used_at)}</Table.Td>
                  <Table.Td>{k.expires_at ? when(k.expires_at) : "never"}</Table.Td>
                  <Table.Td>
                    {write && k.status === "ACTIVE" && (
                      <Group gap={4} wrap="nowrap">
                        <Button
                          size="xs"
                          variant="default"
                          onClick={async () => {
                            try {
                              setIssued(await api.post<Issued>(`/keys/${k.key_id}/rotate`));
                              await qc.invalidateQueries({ queryKey: ["keys"] });
                            } catch (e) {
                              notifyError(e);
                            }
                          }}
                        >
                          Rotate
                        </Button>
                        <Button size="xs" color="red" variant="light" onClick={() => setRevoke(k)}>
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
      <Modal opened={!!issued} onClose={() => setIssued(null)} title="Copy your new key" centered size="lg" closeOnClickOutside={false}>
        {issued && (
          <Stack>
            <Alert color="yellow">{issued.notice}</Alert>
            <Code block style={{ wordBreak: "break-all" }}>
              {issued.raw_key}
            </Code>
            <Group justify="flex-end">
              <CopyButton value={issued.raw_key}>
                {({ copied, copy }) => <Button onClick={copy} color={copied ? "teal" : undefined}>{copied ? "Copied" : "Copy key"}</Button>}
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
        description={<>Key <Code>{revoke?.key_id}</Code> stops working on every Captain instance within seconds. This cannot be undone.</>}
        onConfirm={async (reason) => {
          await api.post(`/keys/${revoke!.key_id}/revoke`, { reason });
          notifyOk("Key revoked");
          await qc.invalidateQueries({ queryKey: ["keys"] });
        }}
      />
    </>
  );
}

function IssueModal({ opened, onClose, onIssued }: { opened: boolean; onClose: () => void; onIssued: (i: Issued) => Promise<void> }) {
  const reg = useQuery({ queryKey: ["register"], queryFn: () => api.get<RegisterEntry[]>("/register"), enabled: opened });
  const [f, setF] = useState({ principal_id: "", register_id: "", project_id: "", application_id: "", environment: "DEVELOPMENT", label: "" });
  const [ttl, setTtl] = useState<number | string>(90);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (v: string | null) => setF({ ...f, [k]: v ?? "" });
  return (
    <Modal opened={opened} onClose={onClose} title="Issue a virtual key" centered>
      <Stack>
        <TextInput label="Principal id" value={f.principal_id} onChange={(e) => set("principal_id")(e.currentTarget.value)} required />
        <Select label="AI Register entry" data={(reg.data ?? []).map((r) => ({ value: r.id, label: `${r.id} · ${r.name} (${r.risk_tier})` }))} value={f.register_id} onChange={set("register_id")} searchable required />
        <TextInput label="Project id" value={f.project_id} onChange={(e) => set("project_id")(e.currentTarget.value)} required />
        <TextInput label="Application id" value={f.application_id} onChange={(e) => set("application_id")(e.currentTarget.value)} required />
        <Select label="Environment" data={["DEVELOPMENT", "QA", "PRODUCTION"]} value={f.environment} onChange={set("environment")} />
        <NumberInput label="Expires after (days)" description="Leave empty for no expiry (not recommended)" min={1} max={365} value={ttl} onChange={setTtl} />
        <TextInput label="Label" value={f.label} onChange={(e) => set("label")(e.currentTarget.value)} />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={!f.principal_id || !f.register_id || !f.project_id || !f.application_id}
            onClick={async () => {
              setBusy(true);
              try {
                await onIssued(await api.post<Issued>("/keys", { ...f, ttl_days: ttl === "" ? null : Number(ttl) }));
              } catch (e) {
                notifyError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Issue key
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** Erasure (ADR-005): deletes the person's key so their evidence can never be linked to
 * them again. Typing the id a second time guards against erasing the wrong person. */
function EraseModal({ opened, onClose, keys }: { opened: boolean; onClose: () => void; keys: KeyView[] }) {
  const [id, setId] = useState("");
  const [again, setAgain] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const live = keys.filter((k) => k.principal_id === id.trim() && k.status === "ACTIVE");
  const close = () => {
    setId("");
    setAgain("");
    setReason("");
    onClose();
  };
  const ready = id.trim() !== "" && again.trim() === id.trim() && reason.trim().length >= 3 && live.length === 0;
  return (
    <Modal opened={opened} onClose={close} title="Erase a person" centered size="lg">
      <Stack>
        <Alert color="red" title="This cannot be undone">
          The person's events and records stay, and stay in every total, but from now on they are shown as "erased
          subject" and can never be linked to this person again. Their console sign-in stops working. Backups taken
          before now still hold the link until they expire (at most 30 days).
        </Alert>
        <TextInput label="Person (principal id)" value={id} onChange={(e) => setId(e.currentTarget.value)} data-autofocus required />
        {live.length > 0 && (
          <Alert color="yellow">
            This person still has {live.length} active key{live.length > 1 ? "s" : ""} ({live.map((k) => k.key_id).join(", ")}).
            Revoke {live.length > 1 ? "them" : "it"} first, so no new evidence is written for them.
          </Alert>
        )}
        <TextInput label="Type the id again to confirm" value={again} onChange={(e) => setAgain(e.currentTarget.value)} required />
        <TextInput
          label="Reason (recorded in the evidence ledger)"
          description="Use a ticket number. Do not write the person's name here: the reason is kept for good."
          value={reason}
          onChange={(e) => setReason(e.currentTarget.value)}
          required
        />
        <Text size="xs" c="dimmed">
          Their principal record, keys, approval tickets, alerts, budgets, rate limits and kill switches are changed to the
          same "erased subject" at the same time.
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
                const r = await api.post<{ status: string }>(`/subjects/${encodeURIComponent(id.trim())}/erase`, { reason: reason.trim() });
                notifyOk(r.status === "erased" ? `${id.trim()} erased. Their events now show as "erased subject".` : "Nothing to erase: no evidence links to this id.");
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
