import coordinatorOperator from "@/assets/command-grid/operators/coordinator-sprite.png";
import researcherOperator from "@/assets/command-grid/operators/researcher-sprite.png";
import builderOperator from "@/assets/command-grid/operators/builder-sprite.png";
import testerOperator from "@/assets/command-grid/operators/tester-sprite.png";
import reviewerOperator from "@/assets/command-grid/operators/reviewer-sprite.png";
import systemsOperator from "@/assets/command-grid/operators/systems-sprite.png";
import scoutHound from "@/assets/command-grid/familiars/scout-hound-sprite.png";
import archiveOwl from "@/assets/command-grid/familiars/archive-owl-sprite.png";
import builderBeetle from "@/assets/command-grid/familiars/builder-beetle-sprite.png";
import testSpider from "@/assets/command-grid/familiars/test-spider-sprite.png";
import reviewRaven from "@/assets/command-grid/familiars/review-raven-sprite.png";
import opsRepairCrab from "@/assets/command-grid/familiars/ops-repair-crab-sprite.png";
import type { CommandFloorActor, CrewSpecialty, FamiliarKind } from "@/lib/commandFloor";

const OPERATOR_ART: Record<CrewSpecialty, string> = {
  coordinator: coordinatorOperator,
  researcher: researcherOperator,
  builder: builderOperator,
  tester: testerOperator,
  reviewer: reviewerOperator,
  systems: systemsOperator,
  operator: coordinatorOperator,
};

const FAMILIAR_ART: Record<FamiliarKind, string> = {
  "scout-hound": scoutHound,
  "archive-owl": archiveOwl,
  "builder-beetle": builderBeetle,
  "test-spider": testSpider,
  "review-raven": reviewRaven,
  "ops-crab": opsRepairCrab,
};

export function CrewSprite({ actor }: { actor: CommandFloorActor }) {
  const src = actor.kind === "operator"
    ? OPERATOR_ART[actor.specialty]
    : FAMILIAR_ART[actor.familiar];
  return (
    <img
      className={`crew-sprite crew-sprite--${actor.kind}`}
      src={src}
      alt=""
      aria-hidden="true"
      draggable={false}
    />
  );
}
