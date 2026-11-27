import { Autocomplete } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";

import { api } from "../api";

/** Pick a team already in use, or type a new one. The server keeps one spelling per team,
 * so "payments" joins the existing "Payments" and reports stay together. */
export function TeamInput({
  value,
  onChange,
  label = "Team",
  description = "Pick an existing team or type a new one",
  placeholder = "e.g. Payments",
}: {
  value: string;
  onChange: (v: string) => void;
  label?: string;
  description?: string;
  placeholder?: string;
}) {
  const teams = useQuery({
    queryKey: ["teams"],
    queryFn: () => api.get<string[]>("/teams"),
  });
  return (
    <Autocomplete
      label={label}
      description={description}
      placeholder={placeholder}
      data={teams.data ?? []}
      value={value}
      onChange={onChange}
    />
  );
}
