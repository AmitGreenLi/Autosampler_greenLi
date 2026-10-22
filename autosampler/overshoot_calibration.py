"""Motion-independent sweep configuration, trial records and conservative ranking."""
from dataclasses import dataclass
import math
from statistics import median, pstdev

from autosampler.errors import ValidationError
from autosampler.motion.carousel import degrees_to_steps, steps_to_degrees


@dataclass(frozen=True)
class OvershootOptions:
    spots: tuple[str, ...]
    minimum_deg: float = 0.0
    maximum_deg: float = 5.0
    increment_deg: float = 1.0
    repetitions: int = 3
    deadline_s: float = 2.0
    stability_hold_s: float = 0.5
    secondary_minimum_deg: float = 0.0
    secondary_maximum_deg: float = 0.0
    secondary_increment_deg: float = 1.0

    def candidates(self):
        values = (self.minimum_deg, self.maximum_deg, self.increment_deg, self.deadline_s, self.stability_hold_s)
        if not all(math.isfinite(v) for v in values):
            raise ValidationError("Sweep values must be finite.")
        if self.minimum_deg < 0 or self.maximum_deg < self.minimum_deg or self.increment_deg <= 0 or self.deadline_s <= 0:
            raise ValidationError("Use a nonnegative increasing angle range, positive increment and deadline.")
        if self.stability_hold_s < 0:
            raise ValidationError("Stable-reading hold must be nonnegative (0 accepts the first fresh stable reading).")
        if isinstance(self.repetitions, bool) or int(self.repetitions) != self.repetitions or self.repetitions < 2:
            raise ValidationError("At least two repetitions per spot and direction are required.")
        if not self.spots or any(not spot.strip() for spot in self.spots) or len(set(self.spots)) != len(self.spots):
            raise ValidationError("Select at least one vial spot; spot IDs must be nonempty and distinct.")
        primary = self._angles(self.minimum_deg, self.maximum_deg, self.increment_deg)
        secondary = self._angles(self.secondary_minimum_deg, self.secondary_maximum_deg, self.secondary_increment_deg)
        count = len(primary) * len(secondary)
        if count > 1000 or count * len(self.spots) * self.repetitions * 2 > 100000:
            raise ValidationError("Sweep is too large; reduce candidates, spots or repetitions.")
        return primary

    @staticmethod
    def _angles(minimum, maximum, increment):
        if not all(math.isfinite(v) for v in (minimum, maximum, increment)) or minimum < 0 or maximum < minimum or increment <= 0:
            raise ValidationError("Both A and B need finite nonnegative increasing ranges and positive increments.")
        count = math.floor((maximum - minimum) / increment + 1e-9) + 1
        if count > 1000:
            raise ValidationError("Sweep is too large; reduce candidates.")
        steps = [degrees_to_steps(minimum + i * increment) for i in range(count)]
        if len(set(steps)) != count or minimum > 0 and steps[0] == 0:
            raise ValidationError("Angles/increment must resolve to distinct motor steps.")
        return tuple(steps_to_degrees(s) for s in steps)

    def candidate_pairs(self):
        primary = self.candidates()
        secondary = self._angles(self.secondary_minimum_deg, self.secondary_maximum_deg, self.secondary_increment_deg)
        return tuple((a, b) for a in primary for b in secondary)


@dataclass(frozen=True)
class OvershootTrial:
    spot: str
    direction: str
    angle_deg: float
    stabilization_s: float | None
    outcome: str
    secondary_deg: float = 0.0


@dataclass(frozen=True)
class CandidateSummary:
    angle_deg: float
    successes: int
    total: int
    median_s: float
    spread_s: float
    reliable: bool
    secondary_deg: float = 0.0


@dataclass(frozen=True)
class OvershootResult:
    trials: tuple[OvershootTrial, ...]
    candidates: tuple[CandidateSummary, ...]
    recommendation_deg: float | None
    recommendation_secondary_deg: float | None = 0.0

    @property
    def recommendation_pair(self):
        if self.recommendation_deg is None or self.recommendation_secondary_deg is None:
            return None
        return (self.recommendation_deg, self.recommendation_secondary_deg)


def rank_trials(options, trials):
    summaries = []
    for angle, secondary in options.candidate_pairs():
        subset = [t for t in trials if t.angle_deg == angle and t.secondary_deg == secondary]
        times = [t.stabilization_s for t in subset if t.outcome == "success"]
        reliable = True
        for spot in options.spots:
            for direction in ("P1→P2", "P2→P1"):
                cell = [t for t in subset if t.spot == spot and t.direction == direction]
                successes = sum(t.outcome == "success" for t in cell)
                reliable &= len(cell) == options.repetitions and successes >= 2 and successes / len(cell) >= 0.8
        summaries.append(CandidateSummary(angle, len(times), len(subset),
                         median(times) if times else math.inf,
                         pstdev(times) if times else math.inf, reliable, secondary))
    summaries.sort(key=lambda s: (-s.successes / max(s.total, 1), s.median_s, s.spread_s, s.angle_deg, s.secondary_deg))
    eligible = [s for s in summaries if s.reliable]
    return OvershootResult(tuple(trials), tuple(summaries), eligible[0].angle_deg if eligible else None,
                           eligible[0].secondary_deg if eligible else None)
