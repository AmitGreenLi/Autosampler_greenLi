from autosampler.config import CAROUSEL_STEPS_PER_DEGREE


def degrees_to_steps(angle_deg: float) -> int:
    """Convert an angle to the nearest achievable integer full-step position."""
    return round(float(angle_deg) * CAROUSEL_STEPS_PER_DEGREE)


def steps_to_degrees(steps: int) -> float:
    return int(steps) / CAROUSEL_STEPS_PER_DEGREE


def normalize_steps(steps: int) -> int:
    steps_per_rev = round(360 * CAROUSEL_STEPS_PER_DEGREE)
    return int(steps) % steps_per_rev

