import { describe, expect, it } from "vitest";
import { abilityModifier, applyBoost, armorClass, proficiencyBonus, skillModifier } from "./calculations";
import { sampleCharacters } from "../data/sample-characters";
import { gameData } from "../data/game-data";

describe("PF2e calculation engine", () => {
  it("calculates ability modifiers", () => {
    expect(abilityModifier(10)).toBe(0);
    expect(abilityModifier(18)).toBe(4);
    expect(abilityModifier(8)).toBe(-1);
  });

  it("uses remaster boost size above 18", () => {
    expect(applyBoost(16)).toBe(18);
    expect(applyBoost(18)).toBe(19);
  });

  it("does not add level for untrained proficiency", () => {
    expect(proficiencyBonus("untrained", 7)).toBe(0);
    expect(proficiencyBonus("trained", 7)).toBe(9);
    expect(proficiencyBonus("legendary", 7)).toBe(15);
  });

  it("calculates AC and armor skill penalty from character state", () => {
    const fighter = sampleCharacters[0];
    const chain = gameData.items.find((item) => item.id === "chain-mail");
    expect(armorClass(fighter, chain)).toBe(18);
    expect(skillModifier(fighter, "athletics", chain)).toBe(5);
  });
});
