import { Alert, Button, Checkbox, Group, List, Loader, Modal, Stack, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import { api } from "../api";
import { notifyError, notifyOk } from "./common";

export interface Check {
  name: string;
  active_keys?: number;
  spent_micros?: number;
  blockers: string[];
  calls_recent: number;
  recent_days: number;
  can_delete: boolean;
}

/** Delete with a safety check first. The server says what still depends on the item
 * (those must be removed first) and how much it was used lately (needs a tick to go on).
 * Deleting is soft: the item is switched off and kept, so history and costs stay linked. */
export function SafeDeleteModal({
  what,
  checkPath,
  deletePath,
  onClose,
  onDeleted,
  actionLabel = "Delete",
  onConfirm,
  details,
  effect = "It is switched off, not erased: past activity, costs and the audit trail keep showing it.",
}: {
  what: string; // e.g. "the AI key openai-main"
  checkPath: string;
  deletePath?: string;
  onClose: () => void;
  onDeleted: () => Promise<unknown>;
  actionLabel?: string; // Delete, Retire, Deactivate, Switch off...
  onConfirm?: () => Promise<unknown>; // instead of DELETE deletePath
  details?: (c: Check) => ReactNode; // extra facts, e.g. keys that will be revoked
  effect?: string;
}) {
  const q = useQuery({
    queryKey: ["delete-check", checkPath],
    queryFn: () => api.get<Check>(checkPath),
    gcTime: 0,
  });
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState(false);
  const c = q.data;
  return (
    <Modal opened onClose={onClose} title={`${actionLabel} ${what}?`} centered>
      {q.isLoading || !c ? (
        <Group>
          <Loader size="sm" />
          <Text size="sm">Checking what still uses it…</Text>
        </Group>
      ) : (
        <Stack>
          {c.blockers.length > 0 ? (
            <Alert color="red" title={`It is still needed, so you cannot ${actionLabel.toLowerCase()} it yet`}>
              <List size="sm">
                {c.blockers.map((b) => (
                  <List.Item key={b}>{b}</List.Item>
                ))}
              </List>
              <Text size="sm" mt="xs">
                Change these first, then try again.
              </Text>
            </Alert>
          ) : (
            <>
              {c.calls_recent > 0 ? (
                <Alert color="yellow" title="People are using it">
                  It handled {c.calls_recent.toLocaleString()} AI calls in the last{" "}
                  {c.recent_days} days. Those calls will be refused after you delete it.
                </Alert>
              ) : (
                <Text size="sm">Nothing depends on it and it has not been used lately.</Text>
              )}
              {details?.(c)}
              <Text size="sm" c="dimmed">
                {effect}
              </Text>
              {c.calls_recent > 0 && (
                <Checkbox
                  label="I understand these calls will stop"
                  checked={sure}
                  onChange={(e) => setSure(e.currentTarget.checked)}
                />
              )}
            </>
          )}
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              {c.blockers.length ? "Close" : "Cancel"}
            </Button>
            {c.blockers.length === 0 && (
              <Button
                color="red"
                loading={busy}
                disabled={c.calls_recent > 0 && !sure}
                onClick={async () => {
                  setBusy(true);
                  try {
                    if (onConfirm) await onConfirm();
                    else
                      await api.del(
                        `${deletePath}${deletePath?.includes("?") ? "&" : "?"}confirm_in_use=${c.calls_recent > 0}`,
                      );
                    notifyOk(`${actionLabel}: done`);
                    await onDeleted();
                    onClose();
                  } catch (e) {
                    notifyError(e);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {actionLabel}
              </Button>
            )}
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
