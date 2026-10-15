// Copyright (c) 2026 shipap. SPDX-License-Identifier: AGPL-3.0-only
import type { ReactElement } from "react";
export type CodeSlotsStatus = "idle" | "error" | "success";
export type CodeSlotsProps = {
  length?: number;
  value?: string;
  defaultValue?: string;
  onChange?: (code: string) => void;
  onComplete?: (code: string) => void | Promise<void>;
  status?: CodeSlotsStatus;
  mask?: boolean;
  caret?: boolean;
  disabled?: boolean;
  autoFocus?: boolean;
  required?: boolean;
  ariaLabel?: string;
  ariaDescribedBy?: string;
  name?: string;
  className?: string;
  accentColor?: string;
  inkColor?: string;
  slotColor?: string;
  digitColor?: string;
  dangerColor?: string;
  slotSize?: number;
  gap?: number;
  radius?: number;
  // Устаревшие параметры движения принимаются без переноса прежних эффектов.
  bounce?: number;
  settle?: number;
  rise?: number;
  cascade?: number;
};
export const authCodeSlotsProps: CodeSlotsProps;
export default function CodeSlots(props: CodeSlotsProps): ReactElement;
