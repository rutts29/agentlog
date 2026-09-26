import type { CommandFloorActor, CrewSpecialty, FamiliarKind } from "@/lib/commandFloor";

const SPECIALTY_COLOR: Record<CrewSpecialty, string> = {
  coordinator: "var(--command-cyan)",
  researcher: "var(--command-yellow)",
  builder: "var(--command-green)",
  tester: "var(--command-red)",
  reviewer: "var(--command-amber)",
  systems: "var(--command-cyan)",
  operator: "var(--muted-foreground)",
};

const SKIN_TONES = ["#8d5a42", "#b97856", "#d6a27d", "#704432", "#c88968"];

function numericSeed(value: string): number {
  let result = 0;
  for (const char of value) result = (result * 31 + char.charCodeAt(0)) >>> 0;
  return result;
}

function stateTone(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "waiting":
      return "var(--command-yellow)";
    case "working":
    case "orchestrating":
      return "var(--command-cyan)";
    case "idle":
      return "var(--faint-foreground)";
  }
}

function OperatorEquipment({ specialty }: { specialty: CrewSpecialty }) {
  const color = SPECIALTY_COLOR[specialty];
  switch (specialty) {
    case "coordinator":
      return <><rect x="34" y="27" width="9" height="12" fill={color} /><rect x="36" y="29" width="5" height="6" fill="#071016" /></>;
    case "researcher":
      return <><rect x="16" y="15" width="20" height="4" fill={color} /><rect x="38" y="24" width="3" height="16" fill={color} /></>;
    case "builder":
      return <><rect x="37" y="29" width="3" height="18" fill={color} /><rect x="34" y="42" width="9" height="4" fill={color} /></>;
    case "tester":
      return <><rect x="6" y="31" width="7" height="9" fill={color} /><rect x="35" y="31" width="7" height="9" fill={color} /></>;
    case "reviewer":
      return <><rect x="35" y="25" width="10" height="15" fill="#d9dde0" /><rect x="37" y="28" width="6" height="2" fill={color} /><rect x="37" y="33" width="5" height="2" fill="#71818b" /></>;
    case "systems":
      return <><rect x="7" y="25" width="8" height="20" fill="#263640" /><rect x="9" y="28" width="4" height="4" fill={color} /><rect x="9" y="35" width="4" height="7" fill="#71818b" /></>;
    case "operator":
      return null;
  }
}

function OperatorSprite({ actor }: { actor: CommandFloorActor }) {
  const seed = numericSeed(actor.id);
  const skin = SKIN_TONES[seed % SKIN_TONES.length];
  const hair = seed % 2 === 0 ? "#111820" : "#2a201c";
  const accent = SPECIALTY_COLOR[actor.specialty];
  return (
    <svg className="crew-sprite crew-sprite--operator" viewBox="0 0 48 64" shapeRendering="crispEdges" aria-hidden>
      <rect x="17" y="9" width="16" height="6" fill={hair} />
      <rect x="14" y="13" width="20" height="14" fill={skin} />
      <rect x="14" y="13" width="4" height="9" fill={hair} />
      <rect x="18" y="18" width="3" height="2" fill="#071016" />
      <rect x="28" y="18" width="3" height="2" fill="#071016" />
      <rect x="11" y="27" width="27" height="20" fill="#26323c" />
      <rect x="15" y="29" width="19" height="5" fill={accent} />
      <rect x="5" y="29" width="6" height="17" fill="#1a242c" />
      <rect x="38" y="29" width="6" height="17" fill="#1a242c" />
      <rect x="12" y="47" width="10" height="12" fill="#182128" />
      <rect x="27" y="47" width="10" height="12" fill="#182128" />
      <rect x="9" y="58" width="13" height="4" fill="#394853" />
      <rect x="27" y="58" width="13" height="4" fill="#394853" />
      <rect x="21" y="37" width="7" height="4" fill={stateTone(actor)} />
      <OperatorEquipment specialty={actor.specialty} />
    </svg>
  );
}

function FamiliarBody({ kind, color }: { kind: FamiliarKind; color: string }) {
  switch (kind) {
    case "scout-hound":
      return <><rect x="10" y="25" width="24" height="13" fill="#2b3841" /><rect x="32" y="20" width="10" height="14" fill="#3d4d57" /><rect x="35" y="23" width="4" height="3" fill={color} /><rect x="12" y="38" width="5" height="11" fill="#52636d" /><rect x="27" y="38" width="5" height="11" fill="#52636d" /></>;
    case "archive-owl":
      return <><rect x="17" y="20" width="16" height="22" fill="#38464f" /><rect x="8" y="24" width="10" height="15" fill="#53636c" /><rect x="32" y="24" width="10" height="15" fill="#53636c" /><rect x="19" y="24" width="5" height="5" fill={color} /><rect x="27" y="24" width="5" height="5" fill={color} /></>;
    case "builder-beetle":
      return <><rect x="10" y="26" width="28" height="15" fill="#304039" /><rect x="17" y="21" width="16" height="9" fill={color} /><rect x="6" y="39" width="9" height="6" fill="#53636c" /><rect x="33" y="39" width="9" height="6" fill="#53636c" /><rect x="36" y="17" width="4" height="15" fill={color} /></>;
    case "test-spider":
      return <><rect x="15" y="22" width="20" height="18" fill="#3a3033" /><rect x="20" y="17" width="10" height="8" fill={color} /><path d="M15 27H5M15 34H4M35 27H45M35 34H46M17 39L9 48M33 39L41 48" stroke={color} strokeWidth="3" /></>;
    case "review-raven":
      return <><rect x="18" y="20" width="15" height="22" fill="#30383e" /><path d="M18 24L6 31L17 35M33 24L44 31L34 35" fill={color} /><rect x="31" y="19" width="9" height="6" fill="#4d5961" /><rect x="35" y="20" width="3" height="2" fill={color} /></>;
    case "ops-crab":
      return <><rect x="12" y="23" width="26" height="17" fill="#31434d" /><rect x="19" y="27" width="5" height="4" fill={color} /><rect x="28" y="27" width="5" height="4" fill={color} /><path d="M12 29H4M12 36H3M38 29H46M38 36H47M16 40L9 48M34 40L41 48" stroke="#52636d" strokeWidth="3" /></>;
  }
}

function FamiliarSprite({ actor }: { actor: Extract<CommandFloorActor, { kind: "familiar" }> }) {
  const color = SPECIALTY_COLOR[actor.specialty];
  return (
    <svg className="crew-sprite crew-sprite--familiar" viewBox="0 0 50 54" shapeRendering="crispEdges" aria-hidden>
      <FamiliarBody kind={actor.familiar} color={color} />
      <rect x="22" y="46" width="7" height="3" fill={stateTone(actor)} />
    </svg>
  );
}

export function CrewSprite({ actor }: { actor: CommandFloorActor }) {
  return actor.kind === "operator" ? <OperatorSprite actor={actor} /> : <FamiliarSprite actor={actor} />;
}

