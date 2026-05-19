/**
 * Themed wrapper around ink-spinner. Colors default to text.primary so
 * the spinner reads as ambient motion rather than competing with the
 * thoughtSubject text (which gets text.accent). Tmux fallback handled
 * inside ``ink-spinner`` once we pass the right type from
 * ``theme/spinners``.
 */

import { Text } from "ink";
import InkSpinner from "ink-spinner";
import type { SpinnerName } from "cli-spinners";
import { Theme } from "../../theme/colors.js";

interface SpinnerProps {
  type?: SpinnerName;
  color?: string;
}

export const Spinner: React.FC<SpinnerProps> = ({
  type = "dots",
  color = Theme.text.primary,
}) => (
  <Text color={color}>
    <InkSpinner type={type} />
  </Text>
);
