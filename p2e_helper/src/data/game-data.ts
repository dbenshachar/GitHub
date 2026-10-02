import type { Ability, GameAncestry, GameBackground, GameClass, GameFeat, GameItem, GameSpell, SkillKey } from "@/lib/types";
import ingestedRaw from "./ingested-data.json";

// Type the imported JSON
const ingested = ingestedRaw as {
  generatedAt: string;
  config: Record<string, unknown>;
  ancestries: Array<GameAncestry & { description?: string; fullDescription?: string }>;
  classes: Array<GameClass & { roleSummary?: string; description?: string; fullDescription?: string; featProgression?: Record<string, unknown> }>;
  backgrounds: Array<GameBackground & { description?: string }>;
  feats: Array<GameFeat & { description?: string }>;
  spells: Array<GameSpell & { description?: string }>;
  items: Array<GameItem & { summary?: string; description?: string }>;
};

export const dataSources = [
  "Vendored raw JSON from Pf2eToolsOrg/Pf2eTools dev branch under src/data/pf2etools/raw.",
  "Normalized via scripts/ingest.mjs — config-driven, expandable by editing CONFIG.",
  `Ingested at: ${ingested.generatedAt}. Scope: 4 ancestries, 4 classes, ${ingested.feats.length} feats, ${ingested.spells.length} spells.`
];

export const gameData = {
  ancestries: ingested.ancestries as (GameAncestry & { description?: string; fullDescription?: string })[],
  classes: ingested.classes as (GameClass & { roleSummary?: string; description?: string; fullDescription?: string })[],
  backgrounds: ingested.backgrounds as (GameBackground & { description?: string })[],
  feats: ingested.feats as (GameFeat & { description?: string })[],
  spells: ingested.spells as (GameSpell & { description?: string })[],
  items: ingested.items as (GameItem & { summary?: string; description?: string })[]
};

export const allSkills: SkillKey[] = [
  "acrobatics",
  "arcana",
  "athletics",
  "crafting",
  "deception",
  "diplomacy",
  "intimidation",
  "medicine",
  "nature",
  "occultism",
  "performance",
  "religion",
  "society",
  "stealth",
  "survival",
  "thievery"
];

export const remasterScope = ["PC1", "PC2", "GMC"];

export function titleCase(value: string) {
  return value.replace(/(^|[-\s])\S/g, (letter) => letter.toUpperCase()).replaceAll("-", " ");
}

export function abilityLabel(ability: Ability) {
  return titleCase(ability).slice(0, 3).toUpperCase();
}
