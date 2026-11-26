import { Alert, Badge, Button, Center, Group, Loader, Modal, Paper, Stack, Text, TextInput, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertTriangle, IconLock } from "@tabler/icons-react";
import { useState, type ReactNode } from "react";

import { ApiError } from "../api";
import { DECISION_COLOR, RISK_COLOR } from "../format";

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <Group justify="space-between" align="flex-start" mb="md" wrap="wrap">
      <div>
        <Title order={2}>{title}</Title>
        {description && (
          <Text c="dimmed" size="sm" maw={720}>
            {description}
          </Text>
        )}
      </div>
      {actions}
    </Group>
  );
}

/** Loading / error / empty states for any query. */
export function QueryState({ isLoading, error, empty, emptyText, children }: {
  isLoading: boolean;
  error: unknown;
  empty?: boolean;
  emptyText?: string;
  children: ReactNode;
}) {
  if (isLoading)
    return (
      <Center py="xl">
        <Loader aria-label="Loading" />
      </Center>
    );
  if (error) {
    const e = error as ApiError;
    if (e.status === 403)
      return (
        <Alert icon={<IconLock size={18} />} color="gray" title="Not available for your role">
          {e.message}
        </Alert>
      );
    return (
      <Alert icon={<IconAlertTriangle size={18} />} color="red" title="Could not load">
        {e.message ?? String(error)}
      </Alert>
    );
  }
  if (empty)
    return (
      <Paper withBorder p="xl">
        <Text c="dimmed" ta="center">
          {emptyText ?? "Nothing here yet."}
        </Text>
      </Paper>
    );
  return <>{children}</>;
}

export function StatCard({ label, value, hint, color }: { label: string; value: ReactNode; hint?: string; color?: string }) {
  return (
    <Paper withBorder p="md" radius="md">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text fz={28} fw={700} c={color} mt={4}>
        {value}
      </Text>
      {hint && (
        <Text size="xs" c="dimmed" mt={4}>
          {hint}
        </Text>
      )}
    </Paper>
  );
}

export function DecisionBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <Text c="dimmed">—</Text>;
  return (
    <Badge color={DECISION_COLOR[value] ?? "gray"} variant="light">
      {value.replaceAll("_", " ")}
    </Badge>
  );
}

export function RiskBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <Text c="dimmed">—</Text>;
  return (
    <Badge color={RISK_COLOR[value] ?? "gray"} variant="dot">
      {value}
    </Badge>
  );
}

export function notifyError(e: unknown): void {
  const err = e as ApiError;
  notifications.show({ color: "red", title: err.code ?? "Error", message: err.message ?? String(e) });
}

export function notifyOk(message: string): void {
  notifications.show({ color: "teal", title: "Done", message });
}

/** Confirmation for actions that change controls; a reason is required and audited. */
export function ReasonModal({ opened, title, description, confirmLabel, danger, onClose, onConfirm }: {
  opened: boolean;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened={opened} onClose={onClose} title={title} centered>
      <Stack>
        <Text size="sm">{description}</Text>
        <TextInput
          label="Reason (recorded in the evidence ledger)"
          value={reason}
          onChange={(e) => setReason(e.currentTarget.value)}
          data-autofocus
          required
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            color={danger ? "red" : undefined}
            loading={busy}
            disabled={reason.trim().length < 3}
            onClick={async () => {
              setBusy(true);
              try {
                await onConfirm(reason.trim());
                setReason("");
                onClose();
              } catch (e) {
                notifyError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            {confirmLabel}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
