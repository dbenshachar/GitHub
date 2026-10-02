#!/usr/bin/env node
/**
 * Config-driven Pf2eTools → normalized game data ingestion.
 * Proof-of-concept scope: 4 ancestries, 4 classes, ~12 backgrounds.
 * Expand content by editing CONFIG below, then run: node scripts/ingest.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, "..");
const RAW = path.join(ROOT, "src/data/pf2etools/raw");
const OUT = path.join(ROOT, "src/data/ingested-data.json");

const CONFIG = {
  ancestries: ["dwarf", "elf", "goblin"],
  // Human is manually defined (no pc1 file in raw)
  classes: ["fighter", "wizard", "rogue", "cleric"],
  backgrounds: [
    "Acolyte",
    "Acrobat",
    "Artisan",
    "Criminal",
    "Guard",
    "Merchant",
    "Noble",
    "Scholar",
    "Scout",
    "Street Urchin",
    "Warrior",
    "Farmhand"
  ],
  allowedSources: ["PC1", "PC2", "GMC"],
  classFeatTraits: ["fighter", "wizard", "rogue", "cleric"],
  ancestryFeatTraits: ["dwarf", "elf", "goblin", "human"],
  spellTraditions: ["arcane", "divine", "occult", "primal"],
  maxSpellRank: 3,
  maxFeatLevel: 20
};

const PROF_MAP = { U: "untrained", T: "trained", E: "expert", M: "master", L: "legendary" };
const SKILL_MAP = {
  Acrobatics: "acrobatics",
  Arcana: "arcana",
  Athletics: "athletics",
  Crafting: "crafting",
  Deception: "deception",
  Diplomacy: "diplomacy",
  Intimidation: "intimidation",
  Medicine: "medicine",
  Nature: "nature",
  Occultism: "occultism",
  Performance: "performance",
  Religion: "religion",
  Society: "society",
  Stealth: "stealth",
  Survival: "survival",
  Thievery: "thievery"
};

function slug(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
}

function readJson(relativePath) {
  return JSON.parse(fs.readFileSync(path.join(RAW, relativePath), "utf8"));
}

function stripTags(text) {
  if (typeof text !== "string") return "";
  return text
    .replace(/\{@\w+\s([^|}]+)(?:\|[^}]*)?\}/g, "$1")
    .replace(/\{@\w+\}/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function firstText(entries) {
  if (!entries) return "";
  for (const entry of entries) {
    if (typeof entry === "string") return stripTags(entry);
    if (entry?.entries) {
      const nested = firstText(entry.entries);
      if (nested) return nested;
    }
  }
  return "";
}

function fullText(entries, depth = 0) {
  if (!entries || depth > 4) return "";
  if (!Array.isArray(entries)) {
    if (typeof entries === "string") return stripTags(entries);
    return "";
  }
  const parts = [];
  for (const entry of entries) {
    if (typeof entry === "string") parts.push(stripTags(entry));
    else if (entry?.entries) parts.push(fullText(entry.entries, depth + 1));
    else if (entry?.name && entry?.entries) parts.push(`${entry.name}: ${fullText(entry.entries, depth + 1)}`);
  }
  return parts.filter(Boolean).join(" ");
}

function parseBoosts(rawBoosts) {
  const fixed = [];
  let free = 0;
  for (const boost of rawBoosts ?? []) {
    if (boost === "free") free += 1;
    else fixed.push(boost);
  }
  return { fixed, options: [], free };
}

function parseAncestryFile(id) {
  const data = readJson(`ancestries/ancestry-${id}-pc1.json`);
  const entry = data.ancestry[0];
  const heritages = (entry.heritage ?? []).slice(0, 4).map((h) => ({
    id: slug(h.shortName ?? h.name),
    name: h.name,
    summary: firstText(h.entries) || `${h.name} heritage.`,
    description: fullText(h.entries) || firstText(h.entries)
  }));
  return {
    id,
    name: entry.name,
    source: entry.source,
    hp: entry.hp,
    size: Array.isArray(entry.size) ? entry.size[0] : entry.size,
    speed: entry.speed?.walk ?? entry.speed ?? 25,
    boosts: parseBoosts(entry.boosts),
    flaws: entry.flaw ?? [],
    traits: entry.traits ?? [],
    languages: (entry.languages ?? [])
      .map((lang) => stripTags(lang))
      .filter((lang) => lang && !lang.includes("Additional languages")),
    heritages,
    features: (entry.features ?? []).map((f) => ({
      name: f.name,
      summary: firstText(f.entries)
    })),
    description: firstText(entry.flavor ?? entry.info),
    fullDescription: fullText(entry.flavor ?? entry.info ?? entry.entries)
  };
}

const HUMAN_MANUAL = {
  id: "human",
  name: "Human",
  source: "PC1",
  hp: 8,
  size: "medium",
  speed: 25,
  boosts: { fixed: [], options: [], free: 2 },
  flaws: [],
  traits: ["human", "humanoid"],
  languages: ["Common"],
  heritages: [
    {
      id: "skilled-heritage",
      name: "Skilled Heritage",
      summary: "Gain one additional trained skill of your choice.",
      description: "Humans with diverse backgrounds pick up a broad set of skills. You become trained in one skill of your choice."
    },
    {
      id: "versatile-heritage",
      name: "Versatile Heritage",
      summary: "Gain a general feat of your choice at 1st level.",
      description: "Human versatility manifests as an extra general feat at 1st level."
    },
    {
      id: "wintertouched-human",
      name: "Wintertouched Human",
      summary: "Cold resistance equal to half your level (minimum 1).",
      description: "You have adapted to cold climates and gain cold resistance equal to half your level (minimum 1)."
    }
  ],
  features: [
    {
      name: "Adaptable",
      summary: "Humans are versatile and ambitious, gaining two free ability boosts instead of fixed ones."
    }
  ],
  description: "Humans are adaptable and ambitious, found in nearly every corner of the world.",
  fullDescription:
    "Humans are adaptable and ambitious, found in nearly every corner of the world. Their drive and flexibility make them well suited to any class or background."
};

function parseKeyAbilities(raw) {
  const lower = raw.toLowerCase();
  const options = [];
  if (lower.includes("strength")) options.push("strength");
  if (lower.includes("dexterity")) options.push("dexterity");
  if (lower.includes("constitution")) options.push("constitution");
  if (lower.includes("intelligence")) options.push("intelligence");
  if (lower.includes("wisdom")) options.push("wisdom");
  if (lower.includes("charisma")) options.push("charisma");
  return options.length ? options : ["strength"];
}

function parseProficiencies(initial) {
  const saves = {
    fortitude: PROF_MAP[initial.fort] ?? "trained",
    reflex: PROF_MAP[initial.ref] ?? "trained",
    will: PROF_MAP[initial.will] ?? "trained"
  };
  const skills = [];
  for (const group of initial.skills?.t ?? []) {
    for (const skill of group.skill ?? []) {
      const mapped = SKILL_MAP[skill];
      if (mapped) skills.push(mapped);
    }
  }
  return {
    perception: PROF_MAP[initial.perception] ?? "trained",
    saves,
    classDc: PROF_MAP[initial.classDc?.prof] ?? "trained",
    trainedSkills: skills,
    additionalTrainedSkills: initial.skills?.add ?? 0
  };
}

function parseClassFile(id) {
  const data = readJson(`classes/class-${id}-pc1.json`);
  const entry = data.class[0];
  const prof = parseProficiencies(entry.initialProficiencies);
  const keyAbilityOptions = parseKeyAbilities(entry.keyAbility);
  const roleSummaries = {
    fighter: "Martial striker with expert weapons and reactive combat feats.",
    wizard: "Prepared arcane caster with thesis, school, and spellbook flexibility.",
    rogue: "Skill expert with sneak attack and racket-based tricks.",
    cleric: "Divine caster bound by deity edicts and anathema."
  };
  const casterMap = {
    wizard: { tradition: "arcane", ability: "intelligence", mode: "prepared", cantrips: 5, rank1Slots: 2 },
    cleric: { tradition: "divine", ability: "wisdom", mode: "prepared", cantrips: 5, rank1Slots: 2 }
  };
  const caster = casterMap[id];
  return {
    id,
    name: entry.name,
    source: entry.source,
    hp: entry.hp,
    keyAbilityOptions,
    roleSummary: roleSummaries[id] ?? firstText(entry.entries),
    description: firstText(entry.entries),
    fullDescription: fullText(entry.entries?.slice?.(0, 3) ?? entry.entries),
    perception: prof.perception,
    saves: prof.saves,
    classDc: prof.classDc,
    armor: { armor: "trained", unarmored: "trained" },
    weapons: { simple: "expert", martial: "expert", advanced: "trained", unarmed: "expert" },
    trainedSkills: prof.trainedSkills,
    additionalTrainedSkills: prof.additionalTrainedSkills,
    caster: caster
      ? {
          ...caster,
          dataStatus: id === "cleric" ? "Doctrine, deity, divine font, edicts, and anathema require manual entry." : "School/curriculum and thesis choices not automated yet."
        }
      : undefined,
    level1Features: (entry.classFeatures ?? entry.features ?? [])
      .filter((f) => f.level === 1 || !f.level)
      .slice(0, 6)
      .map((f) => f.name ?? f),
    featProgression: entry.advancement ?? {},
    dataNotes: id === "fighter" ? ["The class grants Acrobatics or Athletics plus additional skills; v1 trains both seed skills for reviewability."] : undefined
  };
}

function parseBackgrounds() {
  const files = ["backgrounds/backgrounds-pc1.json", "backgrounds/backgrounds-pc2.json"];
  const wanted = new Set(CONFIG.backgrounds.map((name) => name.toLowerCase()));
  const results = [];
  for (const file of files) {
    let data;
    try { data = readJson(file); } catch { continue; }
    for (const bg of data.background ?? []) {
      if (!wanted.has(bg.name.toLowerCase())) continue;
      if (!CONFIG.allowedSources.includes(bg.source)) continue;
      const boostOptions = (bg.boosts ?? []).filter((b) => b !== "free");
      const featName = (bg.feats?.[0] ?? "").split("|")[0];
      results.push({
        id: slug(bg.name),
        name: bg.name,
        source: bg.source,
        boostOptions,
        skills: bg.skills ?? [],
        lore: (bg.lore ?? []).map((l) => `${l} Lore`),
        feat: featName,
        summary: firstText(bg.entries),
        description: fullText(bg.entries)
      });
    }
  }
  return results;
}

function featCategory(traits) {
  if (traits.some((t) => CONFIG.classFeatTraits.includes(t))) return "class";
  if (traits.some((t) => CONFIG.ancestryFeatTraits.includes(t))) return "ancestry";
  if (traits.includes("skill")) return "skill";
  return "general";
}

function actionCost(feat) {
  if (feat.activity?.unit === "reaction") return "reaction";
  if (feat.activity?.unit === "free") return "free";
  if (feat.activity?.number && feat.activity?.unit) return `${feat.activity.number} ${feat.activity.unit}${feat.activity.number > 1 ? "s" : ""}`;
  return undefined;
}

function loadFeats() {
  const files = ["feats/feats-pc1.json", "feats/feats-pc2.json"];
  const feats = new Map();
  for (const file of files) {
    let data;
    try { data = readJson(file); } catch { continue; }
    for (const feat of data.feat ?? []) {
      if (!CONFIG.allowedSources.includes(feat.source)) continue;
      if ((feat.level ?? 1) > CONFIG.maxFeatLevel) continue;
      const traits = (feat.traits ?? []).map((t) => t.toLowerCase());
      const category = featCategory(traits);
      const relevant =
        category === "general" ||
        category === "skill" ||
        traits.some((t) => CONFIG.classFeatTraits.includes(t)) ||
        traits.some((t) => CONFIG.ancestryFeatTraits.includes(t));
      if (!relevant) continue;
      const id = slug(feat.name);
      if (feats.has(id)) continue;
      const prereq = feat.prerequisites ?? feat.requirements;
      feats.set(id, {
        id,
        name: feat.name,
        source: feat.source,
        level: feat.level ?? 1,
        category,
        traits,
        prereq: typeof prereq === "string" ? stripTags(prereq) : Array.isArray(prereq) ? prereq.map(stripTags).join("; ") : undefined,
        actionCost: actionCost(feat),
        summary: firstText(feat.entries),
        description: fullText(feat.entries)
      });
    }
  }
  return [...feats.values()].sort((a, b) => a.level - b.level || a.name.localeCompare(b.name));
}

function loadSpells() {
  const files = ["spells/spells-pc1.json", "spells/spells-pc2.json"];
  const spells = new Map();
  for (const file of files) {
    let data;
    try { data = readJson(file); } catch { continue; }
    for (const spell of data.spell ?? []) {
      if (!CONFIG.allowedSources.includes(spell.source)) continue;
      const traits = (spell.traits ?? []).map((t) => t.toLowerCase());
      const isCantrip = traits.includes("cantrip");
      const rawRank = spell.level ?? spell.rank ?? 0;
      // Cantrips are stored as level 1 in pf2etools but are rank 0 in PF2e rules
      const rank = isCantrip ? 0 : rawRank;
      if (rank > CONFIG.maxSpellRank) continue;
      if (!(spell.traditions ?? []).some((t) => CONFIG.spellTraditions.includes(t.toLowerCase()))) continue;
      const id = slug(spell.name);
      if (spells.has(id)) continue;
      spells.set(id, {
        id,
        name: spell.name,
        source: spell.source,
        rank,
        traditions: (spell.traditions ?? []).map((t) => t.toLowerCase()),
        traits,
        summary: firstText(spell.entries),
        description: fullText(spell.entries)
      });
    }
  }
  return [...spells.values()].sort((a, b) => a.rank - b.rank || a.name.localeCompare(b.name));
}

function loadItems() {
  const base = readJson("items/baseitems.json");
  const wanted = [
    "Explorer's Clothing", "Chain Mail", "Longsword", "Shortbow",
    "Adventurer's Pack", "Staff", "Dagger", "Crossbow",
    "Leather Armor", "Shield", "Holy Water", "Healer's Tools",
    "Rapier", "Studded Leather Armor", "Thieves' Tools", "Scale Mail"
  ];
  const names = new Set(wanted.map((n) => n.toLowerCase()));
  const items = [];
  for (const item of base.baseitem ?? []) {
    if (!names.has(item.name.toLowerCase())) continue;
    if (!CONFIG.allowedSources.includes(item.source)) continue;
    const catLower = (item.category ?? "").toLowerCase();
    const category = catLower === "armor" || catLower === "shield"
      ? "armor"
      : catLower === "weapon"
        ? "weapon"
        : "gear";

    // Parse price to silver pieces
    let priceSp = 0;
    if (item.price) {
      const amount = item.price.amount ?? item.price.value ?? item.price ?? 0;
      const coin = item.price.coin ?? "sp";
      if (coin === "gp") priceSp = amount * 10;
      else if (coin === "cp") priceSp = amount / 10;
      else priceSp = amount;
    }

    // Parse armor data
    const armorData = item.armorData ?? {};
    const weaponData = item.weaponData ?? {};

    items.push({
      id: slug(item.name),
      name: item.name,
      source: item.source,
      category,
      priceSp,
      bulk: typeof item.bulk === "number" ? item.bulk : parseFloat(item.bulk?.value ?? item.bulk ?? 0) || 0.1,
      armorBonus: armorData.ac,
      dexCap: armorData.dexCap,
      checkPenalty: armorData.checkPen ? -armorData.checkPen : undefined,
      damage: weaponData.damage ? `${weaponData.damage} ${weaponData.damageType ?? ""}`.trim() : undefined,
      traits: item.traits,
      summary: firstText(item.entries),
      description: fullText(item.entries)
    });
  }
  // Also add manually defined gear items that may not be in baseitems
  const manualGear = [
    { id: "adventurers-pack", name: "Adventurer's Pack", source: "PC1", category: "gear", priceSp: 15, bulk: 1, summary: "Backpack, bedroll, flint and steel, rations (2 weeks), rope, torches (5), waterskin.", description: "A collection of adventuring essentials: backpack, bedroll, 10 pieces of chalk, flint and steel, 50 feet of rope, 2 weeks of rations, soap, 5 torches, and a waterskin." },
    { id: "healers-tools", name: "Healer's Tools", source: "PC1", category: "gear", priceSp: 50, bulk: 1, summary: "Required for Medicine checks to Treat Wounds. +1 item bonus with expanded kit.", description: "This kit of bandages, herbs, and suturing tools is necessary for Medicine checks to Treat Wounds." },
    { id: "thieves-tools", name: "Thieves' Tools", source: "PC1", category: "gear", priceSp: 30, bulk: 0.1, summary: "Required for Thievery checks to Pick Locks or Disable Devices.", description: "You need thieves' tools to Pick Locks or Disable Devices with Thievery." },
    { id: "holy-water", name: "Holy Water", source: "PC1", category: "gear", priceSp: 30, bulk: 0.1, summary: "Splash weapon dealing 1d6 vitality damage to undead and fiends.", description: "A vial of holy water deals 1d6 vitality splash damage to undead and fiends on a hit." }
  ];
  for (const gear of manualGear) {
    if (!items.find(i => i.id === gear.id)) items.push(gear);
  }
  return items;
}

function main() {
  const ancestries = [
    HUMAN_MANUAL,
    ...CONFIG.ancestries.map(parseAncestryFile)
  ];
  const classes = CONFIG.classes.map(parseClassFile);
  const backgrounds = parseBackgrounds();
  const feats = loadFeats();
  const spells = loadSpells();
  const items = loadItems();

  const payload = {
    generatedAt: new Date().toISOString(),
    config: CONFIG,
    ancestries,
    classes,
    backgrounds,
    feats,
    spells,
    items
  };

  fs.writeFileSync(OUT, JSON.stringify(payload, null, 2));
  console.log(`Wrote ${OUT}`);
  console.log(`  ancestries: ${ancestries.length}, classes: ${classes.length}, backgrounds: ${backgrounds.length}`);
  console.log(`  feats: ${feats.length}, spells: ${spells.length}, items: ${items.length}`);
}

main();
