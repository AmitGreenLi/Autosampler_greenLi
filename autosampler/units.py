from .errors import ValidationError


def mass_g_to_volume_ul(mass_g: float, density_g_ml: float) -> float:
    if density_g_ml <= 0:
        raise ValidationError("Density must be greater than zero.")
    return float(mass_g) / float(density_g_ml) * 1000.0


def volume_ul_to_mass_g(volume_ul: float, density_g_ml: float) -> float:
    if density_g_ml <= 0:
        raise ValidationError("Density must be greater than zero.")
    return float(volume_ul) / 1000.0 * float(density_g_ml)
