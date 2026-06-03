/**
 * Themed wrapper around ink-spinner. Colors default to text.primary so
 * the spinner reads as ambient motion rather than competing with the
 * thoughtSubject text (which gets text.accent). Tmux fallback handled
 * inside ``ink-spinner`` once we pass the right type from
 * ``theme/spinners``.
 *
 * Memoised because the only meaningful prop changes (type / color) are
 * rare — parent re-renders triggered by useLoadingIndicator hook state
 * (10-20Hz under LLM streaming) would otherwise reconcile this subtree
 * pointlessly. InkSpinner's own setInterval still drives the frame
 * advance at its native cadence; the memo only blocks parent-triggered
 * reconciles, not the spinner's intrinsic animation.
 */

import { Text } from "ink";
import InkSpinner from "ink-spinner";
import { memo } from "react";
import type { SpinnerName } from "cli-spinners";
import { Theme } from "../../theme/colors.js";

interface SpinnerProps {
  type?: SpinnerName;
  color?: string;
}

const SpinnerInternal: React.FC<SpinnerProps> = ({
  type = "dots",
  color = Theme.text.primary,
}) => (
  <Text color={color}>
    <InkSpinner type={type} />
  </Text>
);

export const Spinner = memo(SpinnerInternal);
