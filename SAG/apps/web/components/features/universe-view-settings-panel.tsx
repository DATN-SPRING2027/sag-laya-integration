"use client";

import { RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";

import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Slider } from "@/components/ui/slider";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  UNIVERSE_VIEW_LIMITS,
  normalizeUniverseViewPreferences,
  type UniverseViewPreferences,
} from "@/lib/universe-view-preferences";
import { cn } from "@/lib/utils";

export interface UniverseViewSettingsProps {
  preferences: UniverseViewPreferences;
  onChange: (preferences: UniverseViewPreferences) => void;
  onReset: () => void;
  compact?: boolean;
  isMobile?: boolean;
}

function SettingSlider({
  ariaLabel,
  value,
  min,
  max,
  step,
  recommended,
  onChange,
}: {
  ariaLabel: string;
  value: number;
  min: number;
  max: number;
  step: number;
  recommended: number;
  onChange: (value: number) => void;
}) {
  const t = useTranslations("GraphSettings");
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-muted-foreground">{t("value.current")}</span>
        <span className="font-mono font-semibold tabular-nums">{value}</span>
      </div>
      <Slider
        aria-label={ariaLabel}
        max={max}
        min={min}
        onValueChange={([next]) => {
          if (next !== undefined) onChange(next);
        }}
        step={step}
        value={[value]}
      />
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>{min}</span>
        <span>{t("value.recommended", { count: recommended })}</span>
        <span>{max}</span>
      </div>
    </div>
  );
}

export function UniverseViewSettings({
  preferences,
  onChange,
  onReset,
  compact = false,
  isMobile,
}: UniverseViewSettingsProps) {
  const t = useTranslations("KnowledgeUniverse.knowledge");
  const graphText = useTranslations("GraphSettings");
  const detectedMobile = useIsMobile();
  const normalized = normalizeUniverseViewPreferences(preferences);
  const emit = (patch: Partial<UniverseViewPreferences>) => {
    onChange(normalizeUniverseViewPreferences({ ...normalized, ...patch }));
  };

  return (
    <div className={cn("flex flex-col", compact ? "gap-4" : "gap-6")}
      data-settings-section="graph" data-settings-device={(isMobile ?? detectedMobile) ? "mobile" : "desktop"}>
      <SettingsSection title={t("settings.cardsTitle")} description={t("settings.cardsHint")}>
        <SettingsRow title={t("settings.cardsEnabled")} description={t("settings.cardsEnabledHint")} layout="inline">
          <Checkbox aria-label={t("settings.cardsEnabled")} checked={normalized.cardsEnabled}
            onCheckedChange={(value) => emit({ cardsEnabled: value === true })} />
        </SettingsRow>
        <SettingsRow title={t("settings.previewTitle")} description={t("settings.previewHint")}>
          <SettingSlider ariaLabel={t("settings.previewTitle")} value={normalized.eventCardPreviewCount}
            min={UNIVERSE_VIEW_LIMITS.eventCardPreviewCount.min}
            max={Math.min(normalized.eventWindowSize, UNIVERSE_VIEW_LIMITS.eventCardPreviewCount.max)}
            step={1} recommended={UNIVERSE_VIEW_LIMITS.eventCardPreviewCount.default}
            onChange={(eventCardPreviewCount) => emit({ eventCardPreviewCount })} />
        </SettingsRow>
      </SettingsSection>
      <SettingsSection title={t("settings.windowTitle")} description={t("settings.windowHint")}>
        <SettingsRow title={t("settings.pageTitle")} description={t("settings.pageHint")}>
          <SettingSlider ariaLabel={t("settings.pageTitle")} value={Math.min(50, normalized.eventWindowSize)}
            min={UNIVERSE_VIEW_LIMITS.eventWindowSize.min} max={50} step={5} recommended={50}
            onChange={(eventWindowSize) => emit({ eventWindowSize })} />
        </SettingsRow>
      </SettingsSection>
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card px-4 py-3">
        <p className="text-xs text-muted-foreground">{graphText("reset.savedLocally")}</p>
        <Button type="button" variant="outline" size="sm" onClick={onReset}>
          <RotateCcw />{graphText("reset.action")}
        </Button>
      </div>
    </div>
  );
}
