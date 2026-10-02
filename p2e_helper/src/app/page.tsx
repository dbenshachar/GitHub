"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { allSkills, abilityLabel, gameData, titleCase } from "@/data/game-data";
import {
  abilityModifier, armorClass, buildAbilityScores, bulkTotal, classDc,
  emptySkillRanks, maxHp, saveModifier, skillModifier, spellDc, validateBoostStep
} from "@/lib/calculations";
import type { Ability, Character, GameAncestry, GameClass, GameFeat, GameSpell, ProficiencyRank, SkillKey } from "@/lib/types";
import { abilities } from "@/lib/types";

/* ─── Constants ─── */
type SheetTab = "overview" | "abilities" | "defenses" | "skills" | "feats" | "spells" | "inventory" | "notes";
const storageKey = "pf2e-helper.characters.v3";

const conditionText: Record<string, string> = {
  frightened: "Status penalty equal to value to checks and DCs; decreases at end of turn.",
  sickened: "Status penalty equal to value to checks and DCs; cannot willingly ingest.",
  clumsy: "Status penalty to Dex-based checks, DCs, Reflex saves, and AC.",
  enfeebled: "Status penalty to Str-based checks and DCs.",
  stupefied: "Status penalty to mental checks/DCs and spellcasting checks.",
  wounded: "Increases dying value when knocked out."
};

/* ─── Helpers ─── */
function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function findClass(character: Character) {
  return (gameData.classes.find((e) => e.id === character.classId) ?? gameData.classes[0]) as GameClass;
}
function findAncestry(character: Character) {
  return (gameData.ancestries.find((e) => e.id === character.ancestryId) ?? gameData.ancestries[0]) as GameAncestry;
}
function equippedArmor(character: Character) {
  const equipped = character.inventory.find((item) => item.equipped);
  return gameData.items.find((item) => item.id === equipped?.id && item.category === "armor");
}

/* ─── Item Preview Popover ─── */
function ItemPreview({ name, description, traits, children }: { name: string; description?: string; traits?: string[]; children: React.ReactNode }) {
  const [show, setShow] = useState(false);
  const [pos, setPos] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const ref = useRef<HTMLSpanElement>(null);
  const timeout = useRef<ReturnType<typeof setTimeout>>(undefined);

  const handleEnter = () => {
    timeout.current = setTimeout(() => {
      if (ref.current) {
        const rect = ref.current.getBoundingClientRect();
        setPos({ x: rect.left + rect.width / 2, y: rect.top });
      }
      setShow(true);
    }, 200);
  };
  const handleLeave = () => {
    clearTimeout(timeout.current);
    setShow(false);
  };

  return (
    <span ref={ref} className="relative inline" onMouseEnter={handleEnter} onMouseLeave={handleLeave} onTouchStart={handleEnter} onTouchEnd={handleLeave}>
      {children}
      {show && (
        <span className="fixed z-50 w-80 rounded-lg border border-line bg-paper p-3 shadow-lg" style={{ left: `${Math.max(8, Math.min(pos.x - 160, window.innerWidth - 328))}px`, top: `${Math.max(8, pos.y - 8)}px`, transform: "translateY(-100%)" }}>
          <span className="block font-medium text-ink">{name}</span>
          {traits && traits.length > 0 && (
            <span className="mt-1 flex flex-wrap gap-1">
              {traits.map((t) => <span key={t} className="rounded bg-mist px-1.5 py-0.5 text-[10px] uppercase">{t}</span>)}
            </span>
          )}
          {description && <span className="mt-1.5 block text-sm text-muted">{description}</span>}
        </span>
      )}
    </span>
  );
}

/* ─── Action Cost Icon ─── */
function ActionIcon({ cost }: { cost?: string }) {
  if (!cost) return <span className="text-xs text-muted">—</span>;
  if (cost === "reaction") return <span className="text-sm" title="Reaction">↺</span>;
  if (cost === "free") return <span className="text-sm" title="Free Action">⟡</span>;
  const n = parseInt(cost);
  if (n >= 1 && n <= 3) return <span className="text-sm" title={cost}>{"◆".repeat(n)}</span>;
  return <span className="text-xs text-muted">{cost}</span>;
}

/* ─── Stat Pill ─── */
function StatPill({ label, value, tone = "bg-warm" }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className={cx("rounded-md border border-line px-3 py-2", tone)}>
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
      <div className="text-lg font-semibold text-ink">{value}</div>
    </div>
  );
}

/* ─── Card-based Selection Components ─── */

function AncestryCard({ ancestry, selected, onSelect }: { ancestry: GameAncestry; selected: boolean; onSelect: () => void }) {
  return (
    <div className={cx("rounded-lg border-2 p-4 transition-all cursor-pointer", selected ? "border-ink bg-sage" : "border-line bg-warm hover:border-muted")} onClick={onSelect}>
      <div className="flex items-start justify-between">
        <div>
          <h3 className="font-title text-lg font-semibold">{ancestry.name}</h3>
          <p className="mt-1 text-sm text-muted">{(ancestry as { description?: string }).description || `${ancestry.name} ancestry from ${ancestry.source}.`}</p>
        </div>
        {selected && <span className="rounded bg-ink px-2 py-0.5 text-xs text-paper">Selected</span>}
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        <span className="rounded bg-mist px-2 py-0.5">HP {ancestry.hp}</span>
        <span className="rounded bg-mist px-2 py-0.5">Speed {ancestry.speed}</span>
        <span className="rounded bg-mist px-2 py-0.5">{ancestry.size}</span>
      </div>
      <div className="mt-2 text-xs text-muted">
        <span className="font-medium">Boosts:</span>{" "}
        {ancestry.boosts.fixed.length > 0 ? ancestry.boosts.fixed.map(titleCase).join(", ") : ""}{ancestry.boosts.fixed.length > 0 && ancestry.boosts.free > 0 ? " + " : ""}{ancestry.boosts.free > 0 ? `${ancestry.boosts.free} free` : ""}
        {ancestry.flaws.length > 0 && <>{" "}<span className="text-red-700">Flaw: {ancestry.flaws.map(titleCase).join(", ")}</span></>}
      </div>
    </div>
  );
}

function ClassCard({ cls, selected, onSelect }: { cls: GameClass; selected: boolean; onSelect: () => void }) {
  return (
    <div className={cx("rounded-lg border-2 p-4 transition-all cursor-pointer", selected ? "border-ink bg-sage" : "border-line bg-warm hover:border-muted")} onClick={onSelect}>
      <div className="flex items-start justify-between">
        <div>
          <h3 className="font-title text-lg font-semibold">{cls.name}</h3>
          <p className="mt-1 text-sm text-muted">{(cls as { roleSummary?: string }).roleSummary || `${cls.name} class.`}</p>
        </div>
        {selected && <span className="rounded bg-ink px-2 py-0.5 text-xs text-paper">Selected</span>}
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        <span className="rounded bg-mist px-2 py-0.5">HP {cls.hp}</span>
        <span className="rounded bg-mist px-2 py-0.5">Key: {cls.keyAbilityOptions.map(titleCase).join(" or ")}</span>
        {cls.caster && <span className="rounded bg-gold px-2 py-0.5">{cls.caster.tradition} {cls.caster.mode}</span>}
        {!cls.caster && <span className="rounded bg-clay px-2 py-0.5">Martial</span>}
      </div>
    </div>
  );
}

function BackgroundCard({ bg, selected, onSelect }: { bg: typeof gameData.backgrounds[0]; selected: boolean; onSelect: () => void }) {
  return (
    <div className={cx("rounded-lg border-2 p-3 transition-all cursor-pointer", selected ? "border-ink bg-sage" : "border-line bg-warm hover:border-muted")} onClick={onSelect}>
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-medium">{bg.name}</h3>
        {selected && <span className="shrink-0 rounded bg-ink px-2 py-0.5 text-xs text-paper">✓</span>}
      </div>
      <p className="mt-1 text-sm text-muted line-clamp-2">{bg.summary || bg.description}</p>
      <div className="mt-2 flex flex-wrap gap-1 text-xs">
        <span className="rounded bg-mist px-1.5 py-0.5">Boosts: {bg.boostOptions.map(titleCase).join(", ")}</span>
        <span className="rounded bg-mist px-1.5 py-0.5">Skill: {bg.skills.map(titleCase).join(", ")}</span>
        <span className="rounded bg-mist px-1.5 py-0.5">Feat: {bg.feat}</span>
      </div>
    </div>
  );
}

/* ─── Feat Card — List-style with full description (Requirement 2) ─── */
function FeatCard({ feat, selected, onSelect, showSelect = true }: { feat: GameFeat; selected?: boolean; onSelect?: () => void; showSelect?: boolean }) {
  return (
    <div className={cx(
      "rounded-lg border p-4 transition-all",
      selected ? "border-ink bg-sage" : "border-line bg-warm",
      showSelect && "cursor-pointer hover:border-muted"
    )} onClick={showSelect ? onSelect : undefined}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 shrink-0">
          <ActionIcon cost={feat.actionCost} />
          <h4 className="font-medium">{feat.name}</h4>
          <span className="text-xs text-muted">Lv {feat.level}</span>
        </div>
        {showSelect && selected && <span className="shrink-0 rounded bg-ink px-2 py-0.5 text-[10px] text-paper">✓</span>}
      </div>
      {feat.prereq && <p className="mt-1 text-xs italic text-muted">Prerequisite: {feat.prereq}</p>}
      <p className="mt-2 text-sm text-muted">{feat.description || feat.summary}</p>
      <div className="mt-2 flex flex-wrap gap-1">
        {feat.traits.map((t) => <span key={t} className="rounded bg-mist px-1.5 py-0.5 text-[10px]">{t}</span>)}
      </div>
    </div>
  );
}

/* ─── Spell Card — List-style with full description (Requirement 2) ─── */
function SpellCard({ spell, selected, onSelect, showSelect = true }: { spell: GameSpell; selected?: boolean; onSelect?: () => void; showSelect?: boolean }) {
  return (
    <div className={cx(
      "rounded-lg border p-4 transition-all",
      selected ? "border-ink bg-sage" : "border-line bg-warm",
      showSelect && "cursor-pointer hover:border-muted"
    )} onClick={showSelect ? onSelect : undefined}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className="font-medium">{spell.name}</h4>
          <span className="text-xs text-muted">{spell.rank === 0 ? "Cantrip" : `Rank ${spell.rank}`}</span>
        </div>
        {showSelect && selected && <span className="shrink-0 rounded bg-ink px-2 py-0.5 text-[10px] text-paper">✓</span>}
      </div>
      <p className="mt-2 text-sm text-muted">{spell.description || spell.summary}</p>
      <div className="mt-2 flex flex-wrap gap-1">
        {spell.traditions.map((t) => <span key={t} className="rounded bg-gold px-1.5 py-0.5 text-[10px]">{t}</span>)}
        {spell.traits.filter(t => t !== "cantrip").map((t) => <span key={t} className="rounded bg-mist px-1.5 py-0.5 text-[10px]">{t}</span>)}
      </div>
    </div>
  );
}

function ItemCard({ item, selected, onSelect }: { item: typeof gameData.items[0]; selected?: boolean; onSelect?: () => void }) {
  return (
    <div className={cx("rounded-lg border p-3 transition-all cursor-pointer", selected ? "border-ink bg-sage" : "border-line bg-warm hover:border-muted")} onClick={onSelect}>
      <div className="flex items-start justify-between gap-2">
        <h4 className="font-medium text-sm">{item.name}</h4>
        {selected && <span className="shrink-0 rounded bg-ink px-2 py-0.5 text-[10px] text-paper">✓</span>}
      </div>
      <p className="mt-1 text-xs text-muted line-clamp-2">{item.summary || `${item.category} item.`}</p>
      <div className="mt-1.5 flex flex-wrap gap-1 text-[10px]">
        <span className="rounded bg-mist px-1 py-0.5">{item.category}</span>
        <span className="rounded bg-mist px-1 py-0.5">{item.priceSp} sp</span>
        <span className="rounded bg-mist px-1 py-0.5">bulk {item.bulk}</span>
        {item.damage && <span className="rounded bg-clay px-1 py-0.5">{item.damage}</span>}
        {item.armorBonus !== undefined && <span className="rounded bg-clay px-1 py-0.5">AC +{item.armorBonus}</span>}
      </div>
    </div>
  );
}

/* ─── Filter Components ─── */
function FilterBar({ children }: { children: React.ReactNode }) {
  return <div className="mb-3 flex flex-wrap gap-2 text-xs">{children}</div>;
}

function FilterChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button className={cx("rounded-full border px-2.5 py-1 transition-all", active ? "border-ink bg-ink text-paper" : "border-line bg-warm text-muted hover:border-muted")} onClick={onClick}>
      {label}
    </button>
  );
}

/* ─── Boost Picker ─── */
function BoostPicker({ label, count, selected, onChange }: { label: string; count: number; selected: Ability[]; onChange: (v: Ability[]) => void }) {
  function toggle(ability: Ability) {
    if (selected.includes(ability)) {
      onChange(selected.filter((e) => e !== ability));
      return;
    }
    if (selected.length >= count) return;
    onChange([...selected, ability]);
  }
  return (
    <div>
      <div className="mb-2 text-sm text-muted">{label} (pick {count})</div>
      <div className="flex flex-wrap gap-2">
        {abilities.map((ability) => (
          <button key={ability} className={cx("rounded-md border px-3 py-1.5 text-sm transition-all", selected.includes(ability) ? "border-ink bg-sage font-medium" : "border-line bg-warm text-muted")} onClick={() => toggle(ability)} type="button">
            {titleCase(ability)}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ─── Feat Progression Preview ─── */
function FeatProgressionPreview({ classId, ancestryId, currentFeats }: { classId: string; ancestryId: string; currentFeats: string[] }) {
  const grouped = useMemo(() => {
    const relevant = gameData.feats.filter((f) =>
      (f.category === "class" && f.traits.includes(classId)) ||
      (f.category === "ancestry" && f.traits.includes(ancestryId))
    );
    const future = relevant.filter((f) => f.level > 1 && !currentFeats.includes(f.id));
    const byLevel: Record<number, GameFeat[]> = {};
    for (const feat of future) {
      if (!byLevel[feat.level]) byLevel[feat.level] = [];
      byLevel[feat.level].push(feat);
    }
    return Object.entries(byLevel).sort(([a], [b]) => Number(a) - Number(b)).slice(0, 8);
  }, [classId, ancestryId, currentFeats]);

  if (grouped.length === 0) return null;

  return (
    <div className="rounded-lg border border-line bg-warm p-4">
      <h4 className="mb-3 font-medium text-sm">Future Feats (not yet eligible)</h4>
      <p className="text-xs text-muted mb-3">These are feats you&apos;ll unlock at higher levels. Use this to plan ahead.</p>
      <div className="space-y-3 max-h-[32rem] overflow-y-auto">
        {grouped.map(([level, feats]) => (
          <div key={level}>
            <div className="text-xs font-medium text-muted mb-1">Level {level}</div>
            <div className="space-y-1">
              {feats.slice(0, 6).map((feat) => (
                <ItemPreview key={feat.id} name={feat.name} description={feat.description || feat.summary} traits={feat.traits}>
                  <span className="flex items-center gap-1.5 rounded border border-line bg-paper px-2 py-1 text-xs">
                    <ActionIcon cost={feat.actionCost} />
                    <span>{feat.name}</span>
                    {feat.prereq && <span className="text-[10px] text-muted italic ml-1">({feat.prereq})</span>}
                  </span>
                </ItemPreview>
              ))}
              {feats.length > 6 && <span className="text-[10px] text-muted">+{feats.length - 6} more</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ─── Collapsible Feat Section (Requirement 1: Guided Feat Selection) ─── */
function FeatSection({ title, context, feats, selectedFeats, onToggleFeat, defaultOpen = true }: {
  title: string;
  context: string;
  feats: GameFeat[];
  selectedFeats: string[];
  onToggleFeat: (id: string) => void;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);

  if (feats.length === 0) return null;

  return (
    <div className="rounded-lg border border-line overflow-hidden">
      <button className="w-full flex items-center justify-between px-4 py-3 bg-warm hover:bg-paper transition-all text-left" onClick={() => setOpen(!open)}>
        <div>
          <h3 className="font-medium">{title} <span className="text-xs text-muted font-normal">({feats.length} available)</span></h3>
        </div>
        <span className="text-muted text-sm">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="border-t border-line p-4">
          <p className="text-sm text-muted mb-4">{context}</p>
          <div className="space-y-3">
            {feats.map((feat) => (
              <FeatCard key={feat.id} feat={feat} selected={selectedFeats.includes(feat.id)} onSelect={() => onToggleFeat(feat.id)} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ─── Creator Wizard State ─── */
interface WizardState {
  ancestryId: string;
  heritageId: string;
  backgroundId: string;
  classId: string;
  keyAbility: Ability;
  useAlternateBoosts: boolean;
  ancestryFreeBoosts: Ability[];
  backgroundBoosts: Ability[];
  freeBoosts: Ability[];
  skills: SkillKey[];
  feats: string[];
  spells: string[];
  items: string[];
  name: string;
  pronouns: string;
  languages: string[];
  appearance: string;
  notes: string;
  edicts: string;
  anathema: string;
}

const blankWizard: WizardState = {
  ancestryId: "human",
  heritageId: "skilled-heritage",
  backgroundId: "artisan",
  classId: "fighter",
  keyAbility: "strength",
  useAlternateBoosts: false,
  ancestryFreeBoosts: ["strength"],
  backgroundBoosts: ["strength", "dexterity"],
  freeBoosts: ["strength", "constitution", "dexterity", "wisdom"],
  skills: ["athletics", "crafting", "intimidation"],
  feats: [],
  spells: [],
  items: ["adventurers-pack"],
  name: "New Pathfinder",
  pronouns: "",
  languages: ["Common"],
  appearance: "",
  notes: "",
  edicts: "",
  anathema: ""
};

function createFromWizard(wizard: WizardState): Character {
  const ancestry = (gameData.ancestries.find((e) => e.id === wizard.ancestryId) ?? gameData.ancestries[0]) as GameAncestry;
  const background = gameData.backgrounds.find((e) => e.id === wizard.backgroundId) ?? gameData.backgrounds[0];
  const selectedClass = (gameData.classes.find((e) => e.id === wizard.classId) ?? gameData.classes[0]) as GameClass;
  const skills = emptySkillRanks();
  for (const skill of [...background.skills, ...selectedClass.trainedSkills, ...wizard.skills]) {
    skills[skill as SkillKey] = "trained";
  }

  const boostData = wizard.useAlternateBoosts
    ? { fixed: [] as Ability[], options: [] as Ability[], free: 2 }
    : ancestry.boosts;

  const partial = {
    ancestryFreeBoosts: wizard.ancestryFreeBoosts.slice(0, boostData.free),
    backgroundBoosts: wizard.backgroundBoosts.slice(0, 2),
    classBoost: wizard.keyAbility,
    freeBoosts: wizard.freeBoosts.slice(0, 4)
  };

  const effectiveAncestry = wizard.useAlternateBoosts
    ? { ...ancestry, boosts: { fixed: [], options: [], free: 2 }, flaws: [] }
    : ancestry;

  const abilityScores = buildAbilityScores(partial, effectiveAncestry);
  const spellcasting = selectedClass.caster
    ? {
        tradition: selectedClass.caster.tradition === "choice" ? "manual" : selectedClass.caster.tradition,
        ability: selectedClass.caster.ability,
        mode: selectedClass.caster.mode,
        spellIds: wizard.spells,
        slots: { "1": selectedClass.caster.rank1Slots }
      }
    : undefined;

  const character: Character = {
    schemaVersion: 1,
    id: crypto.randomUUID(),
    name: wizard.name || "New Pathfinder",
    pronouns: wizard.pronouns,
    level: 1,
    ancestryId: ancestry.id,
    heritageId: wizard.heritageId || ancestry.heritages[0]?.id || "",
    backgroundId: background.id,
    classId: selectedClass.id,
    keyAbility: wizard.keyAbility,
    ...partial,
    abilityScores,
    skills,
    loreSkills: background.lore,
    saves: selectedClass.saves,
    perception: selectedClass.perception,
    classDc: selectedClass.classDc,
    armorProficiencies: selectedClass.armor as Record<string, ProficiencyRank>,
    weaponProficiencies: selectedClass.weapons as Record<string, ProficiencyRank>,
    feats: [
      ...wizard.feats.map((id) => ({ id, source: "manual" as const })),
      { id: background.feat.toLowerCase().replaceAll(" ", "-"), source: "background" as const }
    ],
    inventory: wizard.items.map((id) => ({ id, quantity: 1, equipped: id.includes("clothing") || id.includes("armor") || id.includes("mail") })),
    currency: { cp: 0, sp: 150, gp: 0, pp: 0 },
    hp: { current: 1, max: 1, temp: 0 },
    spellcasting,
    languages: Array.from(new Set([...ancestry.languages, ...wizard.languages].filter(Boolean))),
    edicts: wizard.edicts,
    anathema: wizard.anathema,
    conditions: Object.fromEntries(Object.keys(conditionText).map((k) => [k, false])),
    notes: wizard.notes,
    appearance: wizard.appearance,
    unresolvedRules: []
  };
  const hp = maxHp(character, effectiveAncestry, selectedClass);
  return { ...character, hp: { current: hp, max: hp, temp: 0 } };
}

/* ─── Character Creator ─── */
function CharacterCreator({ onFinish }: { onFinish: (c: Character) => void }) {
  const [wizard, setWizard] = useState<WizardState>(blankWizard);
  const [step, setStep] = useState(0);
  const [spellTraitFilters, setSpellTraitFilters] = useState<string[]>([]);
  const [spellRankFilter, setSpellRankFilter] = useState<string>("");
  const [showFeatProgression, setShowFeatProgression] = useState(false);
  const [ancestryBoostFilter, setAncestryBoostFilter] = useState<Ability | "">("");

  const wizardAncestry = useMemo(() => gameData.ancestries.find((e) => e.id === wizard.ancestryId) ?? gameData.ancestries[0], [wizard.ancestryId]);
  const wizardClass = useMemo(() => gameData.classes.find((e) => e.id === wizard.classId) ?? gameData.classes[0], [wizard.classId]);
  const wizardBackground = useMemo(() => gameData.backgrounds.find((e) => e.id === wizard.backgroundId) ?? gameData.backgrounds[0], [wizard.backgroundId]);

  const effectiveBoosts = wizard.useAlternateBoosts
    ? { fixed: [] as Ability[], options: [] as Ability[], free: 2 }
    : wizardAncestry.boosts;
  const effectiveFlaws = wizard.useAlternateBoosts ? [] : wizardAncestry.flaws;

  const previewScores = useMemo(() => {
    const effectiveAncestry = wizard.useAlternateBoosts
      ? { ...wizardAncestry, boosts: { fixed: [], options: [], free: 2 }, flaws: [] }
      : wizardAncestry;
    return buildAbilityScores({
      ancestryFreeBoosts: wizard.ancestryFreeBoosts,
      backgroundBoosts: wizard.backgroundBoosts,
      classBoost: wizard.keyAbility,
      freeBoosts: wizard.freeBoosts
    }, effectiveAncestry);
  }, [wizard, wizardAncestry]);

  const wizardValid = validateBoostStep(wizard.ancestryFreeBoosts, effectiveBoosts.free)
    && validateBoostStep(wizard.backgroundBoosts, 2)
    && validateBoostStep(wizard.freeBoosts, 4)
    && wizardClass.keyAbilityOptions.includes(wizard.keyAbility);

  // Filtered ancestries
  const filteredAncestries = useMemo(() => {
    if (!ancestryBoostFilter) return gameData.ancestries;
    return gameData.ancestries.filter((a) =>
      a.boosts.fixed.includes(ancestryBoostFilter) || a.boosts.free > 0
    );
  }, [ancestryBoostFilter]);

  // Feats split by category, only eligible (level 1, correct class/ancestry)
  const featsByCategory = useMemo(() => {
    const classFeats = gameData.feats.filter((f) => f.level <= 1 && f.category === "class" && f.traits.includes(wizardClass.id));
    const ancestryFeats = gameData.feats.filter((f) => f.level <= 1 && f.category === "ancestry" && f.traits.includes(wizardAncestry.id));
    const skillFeats = gameData.feats.filter((f) => f.level <= 1 && f.category === "skill");
    const generalFeats = gameData.feats.filter((f) => f.level <= 1 && f.category === "general");
    return { classFeats, ancestryFeats, skillFeats, generalFeats };
  }, [wizardClass.id, wizardAncestry.id]);

  // Spell trait options — collect all unique traits from available spells
  const spellData = useMemo(() => {
    if (!wizardClass.caster) return { spells: [] as GameSpell[], allTraits: [] as string[] };
    const tradition = wizardClass.caster.tradition === "choice" ? "" : wizardClass.caster.tradition;
    const base = gameData.spells.filter((s) => {
      if (tradition && !s.traditions.includes(tradition)) return false;
      return s.rank <= 1;
    });
    const traits = new Set<string>();
    for (const s of base) {
      for (const t of s.traits) {
        if (t !== "cantrip") traits.add(t);
      }
    }
    return { spells: base, allTraits: [...traits].sort() };
  }, [wizardClass]);

  // Filtered spells with multi-select trait filter
  const filteredSpells = useMemo(() => {
    return spellData.spells.filter((s) => {
      if (spellRankFilter !== "" && s.rank !== Number(spellRankFilter)) return false;
      if (spellTraitFilters.length > 0 && !spellTraitFilters.every((t) => s.traits.includes(t))) return false;
      return true;
    });
  }, [spellData.spells, spellRankFilter, spellTraitFilters]);

  const steps = [
    "Ancestry", "Heritage", "Background", "Class", "Ability Scores",
    "Skills", "Feats", ...(wizardClass.caster ? ["Spells"] : []),
    "Equipment", "Details"
  ];

  function toggleFeat(id: string) {
    if (wizard.feats.includes(id)) setWizard({ ...wizard, feats: wizard.feats.filter((f) => f !== id) });
    else setWizard({ ...wizard, feats: [...wizard.feats, id] });
  }

  function toggleSpellTrait(trait: string) {
    if (spellTraitFilters.includes(trait)) setSpellTraitFilters(spellTraitFilters.filter((t) => t !== trait));
    else setSpellTraitFilters([...spellTraitFilters, trait]);
  }

  return (
    <div className="flex flex-col h-full">
      {/* Step navigation */}
      <div className="border-b border-line bg-warm px-4 py-3">
        <div className="flex flex-wrap gap-1">
          {steps.map((s, i) => (
            <button key={s} className={cx("rounded-full px-3 py-1 text-xs transition-all", i === step ? "bg-ink text-paper" : "bg-paper border border-line text-muted hover:border-muted")} onClick={() => setStep(i)}>
              {s}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        <div className="mx-auto max-w-4xl">

          {/* Step 0: Ancestry */}
          {step === 0 && (
            <div>
              <h2 className="font-title text-2xl mb-1">Choose Ancestry</h2>
              <p className="text-sm text-muted mb-4">Your ancestry determines your starting HP, speed, ability boosts, and special features.</p>
              <FilterBar>
                <FilterChip label="All" active={ancestryBoostFilter === ""} onClick={() => setAncestryBoostFilter("")} />
                {abilities.map((a) => (
                  <FilterChip key={a} label={titleCase(a)} active={ancestryBoostFilter === a} onClick={() => setAncestryBoostFilter(ancestryBoostFilter === a ? "" : a)} />
                ))}
              </FilterBar>
              <div className="grid gap-3 md:grid-cols-2">
                {filteredAncestries.map((a) => (
                  <AncestryCard key={a.id} ancestry={a as GameAncestry} selected={wizard.ancestryId === a.id} onSelect={() => {
                    const anc = a as GameAncestry;
                    setWizard({ ...wizard, ancestryId: a.id, heritageId: anc.heritages[0]?.id ?? "", ancestryFreeBoosts: [] });
                  }} />
                ))}
              </div>
              <div className="mt-5 rounded-lg border border-line bg-warm p-4">
                <label className="flex items-center gap-3 cursor-pointer">
                  <input type="checkbox" checked={wizard.useAlternateBoosts} onChange={(e) => setWizard({ ...wizard, useAlternateBoosts: e.target.checked, ancestryFreeBoosts: [] })} className="h-4 w-4" />
                  <div>
                    <span className="font-medium text-sm">Use Alternate Ancestry Boosts</span>
                    <p className="text-xs text-muted mt-0.5">Replaces all fixed boosts and flaws with two fully free ability boosts of your choice.</p>
                  </div>
                </label>
              </div>
              <div className="mt-4">
                <BoostPicker
                  label={wizard.useAlternateBoosts ? "Two free ability boosts (alternate rule)" : `Ancestry free boost${effectiveBoosts.free > 1 ? "s" : ""}`}
                  count={effectiveBoosts.free}
                  selected={wizard.ancestryFreeBoosts}
                  onChange={(v) => setWizard({ ...wizard, ancestryFreeBoosts: v })}
                />
                {!wizard.useAlternateBoosts && (
                  <p className="mt-2 text-xs text-muted">
                    Fixed boosts: {effectiveBoosts.fixed.map(titleCase).join(", ") || "None"}
                    {effectiveFlaws.length > 0 && <> · <span className="text-red-700">Flaw: {effectiveFlaws.map(titleCase).join(", ")}</span></>}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Step 1: Heritage */}
          {step === 1 && (
            <div>
              <h2 className="font-title text-2xl mb-1">Choose Heritage</h2>
              <p className="text-sm text-muted mb-4">Heritages represent a specific lineage within your ancestry, granting an additional ability or trait.</p>
              <div className="grid gap-3 md:grid-cols-2">
                {wizardAncestry.heritages.map((h) => (
                  <div key={h.id} className={cx("rounded-lg border-2 p-4 cursor-pointer transition-all", wizard.heritageId === h.id ? "border-ink bg-sage" : "border-line bg-warm hover:border-muted")} onClick={() => setWizard({ ...wizard, heritageId: h.id })}>
                    <div className="flex items-start justify-between">
                      <h3 className="font-medium">{h.name}</h3>
                      {wizard.heritageId === h.id && <span className="rounded bg-ink px-2 py-0.5 text-xs text-paper">Selected</span>}
                    </div>
                    <p className="mt-2 text-sm text-muted">{(h as { description?: string }).description || h.summary}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Step 2: Background */}
          {step === 2 && (
            <div>
              <h2 className="font-title text-2xl mb-1">Choose Background</h2>
              <p className="text-sm text-muted mb-4">Your background represents what you did before adventuring. It grants ability boosts, a skill, lore, and a feat.</p>
              <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
                {gameData.backgrounds.map((bg) => (
                  <BackgroundCard key={bg.id} bg={bg} selected={wizard.backgroundId === bg.id} onSelect={() => setWizard({ ...wizard, backgroundId: bg.id, backgroundBoosts: [] })} />
                ))}
              </div>
              {wizardBackground.description && (
                <div className="mt-4 rounded-lg border border-line bg-warm p-4">
                  <h3 className="font-medium text-sm">{wizardBackground.name}</h3>
                  <p className="mt-1 text-sm text-muted">{wizardBackground.description}</p>
                </div>
              )}
              <div className="mt-4">
                <BoostPicker label="Background boosts: pick from listed options or free" count={2} selected={wizard.backgroundBoosts} onChange={(v) => setWizard({ ...wizard, backgroundBoosts: v })} />
              </div>
            </div>
          )}

          {/* Step 3: Class */}
          {step === 3 && (
            <div>
              <h2 className="font-title text-2xl mb-1">Choose Class</h2>
              <p className="text-sm text-muted mb-4">Your class determines your key abilities, proficiencies, and combat role.</p>
              <div className="grid gap-3 md:grid-cols-2">
                {gameData.classes.map((cls) => (
                  <ClassCard key={cls.id} cls={cls as GameClass} selected={wizard.classId === cls.id} onSelect={() => {
                    const c = cls as GameClass;
                    setWizard({ ...wizard, classId: cls.id, keyAbility: c.keyAbilityOptions[0], spells: [], feats: [] });
                  }} />
                ))}
              </div>
              {wizardClass.keyAbilityOptions.length > 1 && (
                <div className="mt-4">
                  <div className="text-sm text-muted mb-2">Key Ability</div>
                  <div className="flex gap-2">
                    {wizardClass.keyAbilityOptions.map((a) => (
                      <button key={a} className={cx("rounded-md border px-4 py-2 text-sm", wizard.keyAbility === a ? "border-ink bg-sage font-medium" : "border-line bg-warm")} onClick={() => setWizard({ ...wizard, keyAbility: a })}>
                        {titleCase(a)}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <div className="mt-4 rounded-lg border border-line bg-warm p-4">
                <h3 className="font-medium">{wizardClass.name}</h3>
                <p className="mt-1 text-sm text-muted">{(wizardClass as { description?: string }).description || (wizardClass as { roleSummary?: string }).roleSummary}</p>
                <div className="mt-3 flex flex-wrap gap-2 text-xs">
                  <span className="rounded bg-mist px-2 py-0.5">HP/level: {wizardClass.hp}</span>
                  <span className="rounded bg-mist px-2 py-0.5">Perception: {wizardClass.perception}</span>
                  <span className="rounded bg-mist px-2 py-0.5">Fort: {wizardClass.saves.fortitude}</span>
                  <span className="rounded bg-mist px-2 py-0.5">Ref: {wizardClass.saves.reflex}</span>
                  <span className="rounded bg-mist px-2 py-0.5">Will: {wizardClass.saves.will}</span>
                </div>
                <div className="mt-2 text-xs text-muted">Level 1 Features: {wizardClass.level1Features.join(", ")}</div>
              </div>
            </div>
          )}

          {/* Step 4: Ability Scores */}
          {step === 4 && (
            <div>
              <h2 className="font-title text-2xl mb-1">Ability Scores</h2>
              <p className="text-sm text-muted mb-4">Choose four free ability boosts. Each must be unique in this step.</p>
              <BoostPicker label="Four free ability boosts" count={4} selected={wizard.freeBoosts} onChange={(v) => setWizard({ ...wizard, freeBoosts: v })} />
              <div className="mt-5 grid grid-cols-3 gap-2 md:grid-cols-6">
                {abilities.map((a) => (
                  <StatPill key={a} label={abilityLabel(a)} value={`${previewScores[a]} (${abilityModifier(previewScores[a]) >= 0 ? "+" : ""}${abilityModifier(previewScores[a])})`} />
                ))}
              </div>
            </div>
          )}

          {/* Step 5: Skills */}
          {step === 5 && (
            <div>
              <h2 className="font-title text-2xl mb-1">Additional Skills</h2>
              <p className="text-sm text-muted mb-4">Choose {wizardClass.additionalTrainedSkills} additional trained skills. Class and background training is automatic.</p>
              <div className="grid gap-2 md:grid-cols-2">
                {allSkills.map((skill) => {
                  const fromClass = wizardClass.trainedSkills.includes(skill);
                  const fromBg = wizardBackground.skills.includes(skill);
                  const auto = fromClass || fromBg;
                  const selected = wizard.skills.includes(skill);
                  return (
                    <label key={skill} className={cx("flex items-center gap-2 rounded-md border px-3 py-2 text-sm transition-all", auto ? "border-line bg-sage cursor-default" : selected ? "border-ink bg-sage cursor-pointer" : "border-line bg-warm cursor-pointer")}>
                      <input type="checkbox" checked={auto || selected} disabled={auto} onChange={() => {
                        if (auto) return;
                        if (selected) setWizard({ ...wizard, skills: wizard.skills.filter((s) => s !== skill) });
                        else setWizard({ ...wizard, skills: [...wizard.skills, skill] });
                      }} />
                      <span>{titleCase(skill)}</span>
                      {fromClass && <span className="text-[10px] text-muted">(class)</span>}
                      {fromBg && <span className="text-[10px] text-muted">(background)</span>}
                    </label>
                  );
                })}
              </div>
            </div>
          )}

          {/* Step 6: Feats — Guided, sectioned (Requirement 1) */}
          {step === 6 && (
            <div>
              <h2 className="font-title text-2xl mb-1">Feats</h2>
              <p className="text-sm text-muted mb-4">
                Select your level 1 feats below. Each section shows only feats you are currently eligible for.
                Feats you&apos;ll unlock at higher levels are in the Progression Preview panel.
              </p>
              <div className="flex justify-end mb-4">
                <button className={cx("rounded-full border px-3 py-1 text-xs", showFeatProgression ? "border-ink bg-ink text-paper" : "border-line hover:border-muted")} onClick={() => setShowFeatProgression(!showFeatProgression)}>
                  {showFeatProgression ? "Hide" : "Show"} Progression Preview
                </button>
              </div>
              <div className={cx("grid gap-4", showFeatProgression ? "lg:grid-cols-[1fr_340px]" : "")}>
                <div className="space-y-4">
                  <FeatSection
                    title={`${titleCase(wizardClass.id)} Class Feats`}
                    context={`You get 1 class feat at level 1, chosen from the ${titleCase(wizardClass.id)} list. This shapes your core combat or class-specific style — pick what fits how you want to play, not what sounds strongest in isolation.`}
                    feats={featsByCategory.classFeats}
                    selectedFeats={wizard.feats}
                    onToggleFeat={toggleFeat}
                  />
                  <FeatSection
                    title={`${titleCase(wizardAncestry.id)} Ancestry Feats`}
                    context={`You get 1 ancestry feat at level 1. These reflect abilities or training unique to your ${titleCase(wizardAncestry.id)} heritage.`}
                    feats={featsByCategory.ancestryFeats}
                    selectedFeats={wizard.feats}
                    onToggleFeat={toggleFeat}
                  />
                  <FeatSection
                    title="Skill Feats"
                    context="Skill feats enhance what you can do with trained skills. You may get one from your background or class — additional ones become available at later levels."
                    feats={featsByCategory.skillFeats}
                    selectedFeats={wizard.feats}
                    onToggleFeat={toggleFeat}
                    defaultOpen={false}
                  />
                  <FeatSection
                    title="General Feats"
                    context="General feats are available to any character. Most classes don't grant one at level 1, but some backgrounds or heritage choices can open the door."
                    feats={featsByCategory.generalFeats}
                    selectedFeats={wizard.feats}
                    onToggleFeat={toggleFeat}
                    defaultOpen={false}
                  />
                </div>
                {showFeatProgression && (
                  <FeatProgressionPreview classId={wizardClass.id} ancestryId={wizardAncestry.id} currentFeats={wizard.feats} />
                )}
              </div>
            </div>
          )}

          {/* Step 7: Spells — with multi-select trait filtering (Requirements 2 & 3) */}
          {step === 7 && wizardClass.caster && (
            <div>
              <h2 className="font-title text-2xl mb-1">Spells</h2>
              <p className="text-sm text-muted mb-4">
                Choose cantrips and rank 1 spells for your {wizardClass.caster.tradition} spell list.
                {wizardClass.caster.mode === "prepared" && " As a prepared caster, you'll prepare a subset of these each day."}
                {" "}You can select {wizardClass.caster.cantrips} cantrips and have {wizardClass.caster.rank1Slots} rank 1 spell slot{wizardClass.caster.rank1Slots > 1 ? "s" : ""}.
              </p>

              {/* Rank filter */}
              <FilterBar>
                <FilterChip label="All ranks" active={spellRankFilter === ""} onClick={() => setSpellRankFilter("")} />
                <FilterChip label="Cantrips" active={spellRankFilter === "0"} onClick={() => setSpellRankFilter(spellRankFilter === "0" ? "" : "0")} />
                <FilterChip label="Rank 1" active={spellRankFilter === "1"} onClick={() => setSpellRankFilter(spellRankFilter === "1" ? "" : "1")} />
              </FilterBar>

              {/* Trait multi-select filter (Requirement 3) */}
              <div className="mb-4">
                <div className="text-xs text-muted mb-1.5">Filter by traits (multi-select):</div>
                <div className="flex flex-wrap gap-1.5">
                  {spellData.allTraits.map((trait) => (
                    <FilterChip key={trait} label={trait} active={spellTraitFilters.includes(trait)} onClick={() => toggleSpellTrait(trait)} />
                  ))}
                </div>
                {spellTraitFilters.length > 0 && (
                  <button className="mt-2 text-xs text-muted underline" onClick={() => setSpellTraitFilters([])}>Clear trait filters</button>
                )}
              </div>

              <div className="text-xs text-muted mb-3">Showing {filteredSpells.length} spell{filteredSpells.length !== 1 ? "s" : ""}</div>

              <div className="space-y-3">
                {filteredSpells.map((spell) => (
                  <SpellCard key={spell.id} spell={spell} selected={wizard.spells.includes(spell.id)} onSelect={() => {
                    if (wizard.spells.includes(spell.id)) setWizard({ ...wizard, spells: wizard.spells.filter((s) => s !== spell.id) });
                    else setWizard({ ...wizard, spells: [...wizard.spells, spell.id] });
                  }} />
                ))}
                {filteredSpells.length === 0 && <p className="text-sm text-muted">No spells match current filters.</p>}
              </div>
            </div>
          )}

          {/* Equipment */}
          {step === (wizardClass.caster ? 8 : 7) && (
            <div>
              <h2 className="font-title text-2xl mb-1">Equipment</h2>
              <p className="text-sm text-muted mb-4">Choose your starting gear. Starting wealth is 15 gp (150 sp).</p>
              <div className="grid gap-2 md:grid-cols-2 lg:grid-cols-3">
                {gameData.items.map((item) => (
                  <ItemCard key={item.id} item={item} selected={wizard.items.includes(item.id)} onSelect={() => {
                    if (wizard.items.includes(item.id)) setWizard({ ...wizard, items: wizard.items.filter((i) => i !== item.id) });
                    else setWizard({ ...wizard, items: [...wizard.items, item.id] });
                  }} />
                ))}
              </div>
            </div>
          )}

          {/* Details */}
          {step === (wizardClass.caster ? 9 : 8) && (
            <div>
              <h2 className="font-title text-2xl mb-1">Finishing Touches</h2>
              <p className="text-sm text-muted mb-4">Name your character and add any flavor details.</p>
              <div className="space-y-4 max-w-lg">
                <label className="block text-sm">
                  <span className="block text-muted mb-1">Name</span>
                  <input className="w-full rounded-md border border-line bg-warm px-3 py-2" value={wizard.name} onChange={(e) => setWizard({ ...wizard, name: e.target.value })} />
                </label>
                <label className="block text-sm">
                  <span className="block text-muted mb-1">Pronouns</span>
                  <input className="w-full rounded-md border border-line bg-warm px-3 py-2" value={wizard.pronouns} onChange={(e) => setWizard({ ...wizard, pronouns: e.target.value })} />
                </label>
                <label className="block text-sm">
                  <span className="block text-muted mb-1">Appearance</span>
                  <textarea className="w-full min-h-20 rounded-md border border-line bg-warm px-3 py-2" value={wizard.appearance} onChange={(e) => setWizard({ ...wizard, appearance: e.target.value })} />
                </label>
                {wizardClass.id === "cleric" && (
                  <>
                    <label className="block text-sm">
                      <span className="block text-muted mb-1">Edicts</span>
                      <textarea className="w-full min-h-16 rounded-md border border-line bg-warm px-3 py-2" value={wizard.edicts} onChange={(e) => setWizard({ ...wizard, edicts: e.target.value })} />
                    </label>
                    <label className="block text-sm">
                      <span className="block text-muted mb-1">Anathema</span>
                      <textarea className="w-full min-h-16 rounded-md border border-line bg-warm px-3 py-2" value={wizard.anathema} onChange={(e) => setWizard({ ...wizard, anathema: e.target.value })} />
                    </label>
                  </>
                )}
                <label className="block text-sm">
                  <span className="block text-muted mb-1">Notes</span>
                  <textarea className="w-full min-h-16 rounded-md border border-line bg-warm px-3 py-2" value={wizard.notes} onChange={(e) => setWizard({ ...wizard, notes: e.target.value })} />
                </label>
                <button disabled={!wizardValid} className="rounded-md bg-ink px-6 py-2.5 text-sm font-medium text-paper disabled:opacity-40" onClick={() => onFinish(createFromWizard(wizard))}>
                  Create Character
                </button>
                {!wizardValid && <p className="text-xs text-red-700">Complete all boost steps with unique selections and a valid key ability.</p>}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Step nav footer */}
      <div className="border-t border-line bg-warm px-6 py-3 flex justify-between">
        <button className="rounded-md border border-line bg-paper px-4 py-2 text-sm" disabled={step === 0} onClick={() => setStep(step - 1)}>← Previous</button>
        <div className="text-sm text-muted">{step + 1} / {steps.length}</div>
        <button className="rounded-md border border-line bg-paper px-4 py-2 text-sm" disabled={step >= steps.length - 1} onClick={() => setStep(step + 1)}>Next →</button>
      </div>
    </div>
  );
}

/* ─── Character Sheet ─── */
const SHEET_TABS: Array<{ key: SheetTab; label: string; icon: string }> = [
  { key: "overview", label: "Overview", icon: "⌂" },
  { key: "abilities", label: "Abilities", icon: "✦" },
  { key: "defenses", label: "Defenses", icon: "🛡" },
  { key: "skills", label: "Skills", icon: "☑" },
  { key: "feats", label: "Feats", icon: "◆" },
  { key: "spells", label: "Spells", icon: "✨" },
  { key: "inventory", label: "Inventory", icon: "🎒" },
  { key: "notes", label: "Notes", icon: "✎" }
];

const rankOptions: ProficiencyRank[] = ["untrained", "trained", "expert", "master", "legendary"];

function RankSelect({ value, onChange }: { value: ProficiencyRank; onChange: (r: ProficiencyRank) => void }) {
  return (
    <select className="rounded-md border border-line bg-paper px-2 py-1 text-xs" value={value} onChange={(e) => onChange(e.target.value as ProficiencyRank)}>
      {rankOptions.map((r) => <option key={r} value={r}>{titleCase(r)}</option>)}
    </select>
  );
}

function CharacterSheet({ character, onChange }: { character: Character; onChange: (c: Character) => void }) {
  const [tab, setTab] = useState<SheetTab>("overview");
  const ancestry = useMemo(() => findAncestry(character), [character]);
  const characterClass = useMemo(() => findClass(character), [character]);
  const background = useMemo(() => gameData.backgrounds.find((e) => e.id === character.backgroundId) ?? gameData.backgrounds[0], [character]);
  const armor = useMemo(() => equippedArmor(character), [character]);
  const feats = useMemo(() => character.feats.map((e) => {
    const found = gameData.feats.find((f) => f.id === e.id);
    return found ?? { id: e.id, name: titleCase(e.id), source: "Manual", category: e.source as "ancestry" | "class" | "skill" | "general", level: 1, traits: [], summary: "Manually assigned feat." };
  }), [character]);
  const spells = useMemo(() => (character.spellcasting?.spellIds ?? []).map((id) => gameData.spells.find((s) => s.id === id)).filter((s): s is NonNullable<typeof s> => Boolean(s)), [character]);

  return (
    <div className="flex flex-col h-full">
      {/* Tab bar */}
      <div className="border-b border-line bg-warm overflow-x-auto">
        <div className="flex min-w-max px-4">
          {SHEET_TABS.map(({ key, label, icon }) => (
            <button key={key} className={cx("flex items-center gap-1.5 border-b-2 px-4 py-3 text-sm transition-all whitespace-nowrap", tab === key ? "border-ink font-medium text-ink" : "border-transparent text-muted hover:text-ink")} onClick={() => setTab(key)}>
              <span>{icon}</span>
              <span>{label}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-6 py-6">

          {tab === "overview" && (
            <div className="space-y-6">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <StatPill label="Ancestry" value={ancestry.name} tone="bg-sage" />
                <StatPill label="Heritage" value={titleCase(character.heritageId)} />
                <StatPill label="Background" value={background.name} tone="bg-gold" />
                <StatPill label="Class" value={characterClass.name} tone="bg-mist" />
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                <div className="rounded-lg border border-line bg-warm p-4">
                  <h3 className="font-medium mb-2">Level 1 Features</h3>
                  <ul className="space-y-1 text-sm text-muted">
                    {characterClass.level1Features.map((f) => <li key={f}>• {f}</li>)}
                  </ul>
                </div>
                {(character.edicts || character.anathema) && (
                  <div className="rounded-lg border border-line bg-warm p-4">
                    <h3 className="font-medium mb-2">Edicts & Anathema</h3>
                    {character.edicts && <p className="text-sm"><span className="font-medium">Edicts:</span> {character.edicts}</p>}
                    {character.anathema && <p className="mt-1 text-sm"><span className="font-medium">Anathema:</span> {character.anathema}</p>}
                  </div>
                )}
              </div>
              <div className="rounded-lg border border-line bg-warm p-4">
                <h3 className="font-medium mb-2">Conditions</h3>
                <div className="grid gap-2 sm:grid-cols-2 md:grid-cols-3">
                  {Object.entries(conditionText).map(([key, text]) => (
                    <label key={key} className="flex gap-2 rounded border border-line bg-paper p-2 cursor-pointer">
                      <input type="checkbox" checked={Boolean(character.conditions[key])} onChange={(e) => onChange({ ...character, conditions: { ...character.conditions, [key]: e.target.checked } })} />
                      <div>
                        <span className="block text-sm font-medium">{titleCase(key)}</span>
                        <span className="text-xs text-muted">{text}</span>
                      </div>
                    </label>
                  ))}
                </div>
              </div>
            </div>
          )}

          {tab === "abilities" && (
            <div>
              <h2 className="font-title text-2xl mb-4">Ability Scores</h2>
              <div className="grid gap-3 grid-cols-2 md:grid-cols-3 lg:grid-cols-6">
                {abilities.map((a) => (
                  <div key={a} className="rounded-lg border border-line bg-warm p-3">
                    <div className="text-xs uppercase text-muted">{titleCase(a)}</div>
                    <input className="mt-1 w-full bg-transparent text-3xl font-semibold text-ink outline-none" type="number" value={character.abilityScores[a]} onChange={(e) => onChange({ ...character, abilityScores: { ...character.abilityScores, [a]: Number(e.target.value) } })} />
                    <div className="text-sm text-muted">Mod {abilityModifier(character.abilityScores[a]) >= 0 ? "+" : ""}{abilityModifier(character.abilityScores[a])}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {tab === "defenses" && (
            <div className="space-y-5">
              <h2 className="font-title text-2xl">Defenses</h2>
              <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-4">
                <StatPill label="AC" value={armorClass(character, armor)} tone="bg-sage" />
                <StatPill label="HP" value={`${character.hp.current} / ${character.hp.max}`} tone="bg-clay" />
                <StatPill label="Temp HP" value={character.hp.temp} />
                <StatPill label="Class DC" value={classDc(character)} />
              </div>
              <div className="flex flex-wrap gap-4">
                <label className="block text-sm">
                  <span className="block text-muted mb-1">Current HP</span>
                  <input className="w-28 rounded-md border border-line bg-warm px-3 py-2" type="number" value={character.hp.current} onChange={(e) => onChange({ ...character, hp: { ...character.hp, current: Number(e.target.value) } })} />
                </label>
                <label className="block text-sm">
                  <span className="block text-muted mb-1">Temp HP</span>
                  <input className="w-28 rounded-md border border-line bg-warm px-3 py-2" type="number" value={character.hp.temp} onChange={(e) => onChange({ ...character, hp: { ...character.hp, temp: Number(e.target.value) } })} />
                </label>
              </div>
              <div className="grid gap-3 md:grid-cols-3">
                {(["fortitude", "reflex", "will"] as const).map((save) => (
                  <div key={save} className="rounded-lg border border-line bg-warm p-4">
                    <div className="flex items-center justify-between">
                      <div>
                        <div className="font-medium">{titleCase(save)}</div>
                        <div className="text-2xl font-semibold">{saveModifier(character, save) >= 0 ? "+" : ""}{saveModifier(character, save)}</div>
                      </div>
                      <RankSelect value={character.saves[save]} onChange={(r) => onChange({ ...character, saves: { ...character.saves, [save]: r } })} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {tab === "skills" && (
            <div>
              <h2 className="font-title text-2xl mb-4">Skills</h2>
              <div className="grid gap-2 md:grid-cols-2">
                {allSkills.map((skill) => (
                  <div key={skill} className="flex items-center justify-between rounded-lg border border-line bg-warm px-3 py-2">
                    <div>
                      <div className="font-medium text-sm">{titleCase(skill)}</div>
                      <div className="text-xs text-muted">{skillModifier(character, skill, armor) >= 0 ? "+" : ""}{skillModifier(character, skill, armor)}</div>
                    </div>
                    <RankSelect value={character.skills[skill]} onChange={(r) => onChange({ ...character, skills: { ...character.skills, [skill]: r } })} />
                  </div>
                ))}
              </div>
              {character.loreSkills.length > 0 && (
                <div className="mt-4">
                  <h3 className="font-medium mb-2">Lore Skills</h3>
                  <div className="flex flex-wrap gap-2">
                    {character.loreSkills.map((lore) => <span key={lore} className="rounded-full bg-mist px-3 py-1 text-sm">{lore}</span>)}
                  </div>
                </div>
              )}
            </div>
          )}

          {tab === "feats" && (
            <div>
              <h2 className="font-title text-2xl mb-4">Feats & Abilities</h2>
              <div className="space-y-3">
                {feats.map((feat) => (
                  <ItemPreview key={feat.id} name={feat.name} description={feat.description ?? feat.summary} traits={feat.traits}>
                    <div className="rounded-lg border border-line bg-warm p-4 w-full">
                      <div className="flex items-center gap-2">
                        <ActionIcon cost={feat.actionCost} />
                        <span className="font-medium">{feat.name}</span>
                        <span className="text-xs text-muted ml-auto">{feat.source} / {feat.category}</span>
                      </div>
                      <p className="mt-2 text-sm text-muted">{feat.description || feat.summary}</p>
                      {feat.prereq && <p className="mt-1 text-xs text-muted italic">Prereq: {feat.prereq}</p>}
                      <div className="mt-2 flex flex-wrap gap-1">
                        {feat.traits.map((t) => <span key={t} className="rounded bg-mist px-1.5 py-0.5 text-[10px]">{t}</span>)}
                      </div>
                    </div>
                  </ItemPreview>
                ))}
              </div>
            </div>
          )}

          {tab === "spells" && (
            <div>
              <h2 className="font-title text-2xl mb-4">Spells</h2>
              {!character.spellcasting ? (
                <p className="text-muted">This class has no spellcasting.</p>
              ) : (
                <>
                  <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-4">
                    <StatPill label="Tradition" value={character.spellcasting.tradition} />
                    <StatPill label="Mode" value={character.spellcasting.mode} tone="bg-mist" />
                    <StatPill label="Spell DC" value={spellDc(character) ?? "—"} tone="bg-sage" />
                    <StatPill label="Spell Attack" value={spellDc(character) ? `+${(spellDc(character) ?? 10) - 10}` : "—"} />
                  </div>
                  <div className="mt-5 space-y-3">
                    {spells.map((spell) => (
                      <SpellCard key={spell.id} spell={spell} showSelect={false} />
                    ))}
                  </div>
                </>
              )}
            </div>
          )}

          {tab === "inventory" && (
            <div>
              <h2 className="font-title text-2xl mb-4">Inventory</h2>
              <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-4">
                <StatPill label="Bulk" value={bulkTotal(character, gameData.items).toFixed(1)} />
                <StatPill label="SP" value={character.currency.sp} />
                <StatPill label="GP" value={character.currency.gp} />
                <StatPill label="CP" value={character.currency.cp} />
              </div>
              <div className="mt-4 space-y-2">
                {character.inventory.map((entry, idx) => {
                  const item = gameData.items.find((i) => i.id === entry.id);
                  if (!item) return null;
                  return (
                    <div key={`${entry.id}-${idx}`} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-warm px-3 py-2">
                      <ItemPreview name={item.name} description={item.description ?? item.summary} traits={item.traits}>
                        <div>
                          <div className="font-medium text-sm">{item.name}</div>
                          <div className="text-xs text-muted">{item.category} · bulk {item.bulk} · {item.priceSp} sp</div>
                        </div>
                      </ItemPreview>
                      <div className="flex items-center gap-2">
                        <input className="w-16 rounded-md border border-line bg-paper px-2 py-1 text-sm" type="number" min={0} value={entry.quantity} onChange={(e) => onChange({ ...character, inventory: character.inventory.map((c, i) => i === idx ? { ...c, quantity: Number(e.target.value) } : c) })} />
                        <label className="text-xs"><input className="mr-1" type="checkbox" checked={Boolean(entry.equipped)} onChange={(e) => onChange({ ...character, inventory: character.inventory.map((c, i) => i === idx ? { ...c, equipped: e.target.checked } : c) })} /> Eq</label>
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="mt-4">
                <h3 className="font-medium text-sm mb-2">Add Item</h3>
                <div className="grid gap-2 md:grid-cols-3 lg:grid-cols-4">
                  {gameData.items.map((item) => (
                    <button key={item.id} className="rounded-md border border-line bg-paper px-3 py-2 text-xs text-left hover:bg-warm" onClick={() => onChange({ ...character, inventory: [...character.inventory, { id: item.id, quantity: 1 }] })}>
                      <div className="font-medium">{item.name}</div>
                      <div className="text-muted">{item.priceSp} sp · {item.category}</div>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {tab === "notes" && (
            <div className="space-y-4 max-w-2xl">
              <h2 className="font-title text-2xl mb-4">Notes</h2>
              {[
                { key: "appearance", label: "Appearance" },
                { key: "edicts", label: "Edicts" },
                { key: "anathema", label: "Anathema" },
                { key: "notes", label: "Notes" }
              ].map(({ key, label }) => (
                <label key={key} className="block text-sm">
                  <span className="block text-muted mb-1">{label}</span>
                  <textarea className="min-h-20 w-full rounded-md border border-line bg-warm px-3 py-2 outline-none" value={(character as unknown as Record<string, string>)[key] ?? ""} onChange={(e) => onChange({ ...character, [key]: e.target.value })} />
                </label>
              ))}
            </div>
          )}

        </div>
      </div>
    </div>
  );
}

/* ─── Main Home ─── */
export default function Home() {
  const [characters, setCharacters] = useState<Character[]>([]);
  const [currentId, setCurrentId] = useState("");
  const [mode, setMode] = useState<"sheet" | "creator" | "data">("creator");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [importText, setImportText] = useState("");

  // Persist to localStorage
  useEffect(() => {
    const raw = localStorage.getItem(storageKey);
    if (!raw) {
      // No saved data — start in creator mode
      setMode("creator");
      return;
    }
    try {
      const parsed = JSON.parse(raw) as Character[];
      if (Array.isArray(parsed) && parsed.length) {
        setCharacters(parsed);
        setCurrentId(parsed[0].id);
        setMode("sheet");
      } else {
        setMode("creator");
      }
    } catch {
      localStorage.removeItem(storageKey);
      setMode("creator");
    }
  }, []);

  useEffect(() => {
    if (characters.length > 0) {
      localStorage.setItem(storageKey, JSON.stringify(characters));
    } else {
      localStorage.removeItem(storageKey);
    }
  }, [characters]);

  const character = characters.find((e) => e.id === currentId) ?? characters[0] ?? null;
  const ancestry = character ? findAncestry(character) : null;
  const characterClass = character ? findClass(character) : null;
  const background = character ? gameData.backgrounds.find((e) => e.id === character.backgroundId) ?? gameData.backgrounds[0] : null;

  const updateCharacter = useCallback((next: Character) => {
    setCharacters((prev) => prev.map((e) => (e.id === next.id ? next : e)));
  }, []);

  function downloadExport() {
    const blob = new Blob([JSON.stringify(characters, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "pf2e-characters.json";
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="flex h-screen bg-paper text-ink overflow-hidden">
      {/* Sidebar */}
      <aside className={cx("flex flex-col border-r border-line bg-warm transition-all shrink-0", sidebarOpen ? "w-64" : "w-14")}>
        <div className="flex items-center justify-between border-b border-line px-3 py-3">
          {sidebarOpen && <div className="font-title text-lg">PF2e Helper</div>}
          <button className="rounded-md border border-line bg-paper px-2 py-1 text-xs" onClick={() => setSidebarOpen((o) => !o)} aria-label="Toggle sidebar">
            {sidebarOpen ? "‹" : "›"}
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-3">
          {sidebarOpen && <div className="mb-1.5 px-1 text-[10px] uppercase tracking-wide text-muted">Characters</div>}

          {/* Empty state (Requirement 4) */}
          {characters.length === 0 && sidebarOpen && (
            <div className="px-2 py-4 text-center">
              <div className="text-2xl mb-2">⚔️</div>
              <p className="text-sm text-muted">No characters yet.</p>
              <p className="text-xs text-muted mt-1">Create your first one to get started.</p>
            </div>
          )}

          <div className="space-y-1">
            {characters.map((c) => (
              <button key={c.id} className={cx("w-full rounded-md px-2 py-2 text-left text-sm transition-all", c.id === currentId && mode === "sheet" ? "bg-sage font-medium" : "hover:bg-paper")} onClick={() => { setCurrentId(c.id); setMode("sheet"); }}>
                {sidebarOpen ? (
                  <div>
                    <div>{c.name}</div>
                    <div className="text-[10px] text-muted">{gameData.ancestries.find((a) => a.id === c.ancestryId)?.name} {gameData.classes.find((cls) => cls.id === c.classId)?.name}</div>
                  </div>
                ) : c.name.slice(0, 1)}
              </button>
            ))}
          </div>
          <button className={cx("mt-2 w-full rounded-md border border-line px-2 py-2 text-sm transition-all", mode === "creator" ? "bg-sage border-ink" : "bg-paper hover:bg-warm")} onClick={() => setMode("creator")}>
            {sidebarOpen ? "+ New character" : "+"}
          </button>
        </div>

        <div className="border-t border-line px-2 py-2">
          <button className={cx("w-full rounded-md px-2 py-2 text-sm transition-all", mode === "data" ? "bg-mist" : "hover:bg-paper")} onClick={() => setMode("data")}>
            {sidebarOpen ? "⧉  Data & Export" : "⧉"}
          </button>
        </div>
      </aside>

      {/* Main content */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Header */}
        {mode === "sheet" && character && ancestry && characterClass && background && (
          <header className="border-b border-line bg-paper px-6 py-4 shrink-0">
            <div className="text-xs text-muted">{ancestry.name} · {background.name} · {characterClass.name}</div>
            <div className="mt-1 flex flex-wrap items-end justify-between gap-4">
              <div>
                <input className="bg-transparent font-title text-4xl outline-none w-full" value={character.name} onChange={(e) => updateCharacter({ ...character, name: e.target.value })} />
                <input className="mt-1 bg-transparent text-sm text-muted outline-none" placeholder="Pronouns" value={character.pronouns} onChange={(e) => updateCharacter({ ...character, pronouns: e.target.value })} />
              </div>
              <div className="flex items-center gap-2">
                <button className="rounded-md border border-line bg-warm px-3 py-1.5 text-sm" onClick={() => {
                  const a = findAncestry(character);
                  const c = findClass(character);
                  const level = Math.max(1, character.level - 1);
                  const hp = maxHp({ ...character, level }, a, c);
                  updateCharacter({ ...character, level, hp: { ...character.hp, max: hp, current: Math.min(character.hp.current, hp) } });
                }}>− Lv</button>
                <StatPill label="Level" value={character.level} tone="bg-gold" />
                <button className="rounded-md border border-line bg-warm px-3 py-1.5 text-sm" onClick={() => {
                  const a = findAncestry(character);
                  const c = findClass(character);
                  const level = Math.min(20, character.level + 1);
                  const hp = maxHp({ ...character, level }, a, c);
                  updateCharacter({ ...character, level, hp: { ...character.hp, max: hp, current: Math.min(character.hp.current, hp) } });
                }}>+ Lv</button>
              </div>
            </div>
          </header>
        )}

        {mode === "creator" && (
          <header className="border-b border-line bg-paper px-6 py-4 shrink-0 flex items-center justify-between">
            <h1 className="font-title text-2xl">New Character</h1>
            {characters.length > 0 && (
              <button className="rounded-md border border-line bg-warm px-3 py-1.5 text-sm" onClick={() => setMode("sheet")}>← Back to sheet</button>
            )}
          </header>
        )}

        {mode === "data" && (
          <header className="border-b border-line bg-paper px-6 py-4 shrink-0">
            <h1 className="font-title text-2xl">Data & Export</h1>
          </header>
        )}

        {/* Content */}
        <div className="flex-1 overflow-hidden">
          {mode === "sheet" && character && (
            <CharacterSheet character={character} onChange={updateCharacter} />
          )}

          {mode === "sheet" && !character && (
            <div className="flex items-center justify-center h-full">
              <div className="text-center">
                <p className="text-lg text-muted">No character selected.</p>
                <button className="mt-3 rounded-md bg-ink px-4 py-2 text-sm text-paper" onClick={() => setMode("creator")}>Create a character</button>
              </div>
            </div>
          )}

          {mode === "creator" && (
            <CharacterCreator onFinish={(c) => {
              setCharacters((prev) => [c, ...prev]);
              setCurrentId(c.id);
              setMode("sheet");
            }} />
          )}

          {mode === "data" && (
            <div className="overflow-y-auto h-full px-6 py-6 max-w-3xl mx-auto space-y-5">
              <div className="flex flex-wrap gap-2">
                <button className="rounded-md border border-line bg-sage px-4 py-2 text-sm font-medium" onClick={downloadExport} disabled={characters.length === 0}>Export JSON</button>
              </div>
              <div className="rounded-lg border border-line bg-warm p-4">
                <h3 className="font-medium mb-2">Data Status</h3>
                <ul className="space-y-1 text-sm text-muted">
                  <li>Ancestries: {gameData.ancestries.map((a) => a.name).join(", ")}</li>
                  <li>Classes: {gameData.classes.map((c) => c.name).join(", ")}</li>
                  <li>Feats: {gameData.feats.length} (from PC1, PC2)</li>
                  <li>Spells: {gameData.spells.length} (cantrips to rank 3)</li>
                  <li>Items: {gameData.items.length}</li>
                  <li>Backgrounds: {gameData.backgrounds.length}</li>
                </ul>
              </div>
              <div>
                <h3 className="font-medium mb-2">Import JSON</h3>
                <textarea className="h-48 w-full rounded-md border border-line bg-warm p-3 font-mono text-xs outline-none" value={importText} onChange={(e) => setImportText(e.target.value)} placeholder="Paste an exported character array here." />
                <button className="mt-2 rounded-md border border-line bg-mist px-4 py-2 text-sm" onClick={() => {
                  try {
                    const parsed = JSON.parse(importText) as Character[];
                    if (!Array.isArray(parsed)) return;
                    setCharacters(parsed);
                    setCurrentId(parsed[0]?.id ?? "");
                    setMode("sheet");
                  } catch { /* ignore */ }
                }}>Import</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
