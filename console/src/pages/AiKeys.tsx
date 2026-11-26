import { Alert, Badge, Button, Group, Modal, PasswordInput, Select, Stack, Table, Text, TextInput } from "@mantine/core";
import { IconPlus } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api";
import { can, useMe } from "../App";
import { notifyError, notifyOk, PageHeader, QueryState } from "../components/common";
import { when } from "../format";

interface StoredKey {
  id: string;
  last4: string;
  created_by: string;
  created_at: string;
}

interface ProviderRow {
  provider_id: string;
  kind: string;
  base_url: string | null;
  active: boolean;
  key: StoredKey | null;
  key_source: "captain" | "server" | "none";
}

interface AiKeysResponse {
  enabled: boolean;
  items: ProviderRow[];
  kinds: string[];
}

const KIND_LABEL: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  gemini: "Google Gemini",
  azure_openai: "Azure OpenAI",
  mistral: "Mistral",
  cohere: "Cohere",
  self_hosted: "Self-hosted (OpenAI-compatible)",
};
const NEEDS_URL = new Set(["azure_openai", "self_hosted", "cohere"]);

export function AiKeysPage() {
  const me = useMe().data;
  const qc = useQueryClient();
  const write = can(me, "aikeys:write");
  const q = useQuery({ queryKey: ["ai-keys"], queryFn: () => api.get<AiKeysResponse>("/ai-keys") });
  const [editing, setEditing] = useState<{ provider_id: string; kind: string; base_url: string } | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["ai-keys"] });

  return (
    <>
      <PageHeader
        title="AI keys"
        description="Your company's keys for AI providers such as OpenAI or Anthropic. People never get these keys: they use their own Captain key, and Captain uses this key on their behalf. A key is stored encrypted and is never shown again after you save it."
        actions={
          write &&
          q.data?.enabled && (
            <Button leftSection={<IconPlus size={16} />} onClick={() => setEditing({ provider_id: "", kind: "openai", base_url: "" })}>
              Add an AI key
            </Button>
          )
        }
      />
      {q.data && !q.data.enabled && (
        <Alert color="yellow" mb="md" title="Keys are managed on the server for this install">
          Storing AI keys from this page is switched off (the install has no CAPTAIN_SECRETS_KEY). Your platform team manages provider keys in the server's vault instead.
        </Alert>
      )}
      <QueryState isLoading={q.isLoading} error={q.error} empty={q.data?.items.length === 0} emptyText="No AI providers yet. Add your first AI key.">
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Provider</Table.Th>
              <Table.Th>Key</Table.Th>
              <Table.Th>Added</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {q.data?.items.map((p) => (
              <Table.Tr key={p.provider_id}>
                <Table.Td>
                  <Text fw={500}>{p.provider_id}</Text>
                  <Text size="xs" c="dimmed">
                    {KIND_LABEL[p.kind] ?? p.kind}
                    {p.base_url ? ` · ${p.base_url}` : ""}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {p.key_source === "captain" && p.key ? (
                    <Text ff="monospace">…{p.key.last4}</Text>
                  ) : p.key_source === "server" ? (
                    <Text size="sm" c="dimmed">
                      Kept in the server's vault
                    </Text>
                  ) : (
                    <Text size="sm" c="dimmed">
                      No key
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>{p.key ? `${when(p.key.created_at)} by ${p.key.created_by}` : ""}</Table.Td>
                <Table.Td>
                  {p.active ? (
                    <Badge color="green" variant="light">
                      In use
                    </Badge>
                  ) : (
                    <Badge color="gray" variant="light">
                      Switched off
                    </Badge>
                  )}
                </Table.Td>
                <Table.Td>
                  {write && q.data?.enabled && (
                    <Group gap="xs" justify="flex-end" wrap="nowrap">
                      {p.key_source === "captain" && (
                        <Button
                          size="xs"
                          variant="default"
                          loading={testing === p.provider_id}
                          onClick={async () => {
                            setTesting(p.provider_id);
                            try {
                              const r = await api.post<{ ok: boolean; message: string }>(`/ai-keys/${encodeURIComponent(p.provider_id)}/test`);
                              if (r.ok) notifyOk(r.message);
                              else notifyError(new Error(r.message));
                            } catch (e) {
                              notifyError(e);
                            } finally {
                              setTesting(null);
                            }
                          }}
                        >
                          Test
                        </Button>
                      )}
                      <Button size="xs" variant="default" onClick={() => setEditing({ provider_id: p.provider_id, kind: p.kind, base_url: p.base_url ?? "" })}>
                        {p.key_source === "captain" ? "Replace" : "Add key"}
                      </Button>
                      {p.key_source === "captain" && (
                        <Button
                          size="xs"
                          color="red"
                          variant="light"
                          onClick={async () => {
                            if (!window.confirm(`Remove the AI key for ${p.provider_id}? AI calls through this provider will stop until a new key is added.`)) return;
                            try {
                              await api.del(`/ai-keys/${encodeURIComponent(p.provider_id)}`);
                              notifyOk("Key removed");
                              await refresh();
                            } catch (e) {
                              notifyError(e);
                            }
                          }}
                        >
                          Remove
                        </Button>
                      )}
                    </Group>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </QueryState>
      {editing && (
        <KeyModal
          initial={editing}
          kinds={q.data?.kinds ?? []}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await refresh();
          }}
        />
      )}
    </>
  );
}

export function KeyModal({ initial, kinds, onClose, onSaved }: { initial: { provider_id: string; kind: string; base_url: string }; kinds: string[]; onClose: () => void; onSaved: () => Promise<void> }) {
  const isNew = !initial.provider_id;
  const [f, setF] = useState({ ...initial, key: "" });
  const [busy, setBusy] = useState(false);
  const needsUrl = NEEDS_URL.has(f.kind);
  const ready = f.provider_id.trim().length >= 2 && f.key.trim().length >= 8 && (!needsUrl || f.base_url.trim());
  return (
    <Modal opened onClose={onClose} title={isNew ? "Add an AI key" : `New key for ${initial.provider_id}`} centered>
      <Stack>
        <Select label="AI provider" data={kinds.map((k) => ({ value: k, label: KIND_LABEL[k] ?? k }))} value={f.kind} onChange={(v) => setF({ ...f, kind: v ?? "openai" })} disabled={!isNew} allowDeselect={false} />
        <TextInput
          label="Name"
          description="A short name for this provider in Captain, for example openai-main."
          value={f.provider_id}
          onChange={(e) => setF({ ...f, provider_id: e.currentTarget.value })}
          disabled={!isNew}
          required
        />
        {(needsUrl || !isNew) && (
          <TextInput
            label="Address (base URL)"
            description={needsUrl ? "Required for this provider, for example your Azure OpenAI endpoint." : "Leave as it is unless your provider gave you a different address."}
            value={f.base_url}
            onChange={(e) => setF({ ...f, base_url: e.currentTarget.value })}
            required={needsUrl}
          />
        )}
        <PasswordInput
          label="API key"
          description="Paste the key from your provider's website. It is encrypted when saved and never shown again."
          value={f.key}
          onChange={(e) => setF({ ...f, key: e.currentTarget.value })}
          autoComplete="off"
          required
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
                await api.post("/ai-keys", { provider_id: f.provider_id.trim(), kind: f.kind, key: f.key.trim(), base_url: f.base_url.trim() || null });
                notifyOk("AI key saved. Use Test to check it works.");
                await onSaved();
              } catch (e) {
                notifyError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Save key
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
