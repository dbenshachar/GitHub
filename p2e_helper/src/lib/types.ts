export const abilities = [
  "strength",
  "dexterity",
  "constitution",
  "intelligence",
  "wisdom",
  "charisma"
] as const;

export type Ability = (typeof abilities)[number];
export type AbilityScores = Record<Ability, number>;
export type ProficiencyRank = "untrained" | "trained" | "expert" | "master" | "legendary";
export type SaveKey = "fortitude" | "reflex" | "will";

export type SkillKey =
  | "acrobatics"
  | "arcana"
  | "athletics"
  | "crafting"
  | "deception"
  | "diplomacy"
  | "intimidation"
  | "medicine"
  | "nature"
  | "occultism"
  | "performance"
  | "religion"
  | "society"
  | "stealth"
  | "survival"
  | "thievery";

export interface ChoiceBoost {
  fixed: Ability[];
  options: Ability[];
  free: number;
}

export interface GameAncestry {
  id: string;
  name: string;
  source: string;
  hp: number;
  size: string;
  speed: number;
  boosts: ChoiceBoost;
  flaws: Ability[];
  traits: string[];
  languages: string[];
  heritages: Array<{ id: string; name: string; summary: string; description?: string }>;
  features: Array<{ name: string; summary: string }>;
  description?: string;
  fullDescription?: string;
  dataNotes?: string[];
}

export interface GameBackground {
  id: string;
  name: string;
  source: string;
  boostOptions: Ability[];
  skills: SkillKey[];
  lore: string[];
  feat: string;
  summary: string;
  description?: string;
}

export interface GameClass {
  id: string;
  name: string;
  source: string;
  hp: number;
  keyAbilityOptions: Ability[];
  roleSummary?: string;
  description?: string;
  fullDescription?: string;
  perception: ProficiencyRank;
  saves: Record<SaveKey, ProficiencyRank>;
  classDc: ProficiencyRank;
  armor: Record<string, ProficiencyRank>;
  weapons: Record<string, ProficiencyRank>;
  trainedSkills: SkillKey[];
  additionalTrainedSkills: number;
  caster?: {
    tradition: "arcane" | "divine" | "occult" | "primal" | "choice";
    ability: Ability;
    mode: "prepared" | "spontaneous" | "flexible";
    cantrips: number;
    rank1Slots: number;
    dataStatus: string;
  };
  level1Features: string[];
  dataNotes?: string[];
}

export interface GameFeat {
  id: string;
  name: string;
  source: string;
  level: number;
  category: "ancestry" | "class" | "skill" | "general";
  traits: string[];
  prereq?: string;
  actionCost?: string;
  summary: string;
  description?: string;
}

export interface GameSpell {
  id: string;
  name: string;
  source: string;
  rank: number;
  traditions: string[];
  traits: string[];
  summary: string;
  description?: string;
}

export interface GameItem {
  id: string;
  name: string;
  source: string;
  category: "armor" | "weapon" | "gear";
  priceSp: number;
  bulk: number;
  armorBonus?: number;
  dexCap?: number;
  checkPenalty?: number;
  damage?: string;
  traits?: string[];
  dataStatus?: string;
  summary?: string;
  description?: string;
}

export interface CharacterFeat {
  id: string;
  source: "ancestry" | "background" | "class" | "skill" | "general" | "manual";
}

export interface CharacterInventoryItem {
  id: string;
  quantity: number;
  equipped?: boolean;
}

export interface CharacterSpellcasting {
  tradition: string;
  ability: Ability;
  mode: "prepared" | "spontaneous" | "flexible";
  spellIds: string[];
  slots: Record<string, number>;
}

export interface Character {
  schemaVersion: 1;
  id: string;
  name: string;
  pronouns: string;
  level: number;
  ancestryId: string;
  heritageId: string;
  backgroundId: string;
  classId: string;
  keyAbility: Ability;
  ancestryFreeBoosts: Ability[];
  backgroundBoosts: Ability[];
  classBoost: Ability;
  freeBoosts: Ability[];
  abilityScores: AbilityScores;
  skills: Record<SkillKey, ProficiencyRank>;
  loreSkills: string[];
  saves: Record<SaveKey, ProficiencyRank>;
  perception: ProficiencyRank;
  classDc: ProficiencyRank;
  armorProficiencies: Record<string, ProficiencyRank>;
  weaponProficiencies: Record<string, ProficiencyRank>;
  feats: CharacterFeat[];
  inventory: CharacterInventoryItem[];
  currency: { cp: number; sp: number; gp: number; pp: number };
  hp: { current: number; max: number; temp: number };
  spellcasting?: CharacterSpellcasting;
  languages: string[];
  edicts: string;
  anathema: string;
  conditions: Record<string, boolean>;
  notes: string;
  appearance: string;
  unresolvedRules: string[];
}
