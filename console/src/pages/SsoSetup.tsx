import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Code,
  CopyButton,
  Group,
  List,
  MultiSelect,
  PasswordInput,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { IconCheck, IconPlus, IconTrash } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../api";
import { roleText } from "../App";
import { notifyError, notifyOk } from "../components/common";
import { ROLES } from "./Users";

interface SsoPublic {
  provider: string;
  issuer: string;
  client_id: string;
  secret_last4: string;
  roles_claim: string;
  group_roles: Record<string, string[]>;
  tested_by: string | null;
  tested_at: string | null;
  turned_on_by: string | null;
}
interface SsoView {
  on: boolean;
  managed_by_install: boolean;
  redirect_uri: string | null;
  can_edit: boolean;
  active: SsoPublic | null;
  draft: SsoPublic | null;
}

type Provider = "entra" | "okta" | "google" | "oidc";
const PROVIDERS: { value: Provider; label: string }[] = [
  { value: "entra", label: "Microsoft Entra ID" },
  { value: "okta", label: "Okta" },
  { value: "google", label: "Google" },
  { value: "oidc", label: "Other (OpenID Connect)" },
];

const TEST_RESULTS: Record<string, { ok: boolean; text: string }> = {
  ok: { ok: true, text: "Test sign-in worked. You can now turn SSO on." },
  not_admin: {
    ok: false,
    text: "The test sign-in worked, but that account would not be an org admin. Sign in with your own account (the same email as here), or map your admin group to Org admin.",
  },
  no_role: {
    ok: false,
    text: "The provider signed you in, but none of your groups gives a Captain role and your email is not a Captain user.",
  },
  denied: {
    ok: false,
    text: "The provider refused the sign-in (check the redirect address and that you are assigned to the app).",
  },
  state: {
    ok: false,
    text: "The test took too long or was started twice. Try again.",
  },
  unreachable: {
    ok: false,
    text: "Captain could not reach the provider. Check the tenant, domain or address.",
  },
  failed: {
    ok: false,
    text: "The provider's answer could not be verified. Check the client ID and secret.",
  },
  forbidden: { ok: false, text: "Only an org admin can test sign-in." },
  no_settings: { ok: false, text: "Save the settings first." },
};

/** issuer from the friendly field per provider */
function issuerOf(p: Provider, v: string): string {
  const x = v.trim().replace(/\/+$/, "");
  if (p === "entra")
    return x.startsWith("https://")
      ? x
      : `https://login.microsoftonline.com/${x}/v2.0`;
  if (p === "okta") return x.startsWith("https://") ? x : `https://${x}`;
  if (p === "google") return "https://accounts.google.com";
  return x;
}
function fieldOf(p: Provider, issuer: string): string {
  const m = issuer.match(
    /^https:\/\/login\.microsoftonline\.com\/([^/]+)\/v2\.0$/,
  );
  if (p === "entra" && m?.[1]) return m[1];
  if (p === "okta") return issuer.replace(/^https:\/\//, "");
  return issuer;
}

export function SsoSetup() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["sso-settings"],
    queryFn: () => api.get<SsoView>("/sso/settings"),
  });
  const [result] = useState(() => {
    const code = new URLSearchParams(window.location.search).get("sso_test");
    if (code) window.history.replaceState({}, "", window.location.pathname);
    return code ? (TEST_RESULTS[code] ?? TEST_RESULTS.failed) : null;
  });
  const v = q.data;
  const cur = v?.draft ?? v?.active ?? null;
  const [provider, setProvider] = useState<Provider>("entra");
  const [where, setWhere] = useState("");
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [groups, setGroups] = useState<{ group: string; roles: string[] }[]>(
    [],
  );
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    if (!cur) return;
    const p = (cur.provider as Provider) || "oidc";
    setProvider(p);
    setWhere(fieldOf(p, cur.issuer));
    setClientId(cur.client_id);
    setGroups(
      Object.entries(cur.group_roles).map(([group, roles]) => ({
        group,
        roles,
      })),
    );
  }, [cur?.issuer, cur?.client_id, cur?.provider]); // eslint-disable-line react-hooks/exhaustive-deps

  if (q.isLoading || !v) return null;
  const refresh = () => qc.invalidateQueries({ queryKey: ["sso-settings"] });
  const run = async (what: string, fn: () => Promise<unknown>) => {
    setBusy(what);
    try {
      await fn();
      await refresh();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(null);
    }
  };

  if (v.managed_by_install)
    return (
      <Text size="sm" c="dimmed">
        SSO is set by this install's settings ({v.active?.issuer}). Ask whoever
        runs your Captain to change it.
      </Text>
    );
  if (!v.can_edit)
    return (
      <Alert color="yellow">
        SSO cannot be set up here yet: the install needs its public console
        address and a secret store. Ask whoever runs your Captain (Stixis).
      </Alert>
    );

  const tested = !!v.draft?.tested_at;
  const whereLabel = {
    entra: "Directory (tenant) ID",
    okta: "Okta domain",
    google: "",
    oidc: "Issuer URL",
  }[provider];
  const wherePh = {
    entra: "00000000-0000-0000-0000-000000000000",
    okta: "abc.okta.com",
    google: "",
    oidc: "https://login.example.com",
  }[provider];
  return (
    <Stack gap="sm">
      {result && (
        <Alert color={result.ok ? "green" : "red"}>{result.text}</Alert>
      )}
      {v.on ? (
        <Group>
          <Badge color="green" leftSection={<IconCheck size={12} />}>
            SSO is on
          </Badge>
          <Text size="sm" c="dimmed">
            {v.active?.issuer} · turned on by {v.active?.turned_on_by}
          </Text>
        </Group>
      ) : (
        <Text size="sm">
          Connect your identity provider so everyone signs in with their company
          account (and its password and MFA). Until then you are signed in with
          a one-time link.
        </Text>
      )}
      <Stack gap={4}>
        <Text size="sm" fw={500}>
          1. Register Captain in your provider
        </Text>
        <Text size="sm" c="dimmed">
          Create a web app called "Captain" with this redirect address, and add
          a groups claim to its tokens:
        </Text>
        <Group gap="xs" wrap="nowrap">
          <Code style={{ flex: 1 }}>{v.redirect_uri}</Code>
          <CopyButton value={v.redirect_uri ?? ""}>
            {({ copied, copy }) => (
              <Button size="xs" variant="default" onClick={copy}>
                {copied ? "Copied" : "Copy"}
              </Button>
            )}
          </CopyButton>
        </Group>
      </Stack>
      <Text size="sm" fw={500}>
        2. Enter its details
      </Text>
      <SegmentedControl
        data={PROVIDERS}
        value={provider}
        onChange={(p) => {
          setProvider(p as Provider);
          setWhere("");
        }}
      />
      {provider !== "google" && (
        <TextInput
          label={whereLabel}
          placeholder={wherePh}
          value={where}
          onChange={(e) => setWhere(e.currentTarget.value)}
          required
        />
      )}
      <Group grow>
        <TextInput
          label="Application (client) ID"
          value={clientId}
          onChange={(e) => setClientId(e.currentTarget.value)}
          required
        />
        <PasswordInput
          label="Client secret"
          placeholder={
            cur ? `stored (…${cur.secret_last4}); leave empty to keep` : ""
          }
          description="Stored encrypted; never shown again"
          value={secret}
          onChange={(e) => setSecret(e.currentTarget.value)}
          autoComplete="off"
        />
      </Group>
      <Stack gap={4}>
        <Text size="sm">
          Groups → Captain roles{" "}
          <Text span c="dimmed">
            (optional: roles given on the Users page also count)
          </Text>
        </Text>
        {groups.map((g, i) => (
          <Group key={i} gap="xs" wrap="nowrap">
            <TextInput
              placeholder={
                provider === "entra" ? "group object ID" : "group name"
              }
              value={g.group}
              onChange={(e) =>
                setGroups(
                  groups.map((x, j) =>
                    j === i ? { ...x, group: e.currentTarget.value } : x,
                  ),
                )
              }
              style={{ flex: 1 }}
            />
            <MultiSelect
              data={ROLES.map((r) => ({
                value: r,
                label: roleText([r], false),
              }))}
              value={g.roles}
              onChange={(roles) =>
                setGroups(groups.map((x, j) => (j === i ? { ...x, roles } : x)))
              }
              style={{ flex: 1 }}
            />
            <ActionIcon
              variant="subtle"
              color="red"
              aria-label="Remove"
              onClick={() => setGroups(groups.filter((_, j) => j !== i))}
            >
              <IconTrash size={16} />
            </ActionIcon>
          </Group>
        ))}
        <Button
          size="xs"
          variant="subtle"
          leftSection={<IconPlus size={14} />}
          onClick={() => setGroups([...groups, { group: "", roles: [] }])}
          w="fit-content"
        >
          Add a group
        </Button>
      </Stack>
      <Group>
        <Button
          variant="default"
          loading={busy === "save"}
          disabled={
            !clientId.trim() ||
            (provider !== "google" && !where.trim()) ||
            (!cur && secret.trim().length < 16)
          }
          onClick={() =>
            run("save", async () => {
              await api.put("/sso/settings", {
                provider,
                issuer: issuerOf(provider, where),
                client_id: clientId.trim(),
                client_secret: secret.trim() || null,
                group_roles: Object.fromEntries(
                  groups
                    .filter((g) => g.group.trim() && g.roles.length)
                    .map((g) => [g.group.trim(), g.roles]),
                ),
              });
              setSecret("");
              notifyOk("Saved. Now test the sign-in.");
            })
          }
        >
          Save
        </Button>
        <Button
          variant="default"
          disabled={!v.draft}
          loading={busy === "check"}
          onClick={() =>
            run("check", async () => {
              await api.post("/sso/check");
              notifyOk("Captain can reach your provider");
            })
          }
        >
          Check connection
        </Button>
      </Group>
      <Text size="sm" fw={500}>
        3. Test, then turn on
      </Text>
      <List size="sm" c="dimmed">
        <List.Item>
          Test sign-in takes you to your provider and back. Sign in with your
          own account: you stay signed in here either way.
        </List.Item>
        <List.Item>
          Turning SSO on ends one-time and email-link sign-ins; you then sign in
          with SSO.
        </List.Item>
      </List>
      <Group>
        <Button
          component="a"
          href="/admin/v1/auth/login?test=1"
          variant="default"
          disabled={!v.draft}
        >
          Test sign-in
        </Button>
        {tested && (
          <Text size="sm" c="green">
            Tested by {v.draft?.tested_by}
          </Text>
        )}
        {!v.on && (
          <Button
            color="green"
            disabled={!tested}
            loading={busy === "on"}
            onClick={() =>
              run("on", async () => {
                await api.post("/sso/turn-on");
                window.location.href = "/"; // this session has ended: sign in with SSO
              })
            }
          >
            Turn on SSO
          </Button>
        )}
        {v.on && (
          <>
            {tested && v.draft?.tested_at !== v.active?.tested_at && (
              <Button
                color="green"
                loading={busy === "on"}
                onClick={() =>
                  run("on", async () => {
                    await api.post("/sso/turn-on");
                    notifyOk("New SSO settings are in use");
                  })
                }
              >
                Use the new settings
              </Button>
            )}
            <Button
              color="red"
              variant="subtle"
              loading={busy === "off"}
              onClick={() =>
                window.confirm(
                  "Turn SSO off? Everyone's SSO sign-in ends, including yours. You then need a new one-time link from Stixis, or an email sign-in link.",
                ) &&
                run("off", async () => {
                  await api.post("/sso/turn-off");
                  window.location.href = "/";
                })
              }
            >
              Turn SSO off
            </Button>
          </>
        )}
      </Group>
    </Stack>
  );
}
