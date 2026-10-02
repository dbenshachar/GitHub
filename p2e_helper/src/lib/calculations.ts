import { abilities, type Ability, type AbilityScores, type Character, type GameAncestry, type GameClass, type GameItem, type ProficiencyRank, type SkillKey } from "./types";

export const baseAbilityScores: AbilityScores = {
  strength: 10,
  dexterity: 10,
  constitution: 10,
  intelligence: 10,
  wisdom: 10,
  charisma: 10
};

export const skillAbilities: Record<SkillKey, Ability> = {
  acrobatics: "dexterity",
  arcana: "intelligence",
  athletics: "strength",
  crafting: "intelligence",
  deception: "charisma",
  diplomacy: "charisma",
  intimidation: "charisma",
  medicine: "wisdom",
  nature: "wisdom",
  occultism: "intelligence",
  performance: "charisma",
  religion: "wisdom",
  society: "intelligence",
  stealth: "dexterity",
  survival: "wisdom",
  thievery: "dexterity"
};

export function abilityModifier(score: number) {
  return Math.floor((score - 10) / 2);
}

export function proficiencyBonus(rank: ProficiencyRank, level: number) {
  if (rank === "untrained") return 0;
  return level + { trained: 2, expert: 4, master: 6, legendary: 8 }[rank];
}

export function applyBoost(score: number) {
  return score >= 18 ? score + 1 : score + 2;
}

export function applyAbilityBoosts(base: AbilityScores, boosts: Ability[], flaws: Ability[] = []) {
  const next = { ...base };
  for (const ability of boosts) next[ability] = applyBoost(next[ability]);
  for (const ability of flaws) next[ability] -= 2;
  return next;
}

export function buildAbilityScores(character: Pick<Character, "ancestryFreeBoosts" | "backgroundBoosts" | "classBoost" | "freeBoosts">, ancestry: GameAncestry) {
  const ancestryBoosts = [...ancestry.boosts.fixed, ...character.ancestryFreeBoosts];
  const allBoosts = [...ancestryBoosts, ...character.backgroundBoosts, character.classBoost, ...character.freeBoosts];
  return applyAbilityBoosts(baseAbilityScores, allBoosts, ancestry.flaws);
}

export function validateBoostStep(boosts: Ability[], expected: number) {
  return boosts.length === expected && new Set(boosts).size === boosts.length;
}

export function maxHp(character: Character, ancestry: GameAncestry, characterClass: GameClass) {
  const con = abilityModifier(character.abilityScores.constitution);
  return ancestry.hp + character.level * (characterClass.hp + con);
}

export function armorClass(character: Character, armor?: GameItem) {
  const dexMod = abilityModifier(character.abilityScores.dexterity);
  const dexApplied = typeof armor?.dexCap === "number" ? Math.min(dexMod, armor.dexCap) : dexMod;
  const armorBonus = armor?.armorBonus ?? 0;
  const profRank = armor ? character.armorProficiencies[armor.category] ?? "untrained" : character.armorProficiencies["unarmored"] ?? "untrained";
  return 10 + dexApplied + armorBonus + proficiencyBonus(profRank, character.level);
}

export function saveModifier(character: Character, save: "fortitude" | "reflex" | "will") {
  const ability: Ability = save === "fortitude" ? "constitution" : save === "reflex" ? "dexterity" : "wisdom";
  return abilityModifier(character.abilityScores[ability]) + proficiencyBonus(character.saves[save], character.level);
}

export function skillModifier(character: Character, skill: SkillKey, armor?: GameItem) {
  const penalty = armor?.checkPenalty ?? 0;
  const affected = skill === "acrobatics" || skill === "athletics" || skill === "stealth" || skill === "thievery";
  return abilityModifier(character.abilityScores[skillAbilities[skill]]) + proficiencyBonus(character.skills[skill], character.level) + (affected ? penalty : 0);
}

export function classDc(character: Character) {
  return 10 + abilityModifier(character.abilityScores[character.keyAbility]) + proficiencyBonus(character.classDc, character.level);
}

export function spellDc(character: Character) {
  if (!character.spellcasting) return undefined;
  return 10 + abilityModifier(character.abilityScores[character.spellcasting.ability]) + proficiencyBonus(character.classDc, character.level);
}

export function bulkTotal(character: Character, items: GameItem[]) {
  const byId = new Map(items.map((item) => [item.id, item]));
  return character.inventory.reduce((sum, entry) => sum + (byId.get(entry.id)?.bulk ?? 0) * entry.quantity, 0);
}

export function emptySkillRanks(): Record<SkillKey, ProficiencyRank> {
  return Object.fromEntries(Object.keys(skillAbilities).map((skill) => [skill, "untrained"])) as Record<SkillKey, ProficiencyRank>;
}

export function normalizeAbility(value: string): Ability | undefined {
  const lower = value.toLowerCase();
  return abilities.find((ability) => ability === lower);
}
