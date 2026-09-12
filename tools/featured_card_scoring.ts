export type Era =
  | "wotc"
  | "ex"
  | "dp"
  | "bw"
  | "xy"
  | "sm"
  | "swsh"
  | "sv"
  | "other";

export interface CardScoreInput {
  id: string;
  name: string;
  set: string;
  year: number;
  number: string;
  era: Era;

  recognition: 0 | 1;
  associatedArt: 0 | 1;
  collectorPresence: 0 | 0.5 | 1;
  representativeCard: 0 | 1;

  firstAppearance: 0 | 0.67 | 1;
  historicSet: 0 | 0.5 | 1;
  landmarkMechanic: 0 | 1;
  culturalCompetitiveImpact: 0 | 1;

  rarityScarcity: 0 | 0.5 | 1;
  premiumArt: 0 | 0.5 | 1;
  promoChaseCommemorative: 0 | 1;
  recurringDemand: 0 | 1;

  artworkKey?: string;
  reprintGroup?: string;
}

export interface ScoredCard extends CardScoreInput {
  baseScore: number;
  adjustedScore: number;
  penalties: {
    redundancy: number;
    eraDiversity: number;
  };
}

export function calculateBaseScore(card: CardScoreInput): number {
  const score =
    15 * card.recognition +
    10 * card.associatedArt +
    10 * card.collectorPresence +
    5 * card.representativeCard +
    15 * card.firstAppearance +
    10 * card.historicSet +
    5 * card.landmarkMechanic +
    5 * card.culturalCompetitiveImpact +
    10 * card.rarityScarcity +
    8 * card.premiumArt +
    5 * card.promoChaseCommemorative +
    2 * card.recurringDemand;

  return clamp(score, 0, 100);
}

export function selectFeaturedCards(
  cards: CardScoreInput[],
  limit = 3,
): ScoredCard[] {
  if (cards.length === 0 || limit <= 0) return [];

  const candidates = cards.map((card) => ({
    ...card,
    baseScore: calculateBaseScore(card),
  }));

  const selected: ScoredCard[] = [];
  const remaining = [...candidates];

  while (selected.length < limit && remaining.length > 0) {
    const evaluated: ScoredCard[] = remaining.map((card) => {
      const redundancyPenalty = isRedundant(card, selected) ? 15 : 0;
      const eraDiversityPenalty = shouldApplyEraPenalty(
        card,
        selected,
        remaining,
      )
        ? 10
        : 0;

      return {
        ...card,
        adjustedScore: clamp(
          card.baseScore - redundancyPenalty - eraDiversityPenalty,
          0,
          100,
        ),
        penalties: {
          redundancy: redundancyPenalty,
          eraDiversity: eraDiversityPenalty,
        },
      };
    });

    evaluated.sort(compareCards);
    const winner = evaluated[0];
    selected.push(winner);

    const winnerIndex = remaining.findIndex((card) => card.id === winner.id);
    if (winnerIndex >= 0) remaining.splice(winnerIndex, 1);
  }

  return selected;
}

function isRedundant(
  candidate: CardScoreInput,
  selected: ScoredCard[],
): boolean {
  return selected.some((chosen) => {
    const sameArtwork =
      candidate.artworkKey &&
      chosen.artworkKey &&
      candidate.artworkKey === chosen.artworkKey;

    const sameReprintGroup =
      candidate.reprintGroup &&
      chosen.reprintGroup &&
      candidate.reprintGroup === chosen.reprintGroup;

    return Boolean(sameArtwork || sameReprintGroup);
  });
}

function shouldApplyEraPenalty(
  candidate: CardScoreInput & { baseScore: number },
  selected: ScoredCard[],
  remaining: Array<CardScoreInput & { baseScore: number }>,
): boolean {
  if (selected.length < 2) return false;

  const selectedEras = new Set(selected.map((card) => card.era));
  if (selectedEras.size > 1) return false;

  const currentEra = selected[0].era;
  if (candidate.era !== currentEra) return false;

  return remaining
    .filter((card) => card.id !== candidate.id)
    .filter((card) => card.era !== currentEra)
    .some((card) => candidate.baseScore - card.baseScore <= 10);
}

function compareCards(a: ScoredCard, b: ScoredCard): number {
  if (b.adjustedScore !== a.adjustedScore) {
    return b.adjustedScore - a.adjustedScore;
  }

  const historyDifference = getHistoryScore(b) - getHistoryScore(a);
  if (historyDifference !== 0) return historyDifference;

  const fameDifference = getFameScore(b) - getFameScore(a);
  if (fameDifference !== 0) return fameDifference;

  const collectorDifference = getCollectorScore(b) - getCollectorScore(a);
  if (collectorDifference !== 0) return collectorDifference;

  return a.year - b.year;
}

function getFameScore(card: CardScoreInput): number {
  return (
    15 * card.recognition +
    10 * card.associatedArt +
    10 * card.collectorPresence +
    5 * card.representativeCard
  );
}

function getHistoryScore(card: CardScoreInput): number {
  return (
    15 * card.firstAppearance +
    10 * card.historicSet +
    5 * card.landmarkMechanic +
    5 * card.culturalCompetitiveImpact
  );
}

function getCollectorScore(card: CardScoreInput): number {
  return (
    10 * card.rarityScarcity +
    8 * card.premiumArt +
    5 * card.promoChaseCommemorative +
    2 * card.recurringDemand
  );
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
