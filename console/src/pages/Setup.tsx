import {
  Badge,
  Button,
  Group,
  Paper,
  Stack,
  TagsInput,
  Text,
  TextInput,
  ThemeIcon,
  Title,
} from "@mantine/core";
import { IconCheck, IconCircleDashed } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, type Organization } from "../api";
import { can, useMe } from "../App";
import { SsoSetup } from "./SsoSetup";
import { SetupSteps } from "../components/SetupSteps";
import {
  notifyError,
  notifyOk,
  PageHeader,
  QueryState,
} from "../components/common";

/** Onboarding checklist for a new install. The first item is always connecting SSO. */
export function SetupPage() {
  const me = useMe().data;
  const nav = useNavigate();
  const q = useQuery({
    queryKey: ["organization"],
    queryFn: () => api.get<Organization>("/organization"),
  });
  return (
    <>
      <PageHeader
        title="Set up Captain"
        description="A short checklist to get your company ready. You can come back to it at any time."
      />
      <QueryState isLoading={q.isLoading} error={q.error}>
        <Paper withBorder p="md" mb="md">
          <Title order={4}>Ready for the first AI call</Title>
          <Text size="sm" c="dimmed" mb="sm">
            What every company needs before people can use AI through Captain.
            Each step opens right here and ticks itself.
          </Text>
          <SetupSteps onKeyStep={() => nav("/keys")} />
        </Paper>
        {q.data && <Checklist org={q.data} canWrite={can(me, "org:write")} />}
      </QueryState>
    </>
  );
}

function Checklist({
  org,
  canWrite,
}: {
  org: Organization;
  canWrite: boolean;
}) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const save = async (patch: Partial<Organization>, msg: string) => {
    const m = { ...org, ...patch };
    try {
      await api.put("/organization", {
        company_name: m.company_name,
        legal_name: m.legal_name,
        domains: m.domains,
        country: m.country,
        industry: m.industry,
        contacts: m.contacts,
        adopters: m.adopters,
        checklist: m.checklist,
      });
      notifyOk(msg);
      await qc.invalidateQueries({ queryKey: ["organization"] });
    } catch (e) {
      notifyError(e);
    }
  };
  return (
    <Stack>
      {org.checklist_items.map((item) => (
        <Paper key={item.key} withBorder p="md">
          <Group align="flex-start" wrap="nowrap">
            <ThemeIcon
              variant="light"
              color={item.done ? "teal" : "gray"}
              radius="xl"
            >
              {item.done ? (
                <IconCheck size={16} />
              ) : (
                <IconCircleDashed size={16} />
              )}
            </ThemeIcon>
            <Stack gap="xs" style={{ flex: 1 }}>
              <Group justify="space-between">
                <Title order={4}>{item.label}</Title>
                {item.done && (
                  <Badge color="teal" variant="light">
                    Done
                  </Badge>
                )}
              </Group>
              {item.key === "sso" && <SsoStep done={item.done} />}
              {item.key === "profile" && (
                <ProfileStep
                  org={org}
                  canWrite={canWrite}
                  onSave={(p) =>
                    save(
                      { ...p, checklist: { ...org.checklist, profile: true } },
                      "Company profile saved",
                    )
                  }
                />
              )}
              {item.key === "users" && (
                <Group>
                  <Text size="sm" c="dimmed">
                    Add people and give them console roles. Roles add to what
                    your sign-in provider grants.
                  </Text>
                  <Button
                    size="xs"
                    variant="default"
                    onClick={() => nav("/users")}
                  >
                    Open Users
                  </Button>
                  {canWrite && (
                    <Button
                      size="xs"
                      variant="subtle"
                      onClick={() =>
                        void save(
                          {
                            checklist: { ...org.checklist, users: !item.done },
                          },
                          "Saved",
                        )
                      }
                    >
                      {item.done ? "Mark as not done" : "Mark as done"}
                    </Button>
                  )}
                </Group>
              )}
              {item.key === "adopters" && (
                <Group gap="xs">
                  <Text size="sm" c="dimmed">
                    {org.adopters.length
                      ? `Chosen: ${org.adopters.join(", ")}`
                      : "Nothing chosen yet."}
                  </Text>
                  <Button
                    size="xs"
                    variant="light"
                    onClick={() => nav("/connect")}
                  >
                    Choose tools and get downloads
                  </Button>
                </Group>
              )}
            </Stack>
          </Group>
        </Paper>
      ))}
    </Stack>
  );
}

function SsoStep(_: { done: boolean }) {
  return <SsoSetup />;
}

function ProfileStep({
  org,
  canWrite,
  onSave,
}: {
  org: Organization;
  canWrite: boolean;
  onSave: (p: Partial<Organization>) => Promise<void>;
}) {
  const [company, setCompany] = useState(org.company_name);
  const [legal, setLegal] = useState(org.legal_name);
  const [domains, setDomains] = useState<string[]>(org.domains);
  const [country, setCountry] = useState(org.country);
  const [industry, setIndustry] = useState(org.industry);
  const [contactName, setContactName] = useState(org.contacts[0]?.name ?? "");
  const [contactEmail, setContactEmail] = useState(
    org.contacts[0]?.email ?? "",
  );
  const [busy, setBusy] = useState(false);
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        const rest = org.contacts.slice(1);
        const contacts =
          contactName && contactEmail
            ? [
                { role: "primary", name: contactName, email: contactEmail },
                ...rest,
              ]
            : rest;
        await onSave({
          company_name: company,
          legal_name: legal,
          domains,
          country,
          industry,
          contacts,
        });
        setBusy(false);
      }}
    >
      <Stack gap="xs">
        <Group grow>
          <TextInput
            label="Company name"
            value={company}
            onChange={(e) => setCompany(e.currentTarget.value)}
            disabled={!canWrite}
            required
          />
          <TextInput
            label="Legal name"
            value={legal}
            onChange={(e) => setLegal(e.currentTarget.value)}
            disabled={!canWrite}
          />
        </Group>
        <TagsInput
          label="Email domains"
          description="For example: acme.com"
          value={domains}
          onChange={setDomains}
          disabled={!canWrite}
        />
        <Group grow>
          <TextInput
            label="Country"
            value={country}
            onChange={(e) => setCountry(e.currentTarget.value)}
            disabled={!canWrite}
          />
          <TextInput
            label="Industry"
            value={industry}
            onChange={(e) => setIndustry(e.currentTarget.value)}
            disabled={!canWrite}
          />
        </Group>
        <Group grow>
          <TextInput
            label="Primary contact"
            value={contactName}
            onChange={(e) => setContactName(e.currentTarget.value)}
            disabled={!canWrite}
          />
          <TextInput
            label="Contact email"
            type="email"
            value={contactEmail}
            onChange={(e) => setContactEmail(e.currentTarget.value)}
            disabled={!canWrite}
          />
        </Group>
        {canWrite && (
          <Group justify="flex-end">
            <Button type="submit" loading={busy}>
              Save profile
            </Button>
          </Group>
        )}
      </Stack>
    </form>
  );
}
